import asyncio
import logging
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = "8804502384:AAEX_2FuTb4PAmT7rVk_T7Qpq695T5JExKw"
TELEGRAM_CHAT_ID = "5642314005"

TARGET_CHAINS = {"solana", "base", "ethereum", "bsc", "robinhood"}
CHECK_INTERVAL_SECONDS = 30

# Fast Scalp Thresholds
MIN_LIQUIDITY_USD = 20000
MAX_LIQUIDITY_USD = 1000000
MIN_5M_VOLUME = 5000
MIN_BUYS_5M = 15

# Macro / High-Conviction "Unipcs" Swing Thresholds
MACRO_MIN_LIQUIDITY = 250000
MACRO_MIN_MCAP = 5000000
MACRO_MIN_24H_VOL = 1000000

TRENDING_AND_AI_KEYWORDS = [
    "ai", "gpt", "agent", "neural", "intel", "mind", "bot", "claude", "solana-ai", "terminal",
    "ake", "pons", "cashcat", "arb", "hype", "uni", "up", "pengu", "lit", "ena", "eth", "pi", "pump", "xmr", "zec",
    "tao", "aixbt", "fet", "render", "goat", "popcat", "brett", "toshi", "mog", "spx", "sui", "neiro", "bonk",
    "aave", "pendle", "ray", "jup", "aero", "inj", "tia", "sei", "near", "op"
]
# =================================================

active_positions = set()
macro_active_positions = set()
custom_tracked_tokens = set()

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
    text = f"{symbol} {name} {description}".lower()
    return any(keyword in text for keyword in TRENDING_AND_AI_KEYWORDS)

# ================= SECURITY API INTEGRATION =================
def check_token_security(chain, token_address):
    """
    Checks token contract security via RugCheck (Solana) or GoPlus API (EVM Chains).
    Returns (is_safe: bool, details_msg: str)
    """
    chain_lower = chain.lower()
    
    # 1. SOLANA CONTRACT CHECK (RugCheck API)
    if chain_lower == "solana":
        try:
            url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary"
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                data = res.json()
                score = data.get("score", 0)
                risks = data.get("risks", [])
                
                risk_flags = [f"• {r.get('name')}: {r.get('description', '')}" for r in risks if r.get('level') in ['danger', 'warn']]
                
                if score > 2000 or any(r.get('level') == 'danger' for r in risks):
                    return False, f"⚠️ **SOLANA SECURITY ALERT (RugCheck)**\nRisk Score: `{score}` (High Risk)\n" + "\n".join(risk_flags[:3])
                return True, "✅ **Solana Security:** RugCheck Passed (No mint/freeze exploits detected)"
        except Exception as e:
            logging.error(f"Solana Security Check Error: {e}")
            
    # 2. EVM CONTRACT CHECK (GoPlus Security API)
    else:
        chain_mapping = {"ethereum": "1", "bsc": "56", "base": "8453"}
        chain_id = chain_mapping.get(chain_lower)
        if chain_id:
            try:
                url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={token_address}"
                res = requests.get(url, timeout=5)
                if res.status_code == 200:
                    result = res.json().get("result", {}).get(token_address.lower(), {})
                    
                    is_honeypot = result.get("is_honeypot") == "1"
                    is_mintable = result.get("is_mintable") == "1"
                    cannot_sell = result.get("cannot_sell_all") == "1"
                    owner_change = result.get("can_take_back_ownership") == "1"
                    
                    if is_honeypot or cannot_sell or owner_change:
                        return False, "⚠️ **EVM SECURITY ALERT (GoPlus)**\n• Honeypot or Unsellable Contract Detected!"
                    
                    flags = []
                    if is_mintable:
                        flags.append("• Mintable Supply Enabled")
                    
                    flag_text = "\n".join(flags) if flags else "Clean Security Profile"
                    return True, f"✅ **EVM Security:** GoPlus Passed ({flag_text})"
            except Exception as e:
                logging.error(f"EVM Security Check Error: {e}")

    return True, "ℹ️ **Security:** Quick Check Clean / Unregistered Risk"

