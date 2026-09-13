import os
import asyncio
import json
import logging
import re
import html
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import deque, defaultdict
from datetime import datetime
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8804502384:AAHYjDaiM_sj7p3t1MRCSKJA5XMoUmqWINo")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5642314005")
ADMIN_USER_IDS = [int(uid) for uid in os.environ.get("ADMIN_USER_IDS", "5642314005").split(",") if uid.isdigit()]

DEXSCREENER_BATCH_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
DEXSCREENER_BOOSTED_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{}"
GOPLUS_SOLANA_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"

EVM_CHAIN_MAP = {
    "ethereum": "1", "eth": "1", "bsc": "56", "polygon": "137",
    "arbitrum": "42161", "optimism": "10", "avalanche": "43114",
    "base": "8453", "zksync": "324", "linea": "59144"
}

POLL_INTERVAL = 10     # Seconds between market scans
MAX_SEEN_CACHE = 2000

# ==========================================
# CRITERIA THRESHOLDS
# ==========================================
MIN_5M_VOL_EXPANSION = 0.1   # % volume growth in 5m window
MIN_5M_MCAP_EXPANSION = 0.0  # % market cap growth in 5m window
MIN_5M_VOLUME = 10.0        # Minimum $ 5m volume threshold

# ==========================================
# STATE & CACHE MANAGEMENT
# ==========================================
WATCHLIST = set()
SEEN_TOKENS = set()
PRICE_HISTORY = defaultdict(lambda: deque(maxlen=30))
STATE_LOCK = asyncio.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def normalize_address(address: str) -> str:
    """Preserves Solana Base58 casing while lowercasing EVM addresses."""
    if re.match(r"^0x[a-fA-F0-9]{40}$", address):
        return address.lower()
    return address.strip()

async def save_state_async():
    """Thread-safe state persistence."""
    async with STATE_LOCK:
        data = {
            "watchlist": list(WATCHLIST),
            "seen_tokens": list(SEEN_TOKENS)[-MAX_SEEN_CACHE:]
        }
        await asyncio.to_thread(lambda: open("bot_state.json", "w").write(json.dumps(data, indent=2)))

async def load_state_async():
    """Loads saved state on initialization."""
    global WATCHLIST, SEEN_TOKENS
    async with STATE_LOCK:
        try:
            content = await asyncio.to_thread(lambda: open("bot_state.json", "r").read())
            data = json.loads(content)
            WATCHLIST = set(data.get("watchlist", []))
            SEEN_TOKENS = set(data.get("seen_tokens", []))
        except FileNotFoundError:
            WATCHLIST = set()
            SEEN_TOKENS = set()

