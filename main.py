import os
import threading
import httpx
import asyncio
import logging
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# Logging Configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FLASK HEALTH DUMMY SERVER ---
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "Bot is active 24/7", 200

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

# --- YOUR DIRECT HARDCODED CREDENTIALS ---
TELEGRAM_BOT_TOKEN = "8804502384:AAHYjDaiM_sj7p3t1MRCSKJA5XMoUmqWINo"
TELEGRAM_CHAT_ID = "5642314005"

# Sound alert audio file (MP3 / WAV URL) for STRONG signals
ALERT_AUDIO_URL = "https://www.soundjay.com/buttons/sounds/button-3.mp3"

# --- ADJUSTED MOMENTUM FILTERS (Optimized for Boosted Tokens) ---
MIN_MARKET_CAP = 30000.0      # Adjusted to $30k (from $100k) to catch early breakouts
MIN_LIQUIDITY = 15000.0       # Adjusted to $15k (from $30k)
MIN_5M_VOLUME = 8000.0        # Adjusted to $8k 5m volume surge
MIN_BUY_RATIO = 55.0          # 55%+ Buyers vs Sellers
ALLOWED_CHAINS = ["solana", "ethereum", "arbitrum", "base", "bsc"]

alerted_tokens = set()

# --- ASYNCHRONOUS DEXSCREENER FETCH ---
async def fetch_dex_data(client: httpx.AsyncClient, address: str) -> dict:
    """Non-blocking asynchronous fetch of real-time market metrics from DexScreener API."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    try:
        response = await client.get(url, timeout=10.0)
        if response.status_code != 200:
            logger.warning(f"DexScreener API returned HTTP {response.status_code} for {address}")
            return None
            
        res = response.json()
        pairs = res.get("pairs")
        if not pairs:
            return None

        pair = sorted(pairs, key=lambda x: x.get("liquidity", {}).get("usd", 0), reverse=True)[0]
        
        chain_id = pair.get("chainId", "UNKNOWN").lower()
        token_name = pair.get("baseToken", {}).get("name", "Unknown")
        symbol = pair.get("baseToken", {}).get("symbol", "UNKNOWN")
        mcap = pair.get("fdv", 0.0) or pair.get("marketCap", 0.0)
        vol_5m = pair.get("volume", {}).get("m5", 0.0)
        liquidity = pair.get("liquidity", {}).get("usd", 0.0)
        
        buys_5m = pair.get("txns", {}).get("m5", {}).get("buys", 0)
        sells_5m = pair.get("txns", {}).get("m5", {}).get("sells", 0)
        total_txns = buys_5m + sells_5m
        buy_ratio = (buys_5m / total_txns * 100) if total_txns > 0 else 0.0

        # Dynamic Status Rating Matrix
        if buy_ratio >= 65.0 and liquidity >= 30000 and vol_5m >= 20000:
            status = "STRONG 🟢"
            signal_type = "🟢 CONFIRMED BREAKOUT MOMENTUM 🟢"
            is_strong = True
        elif buy_ratio <= 40.0 or liquidity < 10000:
            status = "WEAK / HIGH RISK 🔴"
            signal_type = "🔴 UNTESTED / LOW BUY PRESSURE 🔴"
            is_strong = False
        else:
            status = "NEUTRAL 🟡"
            signal_type = "🟡 EARLY SPECULATIVE CONSOLIDATION 🟡"
            is_strong = False

        # Holding Duration & Execution Strategy
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
            "dex_url": pair.get("url", f"https://dexscreener.com/{chain_id}/{address}")
        }
    except Exception as e:
        logger.error(f"Error fetching DexScreener data: {e}")
        return None

# --- INTERACTIVE ADDRESS LOOKUP ---
async def handle_address_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if len(text) < 30 or len(text) > 50 or " " in text:
        return

    await update.message.reply_text("🔎 Analyzing breakout metrics on DexScreener...")
    
    async with httpx.AsyncClient() as client:
        data = await fetch_dex_data(client, text)

    if not data:
        await update.message.reply_text("❌ Token not found or no active liquidity pairs.")
        return

    msg = (
        f"{data['signal_type']}\n"
        f"**Chain:** {data['chain']}\n"
        f"**Token:** ${data['symbol']} ({data['name']})\n\n"
        f"**Rating:** {data['status']}\n"
        f"**Market Cap:** ${data['mcap']:,.2f}\n"
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
    logger.info("Breakout Engine Active: Scanning DexScreener for Momentum Signals...")
    
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10.0)
                if response.status_code == 200:
                    res = response.json()
                    
                    if isinstance(res, list) and len(res) > 0:
                        logger.info(f"--- Scanning top {min(15, len(res))} boosted tokens ---")
                        for item in res[:15]:
                            address = item.get("tokenAddress")
                            
                            if address and address not in alerted_tokens:
                                data = await fetch_dex_data(client, address)
                                
                                if data:
                                    # Print clear diagnostic log in Render console
                                    logger.info(
                                        f"Check ${data['symbol']} ({data['chain']}) | "
                                        f"MC: ${data['mcap']:,.0f} | Liq: ${data['liquidity']:,.0f} | "
                                        f"Vol5m: ${data['vol_5m']:,.0f} | BuyRatio: {data['buy_ratio']:.1f}%"
                                    )

                                    # Check Filter Criteria
                                    if (data['chain_raw'] in ALLOWED_CHAINS and 
                                        data['mcap'] >= MIN_MARKET_CAP and 
                                        data['liquidity'] >= MIN_LIQUIDITY and 
                                        data['vol_5m'] >= MIN_5M_VOLUME and 
                                        data['buy_ratio'] >= MIN_BUY_RATIO):
                                        
                                        # Store address ONLY when alert triggers
                                        alerted_tokens.add(address)
                                        logger.info(f"🚀 MATCH FOUND! Sending alert for ${data['symbol']}")

                                        msg = (
                                            f"🚨 **CONFIRMED MOMENTUM BREAKOUT** 🚨\n"
                                            f"{data['signal_type']}\n\n"
                                            f"**Chain:** {data['chain']}\n"
                                            f"**Token:** ${data['symbol']} ({data['name']})\n\n"
                                            f"⭐ **Rating:** {data['status']}\n"
                                            f"💰 **Market Cap:** ${data['mcap']:,.2f}\n"
                                            f"🔥 **5m Volume Surge:** ${data['vol_5m']:,.2f}\n"
                                            f"💧 **Liquidity:** ${data['liquidity']:,.2f}\n"
                                            f"🟢 **Buy Ratio:** {data['buy_ratio']:.1f}% ({data['buys']} buys / {data['sells']} sells)\n\n"
                                            f"📊 **RISK & PROFIT EXECUTION PLAN:**\n"
                                            f"{data['forecast']}\n"
                                            f"• **Hold Duration:** {data['hold_time']}\n"
                                            f"• **Take Profit Scale:** TP1 1.5x (50%) | TP2 2x (30%) | Runner 5x (20%)\n"
                                            f"• **Stop Loss:** Max -15% or break of recent 5m swing low\n\n"
                                            f"📋 **Contract Address:**\n`{address}`\n\n"
                                            f"📍 [Open DEXScreener Chart]({data['dex_url']})"
                                        )
                                        
                                        # Send text alert
                                        await app.bot.send_message(
                                            chat_id=TELEGRAM_CHAT_ID,
                                            text=msg,
                                            parse_mode="Markdown",
                                            disable_web_page_preview=True,
                                            disable_notification=not data['is_strong']
                                        )

                                        # Optional audio alert
                                        if data['is_strong']:
                                            try:
                                                await app.bot.send_audio(
                                                    chat_id=TELEGRAM_CHAT_ID,
                                                    audio=ALERT_AUDIO_URL,
                                                    caption="🔔 **STRONG BUY BREAKOUT SIGNAL DETECTED** 🔔",
                                                    parse_mode="Markdown",
                                                    disable_notification=False
                                                )
                                            except Exception as audio_err:
                                                logger.error(f"Failed to send alert sound: {audio_err}")

                                if len(alerted_tokens) > 200:
                                    alerted_tokens.clear()
                else:
                    logger.warning(f"DexScreener Boosts API returned status {response.status_code}")

            except Exception as e:
                logger.error(f"Error in breakout loop: {e}")

            await asyncio.sleep(30)

async def main():
    threading.Thread(target=run_web_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address_paste))

    await app.initialize()
    
    # Prevents conflict errors on new deployments
    await app.bot.delete_webhook(drop_pending_updates=True)

    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    await automated_stream_loop(app)

if __name__ == "__main__":
    asyncio.run(main())