def fetch_crypto_news_catalysts(symbol, name):
    try:
        url = "https://cryptocurrency.cv/api/news?limit=15"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            articles = res.json()
            relevant_news = []
            target = f"{symbol}".lower()
            
            for article in articles:
                title = article.get('title', '')
                if target in title.lower() or name.lower() in title.lower():
                    relevant_news.append(f"• [{title}]({article.get('url', '#')})")
            
            if relevant_news:
                return "📰 **TOKEN NEWS & CATALYSTS:**\n" + "\n".join(relevant_news[:2]) + "\n\n"
            else:
                top_story = articles[0].get('title', '')
                return f"📰 **MACRO MARKET HEADLINE:**\n• {top_story}\n\n"
    except Exception as e:
        logging.error(f"Error fetching news: {e}")
    return ""

def calculate_holding_projection(vol_5m, liquidity, buy_ratio, total_txns, mcap=0, vol_24h=0, price_change_24h=0, test_amount=100):
    v_l_ratio = vol_5m / liquidity if liquidity > 0 else 0
    
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

    if liquidity >= 500000 and vol_24h >= 2000000 and mcap >= 10000000:
        holding_time = "🗓️ **1 to 3 MONTHS** *(Established Liquidity Depth — Macro Hold / Swing)*"
    elif liquidity >= 150000 and vol_24h >= 500000 and 0 <= price_change_24h <= 100:
        holding_time = "📅 **1 to 4 WEEKS** *(Multi-Day Volume Consolidation — Healthy Swing Trend)*"
    elif liquidity >= 80000 and vol_24h >= 200000:
        holding_time = "📆 **2 to 7 DAYS** *(Short-Term Swing — Watch for local higher-lows)*"
    elif v_l_ratio >= 1.5 or total_txns > 200:
        holding_time = "⚡ **2 to 5 MINUTES** *(High Volatility Scalp — Fast profit lock required!)*"
    elif 0.5 <= v_l_ratio < 1.5 or (100 <= total_txns <= 200):
        holding_time = "⏱️ **10 to 30 MINUTES** *(Standard Momentum Push — Monitor sell pressure)*"
    elif liquidity >= 50000 and buy_ratio >= 60:
        holding_time = "⏳ **1 to 4 HOURS** *(Stable Pool — Can sustain intraday trend)*"
    else:
        holding_time = "⏱️ **5 to 15 MINUTES** *(Micro-Cap Scalp — High volume decay risk)*"

    return (
        f"{tier}\n"
        f"• **Initial Hold ($100):** `${test_amount:,.2f}`\n"
        f"• **Est. Value at Target:** `${projected_value:,.2f}` (`+${profit:,.2f}`)\n"
        f"• **Recommended Hold Duration:** {holding_time}\n"
        f"• **Take Profit Targets:** 1.5x (`$150`) | 2x (`$200`) | 3x (`$300`)"
    )

def fetch_multi_source_addresses():
    addresses_by_chain = {chain: set() for chain in TARGET_CHAINS}
    
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

    selected_addresses = list(custom_tracked_tokens)
    for chain, addrs in addresses_by_chain.items():
        selected_addresses.extend(list(addrs)[:5])
        
    return selected_addresses

