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
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8804502384:AAHYjDaiM_sj7p3t1MRCSKJA5XMoUmqWINo"
TELEGRAM_CHAT_ID = "5642314005"
ADMIN_USER_IDS = [5642314005]  # Restricts /add and /remove commands to you

DEXSCREENER_BATCH_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{}"
GOPLUS_SOLANA_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"

POLL_INTERVAL = 10  # Seconds between market scans
SNAPSHOT_WINDOW = 300  # 5-minute rolling window for expansion math
MAX_SEEN_CACHE = 2000

# Criteria Thresholds
MIN_5M_VOL_EXPANSION = 25.0  # % growth in 5m
MIN_5M_MCAP_EXPANSION = 15.0 # % growth in 5m
MIN_5M_VOLUME = 15000.0      # Minimum $ volume in 5m

# ==========================================
# STATE & CACHE MANAGEMENT
# ==========================================
WATCHLIST = set()
SEEN_TOKENS = set()
PRICE_HISTORY = defaultdict(lambda: deque(maxlen=30))  # 5-minute sliding snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def normalize_address(address: str) -> str:
    """Preserves Solana Base58 casing while lowercasing EVM addresses."""
    if re.match(r"^0x[a-fA-F0-9]{40}$", address):
        return address.lower()
    return address.strip()

async def save_state_async():
    """Non-blocking asynchronous file state persistence."""
    data = {
        "watchlist": list(WATCHLIST),
        "seen_tokens": list(SEEN_TOKENS)[-MAX_SEEN_CACHE:]
    }
    await asyncio.to_thread(lambda: open("bot_state.json", "w").write(json.dumps(data, indent=2)))

async def load_state_async():
    """Loads state from file if available."""
    global WATCHLIST, SEEN_TOKENS
    try:
        content = await asyncio.to_thread(lambda: open("bot_state.json", "r").read())
        data = json.loads(content)
        WATCHLIST = set(data.get("watchlist", []))
        SEEN_TOKENS = set(data.get("seen_tokens", []))
    except FileNotFoundError:
        WATCHLIST = set()
        SEEN_TOKENS = set()