# ==========================================
# INSIDER SECURITY AUDIT ENGINE (GOPLUS)
# ==========================================
async def check_goplus_security(session: aiohttp.ClientSession, chain: str, token_address: str) -> dict:
    """Retrieves deep security flags including honeypot metrics, dev holdings, and freeze authority."""
    chain_lower = chain.lower()
    audit = {
        "status": "UNVERIFIED",
        "dev_pct": "N/A",
        "is_mintable": "No",
        "is_freezable": "No",
        "lp_status": "Unknown"
    }
    
    try:
        if chain_lower in ["solana", "sol"]:
            async with session.get(GOPLUS_SOLANA_URL, params={"contract_addresses": [token_address]}, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    res = data.get("result", {}).get(token_address, {})
                    
                    freezable = res.get("freezable", {}).get("status") == "1"
                    mintable = res.get("mintable", {}).get("status") == "1"
                    
                    audit["is_freezable"] = "⚠️ YES" if freezable else "No"
                    audit["is_mintable"] = "⚠️ YES" if mintable else "No"
                    
                    if freezable or mintable:
                        audit["status"] = "UNSAFE"
                    else:
                        audit["status"] = "SAFE"
                    
                    creator_holding = res.get("creator_percent")
                    if creator_holding is not None:
                        audit["dev_pct"] = f"{float(creator_holding) * 100:.1f}%"
        else:
            chain_id = EVM_CHAIN_MAP.get(chain_lower)
            if not chain_id:
                return audit

            url = GOPLUS_EVM_URL.format(chain_id)
            async with session.get(url, params={"contract_addresses": token_address}, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    res = data.get("result", {}).get(token_address.lower(), {})
                    
                    is_honeypot = res.get("is_honeypot") == "1"
                    cannot_sell = float(res.get("cannot_sell_all", 0)) == 1
                    
                    if is_honeypot or cannot_sell:
                        audit["status"] = "UNSAFE"
                    else:
                        audit["status"] = "SAFE"
                        
                    audit["is_mintable"] = "⚠️ YES" if res.get("is_mintable") == "1" else "No"
                    
                    dev_val = res.get("creator_percent")
                    if dev_val:
                        audit["dev_pct"] = f"{float(dev_val) * 100:.1f}%"
                    
                    lp_burned = float(res.get("lp_burned_percent", 0))
                    if lp_burned > 0.8:
                        audit["lp_status"] = "🔥 Burned"
                    elif res.get("lp_holder_count"):
                        audit["lp_status"] = "🔒 Active Pool"
                        
    except Exception as e:
        logging.error(f"GoPlus error for {token_address}: {e}")
        
    return audit

# ==========================================
# ALERT DISPATCHER & MONITOR LOOP
# ==========================================
async def dispatch_telegram_alert(app: Application, pair: dict, security_info: dict, vol_growth: float, mcap_growth: float):
    base_token = pair.get("baseToken", {})
    name = html.escape(base_token.get("name", "Unknown"))
    symbol = html.escape(base_token.get("symbol", "UNKNOWN"))
    address = pair.get("baseToken", {}).get("address", "")
    chain = pair.get("chainId", "N/A").upper()
    
    price_usd = float(pair.get("priceUsd", 0))
    mcap = float(pair.get("fdv", pair.get("marketCap", 0)))
    vol_5m = float(pair.get("volume", {}).get("m5", 0))
    dex_url = pair.get("url", "")

    status = security_info.get("status", "UNVERIFIED")
    sec_badge = "🟢 SAFE" if status == "SAFE" else ("🔴 UNSAFE" if status == "UNSAFE" else "🟡 UNVERIFIED")
    
    # Calculate Dynamic Exit Targets
    tp1 = price_usd * 1.25  # +25%
    tp2 = price_usd * 1.50  # +50%
    tp3 = price_usd * 2.00  # +100% (2x)
    sl = price_usd * 0.85   # -15% Stop Loss

    # Dynamic Strategy Setup based on Volume Profile
    if vol_5m >= 50000:
        hold_time = "⚡ Scalp (5 to 20 mins)"
        entry_strategy = "Immediate market fill on 1m pullback"
    elif vol_5m >= 10000:
        hold_time = "🕒 Short Swing (1 to 4 hours)"
        entry_strategy = "Limit order at 3-5% pullback zone"
    else:
        hold_time = "⏳ Low Volatility Watch (15m to 1 hour)"
        entry_strategy = "Limit order at immediate micro support"

    msg = (
        f"<b>🚨 ACCELERATION ALERT: {symbol}</b>\n\n"
        f"<b>Token:</b> {name} (${symbol})\n"
        f"<b>Chain:</b> {chain}\n"
        f"<b>Security:</b> {sec_badge}\n\n"
        f"<b>Market Cap:</b> ${mcap:,.0f} (<b>+{mcap_growth:.1f}%</b> in 5m)\n"
        f"<b>5m Volume:</b> ${vol_5m:,.0f} (<b>+{vol_growth:.1f}%</b> in 5m)\n"
        f"<b>Current Price:</b> ${price_usd:.8f}\n\n"
        f"<b>🕵️ INSIDER DATA & AUDIT</b>\n"
        f"<b>Dev Holdings:</b> {security_info.get('dev_pct')}\n"
        f"<b>Mintable:</b> {security_info.get('is_mintable')}\n"
        f"<b>Freezable:</b> {security_info.get('is_freezable')}\n"
        f"<b>LP Status:</b> {security_info.get('lp_status')}\n\n"
        f"<b>🎯 EXECUTION SETUP</b>\n"
        f"<b>Entry Strategy:</b> {entry_strategy}\n"
        f"<b>Hold Duration:</b> {hold_time}\n"
        f"<b>Target TP1 (+25%):</b> ${tp1:.8f}\n"
        f"<b>Target TP2 (+50%):</b> ${tp2:.8f}\n"
        f"<b>Target TP3 (+100%):</b> ${tp3:.8f}\n"
        f"<b>Stop Loss (-15%):</b> ${sl:.8f}\n\n"
        f"<b>Contract:</b> <code>{address}</code>\n"
        f"<a href='{dex_url}'>📈 View on DEXScreener</a>"
    )

    try:
        logging.info(f"DISPATCHING TELEGRAM ALERT FOR {symbol}...")
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

async def monitor_market(app: Application):
    async with aiohttp.ClientSession() as session:
        while True:
            async with STATE_LOCK:
                current_watchlist = list(WATCHLIST)

            # High-Volume Trending Token Discovery Endpoint (Robust Parsing)
            if not current_watchlist:
                try:
                    async with session.get(DEXSCREENER_BOOSTED_TOP, timeout=8) as resp:
                        if resp.status == 200:
                            raw_data = await resp.json()
                            items = raw_data if isinstance(raw_data, list) else raw_data.get("tokens", [])
                            if isinstance(items, list):
                                current_watchlist = [
                                    item.get("tokenAddress") for item in items 
                                    if isinstance(item, dict) and item.get("tokenAddress")
                                ][:25]
                except Exception as e:
                    logging.error(f"Trending discovery error: {e}")

            if current_watchlist:
                addresses = ",".join(current_watchlist[:30])
                try:
                    async with session.get(DEXSCREENER_BATCH_URL.format(addresses), timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pairs = data.get("pairs", [])
                            now = datetime.now().timestamp()

                            logging.info(f"Processing {len(pairs)} active pairs...")

                            for pair in pairs:
                                token_addr = normalize_address(pair.get("baseToken", {}).get("address", ""))
                                chain = pair.get("chainId", "")
                                vol_5m = float(pair.get("volume", {}).get("m5", 0))
                                mcap = float(pair.get("fdv", pair.get("marketCap", 0)))

                                history = PRICE_HISTORY[token_addr]
                                history.append((now, vol_5m, mcap))

                                # Requires 2 ticks (approx 10-20 seconds) to determine dynamic volume acceleration
                                if len(history) < 2:
                                    continue

                                base_time, base_vol, base_mcap = history[0]
                                vol_growth = ((vol_5m - base_vol) / max(base_vol, 1.0)) * 100.0
                                mcap_growth = ((mcap - base_mcap) / max(base_mcap, 1.0)) * 100.0

                                logging.info(f"Token: {token_addr[:8]}... | Vol: ${vol_5m:,.0f} ({vol_growth:+.1f}%) | MCap: ${mcap:,.0f} ({mcap_growth:+.1f}%)")

                                if vol_5m >= MIN_5M_VOLUME and vol_growth >= MIN_5M_VOL_EXPANSION and mcap_growth >= MIN_5M_MCAP_EXPANSION:
                                    alert_key = f"{token_addr}_{int(now // 300)}"
                                    
                                    async with STATE_LOCK:
                                        already_seen = alert_key in SEEN_TOKENS

                                    if not already_seen:
                                        sec_info = await check_goplus_security(session, chain, token_addr)
                                        if sec_info.get("status") != "UNSAFE":
                                            await dispatch_telegram_alert(app, pair, sec_info, vol_growth, mcap_growth)
                                            async with STATE_LOCK:
                                                SEEN_TOKENS.add(alert_key)
                                            await save_state_async()

                except Exception as e:
                    logging.error(f"Market scanning cycle error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

# ==========================================
# COMMAND HANDLERS
# ==========================================
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    if not context.args:
        await update.message.reply_text("Usage: /add <TOKEN_ADDRESS>")
        return

    addr = normalize_address(context.args[0])
    async with STATE_LOCK:
        WATCHLIST.add(addr)
    await save_state_async()
    await update.message.reply_text(f"✅ Added to scanner: <code>{addr}</code>", parse_mode="HTML")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    if not context.args:
        await update.message.reply_text("Usage: /remove <TOKEN_ADDRESS>")
        return

    addr = normalize_address(context.args[0])
    async with STATE_LOCK:
        WATCHLIST.discard(addr)
    await save_state_async()
    await update.message.reply_text(f"❌ Removed from scanner: <code>{addr}</code>", parse_mode="HTML")

# ==========================================
# HEALTH SERVER & BOT LAUNCH
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

async def main():
    await load_state_async()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logging.info("Bot initiated successfully with Insider Security Metrics.")
    await monitor_market(app)

if __name__ == "__main__":
    threading.Thread(target=run_health_check_server, daemon=True).start()
    asyncio.run(main())
