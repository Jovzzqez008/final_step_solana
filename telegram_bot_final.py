# Guarda este archivo como: bot_jupiter_v2_pro_v4.py
# (Requisitos: aiohttp, asyncpg, python-telegram-bot, websockets)
import os
import asyncio
import json
import time
import logging
import re
from datetime import datetime
from statistics import pstdev, mean
from collections import deque, defaultdict
import aiohttp
import asyncpg
import websockets
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ------------------ CONFIG & ENV ------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
JUPITER_BASE = os.getenv("JUPITER_LITE_URL", "https://lite-api.jup.ag")

# PUMPFUN_PROGRAM_ID fijo y correcto
PUMPFUN_PROGRAM_ID = "pumpfun1Mt11111111111111111111111111111111"
PUMP_PRE_GRADUATION_THRESHOLD = float(os.getenv("PUMP_PRE_THRESHOLD", "60000"))

# Defaults: ajustables en tiempo real vía Telegram
DEFAULTS = {
    "FLAT_STD_THRESHOLD": float(os.getenv("FLAT_STD_THRESHOLD", "0.15")),
    "VOLUME_SPIKE_THRESHOLD": float(os.getenv("VOLUME_SPIKE_THRESHOLD", "150")),
    "MIN_CONSECUTIVE_LOW": int(os.getenv("MIN_CONSECUTIVE_LOW", "6")),
    "CANDLES_TO_CHECK": int(os.getenv("CANDLES_TO_CHECK", "12")),
}
# DB table
DB_TABLE_NOTIFIED = "notified_mints"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("jupiter_v4")

# ------------------ GLOBAL STATE ------------------
http_session = None
pg_pool = None
telegram_bot = None
app_bot = None

monitored_tokens = set()
token_metadata = defaultdict(dict)
params = DEFAULTS.copy()
stop_evt = asyncio.Event()
stop_evt.set()  # Inicia detenido

# --- Clases de Cliente (para limpieza de código) ---
class JupiterClient:
    def __init__(self, base_url=JUPITER_BASE):
        self.base = base_url
        self.cache = {}
    async def request(self, path: str, cache_key: str = None, ttl: int = 300):
        session = await get_http_session()
        if cache_key and cache_key in self.cache:
            data, ts = self.cache[cache_key]
            if time.time() - ts < ttl: return data
        url = f"{self.base}{path}" if path.startswith("/") else f"{self.base}/{path}"
        try:
            async with session.get(url, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    if cache_key: self.cache[cache_key] = (data, time.time())
                    return data
        except Exception: return None
    async def get_multiple_token_sources(self):
        paths = ["/tokens/v2/toporganicscore/1h?limit=100", "/tokens/v2/toptraded/1h?limit=100", "/tokens/v2/recent?limit=100"]
        out = []
        for i, p in enumerate(paths):
            data = await self.request(p, cache_key=f"ep{i}", ttl=600)
            if data: out.extend(data)
        return out
    async def get_token_by_id(self, mint):
        return await self.request(f"/tokens/v2/search?query={mint}", cache_key=f"t_{mint}", ttl=60)

# ------------------ HELPERS & DB ------------------
async def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session

async def send_telegram_text(text: str, parse_mode=ParseMode.MARKDOWN):
    global telegram_bot
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    if telegram_bot is None: telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=parse_mode, disable_web_page_preview=True)
    except Exception as e: logger.error(f"Telegram send error: {e}")

async def init_db():
    global pg_pool
    if not DATABASE_URL:
        logger.warning("No DATABASE_URL; running without DB")
        return
    try:
        pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with pg_pool.acquire() as conn:
            await conn.execute(f"CREATE TABLE IF NOT EXISTS {DB_TABLE_NOTIFIED} (mint TEXT PRIMARY KEY, first_notified_at TIMESTAMP WITH TIME ZONE DEFAULT now(), data JSONB);")
        logger.info("DB initialized")
    except Exception as e:
        logger.error(f"DB initialization failed: {e}")
        pg_pool = None

