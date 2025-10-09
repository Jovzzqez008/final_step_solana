# bot_jupiter_v3_cazador_FIXED.py
# Requisitos: aiohttp, asyncpg, python-telegram-bot==20.3, websockets
# Manual mode: use /cazar to start workers, /parar to stop them.

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

# ---------------- CONFIG ----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

# Helius proxy fixed as fallback (fast for Raydium / pumpfun)
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "https://helius-proxy.raydium.io")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL", "wss://helius-proxy.raydium.io/ws")

DEXSCREENER_API = os.getenv("DEXSCREENER_API", "")
JUPITER_BASE = os.getenv("JUPITER_LITE_URL", "https://lite-api.jup.ag")

# Pump.fun program id fixed (as requested)
PUMPFUN_PROGRAM_ID = "pumpfun1Mt11111111111111111111111111111111"
PUMP_PRE_GRADUATION_THRESHOLD = float(os.getenv("PUMP_PRE_THRESHOLD", "60000"))
PUMP_PRE_ALERT_MARGIN = float(os.getenv("PUMP_PRE_ALERT_MARGIN", "5000"))  # alert a 60k-5k = 55k

# Runtime-default params (tuneable via Telegram)
DEFAULTS = {
    "MIN_FLAT_DURATION_HOURS": int(os.getenv("MIN_FLAT_DURATION_HOURS", "12")),  # default 12h
    "CANDLES_TO_CHECK": int(os.getenv("CANDLES_TO_CHECK", "48")),  # 48 * 15m = 12h
    "FLAT_STD_THRESHOLD": float(os.getenv("FLAT_STD_THRESHOLD", "0.15")),
    "VOLUME_SPIKE_THRESHOLD": float(os.getenv("VOLUME_SPIKE_THRESHOLD", "150")),
    "MIN_CONSECUTIVE_LOW": int(os.getenv("MIN_CONSECUTIVE_LOW", "6")),
    "BREAKOUT_STEP": float(os.getenv("BREAKOUT_STEP", "10.0")),
    "UPDATE_INTERVAL": int(os.getenv("UPDATE_INTERVAL", "600")),  # 10 minutes
    "MAX_CANDIDATES": int(os.getenv("MAX_CANDIDATES", "120")),
    "CONCURRENCY": int(os.getenv("CONCURRENCY", "12")),
}
params = DEFAULTS.copy()

DB_TABLE_NOTIFIED = "notified_mints"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("jupiter_v3_cazador_fixed")

# ---------------- GLOBAL STATE ----------------
http_session = None
pg_pool = None
telegram_bot = None
app_bot = None

price_histories = defaultdict(lambda: deque(maxlen=500))
flat_tokens = {}
monitored_tokens = set()

# Worker tasks and stop event (manual)
worker_stop_evt = asyncio.Event()
monitor_task = None
pump_task = None

# ---------------- HTTP / TELEGRAM HELPERS ----------------
async def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session

async def send_telegram_text(text: str, parse_mode=ParseMode.MARKDOWN):
    global telegram_bot
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, skipping send.")
        return
    if telegram_bot is None:
        telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=parse_mode, disable_web_page_preview=False)
    except Exception as e:
        logger.error(f"Telegram send error: {e}")

