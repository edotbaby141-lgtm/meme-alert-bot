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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ==========================================
# CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
CRYPTOPANIC_API_KEY = os.environ.get("CRYPTOPANIC_API_KEY", "") # Optional for extended news feed

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

POLL_INTERVAL = 10     # Seconds between scans
PUMP_5M_MIN = 5.0      # +5.0% change triggers a PUMP alert
DUMP_5M_MIN = -5.0     # -5.0% change triggers a DUMP alert
MIN_5M_VOLUME = 5000.0 # Minimum volume filter ($)

# ==========================================
# STATE & CACHE MANAGEMENT
# ==========================================
WATCHLIST = set()
SEEN_TOKENS = set()
PRICE_HISTORY = defaultdict(lambda: deque(maxlen=30))
SOCIAL_MENTIONS = defaultdict(list)
STATE_LOCK = asyncio.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def normalize_address(address: str) -> str:
    if re.match(r"^0x[a-fA-F0-9]{40}$", address):
        return address.lower()
    return address.strip()

# ==========================================
# INSIDER & SECURITY AUDIT (GOPLUS)
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
        logging.error(f"GoPlus Security Error: {e}")
    return audit

# ==========================================
# ALERT DISPATCH ENGINE WITH INLINE BUTTONS
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
    dex_url = pair.get("url", "")

    recent_tweets = SOCIAL_MENTIONS.get(address, [])
    social_text = "No recent viral tweets" if not recent_tweets else f"💬 {recent_tweets[-1]}"

    if alert_type == "BULLISH_PUMP":
        header = f"🚀 <b>BULLISH PUMP DETECTED (+{mcap_change:.1f}%)</b>"
        action = "🟢 ACCORDING TO SYSTEM: ACCEPT / BULLISH ENTRY"
        tp1, sl = price_usd * 1.30, price_usd * 0.88
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
        f"<b>Market Cap:</b> ${mcap:,.0f} (<b>{mcap_change:+.1f}%</b> in 5m)\n"
        f"<b>5m Volume:</b> ${vol_5m:,.0f}\n"
        f"<b>Current Price:</b> ${price_usd:.8f}\n\n"
        f"<b>🐦 HIGH IMPACT NEWS & X (TWITTER) SIGNALS</b>\n"
        f"<code>{html.escape(social_text)}</code>\n\n"
        f"<b>🕵️ INSIDER DATA</b>\n"
        f"<b>Dev Holdings:</b> {security_info.get('dev_pct')}\n"
        f"<b>Mintable:</b> {security_info.get('is_mintable')}\n"
        f"<b>LP Status:</b> {security_info.get('lp_status')}\n\n"
        f"<b>🎯 EXECUTION SETUP</b>\n"
        f"<b>Take Profit Target:</b> ${tp1:.8f}\n"
        f"<b>Stop Loss Threshold:</b> ${sl:.8f}\n\n"
        f"<b>Contract:</b> <code>{address}</code>\n"
        f"<a href='{dex_url}'>📈 View Chart on DEXScreener</a>"
    )

    # CREATING INLINE BUTTONS
    keyboard = [
        [
            InlineKeyboardButton("🟢 Accept / Long", callback_data=f"accept_{address[:10]}"),
            InlineKeyboardButton("🔴 Decline / Skip", callback_data=f"decline_{address[:10]}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"Failed to send alert: {e}")

# ==========================================
# TELEGRAM BUTTON CLICK HANDLER
# ==========================================
async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user = query.from_user.first_name

    if data.startswith("accept_"):
        status_text = f"\n\n<b>✅ VERDICT BY {user.upper()}: ACCEPTED / TRADE LOGGED</b>"
    elif data.startswith("decline_"):
        status_text = f"\n\n<b>❌ VERDICT BY {user.upper()}: DECLINED / SKIPPED</b>"
    else:
        return

    # Update original message to remove buttons and stamp choice
    new_text = query.message.text + status_text
    try:
        await query.edit_message_text(text=new_text, parse_mode="HTML", reply_markup=None)
    except Exception as e:
        logging.error(f"Failed to update message button state: {e}")

# ==========================================
# NEW LAUNCHES & INFLUENTIAL TWEET MONITORING
# ==========================================
async def fetch_new_token_launches(session: aiohttp.ClientSession):
    """Monitors DexScreener for newly launched token profiles."""
    try:
        async with session.get(DEXSCREENER_NEW_PROFILES, timeout=8) as resp:
            if resp.status == 200:
                profiles = await resp.json()
                if isinstance(profiles, list):
                    for prof in profiles[:10]:
                        ca = prof.get("tokenAddress")
                        if ca:
                            norm_ca = normalize_address(ca)
                            async with STATE_LOCK:
                                WATCHLIST.add(norm_ca)
    except Exception as e:
        logging.error(f"Error fetching new launches: {e}")

async def fetch_crypto_news_and_influencers(session: aiohttp.ClientSession):
    """Polls crypto news feeds for breaking tweets, influencer news, or viral token tags."""
    try:
        url = "https://cryptopanic.com/api/v1/posts/?auth_token=" + CRYPTOPANIC_API_KEY if CRYPTOPANIC_API_KEY else "https://cryptopanic.com/api/v1/posts/?public=true"
        async with session.get(url, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                for post in data.get("results", [])[:5]:
                    title = post.get("title", "")
                    currencies = post.get("currencies", [])
                    for c in currencies:
                        code = c.get("code")
                        if code:
                            SOCIAL_MENTIONS[code].append(title)
    except Exception as e:
        logging.error(f"Error fetching crypto news: {e}")

# ==========================================
# MAIN SCANNING LOOP
# ==========================================
async def monitor_market(app: Application):
    async with aiohttp.ClientSession() as session:
        while True:
            # Poll newly launched tokens and high impact news
            await fetch_new_token_launches(session)
            await fetch_crypto_news_and_influencers(session)

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
                                ][:25]
                except Exception as e:
                    logging.error(f"Fallback fetch error: {e}")

            if current_watchlist:
                addresses = ",".join(current_watchlist[:30])
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
                                        already_seen = alert_key in SEEN_TOKENS

                                    if not already_seen:
                                        sec_info = await check_goplus_security(session, chain, token_addr)
                                        if sec_info.get("status") != "UNSAFE":
                                            alert_type = "BULLISH_PUMP" if is_pump else "BEARISH_DUMP"
                                            await dispatch_telegram_alert(app, pair, sec_info, mcap_growth, alert_type)
                                            async with STATE_LOCK:
                                                SEEN_TOKENS.add(alert_key)
                except Exception as e:
                    logging.error(f"Market monitor error: {e}")

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
    
    # Register callback query handler for Accept / Decline inline buttons
    app.add_handler(CallbackQueryHandler(handle_button_click))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logging.info("Bot operational with Inline Buttons, News Feed Ingestion, and New Launch Tracking.")
    await monitor_market(app)

if __name__ == "__main__":
    threading.Thread(target=run_health_check_server, daemon=True).start()
    asyncio.run(main())
