import asyncio
import logging
import requests
from telegram import Bot

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN 8804502384:AAFrkIaOJzd7oDZeQ1HNcmPmg3fnkHgGAVM

5642314005 # Your private user ID numbers

# Filter Parameters
CHAIN = "solana"               # Target chain (e.g., 'solana', 'ethereum', 'base')
CHECK_INTERVAL_SECONDS = 30    # Scan interval (in seconds)
MIN_LIQUIDITY_USD = 10000      # $10k min liquidity (filters pure zero-liquidity scams)
MAX_LIQUIDITY_USD = 150000     # $150k max liquidity (catches micro-caps early)
MIN_5M_VOLUME = 5000           # Minimum $5k volume in the last 5 minutes
MIN_BUYS_5M = 15               # Minimum 15 buy transactions in 5 minutes
# =================================================

# Prevent duplicate alerts
seen_tokens = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=TELEGRAM_BOT_TOKEN)

def get_latest_dex_pairs():
    """Fetches recent profiles and queries DEXScreener for pair details."""
    try:
        url = "https://api.dexscreener.com/token-profiles/latest/v1"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            profiles = response.json()
            # Extract addresses matching the target chain
            addresses = [p['tokenAddress'] for p in profiles if p.get('chainId') == CHAIN][:15]
            if not addresses:
                return []
            
            # Request pair stats for extracted addresses
            pairs_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(addresses)}"
            res = requests.get(pairs_url, timeout=10)
            return res.json().get('pairs', [])
    except Exception as e:
        logging.error(f"Error querying DEX data: {e}")
    return []

async def scan_market():
    """Applies volume and liquidity filters to trigger alerts."""
    pairs = get_latest_dex_pairs()
    
    for pair in pairs:
        pair_address = pair.get('pairAddress')
        if pair_address in seen_tokens:
            continue
            
        token_symbol = pair.get('baseToken', {}).get('symbol', 'UNKNOWN')
        token_address = pair.get('baseToken', {}).get('address', '')
        liquidity = pair.get('liquidity', {}).get('usd', 0)
        vol_5m = pair.get('volume', {}).get('m5', 0)
        buys_5m = pair.get('txns', {}).get('m5', {}).get('buys', 0)
        price_change_5m = pair.get('priceChange', {}).get('m5', 0)
        fdv = pair.get('fdv', 0)
        dex_url = pair.get('url', '')

        # Filter Logic: Must sit inside liquidity window and pass 5m volume/buy threshold
        if (MIN_LIQUIDITY_USD <= liquidity <= MAX_LIQUIDITY_USD and 
            vol_5m >= MIN_5M_VOLUME and 
            buys_5m >= MIN_BUYS_5M):
            
            message = (
                f"🚀 **EARLY VOLUME PUMP DETECTED** 🚀\n\n"
                f"**Token:** `${token_symbol}`\n"
                f"**Chain:** `{CHAIN.upper()}`\n"
                f"**Price 5m Change:** `{price_change_5m}%`\n"
                f"**5m Volume:** `${vol_5m:,.2f}`\n"
                f"**5m Buys:** `{buys_5m} txns`\n"
                f"**Liquidity:** `${liquidity:,.2f}`\n"
                f"**FDV / Market Cap:** `${fdv:,.2f}`\n\n"
                f"📍 [View on DEXScreener]({dex_url})\n"
                f"📋 **Mint Address:** `{token_address}`"
            )
            
            try:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID, 
                    text=message, 
                    parse_mode="Markdown",
                    disable_web_page_preview=False
                )
                logging.info(f"Alert successfully sent for {token_symbol}")
                seen_tokens.add(pair_address)
            except Exception as e:
                logging.error(f"Failed to send Telegram message: {e}")

async def main():
    logging.info("Starting Telegram Bot Scanner...")
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🟢 **Early Pump Bot Activated.** Monitoring live market data...")
    
    while True:
        await scan_market()
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
