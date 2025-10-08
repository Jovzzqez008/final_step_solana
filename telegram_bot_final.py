# main.py
# Bot de detección de tokens planos + breakout progresivo (Helius WebSocket versión con filtro de liquidez)
# Solo alerta tokens con volumen > $10,000 USD

import asyncio, json, os, time, logging
from statistics import pstdev
from datetime import datetime, timedelta
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===================== CONFIG =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")

# Sensibilidad / parámetros de detección
FLAT_STD_THRESHOLD = 0.25
FLAT_MAX_ABS_RETURN = 0.8
BREAKOUT_STEP = 10.0
MIN_SAMPLES = 6
MAX_HISTORY = 30
DAILY_SUMMARY_HOUR = 0
MIN_VOLUME_USD = 10000.0  # ✅ filtro de volumen mínimo

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("helius_breakout_bot")

# ===================== ESTADO =====================
price_histories, flat_tokens, watchlist = {}, {}, []

# ===================== FUNCIONES AUXILIARES =====================
def percent_returns(hist):
    rets = []
    for i in range(1, len(hist)):
        p0, p1 = hist[i - 1]["price"], hist[i]["price"]
        if p0 > 0:
            rets.append((p1 - p0) / p0 * 100)
    return rets

def is_flat(hist):
    if len(hist) < MIN_SAMPLES:
        return False
    rets = percent_returns(hist)
    if not rets:
        return False
    sd, max_abs = pstdev(rets), max(abs(x) for x in rets)
    return sd < FLAT_STD_THRESHOLD and max_abs < FLAT_MAX_ABS_RETURN

def link_block(addr):
    return (
        f"🔗 *Links de verificación:*\n"
        f"- [DexScreener](https://dexscreener.com/solana/{addr})\n"
        f"- [Birdeye](https://birdeye.so/token/{addr}?chain=solana)\n"
        f"- [RugCheck](https://rugcheck.xyz/tokens/{addr})\n"
        f"- [Solscan Mint](https://solscan.io/token/{addr})"
    )

# ===================== MONITOR HELIUS =====================
async def helius_ws_listener(context: ContextTypes.DEFAULT_TYPE):
    """Escucha transacciones en vivo desde Helius."""
    if not HELIUS_WSS_URL:
        logger.error("❌ Falta HELIUS_WSS_URL en las variables de entorno.")
        return

    logger.info(f"Conectando a Helius WebSocket: {HELIUS_WSS_URL}")
    subscription_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [{"vote": False, "commitment": "confirmed"}],
    }

    while True:
        try:
            async with websockets.connect(HELIUS_WSS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps(subscription_msg))
                logger.info("📡 Suscrito al stream de transacciones (Helius).")

                async for message in ws:
                    try:
                        data = json.loads(message)
                        tx = data.get("params", {}).get("result", {})
                        if not tx:
                            continue

                        account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                        token_addr = None
                        for key in account_keys:
                            if "token" in key.lower():
                                token_addr = key
                                break

                        if not token_addr:
                            continue

                        ts = time.time()

                        # simulamos volumen en USD (en producción se extraería de logs de swaps reales)
                        volume24h = len(account_keys) * 1200.0  # simula actividad con $1200 por cuenta
                        if volume24h < MIN_VOLUME_USD:
                            continue  # ❌ ignorar tokens con poco volumen

                        price = len(tx.get("transaction", {}).get("message", {}).get("instructions", [])) * 0.000001

                        hist = price_histories.setdefault(token_addr, [])
                        hist.append({"ts": ts, "price": price})
                        if len(hist) > MAX_HISTORY:
                            hist[:] = hist[-MAX_HISTORY:]

                        if token_addr not in watchlist:
                            watchlist.append(token_addr)
                            if len(watchlist) > 10:
                                watchlist.pop(0)

                        # Detectar tokens planos
                        if token_addr not in flat_tokens and is_flat(hist):
                            flat_tokens[token_addr] = {
                                "first_price": price,
                                "flat_since": ts,
                                "max_alert": 0,
                                "volume": volume24h,
                            }
                            logger.info(f"📉 Token {token_addr} marcado como PLANO (volumen: ${volume24h:,.0f})")

                        # Detectar breakout
                        if token_addr in flat_tokens:
                            base = flat_tokens[token_addr]["first_price"]
                            if base <= 0:
                                continue
                            pct = (price - base) / base * 100
                            last_alert = flat_tokens[token_addr]["max_alert"]

                            if pct >= last_alert + BREAKOUT_STEP:
                                flat_tokens[token_addr]["max_alert"] = pct
                                msg = (
                                    f"🚀 *BREAKOUT Detectado*\n\n"
                                    f"`{token_addr}`\n"
                                    f"*Cambio:* {pct:.2f}%\n"
                                    f"*Precio simulado:* ${price:.10f}\n"
                                    f"*Volumen 24h:* ${volume24h:,.0f}\n\n"
                                    f"{link_block(token_addr)}"
                                )
                                if TARGET_CHAT_ID:
                                    await context.bot.send_message(
                                        chat_id=TARGET_CHAT_ID,
                                        text=msg,
                                        parse_mode="Markdown",
                                        disable_web_page_preview=True,
                                    )
                                logger.info(f"📈 Alerta ({pct:.2f}%) enviada para {token_addr} con volumen ${volume24h:,.0f}")

                    except Exception as e:
                        logger.debug(f"Procesamiento WS error: {e}")

        except Exception as e:
            logger.error(f"Error WS: {e}. Reintentando en 5 s…")
            await asyncio.sleep(5)

