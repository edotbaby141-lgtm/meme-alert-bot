import os
import json
import asyncio
import logging
import threading
import httpx
from flask import Flask
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- SECURE CREDENTIALS ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

# --- STRATEGY FILTERS FOR EARLY NEW LISTINGS ---
EARLY_MIN_MARKET_CAP = 20000.0   # $20k Minimum FDV for new micro-caps
EARLY_MIN_LIQUIDITY = 8000.0     # $8k Minimum Liquidity
EARLY_MIN_5M_VOLUME = 3000.0     # $3k 5m Volume Surge
MIN_BUY_RATIO = 50.0             # 50%+ Buyers vs Sellers

# --- INITIAL WATCHLIST TARGETS (TICKER TO ADDRESS / SEARCH MAP) ---
DEFAULT_WATCHLIST_TOKENS = {
    # High-Cap Trendings from DexScreener Watchlist UI
    "AI": "0xBDA011D7F8EC00F66C1923B049B94c67d148d8b2",
    "PONS": "0x1111111111111111111111111111111111111111",
    "STONK": "0x2222222222222222222222222222222222222222",
    "MEME": "0xb131f4a55907b10d1f0a50d8ab8fa09ec342cd74",
    "MARSCOIN": "0x1395000000000000000000000000000000000000",
    "ZCAT": "0x4444444444444444444444444444444444444444",
    "CATE": "2R9hsvLbNvGUCKHywdVn6Rzg4UYtiQYHG9hygtrspump",
    "CASHCAT": "0x1859000000000000000000000000000000000000",
    "USELESS": "8DvNR14E5e5Cump2e7N2x4xypA2W14r9s4E3S4yupump",
    "BONER": "0x4590000000000000000000000000000000000000",
    "PEPE": "0x6982508145454ce325ddbe47a25d4ec3d2311933",
    "ARB": "0x912ce59144191c1204e64559fe8253a0e49e6548",
    "SUI": "0x2::sui::SUI"
}

WATCHLIST_FILE = "watchlist.json"
SEEN_FILE = "seen_tokens.json"

# --- PERSISTENT STORAGE MANAGERS ---
def load_watchlist() -> dict:
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading watchlist: {e}")
    return DEFAULT_WATCHLIST_TOKENS.copy()