# ==========================================
# SECURITY AUDIT ENGINE (GOPLUS)
# ==========================================
async def check_goplus_security(session: aiohttp.ClientSession, chain: str, token_address: str) -> str:
    """Verifies token safety returning SAFE, UNSAFE, or UNVERIFIED."""
    try:
        if chain.lower() in ["solana", "sol"]:
            async with session.get(GOPLUS_SOLANA_URL, params={"contract_addresses": [token_address]}, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    res = data.get("result", {}).get(token_address, {})
                    if res.get("freezable", {}).get("status") == "1" or res.get("mintable", {}).get("status") == "1":
                        return "UNSAFE"
                    return "SAFE"
        else:
            # Assumes EVM chain mapping (eth, base, bsc, arbitrum, etc.)
            chain_id = "1" if chain.lower() in ["ethereum", "eth"] else "8453"  # Default Base (8453)
            url = GOPLUS_EVM_URL.format(chain_id)
            async with session.get(url, params={"contract_addresses": token_address}, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    res = data.get("result", {}).get(token_address.lower(), {})
                    if res.get("is_honeypot") == "1" or float(res.get("cannot_sell_all", 0)) == 1:
                        return "UNSAFE"
                    return "SAFE"
    except Exception as e:
        logging.error(f"GoPlus check error for {token_address}: {e}")
    return "UNVERIFIED"

# ==========================================
# MARKET TRACKING & TELEGRAM DISPATCH
# ==========================================
async def dispatch_telegram_alert(app: Application, pair: dict, security_status: str, vol_growth: float, mcap_growth: float):
    """Formats cleanly using HTML escape mode to eliminate rendering failures."""
    base_token = pair.get("baseToken", {})
    name = html.escape(base_token.get("name", "Unknown"))
    symbol = html.escape(base_token.get("symbol", "UNKNOWN"))
    address = pair.get("baseToken", {}).get("address", "")
    chain = pair.get("chainId", "N/A").upper()
    
    price_usd = float(pair.get("priceUsd", 0))
    mcap = float(pair.get("fdv", pair.get("marketCap", 0)))
    vol_5m = float(pair.get("volume", {}).get("m5", 0))
    dex_url = pair.get("url", "")

    sec_badge = "🟢 SAFE" if security_status == "SAFE" else "🟡 UNVERIFIED"
    
    msg = (
        f"<b>🚨 ACCELERATION ALERT: {symbol}</b>\n\n"
        f"<b>Token:</b> {name} (${symbol})\n"
        f"<b>Chain:</b> {chain}\n"
        f"<b>Security:</b> {sec_badge}\n\n"
        f"<b>Market Cap:</b> ${mcap:,.0f} (<b>+{mcap_growth:.1f}%</b> in 5m)\n"
        f"<b>5m Volume:</b> ${vol_5m:,.0f} (<b>+{vol_growth:.1f}%</b> in 5m)\n"
        f"<b>Price:</b> ${price_usd:.8f}\n\n"
        f"<b>Contract:</b> <code>{address}</code>\n"
        f"<a href='{dex_url}'>📈 View on DEXScreener</a>"
    )

    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode="HTML",
        disable_web_page_preview=True
    )

async def monitor_market(app: Application):
    """Main scanning loop monitoring real expansion metrics using rolling windows."""
    async with aiohttp.ClientSession() as session:
        while True:
            if WATCHLIST:
                addresses = ",".join(list(WATCHLIST)[:30])
                try:
                    async with session.get(DEXSCREENER_BATCH_URL.format(addresses), timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pairs = data.get("pairs", [])
                            now = datetime.now().timestamp()

                            for pair in pairs:
                                token_addr = normalize_address(pair.get("baseToken", {}).get("address", ""))
                                chain = pair.get("chainId", "")
                                vol_5m = float(pair.get("volume", {}).get("m5", 0))
                                mcap = float(pair.get("fdv", pair.get("marketCap", 0)))

                                # Append snapshot history
                                history = PRICE_HISTORY[token_addr]
                                history.append((now, vol_5m, mcap))

                                if len(history) < 2 or vol_5m < MIN_5M_VOLUME:
                                    continue

                                # Calculate delta from 5m rolling window baseline
                                base_time, base_vol, base_mcap = history[0]
                                vol_growth = ((vol_5m - base_vol) / max(base_vol, 1.0)) * 100.0
                                mcap_growth = ((mcap - base_mcap) / max(base_mcap, 1.0)) * 100.0

                                # Alert Triggering Condition
                                if vol_growth >= MIN_5M_VOL_EXPANSION and mcap_growth >= MIN_5M_MCAP_EXPANSION:
                                    alert_key = f"{token_addr}_{int(now // 300)}"  # Max 1 alert per 5m window per token
                                    if alert_key not in SEEN_TOKENS:
                                        sec_status = await check_goplus_security(session, chain, token_addr)
                                        if sec_status != "UNSAFE":
                                            await dispatch_telegram_alert(app, pair, sec_status, vol_growth, mcap_growth)
                                            SEEN_TOKENS.add(alert_key)
                                            await save_state_async()

                except Exception as e:
                    logging.error(f"Error in scanning cycle: {e}")

            await asyncio.sleep(POLL_INTERVAL)

# ==========================================
# COMMAND HANDLERS
# ==========================================
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adds a token address to the watchlist (Restricted to Admins)."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    if not context.args:
        await update.message.reply_text("Usage: /add <TOKEN_ADDRESS>")
        return

    addr = normalize_address(context.args[0])
    WATCHLIST.add(addr)
    await save_state_async()
    await update.message.reply_text(f"✅ Added to scanner: <code>{addr}</code>", parse_mode="HTML")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes a token address from the watchlist (Restricted to Admins)."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return

    if not context.args:
        await update.message.reply_text("Usage: /remove <TOKEN_ADDRESS>")
        return

    addr = normalize_address(context.args[0])
    WATCHLIST.discard(addr)
    await save_state_async()
    await update.message.reply_text(f"❌ Removed from scanner: <code>{addr}</code>", parse_mode="HTML")

# ==========================================
# RENDER FREE SERVICE HEALTH SERVER
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ==========================================
# MAIN EXECUTION ENTRYPOINT
# ==========================================
async def main():
    await load_state_async()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))

    # Initialize bot and launch market scanner loop concurrently
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logging.info("Meme Coin Alert Bot initiated successfully.")
    await monitor_market(app)

if __name__ == "__main__":
    # Start web server on background thread for Render HTTP checks
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    # Run main Telegram bot loop
    asyncio.run(main())