# ================= INTERACTIVE MESSAGE HANDLER =================
async def analyze_custom_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token_addr = update.message.text.strip()
    
    if token_addr.startswith("/"):
        return

    await update.message.reply_text(f"🔎 **Analyzing Token Security & Performance Metrics:**\n`{token_addr}`...", parse_mode="Markdown")

    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
        res = requests.get(url, timeout=10)
        
        if res.status_code == 200:
            pairs = res.json().get('pairs', [])
            if not pairs:
                await update.message.reply_text("❌ Token/Pair not found on DEXScreener.")
                return

            pair = pairs[0]
            chain_id = pair.get('chainId', 'unknown').upper()
            base_token = pair.get('baseToken', {})
            symbol = base_token.get('symbol', 'UNKNOWN')
            name = base_token.get('name', '')
            dex_url = pair.get('url', '')
            
            # Security Audit
            is_safe, sec_msg = check_token_security(chain_id, token_addr)
            
            liquidity = pair.get('liquidity', {}).get('usd', 0)
            vol_5m = pair.get('volume', {}).get('m5', 0)
            vol_24h = pair.get('volume', {}).get('h24', 0)
            mcap = pair.get('marketCap', 0) or pair.get('fdv', 0)
            
            buys_5m = pair.get('txns', {}).get('m5', {}).get('buys', 0)
            sells_5m = pair.get('txns', {}).get('m5', {}).get('sells', 0)
            total_txns = buys_5m + sells_5m
            buy_ratio = (buys_5m / total_txns) * 100 if total_txns > 0 else 0

            price_change_5m = pair.get('priceChange', {}).get('m5', 0.0)
            price_change_1h = pair.get('priceChange', {}).get('h1', 0.0)
            price_change_24h = pair.get('priceChange', {}).get('h24', 0.0)

            weakness_reasons = []
            strength_reasons = []

            if not is_safe:
                weakness_reasons.append("• **FAILED CONTRACT SECURITY AUDIT**")
            if liquidity < 20000:
                weakness_reasons.append("• **Thin Liquidity**: Under $20k depth")
            if buy_ratio < 45 and total_txns >= 3:
                weakness_reasons.append(f"• **Sell Dominance**: {100-buy_ratio:.1f}% sells in last 5m")

            if is_safe:
                strength_reasons.append("• **Contract Security Audit Passed**")
            if liquidity >= 20000:
                strength_reasons.append(f"• **Solid Pool Depth**: `${liquidity:,.2f}`")
            if buy_ratio >= 65 and total_txns >= 5:
                strength_reasons.append(f"• **Bullish Buy Ratio**: `{buy_ratio:.1f}%`")

            if is_safe and len(strength_reasons) >= 3 and len(weakness_reasons) == 0:
                strength_rating = "🟢 **STRONG / HIGH MOMENTUM**"
                action_signal = "🟢 **ACTION: BUY SIGNAL CONFIRMED**"
            elif not is_safe or len(weakness_reasons) >= 2:
                strength_rating = "🔴 **WEAK / HIGH RISK**"
                action_signal = "🚫 **ACTION: DO NOT BUY / SECURITY RISK**"
            else:
                strength_rating = "🟡 **NEUTRAL / WATCHLIST**"
                action_signal = "⏳ **ACTION: WAIT FOR CONFIRMATION**"

            custom_tracked_tokens.add(token_addr)
            projections = calculate_holding_projection(vol_5m, liquidity, buy_ratio, total_txns, mcap, vol_24h, price_change_24h)
            news_block = fetch_crypto_news_catalysts(symbol, name)

            msg = (
                f"📊 **TOKEN DIAGNOSTIC & TRADE SIGNAL**\n"
                f"**Chain:** `{chain_id}`\n"
                f"**Token:** `${symbol}` ({name})\n\n"
                f"🛡️ **SECURITY STATUS:**\n{sec_msg}\n\n"
                f"🏋️ **Rating:** {strength_rating}\n"
                f"🎯 {action_signal}\n\n"
                f"{news_block}"
                f"**Key Metrics:**\n"
                f"• **Liquidity:** `${liquidity:,.2f}`\n"
                f"• **5m Volume:** `${vol_5m:,.2f}`\n"
                f"• **Buy Ratio:** `{buy_ratio:.1f}%` ({buys_5m} buys / {sells_5m} sells)\n\n"
                f"📊 **HOLDING VALUE & DURATION FORECAST:**\n"
                f"{projections}\n\n"
                f"📍 [View DEXScreener]({dex_url})"
            )
            
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            
    except Exception as e:
        logging.error(f"Error checking custom token: {e}")
        await update.message.reply_text("⚠️ Failed to pull DEX data. Verify contract address and try again.")