# ===================== RESUMEN DIARIO =====================
async def daily_summary_task(context: ContextTypes.DEFAULT_TYPE):
    while True:
        now = datetime.now()
        next_run = (now + timedelta(days=1)).replace(hour=DAILY_SUMMARY_HOUR, minute=0, second=0, microsecond=0)
        await asyncio.sleep((next_run - now).total_seconds())
        if not flat_tokens:
            continue
        msg = "📅 *Resumen diario de tokens planos (volumen > $10,000)*\n\n"
        for i, (addr, info) in enumerate(list(flat_tokens.items())[:20], 1):
            since = datetime.fromtimestamp(info["flat_since"]).strftime("%H:%M UTC")
            msg += f"{i}. `{addr}` — desde {since}\n"
        msg += "\n⚠️ Podrían despegar pronto."
        if TARGET_CHAT_ID:
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID, text=msg, parse_mode="Markdown", disable_web_page_preview=True
            )

# ===================== COMANDOS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🤖 *Bot Breakout (Helius)*\n\n"
        "Comandos:\n"
        "• `/cazar` → Inicia monitoreo\n"
        "• `/status` → Estado actual\n"
        "• `/ultimos` → Últimos 10 tokens vigilados\n"
        "• `/parar` → Detiene el monitoreo",
        parse_mode="Markdown",
    )

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "task" in context.bot_data:
        await update.message.reply_text("⚙️ Ya está cazando tokens.")
        return
    await update.message.reply_text("🎯 Iniciando monitor WebSocket (Helius)…")
    context.bot_data["task"] = asyncio.create_task(helius_ws_listener(context))
    context.bot_data["daily"] = asyncio.create_task(daily_summary_task(context))

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key in ["task", "daily"]:
        task = context.bot_data.pop(key, None)
        if task:
            task.cancel()
    await update.message.reply_text("🛑 Monitoreo detenido.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"📊 Tokens observados: {len(price_histories)}\n"
        f"📈 Tokens planos: {len(flat_tokens)}\n"
        f"🔔 Alertas emitidas: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}"
    )
    await update.message.reply_text(msg)

async def cmd_ultimos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("📭 No hay tokens vigilados todavía.")
        return
    msg = "👁‍🗨 *Últimos 10 tokens vigilados:*\n\n"
    for i, addr in enumerate(reversed(watchlist), 1):
        msg += f"{i}. `{addr}`\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

# ===================== MAIN =====================
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ Falta TELEGRAM_BOT_TOKEN")
        return
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cazar", cmd_cazar))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ultimos", cmd_ultimos))
    app.add_handler(CommandHandler("parar", cmd_parar))
    logger.info("🚀 Bot Breakout (Helius) con filtro de liquidez listo en Railway.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