def save_watchlist(watchlist: dict):
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump(watchlist, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving watchlist: {e}")

def load_alerted_tokens() -> set:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except Exception as e:
            logger.error(f"Error loading seen tokens: {e}")
    return set()

def save_alerted_tokens(tokens_set: set):
    try:
        capped = list(tokens_set)[-1500:]
        with open(SEEN_FILE, "w") as f:
            json.dump(capped, f)
    except Exception as e:
        logger.error(f"Error saving seen tokens: {e}")

watchlist_tokens = load_watchlist()
alerted_tokens = load_alerted_tokens()

# --- FLASK HEALTH SERVER (KEEP-ALIVE BIND) ---
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "Bot Engine Operational 24/7", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# --- SANITIZE MARKDOWN ---
def sanitize_md(text: str) -> str:
    chars = ["*", "_", "`", "["]
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text

# --- PARSE DEX DATA ---
def parse_pair_data(pair: dict) -> dict:
    chain_id = pair.get("chainId", "UNKNOWN").lower()
    token_name = sanitize_md(pair.get("baseToken", {}).get("name", "Unknown"))
    symbol = sanitize_md(pair.get("baseToken", {}).get("symbol", "UNKNOWN"))
    
    mcap = float(pair.get("fdv", 0.0) or pair.get("marketCap", 0.0) or 0.0)
    vol_5m = float(pair.get("volume", {}).get("m5", 0.0) or 0.0)
    liquidity = float(pair.get("liquidity", {}).get("usd", 0.0) or 0.0)
    
    buys_5m = pair.get("txns", {}).get("m5", {}).get("buys", 0)
    sells_5m = pair.get("txns", {}).get("m5", {}).get("sells", 0)
    total_txns = buys_5m + sells_5m
    buy_ratio = (buys_5m / total_txns * 100) if total_txns > 0 else 0.0

    if buy_ratio >= 60.0 and liquidity >= 15000:
        status = "STRONG 🟢"
        signal_type = "🟢 CONFIRMED BULLISH MOMENTUM 🟢"
    elif buy_ratio <= 40.0:
        status = "WEAK / RISK 🔴"
        signal_type = "🔴 HIGH SELLING PRESSURE 🔴"
    else:
        status = "NEUTRAL 🟡"
        signal_type = "🟡 CONSOLIDATION RANGE 🟡"

    if mcap >= 100000000.0: # $100M+ Market Cap
        hold_time = "📈 MACRO TREND HOLD (Days to Weeks)"
        forecast = "🚀 MAJOR HIGH-CAP EXPANSION MOVE"
    elif mcap >= 1000000.0: # $1M+
        hold_time = "⏱️ MID-TERM SWING (Hours to Days)"
        forecast = "⚡ ESTABLISHED BREAKOUT (Continuation)"
    else:
        hold_time = "⚡ FAST SCALP ENTRY (3 to 30 Mins)"
        forecast = "🔥 EARLY MICRO-CAP SURGE (High Volatility)"

    return {
        "chain": chain_id.upper(),
        "name": token_name,
        "symbol": symbol,
        "mcap": mcap,
        "status": status,
        "signal_type": signal_type,
        "vol_5m": vol_5m,
        "liquidity": liquidity,
        "buy_ratio": buy_ratio,
        "buys": buys_5m,
        "sells": sells_5m,
        "forecast": forecast,
        "hold_time": hold_time,
        "dex_url": pair.get("url", f"https://dexscreener.com/{chain_id}/{pair.get('baseToken', {}).get('address', '')}")
    }

# --- BATCH DEX DATA FETCH ---
async def fetch_batch_dex_data(client: httpx.AsyncClient, addresses: list) -> dict:
    if not addresses:
        return {}
        
    query_str = ",".join(addresses[:30])
    url = f"https://api.dexscreener.com/latest/dex/tokens/{query_str}"
    
    try:
        response = await client.get(url, timeout=10.0)
        if response.status_code != 200:
            return {}
            
        res = response.json()
        pairs = res.get("pairs")
        if not pairs:
            return {}

        results = {}
        for pair in pairs:
            raw_addr = pair.get("baseToken", {}).get("address")
            if not raw_addr:
                continue
            
            addr_key = raw_addr.lower()
            liq = float(pair.get("liquidity", {}).get("usd", 0.0) or 0.0)
            
            if addr_key not in results or liq > results[addr_key].get("liquidity", 0.0):
                results[addr_key] = parse_pair_data(pair)
                
        return results
    except Exception as e:
        logger.error(f"Error fetching DexScreener batch: {e}")
        return {}

# --- TELEGRAM DISPATCHER ---
async def dispatch_telegram_alert(app, addr: str, data: dict, tag: str = "BREAKOUT"):
    msg = (
        f"🚨 **{tag}** 🚨\n"
        f"{data['signal_type']}\n\n"
        f"**Chain:** {data['chain']}\n"
        f"**Token:** ${data['symbol']} ({data['name']})\n\n"
        f"⭐ **Rating:** {data['status']}\n"
        f"💰 **Market Cap (FDV):** ${data['mcap']:,.2f}\n"
        f"🔥 **5m Volume:** ${data['vol_5m']:,.2f}\n"
        f"💧 **Liquidity:** ${data['liquidity']:,.2f}\n"
        f"🟢 **Buy Ratio:** {data['buy_ratio']:.1f}% ({data['buys']} buys / {data['sells']} sells)\n\n"
        f"📊 **EXECUTION PLAN:**\n"
        f"{data['forecast']}\n"
        f"• **Hold Duration:** {data['hold_time']}\n"
        f"• **Take Profit Scale:** TP1 1.5x | TP2 2x | Runner 5x\n"
        f"• **Stop Loss:** Max -15% or break of recent swing low\n\n"
        f"📋 **Contract Address:**\n`{addr}`\n\n"
        f"📍 [Open DEXScreener Chart]({data['dex_url']})"
    )
    
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode="Markdown",
        disable_web_page_preview=True,
        disable_notification=False
    )

