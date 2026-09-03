async def analyze_custom_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes pasted token contract addresses and analyzes structural strength."""
    token_addr = update.message.text.strip()
    
    if token_addr.startswith("/"):
        return

    await update.message.reply_text(f"🔎 **Analyzing Token & Structural Strength:**\n`{token_addr}`...", parse_mode="Markdown")

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
            
            liquidity = pair.get('liquidity', {}).get('usd', 0)
            vol_5m = pair.get('volume', {}).get('m5', 0)
            vol_1h = pair.get('volume', {}).get('h1', 0)
            buys_5m = pair.get('txns', {}).get('m5', {}).get('buys', 0)
            sells_5m = pair.get('txns', {}).get('m5', {}).get('sells', 0)
            total_txns = buys_5m + sells_5m
            buy_ratio = (buys_5m / total_txns) * 100 if total_txns > 0 else 0

            # Price Change %
            price_change_5m = pair.get('priceChange', {}).get('m5', 0.0)
            price_change_1h = pair.get('priceChange', {}).get('h1', 0.0)

            # ================= STRENGTH / WEAKNESS ENGINE =================
            weakness_reasons = []
            
            # 1. Low Liquidity Check
            if liquidity < 15000:
                weakness_reasons.append("⚠️ **Thin Liquidity**: Under $15,000 pool depth (High slippage risk)")
            
            # 2. Sell Dominance Check
            if buy_ratio < 45 and total_txns >= 5:
                weakness_reasons.append(f"⚠️ **Heavy Sell Pressure**: Only {buy_ratio:.1f}% buys in last 5m")
                
            # 3. Negative Price Action Trend
            if price_change_5m < -3.0 or price_change_1h < -8.0:
                weakness_reasons.append(f"⚠️ **Downtrending Price**: 5m: `{price_change_5m:.1f}%` | 1h: `{price_change_1h:.1f}%`")
                
            # 4. Volume Decay Check (1h vs 5m projection)
            expected_5m_vol = vol_1h / 12 if vol_1h > 0 else 0
            if expected_5m_vol > 0 and vol_5m < (expected_5m_vol * 0.3):
                weakness_reasons.append("⚠️ **Volume Fading**: 5m volume is over 70% lower than hourly average")

            # Determine Strength Rating
            if len(weakness_reasons) >= 2:
                strength_rating = "🔴 **WEAK / HIGH RISK (Avoid or Exit)**"
                status_msg = "🚨 **WARNING**: This token displays clear structural weakness. High risk of dump or low liquidity trap."
            elif len(weakness_reasons) == 1:
                strength_rating = "🟡 **NEUTRAL / CAUTION (Wait for confirmation)**"
                status_msg = "⏳ **MONITORING**: Minor weakness detected. Added to watchlist to track incoming volume."
            else:
                strength_rating = "🟢 **STRONG / HIGH MOMENTUM**"
                status_msg = "✅ **BULLISH STRUCTURE**: Healthy liquidity, steady buy pressure, and solid price action."

            # Add token to custom tracking
            custom_tracked_tokens.add(token_addr)
            projections = calculate_holding_projection(vol_5m, liquidity, buy_ratio)

            msg = (
                f"📊 **TOKEN DIAGNOSTIC & STRENGTH EVALUATION**\n"
                f"**Chain:** `{chain_id}`\n"
                f"**Token:** `${symbol}` ({name})\n\n"
                f"🏋️ **Strength Rating:** {strength_rating}\n\n"
                f"**Key Metrics:**\n"
                f"• **Liquidity:** `${liquidity:,.2f}`\n"
                f"• **5m Volume:** `${vol_5m:,.2f}`\n"
                f"• **Buy Ratio:** `{buy_ratio:.1f}%` ({buys_5m} buys / {sells_5m} sells)\n"
                f"• **Price Change (5m / 1h):** `{price_change_5m:+.2f}%` / `{price_change_1h:+.2f}%`\n\n"
            )

            if weakness_reasons:
                msg += "⚠️ **Detected Weakness Factors:**\n" + "\n".join(weakness_reasons) + "\n\n"

            msg += (
                f"{status_msg}\n\n"
                f"📊 **HOLDING VALUE FORECAST ($100 Hold):**\n"
                f"{projections}\n\n"
                f"📍 [View DEXScreener]({dex_url})"
            )
            
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)
            
    except Exception as e:
        logging.error(f"Error checking custom token: {e}")
        await update.message.reply_text("⚠️ Failed to pull DEX data. Verify contract address and try again.")
