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

# --- SECURE CREDENTIALS (ENV VARS WITH FALLBACKS) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8804502384:AAHYjDaiM_sj7p3t1MRCSKJA5XMoUmqWINo")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5642314005")

# --- STRATEGY FILTERS ---
MIN_MARKET_CAP = 25000.0      # $25k Minimum FDV
MIN_LIQUIDITY = 10000.0       # $10k Minimum Liquidity
MIN_5M_VOLUME = 5000.0        # $5k 5m Volume Surge
MIN_BUY_RATIO = 50.0          # 50%+ Buyers vs Sellers
ALLOWED_CHAINS = ["solana", "ethereum", "arbitrum", "base", "bsc", "robinhood"]

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
        # Keep set capped to latest 500 entries to manage memory
        capped = list(tokens_set)[-500:]
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

# --- BATCH DEXSCREENER PARSER ---
def parse_pair_data(pair: dict) -> dict:
    chain_id = pair.get("chainId", "UNKNOWN").lower()
    token_name = pair.get("baseToken", {}).get("name", "Unknown")
    symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")
    
    # FDV prioritization to accurately match trading apps like Photon/BullX
    mcap = pair.get("fdv", 0.0) or pair.get("marketCap", 0.0)
    
    vol_5m = pair.get("volume", {}).get("m5", 0.0)
    liquidity = pair.get("liquidity", {}).get("usd", 0.0)
    
    buys_5m = pair.get("txns", {}).get("m5", {}).get("buys", 0)
    sells_5m = pair.get("txns", {}).get("m5", {}).get("sells", 0)
    total_txns = buys_5m + sells_5m
    buy_ratio = (buys_5m / total_txns * 100) if total_txns > 0 else 0.0

    if buy_ratio >= 65.0 and liquidity >= 25000 and vol_5m >= 15000:
        status = "STRONG 🟢"
        signal_type = "🟢 CONFIRMED BREAKOUT MOMENTUM 🟢"
        is_strong = True
    elif buy_ratio <= 40.0 or liquidity < 8000:
        status = "WEAK / HIGH RISK 🔴"
        signal_type = "🔴 UNTESTED / LOW BUY PRESSURE 🔴"
        is_strong = False
    else:
        status = "NEUTRAL 🟡"
        signal_type = "🟡 EARLY SPECULATIVE CONSOLIDATION 🟡"
        is_strong = False

    if mcap >= 1000000.0:
        hold_time = "📈 SWING HOLD (Hours to Days / Multi-Week Trend)"
        forecast = "🚀 ESTABLISHED BREAKOUT (Potential Multi-Bag Move)"
    elif mcap >= 300000.0:
        hold_time = "⏱️ MID-TERM SCALP (15 Mins to 2 Hours)"
        forecast = "⚡ MOMENTUM CONTINUATION (1.5x - 3x Expansion)"
    else:
        hold_time = "⚡ FAST SCALP (3 to 15 Minutes)"
        forecast = "🔥 HIGH VOLATILITY SURGE (Quick Exit Targets)"

    return {
        "chain": chain_id.upper(),
        "chain_raw": chain_id,
        "name": token_name,
        "symbol": symbol,
        "mcap": mcap,
        "status": status,
        "signal_type": signal_type,
        "is_strong": is_strong,
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
    """Fetch metrics for up to 30 tokens in a single HTTP request to prevent rate limits."""
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
        # Group pairs by base token address and select highest liquidity pool per token
        for pair in pairs:
            addr = pair.get("baseToken", {}).get("address")
            if not addr:
                continue
            
            liq = pair.get("liquidity", {}).get("usd", 0.0)
            if addr not in results or liq > results[addr].get("liquidity", 0.0):
                parsed = parse_pair_data(pair)
                results[addr] = parsed
                
        return results
    except Exception as e:
        logger.error(f"Error fetching batch DexScreener data: {e}")
        return {}

# --- INTERACTIVE MANUAL ADDRESS LOOKUP ---
async def handle_address_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if len(text) < 30 or len(text) > 50 or " " in text:
        return

    await update.message.reply_text("🔎 Analyzing breakout metrics on DexScreener...")
    
    async with httpx.AsyncClient() as client:
        data_map = await fetch_batch_dex_data(client, [text])

    data = data_map.get(text)
    if not data:
        await update.message.reply_text("❌ Token not found or no active liquidity pairs.")
        return

    msg = (
        f"{data['signal_type']}\n"
        f"**Chain:** {data['chain']}\n"
        f"**Token:** ${data['symbol']} ({data['name']})\n\n"
        f"**Rating:** {data['status']}\n"
        f"**Market Cap (FDV):** ${data['mcap']:,.2f}\n"
        f"**5m Volume:** ${data['vol_5m']:,.2f}\n"
        f"**Liquidity:** ${data['liquidity']:,.2f}\n"
        f"**Buy Pressure:** {data['buy_ratio']:.1f}% ({data['buys']} buys / {data['sells']} sells)\n\n"
        f"📊 **RISK & PROFIT EXECUTION PLAN:**\n"
        f"{data['forecast']}\n"
        f"• **Hold Style:** {data['hold_time']}\n"
        f"• **Scale TP:** 1.5x (50%) | 2.0x (30%) | Runner (20%)\n"
        f"• **Hard Stop Loss:** -15% from entry or invalidation under liquidity low\n\n"
        f"📍 [View Chart on DEXScreener]({data['dex_url']})"
    )

    await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

# --- AUTOMATED 24/7 BREAKOUT MONITORING LOOP ---
async def automated_stream_loop(app):
    logger.info("⚡ SCANNER ENGINE STARTED: Watching DexScreener Boosts...")
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10.0)
                if response.status_code == 200:
                    res = response.json()
                    items = res.get("data", res) if isinstance(res, dict) else res
                    
                    if isinstance(items, list) and len(items) > 0:
                        # Filter top 15 candidates not yet alerted
                        candidate_addresses = []
                        for item in items[:15]:
                            addr = item.get("tokenAddress")
                            if addr and addr not in alerted_tokens:
                                candidate_addresses.append(addr)

                        if candidate_addresses:
                            logger.info(f"Scanning batch of {len(candidate_addresses)} candidate tokens...")
                            token_data_map = await fetch_batch_dex_data(client, candidate_addresses)

                            for addr, data in token_data_map.items():
                                logger.info(
                                    f"EVALUATING ${data['symbol']} ({data['chain']}) | "
                                    f"FDV: ${data['mcap']:,.0f} | Liq: ${data['liquidity']:,.0f} | "
                                    f"BuyRatio: {data['buy_ratio']:.1f}%"
                                )

                                if (data['chain_raw'] in ALLOWED_CHAINS and 
                                    data['mcap'] >= MIN_MARKET_CAP and 
                                    data['liquidity'] >= MIN_LIQUIDITY and 
                                    data['vol_5m'] >= MIN_5M_VOLUME and 
                                    data['buy_ratio'] >= MIN_BUY_RATIO):
                                    
                                    # Save to memory and disk
                                    alerted_tokens.add(addr)
                                    save_alerted_tokens(alerted_tokens)
                                    
                                    logger.info(f"🚨 ALERT TRIGGERED: Sending Telegram alert for ${data['symbol']}")

                                    msg = (
                                        f"🚨 **CONFIRMED MOMENTUM BREAKOUT** 🚨\n"
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
                                        f"• **Take Profit Scale:** TP1 1.5x (50%) | TP2 2x (30%) | Runner 5x (20%)\n"
                                        f"• **Stop Loss:** Max -15% or break of recent 5m swing low\n\n"
                                        f"📋 **Contract Address:**\n`{addr}`\n\n"
                                        f"📍 [Open DEXScreener Chart]({data['dex_url']})"
                                    )
                                    
                                    # Force loud notification alert for strong signals
                                    await app.bot.send_message(
                                        chat_id=TELEGRAM_CHAT_ID,
                                        text=msg,
                                        parse_mode="Markdown",
                                        disable_web_page_preview=True,
                                        disable_notification=not data['is_strong']
                                    )
                else:
                    logger.warning(f"DexScreener API Status: {response.status_code}")

            except Exception as e:
                logger.error(f"Error in breakout loop: {e}")

            await asyncio.sleep(20)

# --- MAIN RUNNER ---
async def main():
    threading.Thread(target=run_web_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address_paste))

    await app.initialize()
    await app.bot.delete_webhook(drop_pending_updates=True)
    await app.start()

    # Non-blocking async background worker loop
    asyncio.create_task(automated_stream_loop(app))

    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