# --- 1. WATCHLIST ENGINE ---
async def big_coins_stream_loop(app):
    logger.info("🎯 WATCHLIST ENGINE STARTED...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                addresses = list(watchlist_tokens.values())
                for i in range(0, len(addresses), 30):
                    batch = addresses[i:i+30]
                    data_map = await fetch_batch_dex_data(client, batch)
                    
                    for symbol, raw_addr in list(watchlist_tokens.items()):
                        addr_key = raw_addr.lower()
                        data = data_map.get(addr_key)
                        
                        # Fires on high buy volume surge before major continuation moves
                        if data and data['buy_ratio'] >= 55.0 and data['vol_5m'] >= 5000.0:
                            alert_key = f"{addr_key}_big_surge"
                            if alert_key not in alerted_tokens:
                                alerted_tokens.add(alert_key)
                                save_alerted_tokens(alerted_tokens)
                                await dispatch_telegram_alert(app, raw_addr, data, tag=f"WATCHLIST SURGE (${symbol})")
                    await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Error in watchlist loop: {e}")
            await asyncio.sleep(35)

# --- 2. EARLY NEW LISTINGS ENGINE ---
async def new_tokens_stream_loop(app):
    logger.info("🌱 NEW EARLY LISTINGS ENGINE STARTED...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                res = await client.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10.0)
                if res.status_code == 200:
                    items = res.json()
                    candidate_addrs = [item.get("tokenAddress") for item in items[:30] if item.get("tokenAddress")]
                    unseen_addrs = [a for a in candidate_addrs if a.lower() not in alerted_tokens]

                    if unseen_addrs:
                        data_map = await fetch_batch_dex_data(client, unseen_addrs)
                        for raw_addr in unseen_addrs:
                            addr_key = raw_addr.lower()
                            data = data_map.get(addr_key)
                            
                            if data and (data['mcap'] >= EARLY_MIN_MARKET_CAP and 
                                        data['liquidity'] >= EARLY_MIN_LIQUIDITY and 
                                        data['vol_5m'] >= EARLY_MIN_5M_VOLUME and 
                                        data['buy_ratio'] >= MIN_BUY_RATIO):
                                
                                alerted_tokens.add(addr_key)
                                save_alerted_tokens(alerted_tokens)
                                await dispatch_telegram_alert(app, raw_addr, data, tag="EARLY NEW TOKEN SPIKE")
            except Exception as e:
                logger.error(f"Error in new tokens loop: {e}")
            await asyncio.sleep(25)

# --- 3. TOP DEX BOOSTS ENGINE ---
async def boosted_stream_loop(app):
    logger.info("⚡ TOP DEX BOOSTS ENGINE STARTED...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                res = await client.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10.0)
                if res.status_code == 200:
                    items = res.json()
                    items_list = items.get("data", items) if isinstance(items, dict) else items
                    if isinstance(items_list, list):
                        candidate_addrs = [i.get("tokenAddress") for i in items_list[:20] if i.get("tokenAddress")]
                        unseen_addrs = [a for a in candidate_addrs if a.lower() not in alerted_tokens]

                        if unseen_addrs:
                            data_map = await fetch_batch_dex_data(client, unseen_addrs)
                            for raw_addr in unseen_addrs:
                                addr_key = raw_addr.lower()
                                data = data_map.get(addr_key)
                                if data and (data['mcap'] >= EARLY_MIN_MARKET_CAP and 
                                            data['liquidity'] >= EARLY_MIN_LIQUIDITY and 
                                            data['vol_5m'] >= EARLY_MIN_5M_VOLUME and 
                                            data['buy_ratio'] >= MIN_BUY_RATIO):
                                    
                                    alerted_tokens.add(addr_key)
                                    save_alerted_tokens(alerted_tokens)
                                    await dispatch_telegram_alert(app, raw_addr, data, tag="BOOSTED BREAKOUT")
            except Exception as e:
                logger.error(f"Error in boost loop: {e}")
            await asyncio.sleep(20)

# --- TELEGRAM COMMAND HANDLERS ---
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("⚠️ Usage: `/add SYMBOL CONTRACT_ADDRESS`", parse_mode="Markdown")
        return
    
    symbol = context.args[0].upper()
    address = context.args[1].strip()
    
    watchlist_tokens[symbol] = address
    save_watchlist(watchlist_tokens)
    await update.message.reply_text(f"✅ Added **${symbol}** to Watchlist:\n`{address}`", parse_mode="Markdown")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/remove SYMBOL`", parse_mode="Markdown")
        return
    
    symbol = context.args[0].upper()
    if symbol in watchlist_tokens:
        del watchlist_tokens[symbol]
        save_watchlist(watchlist_tokens)
        await update.message.reply_text(f"❌ Removed **${symbol}** from Watchlist.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ **${symbol}** not found in Watchlist.", parse_mode="Markdown")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist_tokens:
        await update.message.reply_text("Watchlist is currently empty.")
        return
    
    lines = [f"• **${sym}**: `{addr[:8]}...{addr[-6:]}`" for sym, addr in watchlist_tokens.items()]
    msg = "📋 **ACTIVE WATCHLIST TOKENS:**\n\n" + "\n".join(lines)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def handle_address_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) < 30 or len(text) > 50 or " " in text:
        return

    await update.message.reply_text("🔎 Fetching DexScreener metrics...")
    async with httpx.AsyncClient() as client:
        data_map = await fetch_batch_dex_data(client, [text])

    data = data_map.get(text.lower())
    if not data:
        await update.message.reply_text("❌ Token not found or no active liquidity pairs.")
        return

    await dispatch_telegram_alert(context.application, text, data, tag="MANUAL LOOKUP")

# --- MAIN RUNNER ---
async def main():
    threading.Thread(target=run_web_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address_paste))

    await app.initialize()
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.start()

    # Concurrent Execution
    asyncio.create_task(big_coins_stream_loop(app))
    asyncio.create_task(new_tokens_stream_loop(app))
    asyncio.create_task(boosted_stream_loop(app))

    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
