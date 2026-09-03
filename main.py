import asyncio
import logging
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
from telegram import Bot

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = "8804502384:AAEX_2FuTb4PAmT7rVk_T7Qpq695T5JExKw"
TELEGRAM_CHAT_ID = "5642314005"

# Target chains supported on DEXScreener
TARGET_CHAINS = {"solana", "base", "ethereum", "bsc", "robinhood"}
CHECK_INTERVAL_SECONDS = 30

MIN_LIQUIDITY_USD = 10000
MAX_LIQUIDITY_USD = 1000000
MIN_5M_VOLUME = 5000       # Fast test threshold ($5k)
MIN_BUYS_5M = 15          # Fast test threshold (15 buys)

# Extended keywords and top trending coins
TRENDING_AND_AI_KEYWORDS = [
    # AI Keywords
    "ai", "gpt", "agent", "neural", "intel", "mind", "bot", "claude", "solana-ai", "terminal",
    # Trending & Popular Tokens
    "ake", "pons", "cashcat", "arb", "hype", "uni", "up", "pengu", "lit", "ena", "eth", "pi", "pump", "xmr", "zec",
    "tao", "aixbt", "fet", "render", "goat", "popcat", "brett", "toshi", "mog", "spx", "sui", "neiro", "bonk",
    "aave", "pendle", "ray", "jup", "aero", "inj", "tia", "sei", "near", "op"
]
# =================================================

active_positions = set()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def is_priority_token(symbol, name, description=""):
    """Check if token matches AI or top trending keywords."""
    text = f"{symbol} {name} {description}".lower()
    return any(keyword in text for keyword in TRENDING_AND_AI_KEYWORDS)

async def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()

    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = str(TELEGRAM_CHAT_ID).strip()
    bot = Bot(token=token)
    
    try:
        await bot.send_message(
            chat_id=chat_id, 
            text="🟢 **Multi-Chain Signal Engine Online.**\nScanning Solana, Base, ETH, BSC & Robinhood with Trending/AI filter..."
        )
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
                
                # Balanced sampling across target chains
                chain_counts = {chain: 0 for chain in TARGET_CHAINS}
                balanced_profiles = []
                
                for p in profiles:
                    chain = p.get('chainId', '').lower()
                    if chain in TARGET_CHAINS and chain_counts[chain] < 5:
                        balanced_profiles.append(p)
                        chain_counts[chain] += 1

                addresses = [p['tokenAddress'] for p in balanced_profiles]
                
                if addresses:
                    pairs_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(addresses)}"
                    pairs_res = requests.get(pairs_url, timeout=10)
                    pairs = pairs_res.json().get('pairs', [])
                    
                    for pair in pairs:
                        pair_addr = pair.get('pairAddress')
                        chain_id = pair.get('chainId', 'unknown').upper()
                        base_token = pair.get('baseToken', {})
                        symbol = base_token.get('symbol', 'UNKNOWN')
                        name = base_token.get('name', '')
                        dex_url = pair.get('url', '')
                        token_addr = base_token.get('address', '')
                        
                        liquidity = pair.get('liquidity', {}).get('usd', 0)
                        vol_5m = pair.get('volume', {}).get('m5', 0)
                        buys_5m = pair.get('txns', {}).get('m5', {}).get('buys', 0)
                        sells_5m = pair.get('txns', {}).get('m5', {}).get('sells', 0)
                        total_txns = buys_5m + sells_5m

                        # Check for AI / Trending token match
                        ai_flag = "🤖 **AI / TRENDING TOKEN DETECTED** 🤖\n" if is_priority_token(symbol, name) else ""

                        # 🟢 ENTRY SIGNAL
                        if pair_addr not in active_positions:
                            if (MIN_LIQUIDITY_USD <= liquidity <= MAX_LIQUIDITY_USD and 
                                vol_5m >= MIN_5M_VOLUME and buys_5m >= MIN_BUYS_5M):
                                
                                buy_ratio = (buys_5m / total_txns) * 100 if total_txns > 0 else 0
                                if buy_ratio >= 65:
                                    msg = (
                                        f"🟢 **TAKE POSITION (ENTRY SIGNAL)** 🟢\n"
                                        f"{ai_flag}"
                                        f"**Chain:** `{chain_id}`\n"
                                        f"**Token:** `${symbol}` ({name})\n"
                                        f"**5m Volume:** `${vol_5m:,.2f}`\n"
                                        f"**Liquidity:** `${liquidity:,.2f}`\n"
                                        f"**Buy Ratio:** `{buy_ratio:.1f}%` ({buys_5m} buys / {sells_5m} sells)\n\n"
                                        f"📍 [View DEXScreener]({dex_url})\n"
                                        f"📋 `{token_addr}`"
                                    )
                                    await bot.send_message(
                                        chat_id=chat_id, 
                                        text=msg, 
                                        parse_mode="Markdown", 
                                        disable_web_page_preview=True
                                    )
                                    active_positions.add(pair_addr)

                        # 🔴 EXIT SIGNAL
                        elif pair_addr in active_positions:
                            sell_ratio = (sells_5m / total_txns) * 100 if total_txns > 0 else 0
                            if sell_ratio >= 60 or vol_5m < (MIN_5M_VOLUME / 2):
                                msg = (
                                    f"🔴 **STEP OUT (EXIT SIGNAL)** 🔴\n\n"
                                    f"**Chain:** `{chain_id}`\n"
                                    f"**Token:** `${symbol}`\n"
                                    f"**Alert:** Market momentum crashing or heavy selling detected!\n"
                                    f"**Sell Pressure:** `{sell_ratio:.1f}%` sells in last 5m\n\n"
                                    f"📍 [View DEXScreener]({dex_url})\n"
                                    f"📋 `{token_addr}`"
                                )
                                await bot.send_message(
                                    chat_id=chat_id, 
                                    text=msg, 
                                    parse_mode="Markdown", 
                                    disable_web_page_preview=True
                                )
                                active_positions.remove(pair_addr)

        except Exception as e:
            logging.error(f"Loop exception caught: {e}")
            
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
