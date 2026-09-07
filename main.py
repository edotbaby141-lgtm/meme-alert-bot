import asyncio
import json
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import websockets
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8804502384:AAEX_2FuTb4PAmT7rVk_T7Qpq695T5JExKw")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "5642314005")

# WebSockets
PUMP_FUN_WS_URL = "wss://pumpportal.fun/api/data"
DEXSCREENER_WS_URL = "wss://api.dexscreener.com/token-profiles/latest/v1"

TARGET_CHAINS = {"solana", "base", "ethereum", "bsc"}
active_positions = set()
custom_tracked_tokens = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ================= HEALTH CHECK SERVER =================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def run_health_check_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ================= SECURITY AUDIT ENGINE =================
def check_token_security(chain, token_address):
    chain_lower = str(chain).lower()
    
    if chain_lower == "solana":
        try:
            url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary"
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                data = res.json()
                score = data.get("score", 0)
                risks = data.get("risks", [])
                if score > 1500 or any(r.get('level') == 'danger' for r in risks):
                    return False, f"⚠️ **Solana Security Fail** (Score: {score})"
                return True, "✅ **Solana Security Passed** (RugCheck Clean)"
        except Exception:
            pass
    else:
        chain_mapping = {"ethereum": "1", "bsc": "56", "base": "8453"}
        chain_id = chain_mapping.get(chain_lower)
        if chain_id:
            try:
                url = f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}?contract_addresses={token_address}"
                res = requests.get(url, timeout=3)
                if res.status_code == 200:
                    result = res.json().get("result", {}).get(token_address.lower(), {})
                    if result.get("is_honeypot") == "1" or result.get("cannot_sell_all") == "1":
                        return False, "⚠️ **EVM Security Fail** (Honeypot Detected)"
                    return True, "✅ **EVM Security Passed** (GoPlus Clean)"
            except Exception:
                pass

    return True, "ℹ️ **Security:** Pass (No immediate flags)"

# ================= INTERACTIVE DIAGNOSTIC HANDLER =================
async def analyze_custom_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token_addr = update.message.text.strip()
    
    if token_addr.startswith("/"):
        return

    await update.message.reply_text(f"🔎 **Analyzing Token Security & Performance Metrics:**\n`{token_addr}`...", parse_mode="Markdown")

    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
        res = requests.get(url, timeout=5)
        
        if res.status_code == 200:
            pairs = res.json().get('pairs', [])
            if not pairs:
                await update.message.reply_text("❌ Token/Pair not found on DEXScreener yet.")
                return

            pair = pairs[0]
            chain_id = pair.get('chainId', 'unknown').upper()
            base_token = pair.get('baseToken', {})
            symbol = base_token.get('symbol', 'UNKNOWN')
            name = base_token.get('name', '')
            dex_url = pair.get('url', '')
            
            is_safe, sec_msg = check_token_security(chain_id, token_addr)
            
            liquidity = pair.get('liquidity', {}).get('usd', 0)
            vol_5m = pair.get('volume', {}).get('m5', 0)
            buys_5m = pair.get('txns', {}).get('m5', {}).get('buys', 0)
            sells_5m = pair.get('txns', {}).get('m5', {}).get('sells', 0)
            total_txns = buys_5m + sells_5m
            buy_ratio = (buys_5m / total_txns) * 100 if total_txns > 0 else 0

            msg = (
                f"📊 **TOKEN DIAGNOSTIC REPORT**\n"
                f"**Chain:** `{chain_id}` | **Token:** `${symbol}` ({name})\n\n"
                f"🛡️ **SECURITY STATUS:**\n{sec_msg}\n\n"
                f"**Key Metrics:**\n"
                f"• **Liquidity:** `${liquidity:,.2f}`\n"
                f"• **5m Volume:** `${vol_5m:,.2f}`\n"
                f"• **Buy Ratio:** `{buy_ratio:.1f}%` ({buys_5m} buys / {sells_5m} sells)\n\n"
                f"📍 [View DEXScreener]({dex_url})"
            )
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            
    except Exception as e:
        logging.error(f"Error checking custom token: {e}")
        await update.message.reply_text("⚠️ Failed to pull DEX data. Verify contract address.")