async def mark_notified(mint: str, data: dict):
    if pg_pool is None: return
    async with pg_pool.acquire() as conn:
        await conn.execute(f"INSERT INTO {DB_TABLE_NOTIFIED}(mint, data) VALUES($1, $2) ON CONFLICT (mint) DO NOTHING", mint, json.dumps(data))

async def is_notified(mint: str) -> bool:
    if pg_pool is None: return False
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT mint FROM {DB_TABLE_NOTIFIED} WHERE mint=$1", mint)
        return bool(row)
        
# ------------------ DEXSCREENER & FLAT DETECTION ------------------
async def fetch_candles_dexscreener(mint: str):
    session = await get_http_session()
    limit = params['CANDLES_TO_CHECK']
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with session.get(url, timeout=8) as resp:
            data = await resp.json()
            if resp.status == 200 and data.get('pairs'):
                # Idealmente, buscar el par con más liquidez o volumen (ej. vs SOL)
                pair = data['pairs'][0] # Tomamos el primero por simplicidad
                pair_address = pair['pairAddress']
                candles_url = f"https://io.dexscreener.com/u/chart/bars/solana/{pair_address}?q=SOL&t=15&c=30" # velas de 15 min
                async with session.get(candles_url, timeout=8) as r_candles:
                    candle_data = await r_candles.json()
                    if r_candles.status == 200 and candle_data.get('bars'):
                        normalized = []
                        for c in candle_data['bars'][-limit:]:
                            normalized.append({'time': c['timestamp'], 'open': float(c['open']), 'high': float(c['high']), 'low': float(c['low']), 'close': float(c['close']), 'volume': float(c['volume'])})
                        return normalized
    except Exception as e:
        logger.debug(f"Dexscreener fetch error for {mint}: {e}")
    return []

def analyze_flat_pattern(candles):
    if len(candles) < 8: return False, {}
    prices = [c['close'] for c in candles]
    volumes = [c['volume'] for c in candles]
    
    # Volatilidad
    returns = [(prices[i] - prices[i-1]) / prices[i-1] * 100 for i in range(1, len(prices)) if prices[i-1] != 0]
    if not returns: return False, {}
    std_dev = pstdev(returns) if len(returns) > 1 else 0

    # Volumen
    low_vol_count = sum(1 for v in volumes if v < 20)
    isolated_spikes = 0
    for i in range(1, len(volumes)-1):
        if volumes[i] > params['VOLUME_SPIKE_THRESHOLD'] and volumes[i-1] < 20 and volumes[i+1] < 20:
            isolated_spikes += 1

    # Condición
    is_flat = std_dev < params['FLAT_STD_THRESHOLD'] and low_vol_count >= params['MIN_CONSECUTIVE_LOW'] and isolated_spikes >= 1
    
    details = {
        'std_dev': round(std_dev, 4),
        'low_vol_count': low_vol_count,
        'isolated_spikes': isolated_spikes
    }
    return is_flat, details

# ------------------ ALERTS & FORMATTING ------------------
def format_verification_links(mint: str) -> str:
    return (
        f"🔗 *Verificación Rápida:*\n"
        f"[DexScreener](https://dexscreener.com/solana/{mint}) | "
        f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | "
        f"[RugCheck](https://rugcheck.xyz/tokens/{mint})"
    )

async def alert_flat_found(mint: str, token_meta: dict, flat_info: dict):
    if await is_notified(mint): return
    jupiter = JupiterClient()
    audit_data = await jupiter.get_token_by_id(mint)
    audit = audit_data[0].get('audit', {}) if audit_data and isinstance(audit_data, list) else {}
    ma = audit.get('mintAuthorityDisabled', False)
    fa = audit.get('freezeAuthorityDisabled', False)
    links = format_verification_links(mint)
    msg = (
        f"🚨 *TOKEN EN PUNTO FRÍO (FLAT) DETECTADO* 🚨\n\n"
        f"*Token:* {token_meta.get('symbol','N/A')} - _{token_meta.get('name','N/A')}_\n"
        f"*Mint:* `{mint}`\n\n"
        f"*Auditoría:* Mint {'✅' if ma else '❌'} | Freeze {'✅' if fa else '❌'}\n"
        f"*Detalles:* STD={flat_info['std_dev']}, Velas Bajas={flat_info['low_vol_count']}, Picos Aislados={flat_info['isolated_spikes']}\n\n"
        f"{links}"
    )
    await send_telegram_text(msg)
    await mark_notified(mint, {'type':'flat', 'meta': token_meta})

