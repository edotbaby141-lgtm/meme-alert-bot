import os
import json
import asyncio
import logging
import threading
import httpx
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# --- LOGGING CONFIGURATION ---
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- SECURE CREDENTIALS ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8804502384:AAHYjDaiM_sj7p3t1MRCSKJA5XMoUmqWINo")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5642314005")

# --- STRATEGY FILTERS ---
MIN_MARKET_CAP = 20000.0      # $20k Minimum FDV
MIN_LIQUIDITY = 8000.0        # $8k Minimum Liquidity
MIN_5M_VOLUME = 3000.0        # $3k 5m Volume Surge
MIN_BUY_RATIO = 50.0          # 50%+ Buyers vs Sellers

# --- TARGET WATCHLIST CONTRACT ADDRESSES (EXACT TOKEN MATCHES) ---
# Prevents fake/scam coin triggers caused by raw string search queries
TARGET_WATCHLIST_ADDRESSES = [
    "6p6xgHyF7AeE6TZGeB3S1EPb4343q84343q84343q", # Solana / EVM official token contracts
    "0x6b175474e89094c44da98b954eedeac495271d0f", # Add exact CA strings here
]

# --- PERSISTENT SEEN TOKENS STORE ---
SEEN_FILE = "seen_tokens.json"

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
        capped = list(tokens_set)[-1000:]
        with open(SEEN_FILE, "w") as f:
            json.dump(capped, f)
    except Exception as e:
        logger.error(f"Error saving seen tokens: {e}")

alerted_tokens = load_alerted_tokens()

# --- FLASK HEALTH DUMMY SERVER ---
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "Bot is active 24/7", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# --- ESCAPE MARKDOWN FOR TELEGRAM V1 ---
def sanitize_md(text: str) -> str:
    chars = ["*", "_", "`", "["]
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text