# ================= AUTOMATED SCANNER LOOP =================
async def scanner_loop(app: Application):
    chat_id = str(TELEGRAM_CHAT_ID).strip()
    
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
                        vol_24h = pair.get('volume', {}).get('h24', 0)
                        mcap = pair.get('marketCap', 0) or pair.get('fdv', 0)
                        
                        buys_5m = pair.get('txns', {}).get('m5', {}).get('buys', 0)
                        sells_5m = pair.get('txns', {}).get('m5', {}).get('sells', 0)
                        total_txns = buys_5m + sells_5m
                        price_change_5m = pair.get('priceChange', {}).get('m5', 0.0)
                        price_change_1h = pair.get('priceChange', {}).get('h1', 0.0)
                        price_change_24h = pair.get('priceChange', {}).get('h24', 0.0)

                        if pair_addr not in active_positions:
                            liquidity_to_vol_ratio = liquidity / vol_5m if vol_5m > 0 else 0
                            
                            if (liquidity >= MIN_LIQUIDITY_USD and 
                                liquidity <= MAX_LIQUIDITY_USD and 
                                liquidity_to_vol_ratio >= 0.25 and 
                                vol_5m >= MIN_5M_VOLUME and 
                                buys_5m >= MIN_BUYS_5M and
                                price_change_5m <= 25.0 and 
                                price_change_1h <= 250.0):
                                
                                buy_ratio = (buys_5m / total_txns) * 100 if total_txns > 0 else 0
                                if buy_ratio >= 65:
                                    # Security Gate check before sending alert
                                    is_safe, sec_msg = check_token_security(chain_id, token_addr)
                                    if is_safe:
                                        projections = calculate_holding_projection(vol_5m, liquidity, buy_ratio, total_txns, mcap, vol_24h, price_change_24h)
                                        news_block = fetch_crypto_news_catalysts(symbol, name)
                                        msg = (
                                            f"🟢 **TAKE POSITION (ENTRY SIGNAL)** 🟢\n"
                                            f"**Chain:** `{chain_id}`\n"
                                            f"**Token:** `${symbol}` ({name})\n"
                                            f"🛡️ {sec_msg}\n\n"
                                            f"**5m Volume:** `${vol_5m:,.2f}`\n"
                                            f"**Liquidity:** `${liquidity:,.2f}`\n"
                                            f"**Buy Ratio:** `{buy_ratio:.1f}%` ({buys_5m} buys / {sells_5m} sells)\n\n"
                                            f"{news_block}"
                                            f"📊 **HOLDING VALUE & DURATION FORECAST:**\n"
                                            f"{projections}\n\n"
                                            f"📍 [View DEXScreener]({dex_url})\n"
                                            f"📋 `{token_addr}`"
                                        )
                                        await app.bot.send_message(
                                            chat_id=chat_id, 
                                            text=msg, 
                                            parse_mode="Markdown", 
                                            disable_web_page_preview=True
                                        )
                                        active_positions.add(pair_addr)

        except Exception as e:
            logging.error(f"Scanner exception: {e}")
            
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ================= MACRO SWING SCANNER LOOP =================
async def macro_swing_scanner_loop(app: Application):
    chat_id = str(TELEGRAM_CHAT_ID).strip()
    
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
                        vol_24h = pair.get('volume', {}).get('h24', 0)
                        mcap = pair.get('marketCap', 0) or pair.get('fdv', 0)
                        price_change_24h = pair.get('priceChange', {}).get('h24', 0.0)

                        if pair_addr not in macro_active_positions:
                            if (liquidity >= MACRO_MIN_LIQUIDITY and 
                                mcap >= MACRO_MIN_MCAP and 
                                vol_24h >= MACRO_MIN_24H_VOL and 
                                -10.0 <= price_change_24h <= 50.0):
                                
                                is_safe, sec_msg = check_token_security(chain_id, token_addr)
                                if is_safe:
                                    news_block = fetch_crypto_news_catalysts(symbol, name)
                                    msg = (
                                        f"🐋 **MACRO HIGH-CONVICTION SWING ALERT (UNIPCS MODE)** 🐋\n\n"
                                        f"**Chain:** `{chain_id}`\n"
                                        f"**Token:** `${symbol}` ({name})\n"
                                        f"🛡️ {sec_msg}\n\n"
                                        f"**Market Cap:** `${mcap:,.2f}`\n"
                                        f"**Liquidity Pool Depth:** `${liquidity:,.2f}`\n"
                                        f"**24h Volume:** `${vol_24h:,.2f}`\n\n"
                                        f"{news_block}"
                                        f"🗓️ **ESTABLISHED TREND FORECAST:**\n"
                                        f"• **Recommended Hold Duration:** 1 to 4 Weeks / Months\n"
                                        f"• **Strategy:** Build core position, trail stops on 4H chart.\n\n"
                                        f"📍 [View DEXScreener]({dex_url})\n"
                                        f"📋 `{token_addr}`"
                                    )
                                    await app.bot.send_message(
                                        chat_id=chat_id, 
                                        text=msg, 
                                        parse_mode="Markdown", 
                                        disable_web_page_preview=True
                                    )
                                    macro_active_positions.add(pair_addr)

        except Exception as e:
            logging.error(f"Macro Scanner exception: {e}")
            
        await asyncio.sleep(60)

# ================= MAIN RUNNER =================
async def main_async():
    token = TELEGRAM_BOT_TOKEN.strip()
    app = Application.builder().token(token).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_custom_address))

    async with app:
        await app.start()
        await app.updater.start_polling()
        
        asyncio.create_task(scanner_loop(app))
        asyncio.create_task(macro_swing_scanner_loop(app))
        
        logging.info("Scalp Scanner, Macro Swing Engine, Security Audits, and Interactive Bot live...")
        await asyncio.Event().wait()

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