async def alert_pumpfun_pre_graduation(mint: str, mc: float, token_meta: dict = None):
    if await is_notified(mint): return
    links = format_verification_links(mint)
    msg = (
        f"🔥 *Pump.fun Pre-Graduación* 🔥\n\n"
        f"*Token:* {token_meta.get('symbol','N/A')} - _{token_meta.get('name','N/A')}_\n"
        f"*Mint:* `{mint}`\n"
        f"*MC estimado:* ${mc:,.0f}\n"
        f"*Umbral:* ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}\n\n"
        f"{links}"
    )
    await send_telegram_text(msg)
    await mark_notified(mint, {'type':'pump', 'mc':mc})

# ------------------ WORKERS ------------------
async def flat_scanner_worker(stop_event: asyncio.Event):
    logger.info("Flat scanner V4 started")
    jupiter = JupiterClient()
    while not stop_event.is_set():
        try:
            candidates = await jupiter.get_multiple_token_sources()
            uniq = {t['id']: t for t in candidates if isinstance(t, dict) and t.get('id')}
            token_list = list(uniq.keys())
            logger.info(f"Scanning {len(token_list)} candidates for flat pattern")
            
            global monitored_tokens, token_metadata
            monitored_tokens.update(token_list)
            for mint, meta in uniq.items():
                if mint in monitored_tokens:
                    token_metadata[mint]['symbol'] = meta.get('symbol', 'N/A')
                    token_metadata[mint]['name'] = meta.get('name', 'N/A')

            async def eval_one(mint):
                if await is_notified(mint): return
                candles = await fetch_candles_dexscreener(mint)
                is_flat, details = analyze_flat_pattern(candles)
                if is_flat: await alert_flat_found(mint, token_metadata[mint], details)

            tasks = [eval_one(m) for m in token_list]
            await asyncio.gather(*tasks)
        except Exception as e: logger.error(f"Flat worker error: {e}", exc_info=True)
        await asyncio.sleep(120)

async def pumpfun_wss_worker(stop_event: asyncio.Event):
    if not HELIUS_WSS_URL: return
    logger.info("Pump.fun WSS worker started")
    jupiter = JupiterClient()
    while not stop_event.is_set():
        try:
            async with websockets.connect(HELIUS_WSS_URL) as ws:
                sub = {"jsonrpc":"2.0", "id":1, "method":"logsSubscribe", "params":[{"mentions":[PUMPFUN_PROGRAM_ID]}, {"commitment":"processed"}]}
                await ws.send(json.dumps(sub))
                while not stop_event.is_set():
                    msg_raw = await ws.recv()
                    msg = json.loads(msg_raw)
                    logs = msg.get('params', {}).get('result', {}).get('value', {}).get('logs', [])
                    log_text = " ".join(logs)
                    if "buy" in log_text:
                        mc_match = re.search(r"market_cap: (\d+)", log_text)
                        mint_match = re.search(r"mint: ([A-Za-z0-9]{32,44})", log_text)
                        if mc_match and mint_match:
                            mc = float(mc_match.group(1))
                            mint = mint_match.group(1)
                            if mc >= PUMP_PRE_GRADUATION_THRESHOLD:
                                meta_list = await jupiter.get_token_by_id(mint)
                                meta = meta_list[0] if meta_list else {}
                                await alert_pumpfun_pre_graduation(mint, mc, meta)
        except Exception as e:
            logger.error(f"Pumpfun WSS error: {e}")
            await asyncio.sleep(5)

