import os
import asyncio
import json
import logging
import re
import html
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import deque, defaultdict, OrderedDict
from datetime import datetime
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
# New updated Bot Token:
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8804502384:AAGZ7MjOc52VX6pHbmjq_57OWyTLSaB-8LI")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5642314005")
CRYPTOPANIC_API_KEY = os.environ.get("CRYPTOPANIC_API_KEY", "")

DEXSCREENER_BATCH_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
DEXSCREENER_BOOSTED_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_NEW_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
GOPLUS_EVM_URL = "https://api.gopluslabs.io/api/v1/token_security/{}"
GOPLUS_SOLANA_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"

EVM_CHAIN_MAP = {
    "ethereum": "1", "eth": "1", "bsc": "56", "polygon": "137",
    "arbitrum": "42161", "optimism": "10", "avalanche": "43114",
    "base": "8453", "zksync": "324", "linea": "59144"
}

POLL_INTERVAL = 10     
PUMP_5M_MIN = 5.0      
DUMP_5M_MIN = -5.0     
MIN_5M_VOLUME = 5000.0 
MIN_LIQUIDITY_USD = 10000.0  # RUG PULL FILTER: Rejects pools with < $10k liquidity

# ==========================================
# STATE & CACHE MANAGEMENT
# ==========================================
WATCHLIST = set()
SEEN_TOKENS = OrderedDict()  # Max size bounded cache to prevent RAM memory leaks
MAX_SEEN_CACHE = 2000

PRICE_HISTORY = defaultdict(lambda: deque(maxlen=30))
TOKEN_IMAGES = {}
SOCIAL_MENTIONS = defaultdict(list)
CALLBACK_LOOKUP = {}
STATE_LOCK = asyncio.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def normalize_address(address: str) -> str:
    if re.match(r"^0x[a-fA-F0-9]{40}$", address):
        return address.lower()
    return address.strip()

def track_seen_alert(key: str):
    """Bounds seen tokens set size to prevent server RAM memory leaks."""
    SEEN_TOKENS[key] = True
    if len(SEEN_TOKENS) > MAX_SEEN_CACHE:
        SEEN_TOKENS.popitem(last=False)

# ==========================================
# RUG PULL & SECURITY CHECK (GOPLUS)
# ==========================================
async def check_goplus_security(session: aiohttp.ClientSession, chain: str, token_address: str) -> dict:
    chain_lower = chain.lower()
    audit = {"status": "UNVERIFIED", "dev_pct": "N/A", "is_mintable": "No", "is_freezable": "No", "lp_status": "Unknown"}
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
                    audit["status"] = "UNSAFE" if (freezable or mintable) else "SAFE"
                    
                    creator_holding = res.get("creator_percent")
                    if creator_holding is not None:
                        audit["dev_pct"] = f"{float(creator_holding) * 100:.1f}%"
        else:
            chain_id = EVM_CHAIN_MAP.get(chain_lower)
            if chain_id:
                url = GOPLUS_EVM_URL.format(chain_id)
                async with session.get(url, params={"contract_addresses": token_address}, timeout=8) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        res = data.get("result", {}).get(token_address.lower(), {})
                        is_honeypot = res.get("is_honeypot") == "1"
                        cannot_sell = float(res.get("cannot_sell_all", 0)) == 1
                        audit["status"] = "UNSAFE" if (is_honeypot or cannot_sell) else "SAFE"
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
        logging.error(f"GoPlus Security Error for {token_address}: {e}")
    return audit