# ---------------- DB ----------------
async def init_db():
    global pg_pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not provided; running without persistence.")
        return
    pg_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8)
    async with pg_pool.acquire() as conn:
        await conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {DB_TABLE_NOTIFIED} (
                mint TEXT PRIMARY KEY,
                first_notified_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
                data JSONB
            );
        """)
    logger.info("DB initialized")

async def mark_notified(mint: str, data: dict):
    if pg_pool is None:
        flat_tokens.setdefault(mint, {})['notified'] = True
        return
    async with pg_pool.acquire() as conn:
        await conn.execute(f"INSERT INTO {DB_TABLE_NOTIFIED}(mint, data) VALUES($1, $2) ON CONFLICT (mint) DO NOTHING", mint, json.dumps(data))

async def is_notified(mint: str) -> bool:
    if pg_pool is None:
        return flat_tokens.get(mint, {}).get('notified', False)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(f"SELECT mint FROM {DB_TABLE_NOTIFIED} WHERE mint=$1", mint)
        return bool(row)

# ---------------- JUPITER CLIENT (lite) ----------------
class JupiterClient:
    def __init__(self, base=JUPITER_BASE):
        self.base = base.rstrip("/")
        self.cache = {}

    async def request(self, path, cache_key=None, ttl=300):
        session = await get_http_session()
        if cache_key and cache_key in self.cache:
            data, ts = self.cache[cache_key]
            if time.time() - ts < ttl:
                return data
        url = f"{self.base}{path}" if path.startswith("/") else f"{self.base}/{path}"
        try:
            async with session.get(url, timeout=12) as r:
                if r.status == 200:
                    data = await r.json()
                    if cache_key:
                        self.cache[cache_key] = (data, time.time())
                    return data
        except Exception as e:
            logger.debug(f"Jupiter request error: {e}")
        return None

    async def get_multiple_token_sources(self):
        paths = [
            "/tokens/v2/toporganicscore/1h?limit=80",
            "/tokens/v2/toptraded/1h?limit=80",
            "/tokens/v2/tag?query=verified",
            "/tokens/v2/recent?limit=50"
        ]
        out = []
        for i,p in enumerate(paths):
            data = await self.request(p, cache_key=f"ep{i}", ttl=600)
            if data:
                out.extend(data)
        return out

    async def get_token_by_id(self, mint):
        return await self.request(f"/tokens/v2/search?query={mint}", cache_key=f"t_{mint}", ttl=60)

    async def compute_universe_avg_volume(self, sample_limit: int = 50):
        tokens = await self.get_multiple_token_sources()
        vols = []
        for t in tokens[:sample_limit]:
            try:
                v = (t.get('stats24h', {}).get('buyVolume', 0) + t.get('stats24h', {}).get('sellVolume', 0))
                if v and v > 0:
                    vols.append(v / 96.0)  # approximate 15m candles in 24h -> 96
            except Exception:
                continue
        if not vols:
            return 50.0
        return sum(vols) / len(vols)

jupiter = JupiterClient()

# ---------------- DEXSCREENER CANDLES ----------------
async def fetch_candles_dexscreener(mint: str, interval_minutes=15, limit=None):
    limit = limit or params['CANDLES_TO_CHECK']
    session = await get_http_session()
    try:
        search_url = f"https://api.dexscreener.com/latest/dex/search/?q={mint}"
        async with session.get(search_url, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get('pairs') or []
                if pairs:
                    pair = pairs[0]
                    pair_id = pair.get('pairAddress') or pair.get('pair')
                    pair_url = f"https://api.dexscreener.com/latest/dex/pair/{pair_id}"
                    async with session.get(pair_url, timeout=8) as r2:
                        if r2.status == 200:
                            pair_data = await r2.json()
                            candles = pair_data.get('candles') or pair_data.get('chart') or []
                            normalized = []
                            for c in candles[-limit:]:
                                if isinstance(c, list) and len(c) >= 6:
                                    normalized.append({
                                        'time': c[0], 'open': c[1], 'high': c[2], 'low': c[3], 'close': c[4], 'volume': c[5]
                                    })
                            return normalized
    except Exception as e:
        logger.debug(f"Dexscreener fetch error {e}")
    return []

# ---------------- FLAT DETECTION (adaptative + advanced) ----------------
def calculate_volatility_from_candles(candles):
    prices = [c['close'] for c in candles if c.get('close') is not None]
    if len(prices) < 4:
        return None
    returns = []
    for i in range(1, len(prices)):
        prev = prices[i-1]
        if prev == 0:
            continue
        returns.append((prices[i] - prev) / prev * 100)
    if not returns:
        return None
    return {'std_dev': pstdev(returns) if len(returns) > 1 else 0,
            'max_move': max(abs(x) for x in returns),
            'avg_move': mean([abs(x) for x in returns]),
            'min_price': min(prices), 'max_price': max(prices)}

def analyze_volume_sequence(volumes, colors, adaptive_low_cut, adaptive_spike):
    low_cut = adaptive_low_cut
    spike_thresh = adaptive_spike
    min_consec_low = params['MIN_CONSECUTIVE_LOW']

    low_count = sum(1 for v in volumes if v < low_cut)
    med_count = sum(1 for v in volumes if low_cut <= v <= (spike_thresh))
    high_count = sum(1 for v in volumes if v > spike_thresh)

    isolated_spikes = 0
    for i in range(1, len(volumes)-1):
        prev_low = volumes[i-1] < low_cut * 0.9
        cur_high = volumes[i] > spike_thresh
        next_low = volumes[i+1] < low_cut * 1.1
        if prev_low and cur_high and next_low:
            isolated_spikes += 1

    condition = (low_count >= min_consec_low and med_count <= max(6, int(len(volumes)*0.25)) and high_count <= max(3, int(len(volumes)*0.1)) and isolated_spikes >= 1)
    return condition, {'low_count': low_count, 'med_count': med_count, 'high_count': high_count, 'isolated_spikes': isolated_spikes}

def detect_volume_manipulation(volumes, window=4):
    if len(volumes) < window:
        return False
    avg = sum(volumes)/len(volumes)
    if avg == 0:
        return False
    anomalous = 0
    for i, v in enumerate(volumes):
        if v > avg * 10:
            s = max(0, i-2); e = min(len(volumes), i+3)
            w = volumes[s:e]
            if sum(1 for x in w if x > avg * 5) <= 1:
                anomalous += 1
    return anomalous > 1

async def detect_ghost_volume_pattern(candles):
    if len(candles) < 8:
        return False, {}
    volumes = [c.get('volume', 0) for c in candles]
    colors = ['green' if c.get('close',0) >= c.get('open',0) else 'red' for c in candles]

    # adaptive thresholds (universe-aware)
    universe_avg = await jupiter.compute_universe_avg_volume()
    adaptive_low_cut = min(max(5, universe_avg * 0.01), 120)  # between $5 and $120
    adaptive_spike = max(params['VOLUME_SPIKE_THRESHOLD'], universe_avg * 3)

    vol_cond, vol_details = analyze_volume_sequence(volumes, colors, adaptive_low_cut, adaptive_spike)
    metrics = calculate_volatility_from_candles(candles)
    if not metrics:
        return False, {}
    manipulated = detect_volume_manipulation(volumes)
    flat_cond = (vol_cond and not manipulated and metrics['std_dev'] < params['FLAT_STD_THRESHOLD']
                 and metrics['max_move'] < 1.0 and ((metrics['max_price']-metrics['min_price'])/(metrics['min_price'] or 1)*100) < 2.5)
    return flat_cond, {**vol_details, 'metrics': metrics, 'manipulated': manipulated, 'avg_vol': sum(volumes)/len(volumes),
                       'adaptive_low_cut': adaptive_low_cut, 'adaptive_spike': adaptive_spike}

async def evaluate_token_flat(mint: str):
    candles = await fetch_candles_dexscreener(mint)
    if not candles:
        return False, {'reason':'no_candles'}
    last = candles[-params['CANDLES_TO_CHECK']:]
    is_flat, details = await detect_ghost_volume_pattern(last)
    return is_flat, {'candles': len(last), **details}

# ---------------- ALERTS ----------------
def format_links(mint: str) -> str:
    return (f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
            f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
            f"• [Solscan](https://solscan.io/token/{mint})\n"
            f"• [Jupiter token](https://jup.ag/tokens/{mint})\n")

async def basic_jupiter_audit(mint: str):
    try:
        res = await jupiter.get_token_by_id(mint)
        if not res:
            return {}
        token = res[0] if isinstance(res,list) and res else (res if isinstance(res,dict) else {})
        return token.get('audit', {})
    except Exception as e:
        logger.debug(f"audit error {e}")
        return {}

async def alert_flat_found(mint: str, token_meta: dict, flat_info: dict):
    if await is_notified(mint):
        logger.info(f"{mint} already notified")
        return
    audit = await basic_jupiter_audit(mint)
    ma = audit.get('mintAuthorityDisabled', False)
    fa = audit.get('freezeAuthorityDisabled', False)
    msg = (
        f"🚨 *TOKEN EN PUNTO FRÍO (FLAT) DETECTADO* 🚨\n\n"
        f"*Mint:* `{mint}`\n"
        f"*Symbol:* {token_meta.get('symbol','N/A')} — {token_meta.get('name','N/A')}\n"
        f"*Liq:* ${token_meta.get('liquidity',0):,.0f} | *Vol24:* ${token_meta.get('volume24h',0):,.0f}\n"
        f"*MintDisabled:* {'✅' if ma else '❌'} | *FreezeDisabled:* {'✅' if fa else '❌'}\n\n"
        f"*Análisis Volumen:*\n - Velas bajo < ${flat_info.get('adaptive_low_cut', 'N/A'):.1f}: {flat_info.get('low_count','N/A')}\n - Picos aislados: {flat_info.get('isolated_spikes','N/A')}\n - Volumen promedio: ${flat_info.get('avg_vol',0):.2f}\n - Manipulación detectada: {'❌' if flat_info.get('manipulated') else '✅'}\n\n"
        f"*Metrics:* {json.dumps(flat_info.get('metrics',{}), default=str)}\n\n"
        f"🔗 Enlaces:\n{format_links(mint)}\n"
    )
    await send_telegram_text(msg)
    await mark_notified(mint, {'meta': token_meta, 'flat_info': flat_info, 'type':'flat'})

async def alert_pumpfun_pre_graduation(mint: str, mc: float, token_meta: dict=None):
    if await is_notified(mint):
        return
    msg = (
        f"🔥 *Pump.fun Pre-Graduation* 🔥\n\n"
        f"*Mint:* `{mint}`\n"
        f"*MC estimado:* ${mc:,.0f}\n"
        f"*Umbral:* ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}\n\n"
        f"🔗 Enlaces:\n{format_links(mint)}\n"
    )
    await send_telegram_text(msg)
    await mark_notified(mint, {'type':'pump','mc':mc})

# ---------------- WORKERS ----------------
async def flat_scanner_worker(stop_event: asyncio.Event):
    logger.info("Flat scanner V3 started")
    while not stop_event.is_set():
        try:
            candidates = await jupiter.get_multiple_token_sources()
            uniq = {}
            for t in candidates:
                tid = t.get('id') if isinstance(t,dict) else None
                if tid:
                    # keep micro tokens too; later analysis decides
                    uniq[tid] = t
            token_list = list(uniq.keys())
            logger.info(f"Scanning {len(token_list)} candidates")
            sem = asyncio.Semaphore(params['CONCURRENCY'])

            async def eval_one(mint):
                async with sem:
                    if await is_notified(mint):
                        return
                    meta = uniq.get(mint,{})
                    is_flat, details = await evaluate_token_flat(mint)
                    if is_flat:
                        details['detected_at'] = datetime.utcnow().isoformat()
                        token_meta = {
                            'symbol': meta.get('symbol'), 'name': meta.get('name'),
                            'liquidity': meta.get('liquidity'),
                            'volume24h': (meta.get('stats24h',{}).get('buyVolume',0)+meta.get('stats24h',{}).get('sellVolume',0))
                        }
                        await alert_flat_found(mint, token_meta, details)

            tasks = [asyncio.create_task(eval_one(m)) for m in token_list[:params['MAX_CANDIDATES']]]
            await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"Flat worker error: {e}")
        await asyncio.sleep(params['UPDATE_INTERVAL'])

async def pumpfun_wss_worker(stop_event: asyncio.Event):
    if not HELIUS_WSS_URL:
        logger.warning("HELIUS_WSS_URL not set; pumpfun worker disabled")
        return
    logger.info("Pump.fun WSS worker started")
    while not stop_event.is_set():
        try:
            async with websockets.connect(HELIUS_WSS_URL, ping_interval=20, ping_timeout=10) as ws:
                logger.info("Connected to Helius WSS")
                sub = {"jsonrpc":"2.0","id":1,"method":"logsSubscribe","params":[{"mentions":[PUMPFUN_PROGRAM_ID]},{"commitment":"processed"}]}
                await ws.send(json.dumps(sub))
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        msg = json.loads(raw)
                        params_msg = msg.get('params')
                        if not params_msg:
                            continue
                        result = params_msg.get('result') if isinstance(params_msg,dict) else params_msg[0].get('result')
                        if not result:
                            continue
                        value = result.get('value') if isinstance(result,dict) else None
                        logs = value.get('logs') if value else []
                        text = "\n".join([str(l) for l in logs])
                        # detect market_cap mentions
                        for m in re.finditer(r"market_cap\W*[:=]\W*(\d+[.,]?\d*)", text, re.IGNORECASE):
                            mc = float(m.group(1).replace(',',''))
                            if mc >= (PUMP_PRE_GRADUATION_THRESHOLD - PUMP_PRE_ALERT_MARGIN):
                                mm = re.search(r"mint\W*[:=]\W*([A-Za-z0-9]{32,44})", text)
                                mint = mm.group(1) if mm else None
                                if mint and not await is_notified(mint):
                                    meta = await jupiter.get_token_by_id(mint)
                                    token_meta = meta[0] if isinstance(meta,list) and meta else (meta if isinstance(meta,dict) else {})
                                    await alert_pumpfun_pre_graduation(mint, mc, token_meta)
                    except asyncio.TimeoutError:
                        try:
                            await ws.send(json.dumps({"jsonrpc":"2.0","id":9999,"method":"ping"}))
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"WSS connection error: {e}")
            await asyncio.sleep(5)

# ---------------- TELEGRAM COMMANDS (tuning/manual) ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot V3 Cazador listo. Usa /cazar para iniciar, /parar para detener.")

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_task, pump_task, worker_stop_evt
    if monitor_task and not monitor_task.done():
        await update.message.reply_text("Monitoreo ya activo.")
        return
    # clear stop event and start workers
    worker_stop_evt.clear()
    monitor_task = asyncio.create_task(flat_scanner_worker(worker_stop_evt))
    pump_task = asyncio.create_task(pumpfun_wss_worker(worker_stop_evt))
    await update.message.reply_text("Monitoreo iniciado (flat scanner + pumpfun watcher).")

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global monitor_task, pump_task, worker_stop_evt
    if not (monitor_task or pump_task):
        await update.message.reply_text("No hay monitoreo en ejecución.")
        return
    worker_stop_evt.set()
    # wait a short moment for tasks to finish cleanly
    await asyncio.sleep(1.0)
    if monitor_task:
        try:
            await asyncio.wait_for(monitor_task, timeout=5)
        except Exception:
            pass
    if pump_task:
        try:
            await asyncio.wait_for(pump_task, timeout=5)
        except Exception:
            pass
    monitor_task = None
    pump_task = None
    # reset stop evt for future use
    worker_stop_evt = asyncio.Event()
    await update.message.reply_text("Monitoreo detenido.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flat_count = len(flat_tokens)
    notified = 'DB' if pg_pool else 'Memory'
    msg = (f"Bot V3 status:\nMonitored tokens: {len(monitored_tokens)}\nFlat tokens: {flat_count}\nPersistence: {notified}\nParams: {json.dumps(params)}")
    await update.message.reply_text(msg)

async def cmd_ajustar_std(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0])
        params['FLAT_STD_THRESHOLD'] = val
        await update.message.reply_text(f"FLAT_STD_THRESHOLD actualizado a {val}")
    except Exception:
        await update.message.reply_text("Usage: /ajustar_std 0.12")

async def cmd_ajustar_vol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0])
        params['VOLUME_SPIKE_THRESHOLD'] = val
        await update.message.reply_text(f"VOLUME_SPIKE_THRESHOLD actualizado a {val}")
    except Exception:
        await update.message.reply_text("Usage: /ajustar_vol 150")

# ---------------- MAIN ----------------
async def main():
    global app_bot, monitor_task, pump_task, worker_stop_evt
    logger.info("Starting Bot Jupiter V3 - Cazador (FIXED manual mode)")
    await init_db()

    # Build Telegram application but DO NOT start workers automatically
    if TELEGRAM_BOT_TOKEN:
        app_bot = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        app_bot.add_handler(CommandHandler('start', cmd_start))
        app_bot.add_handler(CommandHandler('cazar', cmd_cazar))
        app_bot.add_handler(CommandHandler('parar', cmd_parar))
        app_bot.add_handler(CommandHandler('status', cmd_status))
        app_bot.add_handler(CommandHandler('ajustar_std', cmd_ajustar_std))
        app_bot.add_handler(CommandHandler('ajustar_vol', cmd_ajustar_vol))

        # initialize and start polling in the same event loop safely
        await app_bot.initialize()
        await app_bot.start()
        await app_bot.updater.start_polling()
        logger.info("Telegram polling started (manual control mode).")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot will run without telegram commands.")

    # Keep application alive until interrupted; workers are started by /cazar
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Main cancelled, shutting down...")
    finally:
        # stop workers if running
        worker_stop_evt.set()
        if monitor_task and not monitor_task.done():
            try:
                await monitor_task
            except Exception:
                pass
        if pump_task and not pump_task.done():
            try:
                await pump_task
            except Exception:
                pass
        # shutdown telegram app cleanly if it was started
        if app_bot:
            try:
                await app_bot.updater.stop_polling()
            except Exception:
                pass
            try:
                await app_bot.stop()
                await app_bot.shutdown()
            except Exception:
                pass
        # close http and db
        if http_session:
            try:
                await http_session.close()
            except Exception:
                pass
        if pg_pool:
            try:
                await pg_pool.close()
            except Exception:
                pass
        logger.info("Shutdown complete.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # graceful exit on Ctrl-C
        logger.info("Interrupted by user - exiting")