# ------------------ TELEGRAM COMMANDS ------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = ("🤖 *Bot de Caza v4 Activo*\n\nComandos:\n`/cazar` - Inicia monitores.\n`/parar` - Detiene monitores.\n`/status` - Muestra estado.\n`/tokens` - Lista tokens en radar.\n`/ajustar_std [valor]`\n`/ajustar_pico [valor]`")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_task, pump_task
    if not stop_evt.is_set(): await update.message.reply_text("✅ Monitoreo ya activo."); return
    stop_evt.clear()
    monitor_task = asyncio.create_task(flat_scanner_worker(stop_evt))
    pump_task = asyncio.create_task(pumpfun_wss_worker(stop_evt))
    await update.message.reply_text("🚀 Monitoreo *iniciado*.")

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if stop_evt.is_set(): await update.message.reply_text("⛔️ Monitoreo ya detenido."); return
    stop_evt.set()
    await update.message.reply_text("🛑 Monitoreo *detenido*.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = "🟢 ACTIVO" if not stop_evt.is_set() else "🔴 DETENIDO"
    msg = (f"🤖 *Estado del Bot v4*\n\n*Monitoreo:* {status}\n*Tokens en Radar:* {len(monitored_tokens)}\n*Persistencia:* {'PostgreSQL ✅' if pg_pool else 'Memoria ⚠️'}\n\n"
           f"⚙️ *Parámetros Actuales:*\n`  - STD Flat:` {params['FLAT_STD_THRESHOLD']}\n`  - Pico Volumen:` {params['VOLUME_SPIKE_THRESHOLD']}\n`  - Velas Bajas:` {params['MIN_CONSECUTIVE_LOW']}")
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitored_tokens: await update.message.reply_text("📡 No hay tokens en el radar."); return
    await update.message.reply_text(f"📋 Enviando lista de *{len(monitored_tokens)}* tokens...", parse_mode=ParseMode.MARKDOWN)
    batch, count = [], 0
    for mint in list(monitored_tokens):
        symbol = token_metadata[mint].get('symbol', 'N/A')
        links = f"[DS](https://dexscreener.com/solana/{mint}) | [BE](https://birdeye.so/token/{mint}?chain=solana) | [RC](https://rugcheck.xyz/tokens/{mint})"
        batch.append(f"*{symbol}* (`{mint[:4]}...{mint[-4:]}`): {links}")
        if len(batch) >= 10:
            await update.message.reply_text("\n".join(batch), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            batch = []
            await asyncio.sleep(1)
    if batch: await update.message.reply_text("\n".join(batch), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

async def cmd_ajustar_std(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0])
        params['FLAT_STD_THRESHOLD'] = val
        await update.message.reply_text(f"✅ Umbral STD actualizado a: *{val}*", parse_mode=ParseMode.MARKDOWN)
    except (IndexError, ValueError): await update.message.reply_text("Uso: `/ajustar_std 0.15`")

async def cmd_ajustar_pico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0])
        params['VOLUME_SPIKE_THRESHOLD'] = val
        await update.message.reply_text(f"✅ Umbral de Pico de Volumen actualizado a: *{val}*", parse_mode=ParseMode.MARKDOWN)
    except (IndexError, ValueError): await update.message.reply_text("Uso: `/ajustar_pico 150.0`")

# ------------------ MAIN ------------------
async def main():
    global stop_evt, monitor_task, pump_task, app_bot
    logger.info("Starting Bot V4")
    await init_db()
    if not TELEGRAM_BOT_TOKEN: logger.error("TELEGRAM_BOT_TOKEN no configurado."); return
    app_bot = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    handlers = [CommandHandler('start', cmd_start), CommandHandler('cazar', cmd_cazar), CommandHandler('parar', cmd_parar), CommandHandler('status', cmd_status), CommandHandler('tokens', cmd_tokens), CommandHandler('ajustar_std', cmd_ajustar_std), CommandHandler('ajustar_pico', cmd_ajustar_pico)]
    for handler in handlers: app_bot.add_handler(handler)
    await app_bot.initialize()
    await app_bot.start()
    await app_bot.updater.start_polling()
    await send_telegram_text("🤖 Bot V4 reiniciado. Usa `/cazar` para empezar.")
    try:
        while True: await asyncio.sleep(3600)
    finally:
        stop_evt.set()
        if 'monitor_task' in locals() and not monitor_task.done(): monitor_task.cancel()
        if 'pump_task' in locals() and not pump_task.done(): pump_task.cancel()
        if http_session: await http_session.close()
        if pg_pool: await pg_pool.close()
        await app_bot.shutdown()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