# ==========================================
# ALERT DISPATCH ENGINE WITH IMAGES & WHALE DATA
# ==========================================
async def dispatch_telegram_alert(app: Application, pair: dict, security_info: dict, mcap_change: float, alert_type: str):
    base_token = pair.get("baseToken", {})
    name = html.escape(base_token.get("name", "Unknown"))
    symbol = html.escape(base_token.get("symbol", "UNKNOWN"))
    address = pair.get("baseToken", {}).get("address", "")
    chain = pair.get("chainId", "N/A").upper()
    
    price_usd = float(pair.get("priceUsd", 0))
    mcap = float(pair.get("fdv", pair.get("marketCap", 0)))
    vol_5m = float(pair.get("volume", {}).get("m5", 0))
    liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0))
    dex_url = pair.get("url", "")

    # Retrieve image icon if available
    img_url = TOKEN_IMAGES.get(address)

    # Dynamic Hold & Sell Strategy based on Liquidity/Volume Ratio
    if liquidity_usd > 100000:
        hold_time = "🕒 Hold: 30 mins to 2 hours (High Liquidity Pool)"
    elif liquidity_usd > 25000:
        hold_time = "⚡ Hold: 10 to 30 mins (Quick Scalp)"
    else:
        hold_time = "⚠️ Hold: 2 to 10 mins MAX (Low Liquidity - Take Fast Profits)"

    # Detect Big Trader / Whale Pressure
    buys_5m = pair.get("txns", {}).get("m5", {}).get("buys", 0)
    sells_5m = pair.get("txns", {}).get("m5", {}).get("sells", 0)
    whale_activity = "🐳 Whale Accumulation" if buys_5m > (sells_5m * 2) else "⚖️ Balanced Trading"

    recent_tweets = SOCIAL_MENTIONS.get(address, []) or SOCIAL_MENTIONS.get(symbol.upper(), [])
    social_text = "No recent viral tweets" if not recent_tweets else f"💬 {recent_tweets[-1]}"

    if alert_type == "BULLISH_PUMP":
        header = f"🚀 <b>BULLISH PUMP DETECTED (+{mcap_change:.1f}%)</b>"
        action = "🟢 ACCORDING TO SYSTEM: ACCEPT / BULLISH ENTRY"
        tp1, sl = price_usd * 1.25, price_usd * 0.90
    else:
        header = f"🚨 <b>BEARISH DUMP DETECTED ({mcap_change:.1f}%)</b>"
        action = "🔴 ACCORDING TO SYSTEM: DECLINE / LIQUIDATE"
        tp1, sl = price_usd * 0.70, price_usd * 1.10

    sec_status = security_info.get("status", "UNVERIFIED")
    sec_badge = "🟢 SAFE" if sec_status == "SAFE" else ("🔴 UNSAFE" if sec_status == "UNSAFE" else "🟡 UNVERIFIED")

    msg = (
        f"{header}\n\n"
        f"<b>Token:</b> {name} (${symbol})\n"
        f"<b>Chain:</b> {chain}\n"
        f"<b>Security:</b> {sec_badge}\n"
        f"<b>Action Guidance:</b> <code>{action}</code>\n\n"
        f"<b>Price USD:</b> ${price_usd:.8f}\n"
        f"<b>Liquidity:</b> ${liquidity_usd:,.0f}\n"
        f"<b>Market Cap:</b> ${mcap:,.0f} (<b>{mcap_change:+.1f}%</b> in 5m)\n"
        f"<b>5m Volume:</b> ${vol_5m:,.0f}\n"
        f"<b>DEX Activity:</b> {whale_activity} ({buys_5m} Buys / {sells_5m} Sells)\n\n"
        f"<b>⏱️ RECOMMENDED TIMEFRAME</b>\n"
        f"<b>Strategy:</b> {hold_time}\n"
        f"<b>Take Profit Target:</b> ${tp1:.8f}\n"
        f"<b>Stop Loss Threshold:</b> ${sl:.8f}\n\n"
        f"<b>🐦 HIGH IMPACT NEWS & SOCIALS</b>\n"
        f"<code>{html.escape(social_text)}</code>\n\n"
        f"<b>🕵️ INSIDER & RUG AUDIT</b>\n"
        f"<b>Dev Holdings:</b> {security_info.get('dev_pct')}\n"
        f"<b>Mintable:</b> {security_info.get('is_mintable')}\n"
        f"<b>LP Status:</b> {security_info.get('lp_status')}\n\n"
        f"<b>Contract:</b> <code>{address}</code>\n"
        f"<a href='{dex_url}'>📈 View Chart on DEXScreener</a>"
    )

    cb_id = str(abs(hash(address + str(datetime.now().timestamp()))))[:12]
    CALLBACK_LOOKUP[cb_id] = address

    keyboard = [
        [
            InlineKeyboardButton("🟢 Accept / Long", callback_data=f"acc_{cb_id}"),
            InlineKeyboardButton("🔴 Decline / Skip", callback_data=f"dec_{cb_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if img_url:
            await app.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=img_url,
                caption=msg,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        else:
            await app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
    except Exception as e:
        logging.error(f"Failed to send alert with image: {e}")

# ==========================================
# BUTTON HANDLER
# ==========================================
async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user = html.escape(query.from_user.first_name)

    action, _, cb_id = data.partition("_")
    full_address = CALLBACK_LOOKUP.get(cb_id, "Unknown Contract")

    if action == "acc":
        verdict = f"\n\n<b>✅ VERDICT BY {user.upper()}: ACCEPTED / LOGGED</b>\n<code>{full_address}</code>"
    elif action == "dec":
        verdict = f"\n\n<b>❌ VERDICT BY {user.upper()}: DECLINED / SKIPPED</b>\n<code>{full_address}</code>"
    else:
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=verdict,
            reply_to_message_id=query.message.message_id,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Failed to handle button callback: {e}")

# ==========================================
# IMAGE ICON & NEW LAUNCH INGESTION
# ==========================================
async def fetch_new_token_launches(session: aiohttp.ClientSession):
    try:
        async with session.get(DEXSCREENER_NEW_PROFILES, timeout=8) as resp:
            if resp.status == 200:
                profiles = await resp.json()
                if isinstance(profiles, list):
                    for prof in profiles[:15]:
                        ca = prof.get("tokenAddress")
                        icon = prof.get("icon")
                        if ca:
                            norm_ca = normalize_address(ca)
                            if icon:
                                TOKEN_IMAGES[norm_ca] = icon
                            async with STATE_LOCK:
                                WATCHLIST.add(norm_ca)
    except Exception as e:
        logging.error(f"Error fetching token profiles and images: {e}")

# ==========================================
# MAIN SCANNING LOOP
# ==========================================
async def monitor_market(app: Application):
    async with aiohttp.ClientSession() as session:
        while True:
            await fetch_new_token_launches(session)

            async with STATE_LOCK:
                current_watchlist = list(WATCHLIST)

            if not current_watchlist:
                try:
                    async with session.get(DEXSCREENER_BOOSTED_TOP, timeout=8) as resp:
                        if resp.status == 200:
                            raw = await resp.json()
                            items = raw if isinstance(raw, list) else raw.get("tokens", [])
                            if isinstance(items, list):
                                current_watchlist = [
                                    item.get("tokenAddress") for item in items 
                                    if isinstance(item, dict) and item.get("tokenAddress")
                                ]
                except Exception as e:
                    logging.error(f"Fallback fetch error: {e}")

            chunk_size = 30
            for i in range(0, len(current_watchlist), chunk_size):
                chunk = current_watchlist[i:i + chunk_size]
                addresses = ",".join(chunk)
                
                try:
                    async with session.get(DEXSCREENER_BATCH_URL.format(addresses), timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pairs = data.get("pairs", []) or []
                            now = datetime.now().timestamp()

                            pending_alerts = []

                            for pair in pairs:
                                token_addr = normalize_address(pair.get("baseToken", {}).get("address", ""))
                                chain = pair.get("chainId", "")
                                vol_5m = float(pair.get("volume", {}).get("m5", 0))
                                mcap = float(pair.get("fdv", pair.get("marketCap", 0)))
                                liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0))

                                # RUG PULL FILTER: Reject low-liquidity coins instantly
                                if liquidity_usd < MIN_LIQUIDITY_USD:
                                    continue

                                history = PRICE_HISTORY[token_addr]
                                history.append((now, vol_5m, mcap))

                                if len(history) < 2:
                                    continue

                                base_time, base_vol, base_mcap = history[0]
                                mcap_growth = ((mcap - base_mcap) / max(base_mcap, 1.0)) * 100.0

                                is_pump = mcap_growth >= PUMP_5M_MIN and vol_5m >= MIN_5M_VOLUME
                                is_dump = mcap_growth <= DUMP_5M_MIN and vol_5m >= MIN_5M_VOLUME

                                if is_pump or is_dump:
                                    alert_key = f"{token_addr}_{int(now // 300)}"
                                    
                                    async with STATE_LOCK:
                                        if alert_key not in SEEN_TOKENS:
                                            track_seen_alert(alert_key)
                                            alert_type = "BULLISH_PUMP" if is_pump else "BEARISH_DUMP"
                                            pending_alerts.append((pair, chain, token_addr, mcap_growth, alert_type))

                            if pending_alerts:
                                tasks = [
                                    check_goplus_security(session, chain, token_addr)
                                    for _, chain, token_addr, _, _ in pending_alerts
                                ]
                                security_results = await asyncio.gather(*tasks)

                                for (pair, _, _, mcap_growth, alert_type), sec_info in zip(pending_alerts, security_results):
                                    if sec_info.get("status") != "UNSAFE":
                                        await dispatch_telegram_alert(app, pair, sec_info, mcap_growth, alert_type)

                except Exception as e:
                    logging.error(f"Market monitor chunk error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

# ==========================================
# SERVER & APPLICATION LAUNCH
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
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_button_click))

    await app.initialize()
    await app.start()
    
    # Drops pending updates on launch to avoid HTTP 409 conflict errors
    await app.updater.start_polling(drop_pending_updates=True)

    logging.info("Bot online with updated token: 8804502384:AAGZ7MjOc52VX6pHbmjq_57OWyTLSaB-8LI")
    await monitor_market(app)

if __name__ == "__main__":
    threading.Thread(target=run_health_check_server, daemon=True).start()
    asyncio.run(main())