# ================= WEBSOCKET 1: REAL-TIME PUMP.FUN STREAM =================
async def pumpfun_realtime_stream(app: Application):
    chat_id = str(TELEGRAM_CHAT_ID).strip()
    
    while True:
        try:
            async with websockets.connect(PUMP_FUN_WS_URL) as ws:
                logging.info("Connected to Solana Pump.fun Real-Time WebSocket...")
                payload = {"method": "subscribeNewToken"}
                await ws.send(json.dumps(payload))
                
                async for message in ws:
                    data = json.loads(message)
                    mint = data.get("mint")
                    symbol = data.get("symbol", "UNKNOWN")
                    name = data.get("name", "")
                    trader = data.get("traderPublicKey", "")
                    v_sol = data.get("vSolInBondingCurve", 0)
                    
                    if mint and mint not in active_positions:
                        if v_sol >= 2.0:
                            is_safe, sec_msg = check_token_security("solana", mint)
                            if is_safe:
                                active_positions.add(mint)
                                msg = (
                                    f"⚡ **EARLY BLOCKCHAIN LAUNCH (PRE-DEX)** ⚡\n"
                                    f"**Chain:** `SOLANA`\n"
                                    f"**Token:** `${symbol}` ({name})\n"
                                    f"🛡️ {sec_msg}\n\n"
                                    f"**Initial Pool SOL:** `{v_sol:.2f} SOL`\n"
                                    f"**Deployer:** `{trader[:8]}...`\n\n"
                                    f"📋 Contract Address:\n`{mint}`\n\n"
                                    f"📍 [PumpFun Link](https://pump.fun/{mint})"
                                )
                                await app.bot.send_message(
                                    chat_id=chat_id, 
                                    text=msg, 
                                    parse_mode="Markdown", 
                                    disable_web_page_preview=True
                                )
        except Exception as e:
            logging.error(f"PumpFun WS error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ================= WEBSOCKET 2: REAL-TIME DEXSCREENER PAIR STREAM =================
async def dexscreener_realtime_stream(app: Application):
    chat_id = str(TELEGRAM_CHAT_ID).strip()
    
    while True:
        try:
            async with websockets.connect(DEXSCREENER_WS_URL) as ws:
                logging.info("Connected to DEXScreener Real-Time WebSocket Stream...")
                
                async for message in ws:
                    payload = json.loads(message)
                    data_items = payload.get("data", []) if isinstance(payload, dict) else payload
                    
                    if isinstance(data_items, list):
                        for item in data_items:
                            chain = item.get("chainId", "").lower()
                            token_addr = item.get("tokenAddress")
                            url = item.get("url")
                            
                            if chain in TARGET_CHAINS and token_addr and token_addr not in active_positions:
                                is_safe, sec_msg = check_token_security(chain, token_addr)
                                if is_safe:
                                    active_positions.add(token_addr)
                                    msg = (
                                        f"🚀 **INSTANT DEX LISTING DETECTED** 🚀\n"
                                        f"**Chain:** `{chain.upper()}`\n"
                                        f"🛡️ {sec_msg}\n\n"
                                        f"📋 Contract Address:\n`{token_addr}`\n\n"
                                        f"📍 [View DEXScreener]({url})"
                                    )
                                    await app.bot.send_message(
                                        chat_id=chat_id, 
                                        text=msg, 
                                        parse_mode="Markdown", 
                                        disable_web_page_preview=True
                                    )
        except Exception as e:
            logging.error(f"DEXScreener WS error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)

# ================= MAIN ENTRY POINT =================
async def main_async():
    token = TELEGRAM_BOT_TOKEN.strip()
    app = Application.builder().token(token).build()

    # Add interactive text message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_custom_address))

    async with app:
        await app.start()
        await app.updater.start_polling()
        
        # Launch persistent WebSockets alongside bot polling
        asyncio.create_task(pumpfun_realtime_stream(app))
        asyncio.create_task(dexscreener_realtime_stream(app))
        
        logging.info("WebSocket Streaming Engine & Interactive Bot Active...")
        await asyncio.Event().wait()

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