# --- BATCH DEXSCREENER PARSER ---
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

    if buy_ratio >= 65.0 and liquidity >= 20000 and vol_5m >= 10000:
        status = "STRONG 🟢"
        signal_type = "🟢 CONFIRMED BREAKOUT MOMENTUM 🟢"
    elif buy_ratio <= 40.0 or liquidity < 5000:
        status = "WEAK / HIGH RISK 🔴"
        signal_type = "🔴 UNTESTED / LOW BUY PRESSURE 🔴"
    else:
        status = "NEUTRAL 🟡"
        signal_type = "🟡 EARLY SPECULATIVE CONSOLIDATION 🟡"

    if mcap >= 1000000.0:
        hold_time = "📈 SWING HOLD (Hours to Days)"
        forecast = "🚀 ESTABLISHED BREAKOUT (Potential Continuation)"
    elif mcap >= 200000.0:
        hold_time = "⏱️ MID-TERM SCALP (15 Mins to 2 Hours)"
        forecast = "⚡ MOMENTUM EXPANSION (1.5x - 3x Move)"
    else:
        hold_time = "⚡ FAST SCALP (3 to 15 Minutes)"
        forecast = "🔥 EARLY ENTRY SURGE (Quick Profit Targets)"

    return {
        "chain": chain_id.upper(),
        "chain_raw": chain_id,
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
        
    query_str = ",".join(addresses)
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
        logger.error(f"Error fetching batch DexScreener data: {e}")
        return {}

# --- TELEGRAM DISPATCHER ---
async def dispatch_telegram_alert(app, addr: str, data: dict, tag: str = "BREAKOUT"):
    msg = (
        f"🚨 **{tag} DETECTED** 🚨\n"
        f"{data['signal_type']}\n\n"
        f"**Chain:** {data['chain']}\n"
        f"**Token:** ${data['symbol']} ({data['name']})\n\n"
        f"⭐ **Rating:** {data['status']}\n"
        f"💰 **Market Cap (FDV):** ${data['mcap']:,.2f}\n"
        f"🔥 **5m Volume Surge:** ${data['vol_5m']:,.2f}\n"
        f"💧 **Liquidity:** ${data['liquidity']:,.2f}\n"
        f"🟢 **Buy Ratio:** {data['buy_ratio']:.1f}% ({data['buys']} buys / {data['sells']} sells)\n\n"
        f"📊 **RISK & PROFIT EXECUTION PLAN:**\n"
        f"{data['forecast']}\n"
        f"• **Hold Duration:** {data['hold_time']}\n"
        f"• **Take Profit Scale:** TP1 1.5x | TP2 2x | Runner 5x\n"
        f"• **Stop Loss:** Max -15% or break of recent 5m swing low\n\n"
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

# --- 1. OPTIMIZED WATCHLIST LOOP ---
async def target_coins_stream_loop(app):
    logger.info("🎯 WATCHLIST ENGINE STARTED: Batch scanning target CAs...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if TARGET_WATCHLIST_ADDRESSES:
                    data_map = await fetch_batch_dex_data(client, TARGET_WATCHLIST_ADDRESSES)
                    for raw_addr in TARGET_WATCHLIST_ADDRESSES:
                        addr_key = raw_addr.lower()
                        data = data_map.get(addr_key)
                        if data and addr_key not in alerted_tokens:
                            if (data['mcap'] >= MIN_MARKET_CAP and 
                                data['liquidity'] >= MIN_LIQUIDITY and 
                                data['vol_5m'] >= MIN_5M_VOLUME and 
                                data['buy_ratio'] >= MIN_BUY_RATIO):
                                
                                alerted_tokens.add(addr_key)
                                save_alerted_tokens(alerted_tokens)
                                await dispatch_telegram_alert(app, raw_addr, data, tag=f"WATCHLIST MATCH (${data['symbol']})")
            except Exception as e:
                logger.error(f"Error in watchlist loop: {e}")
            await asyncio.sleep(45)

# --- 2. OPTIMIZED NEW TOKENS LOOP ---
async def new_tokens_stream_loop(app):
    logger.info("🌱 NEW LISTINGS ENGINE STARTED: Tracking early tokens...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                res = await client.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10.0)
                if res.status_code == 200:
                    items = res.json()
                    candidate_addrs = [item.get("tokenAddress") for item in items[:15] if item.get("tokenAddress")]
                    unseen_addrs = [a for a in candidate_addrs if a.lower() not in alerted_tokens]

                    if unseen_addrs:
                        data_map = await fetch_batch_dex_data(client, unseen_addrs)
                        for raw_addr in unseen_addrs:
                            addr_key = raw_addr.lower()
                            data = data_map.get(addr_key)
                            if data and (data['mcap'] >= MIN_MARKET_CAP and 
                                        data['liquidity'] >= MIN_LIQUIDITY and 
                                        data['vol_5m'] >= MIN_5M_VOLUME and 
                                        data['buy_ratio'] >= MIN_BUY_RATIO):
                                
                                alerted_tokens.add(addr_key)
                                save_alerted_tokens(alerted_tokens)
                                await dispatch_telegram_alert(app, raw_addr, data, tag="EARLY NEW TOKEN LISTING")
            except Exception as e:
                logger.error(f"Error in new tokens loop: {e}")
            await asyncio.sleep(30)

# --- 3. BOOSTED TOKENS LOOP ---
async def boosted_stream_loop(app):
    logger.info("⚡ BOOST ENGINE STARTED: Watching DexScreener Top Boosts...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                res = await client.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10.0)
                if res.status_code == 200:
                    items = res.json()
                    items_list = items.get("data", items) if isinstance(items, dict) else items
                    if isinstance(items_list, list):
                        candidate_addrs = [i.get("tokenAddress") for i in items_list[:15] if i.get("tokenAddress")]
                        unseen_addrs = [a for a in candidate_addrs if a.lower() not in alerted_tokens]

                        if unseen_addrs:
                            data_map = await fetch_batch_dex_data(client, unseen_addrs)
                            for raw_addr in unseen_addrs:
                                addr_key = raw_addr.lower()
                                data = data_map.get(addr_key)
                                if data and (data['mcap'] >= MIN_MARKET_CAP and 
                                            data['liquidity'] >= MIN_LIQUIDITY and 
                                            data['vol_5m'] >= MIN_5M_VOLUME and 
                                            data['buy_ratio'] >= MIN_BUY_RATIO):
                                    
                                    alerted_tokens.add(addr_key)
                                    save_alerted_tokens(alerted_tokens)
                                    await dispatch_telegram_alert(app, raw_addr, data, tag="BOOSTED BREAKOUT")
            except Exception as e:
                logger.error(f"Error in boost loop: {e}")
            await asyncio.sleep(25)

# --- INTERACTIVE PASTE HANDLER ---
async def handle_address_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) < 30 or len(text) > 50 or " " in text:
        return

    await update.message.reply_text("🔎 Analyzing metrics on DexScreener...")
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address_paste))

    await app.initialize()
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.start()

    # Run tasks concurrently
    asyncio.create_task(boosted_stream_loop(app))
    asyncio.create_task(new_tokens_stream_loop(app))
    asyncio.create_task(target_coins_stream_loop(app))

    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
