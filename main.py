import asyncio
import logging
import requests
from telegram import Bot

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = "8804502384:AAFrkIaOJzd7oDZeQ1HNcmPmg3fnkHgGAVM"
TELEGRAM_CHAT_ID = "5642314005"

CHAIN = "solana"
CHECK_INTERVAL_SECONDS = 30
MIN_LIQUIDITY_USD = 10000
MAX_LIQUIDITY_USD = 150000
MIN_5M_VOLUME = 5000
MIN_BUYS_5M = 15
# =================================================

seen_tokens = set()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def main():
    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = str(TELEGRAM_CHAT_ID).strip()
    bot = Bot(token=token)
    
    try:
        await bot.send_message(chat_id=chat_id, text="🟢 **Early Pump Bot Activated.** Monitoring market...")
        logging.info("Startup alert sent successfully.")
    except Exception as e:
        logging.error(f"Startup failed: {e}")
        return

    while True:
        try:
            url = "https://api.dexscreener.com/token-profiles/latest/v1"
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                profiles = res.json()
                addresses = [p['tokenAddress'] for p in profiles if p.get('chainId') == CHAIN][:15]
                
                if addresses:
                    pairs_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(addresses)}"
                    pairs_res = requests.get(pairs_url, timeout=10)
                    pairs = pairs_res.json().get('pairs', [])
                    
                    for pair in pairs:
                        pair_addr = pair.get('pairAddress')
                        if pair_addr in seen_tokens:
                            continue
                            
                        liquidity = pair.get('liquidity', {}).get('usd', 0)
                        vol_5m = pair.get('volume', {}).get('m5', 0)
                        buys_5m = pair.get('txns', {}).get('m5', {}).get('buys', 0)
                        
                        if (MIN_LIQUIDITY_USD <= liquidity <= MAX_LIQUIDITY_USD and 
                            vol_5m >= MIN_5M_VOLUME and buys_5m >= MIN_BUYS_5M):
                            
                            symbol = pair.get('baseToken', {}).get('symbol', 'UNKNOWN')
                            dex_url = pair.get('url', '')
                            token_addr = pair.get('baseToken', {}).get('address', '')
                            
                            msg = (
                                f"🚀 **EARLY VOLUME PUMP DETECTED** 🚀\n\n"
                                f"**Token:** `${symbol}`\n"
                                f"**5m Volume:** `${vol_5m:,.2f}`\n"
                                f"**Liquidity:** `${liquidity:,.2f}`\n\n"
                                f"📍 [View DEXScreener]({dex_url})\n"
                                f"📋 `{token_addr}`"
                            )
                            await bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
                            seen_tokens.add(pair_addr)
        except Exception as e:
            logging.error(f"Loop exception caught: {e}")
            
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
