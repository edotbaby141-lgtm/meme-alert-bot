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

# Multi-chain target IDs
TARGET_CHAINS = {"solana", "base", "ethereum", "bsc", "robinhood"}
CHECK_INTERVAL_SECONDS = 30

MIN_LIQUIDITY_USD = 10000
MAX_LIQUIDITY_USD = 1000000
MIN_5M_VOLUME = 5000       # Fast test threshold ($5k)
MIN_BUYS_5M = 15          # Fast test threshold (15 buys)

# Keywords to detect AI or top trending coins
TRENDING_AND_AI_KEYWORDS = [
    "ai", "gpt", "agent", "neural", "intel", "mind", "bot", "claude", "solana-ai", "terminal",
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
    """Check if token symbol, name, or description contains target keywords."""
    text = f"{symbol} {name} {description}".lower()
    return any(keyword in text for keyword in TRENDING_AND_AI_KEYWORDS)

def calculate_holding_projection(vol_5m, liquidity, buy_ratio, test_amount=100):
    """
    Foresee potential market cap targets and holding value on a baseline investment.
    Uses Volume-to-Liquidity velocity & buy ratio momentum.
    """
    v_l_ratio = vol_5m / liquidity if liquidity > 0 else 0
    
    # Calculate projected multiplier based on order flow pressure
    if v_l_ratio > 0.5 and buy_ratio >= 80:
        tier = "🔥 **ULTRA HIGH MOMENTUM (3x - 5x Potential)**"
        multiplier = 3.0
    elif v_l_ratio > 0.25 and buy_ratio >= 70:
        tier = "⚡ **HIGH MOMENTUM (1.5x - 2.5x Potential)**"
        multiplier = 2.0
    else:
        tier = "📈 **MODERATE PUMP (1.2x - 1.5x Potential)**"
        multiplier = 1.35
        
    projected_value = test_amount * multiplier
    profit = projected_value - test_amount
    
    projection_text = (
        f"{tier}\n"
        f"• **Initial Hold ($100):** `${test_amount:,.2f}`\n"
        f"• **Est. Value at Target:** `${projected_value:,.2f}` (`+${profit:,.2f}`)\n"
        f"• **Take Profit Levels:** 1.5x (`$150`) | 2x (`$200`) | 3x (`$300`)"
    )
    return projection_text

def fetch_multi_source_addresses():
    """Fetch token addresses from multiple DEXScreener feeds to ensure Solana/Base visibility."""
    addresses_by_chain = {chain: set() for chain in TARGET_CHAINS}
    
    # Endpoint 1: Latest Token Profiles
    try:
        r1 = requests.get("https://api.dexscreener.com/token-profiles/latest/v1", timeout=10)
        if r1.status_code == 200:
            for item in r1.json():
                chain = item.get('chainId', '').lower()
                addr = item.get('tokenAddress')
                if chain in TARGET_CHAINS and addr:
                    addresses_by_chain[chain].add(addr)
    except Exception as e:
        logging.error(f"Error fetching profiles: {e}")

    # Endpoint 2: Boosted Tokens (Captures active Solana/Base/ETH/BSC tokens)
    try:
        r2 = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=10)
        if r2.status_code == 200:
            for item in r2.json():
                chain = item.get('chainId', '').lower()
                addr = item.get('tokenAddress')
                if chain in TARGET_CHAINS and addr:
                    addresses_by_chain[chain].add(addr)
    except Exception as e:
        logging.error(f"Error fetching boosts: {e}")

    # Pick up to 5 tokens per chain per loop cycle
    selected_addresses = []
    for chain, addrs in addresses_by_chain.items():
        selected_addresses.extend(list(addrs)[:5])
        
    return selected_addresses

async def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()

    token = TELEGRAM_BOT_TOKEN.strip()
    chat_id = str(TELEGRAM_CHAT_ID).strip()
    bot = Bot(token=token)
    
    try:
        await bot.send_message(
            chat_id=chat_id, 
            text="🟢 **Multi-Chain Signal Engine Online.**\nScanning with Holding Value & Price Target Forecasting..."
        )
        logging.info("Startup alert sent successfully.")
    except Exception as e:
        logging.error(f"Startup failed: {e}")
        return

    while True:
        try:
            addresses = fetch_multi_source_addresses()
            
            if addresses:
                batch = addresses[:30]
                pairs_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}"
                pairs_res = requests.get(pairs_url, timeout=10)
                
                if pairs_res.status_code == 200:
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

                        # AI / Priority Keyword Tagging
                        ai_flag = "🤖 **AI / TRENDING TOKEN DETECTED** 🤖\n" if is_priority_token(symbol, name) else ""

                        # 🟢 ENTRY SIGNAL
                        if pair_addr not in active_positions:
                            if (MIN_LIQUIDITY_USD <= liquidity <= MAX_LIQUIDITY_USD and 
                                vol_5m >= MIN_5M_VOLUME and buys_5m >= MIN_BUYS_5M):
                                
                                buy_ratio = (buys_5m / total_txns) * 100 if total_txns > 0 else 0
                                if buy_ratio >= 65:
                                    # Calculate projections
                                    projections = calculate_holding_projection(vol_5m, liquidity, buy_ratio)
                                    
                                    msg = (
                                        f"🟢 **TAKE POSITION (ENTRY SIGNAL)** 🟢\n"
                                        f"{ai_flag}"
                                        f"**Chain:** `{chain_id}`\n"
                                        f"**Token:** `${symbol}` ({name})\n"
                                        f"**5m Volume:** `${vol_5m:,.2f}`\n"
                                        f"**Liquidity:** `${liquidity:,.2f}`\n"
                                        f"**Buy Ratio:** `{buy_ratio:.1f}%` ({buys_5m} buys / {sells_5m} sells)\n\n"
                                        f"📊 **HOLDING VALUE FORECAST ($100 Hold):**\n"
                                        f"{projections}\n\n"
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
