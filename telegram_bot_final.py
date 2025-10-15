#!/usr/bin/env python3
# telegram_bot_final.py
"""
Pump.fun Alert Bot - telegram_bot_final.py
- Manual start: /cazar
- Manual stop: /parar
- Reads config.json (advanced structure) + .env
- Uses PumpPortal WS subscribeNewToken
- Uses RPC primary/fallback (Helius / QuickNode)
- Persists to Postgres via asyncpg (optional)
- Sends Telegram alerts (one per token) when ROI condition met
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import httpx
import websockets
import asyncpg
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Load env
load_dotenv()

# -------------------- CONFIG LOAD --------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# default config
DEFAULT_CONFIG = {
    "telegram": {"chat_id": None, "parse_mode": "Markdown"},
    "filters": {
        "min_marketcap": 0,
        "min_liquidity_sol": 0.5,
        "min_holders": 0,
        "roi_targets": [{"percent": 100, "minutes": 15}]
    },
    "rpc": {"primary": None, "fallback": None, "timeout_seconds": 5, "max_retries": 3},
    "database": {"enabled": True, "table_name": "signals_history"},
    "advanced": {"track_only_new_tokens": True, "max_concurrent_monitors": 50}
}

if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            CONFIG = json.load(f)
        except Exception:
            CONFIG = DEFAULT_CONFIG
else:
    CONFIG = DEFAULT_CONFIG

# -------------------- ENV and Combined CONFIG --------------------
# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ENV_TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
try:
    TELEGRAM_CHAT_ID = int(ENV_TELEGRAM_CHAT_ID) if ENV_TELEGRAM_CHAT_ID else CONFIG.get("telegram", {}).get("chat_id")
except Exception:
    TELEGRAM_CHAT_ID = CONFIG.get("telegram", {}).get("chat_id")

# RPC endpoints
RPC_PRIMARY = os.getenv("RPC_PRIMARY") or CONFIG.get("rpc", {}).get("primary")
RPC_FALLBACK = os.getenv("RPC_FALLBACK") or CONFIG.get("rpc", {}).get("fallback")

# PumpPortal
PUMPPORTAL_WS = os.getenv("PUMPPORTAL_WS") or CONFIG.get("advanced", {}).get("pumpportal_ws") or "wss://pumpportal.fun/api/data"
PUMPPORTAL_API_KEY = os.getenv("PUMPPORTAL_API_KEY") or ""

# DB
DATABASE_URL = os.getenv("DATABASE_URL")

# Behavioral environment overrides
ENABLE_DB = str(os.getenv("ENABLE_DB") or str(CONFIG.get("database", {}).get("enabled", True))).lower() in ("1", "true", "yes")
PRICE_POLL_INTERVAL_SEC = float(os.getenv("PRICE_POLL_INTERVAL_SEC") or 8)
DUMP_THRESHOLD_PERCENT = float(os.getenv("DUMP_THRESHOLD_PERCENT") or -50)
MAX_MONITOR_TIME_MIN = float(os.getenv("MAX_MONITOR_TIME_MIN") or 30)
LOG_LEVEL = os.getenv("LOG_LEVEL") or "INFO"
MODE = os.getenv("MODE") or "PROD"

# ROI targets: from config.json (list)
ROI_TARGETS: List[Dict[str, Any]] = CONFIG.get("filters", {}).get("roi_targets") or [{"percent": 100, "minutes": 15}]

# Limits
MAX_CONCURRENT_MONITORS = int(CONFIG.get("advanced", {}).get("max_concurrent_monitors", 50))

# Derived
ENABLE_TELEGRAM = True if TELEGRAM_BOT_TOKEN else False

# -------------------- LOGGING --------------------
numeric_level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
logging.basicConfig(level=numeric_level,
                    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("pumpfun_bot")

# -------------------- GLOBALS --------------------
monitored_tokens: Dict[str, Dict[str, Any]] = {}
db_pool: Optional[asyncpg.pool.Pool] = None
ws_task: Optional[asyncio.Task] = None
ws_stop_event = asyncio.Event()
application: Optional[Application] = None
semaphore = asyncio.Semaphore(MAX_CONCURRENT_MONITORS)

# -------------------- UTIL --------------------
def now_utc():
    return datetime.now(timezone.utc)

def choose_rpc_url() -> Optional[str]:
    return RPC_PRIMARY or RPC_FALLBACK or "https://api.mainnet-beta.solana.com"

# -------------------- DATABASE (asyncpg) --------------------
CREATE_TABLE_QUERIES = [
"""
CREATE TABLE IF NOT EXISTS token_monitoring (
  token_address TEXT PRIMARY KEY,
  symbol TEXT,
  name TEXT,
  initial_price NUMERIC,
  initial_market_cap NUMERIC,
  max_price NUMERIC,
  start_time TIMESTAMPTZ,
  last_checked TIMESTAMPTZ,
  status TEXT,
  metadata JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS token_alerts (
  id SERIAL PRIMARY KEY,
  token_address TEXT NOT NULL,
  gain_percent NUMERIC,
  time_elapsed_min NUMERIC,
  price_at_alert NUMERIC,
  created_at TIMESTAMPTZ DEFAULT now(),
  extra JSONB
);
""",
"""
CREATE TABLE IF NOT EXISTS token_price_history (
  id SERIAL PRIMARY KEY,
  token_address TEXT NOT NULL,
  ts TIMESTAMPTZ DEFAULT now(),
  price NUMERIC,
  market_cap NUMERIC,
  volume NUMERIC
);
"""
]

async def init_db():
    global db_pool
    if not ENABLE_DB:
        logger.info("DB disabled in config.")
        return
    if not DATABASE_URL:
        logger.warning("ENABLE_DB is true but DATABASE_URL not provided.")
        return
    try:
        db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=6)
        async with db_pool.acquire() as conn:
            for q in CREATE_TABLE_QUERIES:
                await conn.execute(q)
        logger.info("Postgres pool initialized and tables ensured.")
    except Exception as e:
        logger.error("Error initializing DB: %s", e)
        db_pool = None

async def db_insert_monitored(mint: str, info: Dict[str, Any]):
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO token_monitoring (token_address, symbol, name, initial_price, initial_market_cap, max_price, start_time, last_checked, status, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$7,$8,$9)
                ON CONFLICT (token_address) DO UPDATE SET last_checked = $7, status = $8, max_price = EXCLUDED.max_price, metadata = EXCLUDED.metadata
            """, mint, info.get("symbol"), info.get("name"), info.get("initial_price"),
                 info.get("initial_market_cap"), info.get("max_price"), now_utc(), info.get("status","monitoring"), json.dumps(info.get("metadata", {})))
    except Exception as e:
        logger.debug("db_insert_monitored error: %s", e)

async def db_remove_monitored(mint: str):
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM token_monitoring WHERE token_address = $1", mint)
    except Exception as e:
        logger.debug("db_remove_monitored error: %s", e)

async def db_record_alert(mint: str, gain_percent: float, elapsed_min: float, price_at_alert: float, extra: dict = None):
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO token_alerts (token_address, gain_percent, time_elapsed_min, price_at_alert, extra)
                VALUES ($1,$2,$3,$4,$5)
            """, mint, gain_percent, elapsed_min, price_at_alert, json.dumps(extra or {}))
    except Exception as e:
        logger.debug("db_record_alert error: %s", e)

async def db_record_price(mint: str, price: float, market_cap: float, volume: float):
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO token_price_history (token_address, price, market_cap, volume)
                VALUES ($1,$2,$3,$4)
            """, mint, price, market_cap, volume)
    except Exception as e:
        logger.debug("db_record_price error: %s", e)

# -------------------- PUMPPORTAL WS LISTENER --------------------
async def pumpportal_listener():
    uri = PUMPPORTAL_WS
    if PUMPPORTAL_API_KEY:
        uri = uri + ("&" if "?" in uri else "?") + f"api-key={PUMPPORTAL_API_KEY}"
    logger.info("Connecting to PumpPortal WS: %s", uri)
    backoff = 1
    while not ws_stop_event.is_set():
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20, max_size=None) as ws:
                logger.info("Connected to PumpPortal WS")
                # subscribe to new token births
                await ws.send(json.dumps({"method":"subscribeNewToken"}))
                logger.info("Sent subscribeNewToken")
                async for raw in ws:
                    if ws_stop_event.is_set():
                        break
                    try:
                        payload = json.loads(raw)
                        # dispatch non-blocking
                        asyncio.create_task(handle_new_token(payload))
                    except Exception as e:
                        logger.debug("WS parse error: %s", e)
                logger.warning("Websocket loop ended; reconnecting")
        except Exception as e:
            logger.error("PumpPortal WS error: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

# -------------------- NEW TOKEN HANDLER --------------------
async def handle_new_token(payload: Dict[str, Any]):
    """
    Extract mint/symbol/name. Start monitor task if not already monitored.
    """
    try:
        # flexible extraction
        mint = payload.get("mint") or payload.get("token") or (payload.get("data") or {}).get("mint")
        if not mint:
            logger.debug("Payload with no mint; skipping.")
            return
        symbol = payload.get("symbol") or (payload.get("data") or {}).get("symbol") or f"TKN_{mint[:6]}"
        name = payload.get("name") or (payload.get("data") or {}).get("name") or symbol

        # Optionally extract price/marketcap if pumpportal provides it
        initial_price = 0.0
        initial_marketcap = 0.0
        pairs = payload.get("pairs") or (payload.get("data") or {}).get("pairs")
        if isinstance(pairs, list) and pairs:
            p = pairs[0]
            try:
                initial_price = float(p.get("priceUsd") or p.get("price") or 0)
            except Exception:
                initial_price = 0.0
            try:
                initial_marketcap = float(p.get("marketCap") or 0)
            except Exception:
                initial_marketcap = 0.0

        # Avoid duplicates
        if mint in monitored_tokens:
            logger.debug("Mint %s already monitored.", mint)
            return

        # Basic filter if you want (min_marketcap / min_liquidity) - optional
        min_mc = CONFIG.get("filters", {}).get("min_marketcap", 0)
        if initial_marketcap and initial_marketcap < min_mc:
            logger.debug("Mint %s marketcap < min_marketcap (%s) skip", mint, min_mc)
            # still could monitor; here we skip only if configured
            # return

        # Create in-memory entry
        entry = {
            "symbol": symbol,
            "name": name,
            "initial_price": initial_price,
            "initial_market_cap": initial_marketcap,
            "start_time": now_utc(),
            "max_price": initial_price or 0.0,
            "monitoring_task": None,
            "status": "monitoring",
            "raw_payload": payload
        }
        monitored_tokens[mint] = entry
        await db_insert_monitored(mint, {
            "symbol": symbol,
            "name": name,
            "initial_price": initial_price,
            "initial_market_cap": initial_marketcap,
            "max_price": entry["max_price"],
            "status": "monitoring",
            "metadata": {"payload": payload}
        })
        # respect concurrency limits
        await semaphore.acquire()
        try:
            task = asyncio.create_task(monitor_single_token(mint))
            monitored_tokens[mint]["monitoring_task"] = task
            logger.info("Monitoring started for %s (%s)", mint, symbol)
        finally:
            semaphore.release()
    except Exception as e:
        logger.error("Error handling new token: %s", e)

# -------------------- MONITOR SINGLE TOKEN --------------------
async def monitor_single_token(mint: str):
    """
    Monitors a single mint:
    - polls price via get_price()
    - calculates gain% from initial_price
    - compares against ROI_TARGETS (list of {percent, minutes})
    - deletes on dump (DUMP_THRESHOLD_PERCENT) or timeout (MAX_MONITOR_TIME_MIN)
    - sends 1 alert and records it
    """
    try:
        token = monitored_tokens.get(mint)
        if not token:
            return
        start = token["start_time"]
        initial_price = token.get("initial_price", 0.0)
        max_price = token.get("max_price", initial_price or 0.0)
        poll = max(1, PRICE_POLL_INTERVAL_SEC)

        # If no initial price, attempt a quick fetch
        if not initial_price:
            fetched = await get_price_data(mint)
            if fetched and fetched.get("price"):
                initial_price = float(fetched["price"])
                token["initial_price"] = initial_price
                token["max_price"] = initial_price
                await db_insert_monitored(mint, {"symbol": token['symbol'], "name": token['name'],
                                                 "initial_price": initial_price, "initial_market_cap": fetched.get("market_cap", 0),
                                                 "max_price": initial_price, "status": "monitoring", "metadata": {}})

        while True:
            if mint not in monitored_tokens:
                return
            elapsed_min = (now_utc() - start).total_seconds() / 60.0

            # Timeout rule
            if elapsed_min >= MAX_MONITOR_TIME_MIN:
                logger.info("Mint %s reached timeout %.2f min -> removing", mint, elapsed_min)
                await cleanup_token(mint, "timeout")
                return

            data = await get_price_data(mint)
            if not data:
                await asyncio.sleep(poll)
                continue

            price = float(data.get("price", 0) or 0)
            market_cap = float(data.get("market_cap", 0) or 0)
            volume = float(data.get("volume_24h", 0) or 0)

            # update max
            if price > max_price:
                max_price = price
                monitored_tokens[mint]["max_price"] = max_price

            # persist history
            await db_record_price(mint, price, market_cap, volume)

            # calculate gain and drop
            gain_pct = ((price - initial_price) / initial_price * 100.0) if initial_price and initial_price > 0 else 0.0
            loss_from_max = ((price - max_price) / max_price * 100.0) if max_price and max_price > 0 else 0.0

            logger.debug("%s price=%.8f gain=%.2f%% elapsed=%.2fmin loss_from_max=%.2f%%",
                         mint, price, gain_pct, elapsed_min, loss_from_max)

            # Dump detection
            if loss_from_max <= DUMP_THRESHOLD_PERCENT:
                logger.info("%s dumped %.2f%% from max -> cleaning", mint, loss_from_max)
                await cleanup_token(mint, "dumped")
                return

            # Check ROI targets array - if any matches, trigger alert
            for target in ROI_TARGETS:
                try:
                    target_pct = float(target.get("percent", 100))
                    target_min = float(target.get("minutes", 15))
                except Exception:
                    continue
                if gain_pct >= target_pct and elapsed_min <= target_min:
                    logger.info("%s matched ROI target %s%% in %s min (gain %.2f%%)", mint, target_pct, target_min, gain_pct)
                    extra = {"initial_price": initial_price, "price_at_alert": price, "market_cap": market_cap, "volume": volume, "matched_target": target}
                    await send_alert(mint, token, gain_pct, elapsed_min, extra)
                    await db_record_alert(mint, gain_pct, elapsed_min, price, extra)
                    monitored_tokens[mint]["status"] = "alert_sent"
                    await cleanup_token(mint, "alert_sent")
                    return

            await asyncio.sleep(poll)
    except asyncio.CancelledError:
        logger.info("Monitor cancelled for %s", mint)
    except Exception as e:
        logger.error("Error in monitor_single_token(%s): %s", mint, e)
        await cleanup_token(mint, "error")

# -------------------- CLEANUP --------------------
async def cleanup_token(mint: str, reason: str):
    try:
        entry = monitored_tokens.get(mint)
        if not entry:
            return
        task = entry.get("monitoring_task")
        if task:
            try:
                task.cancel()
            except Exception:
                pass
        monitored_tokens.pop(mint, None)
        await db_remove_monitored(mint)
        logger.info("Cleaned %s -> %s", mint, reason)
    except Exception as e:
        logger.debug("cleanup_token error: %s", e)

# -------------------- GET PRICE DATA --------------------
async def get_price_data(mint: str) -> Optional[Dict[str, Any]]:
    """
    Strategy:
    1) Try DexScreener public endpoint for token price and market cap
    2) Optionally: try RPC (placeholder) - specialized parsing required for bonding curve
    """
    # DexScreener
    try:
        ds_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(ds_url)
            if r.status_code == 200:
                data = r.json()
                if data.get("pairs") and len(data["pairs"]) > 0:
                    pair = data["pairs"][0]
                    price = float(pair.get("priceUsd") or pair.get("price") or 0)
                    market_cap = float(pair.get("marketCap") or 0)
                    try:
                        vol = float(pair.get("volume", {}).get("h24", 0) or 0)
                    except Exception:
                        vol = 0
                    return {"price": price, "market_cap": market_cap, "volume_24h": vol}
    except Exception as e:
        logger.debug("DexScreener error: %s", e)

    # RPC fallback (placeholder) - can be implemented to parse bonding curve account
    rpc = choose_rpc_url()
    if not rpc:
        return None
    try:
        payload = {"jsonrpc":"2.0","id":1,"method":"getAccountInfo","params":[mint, {"encoding":"base64"}]}
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(rpc, json=payload)
            if resp.status_code == 200:
                # Real on-chain price derivation for bonding curve requires decoding the program state.
                # Placeholder: return None to let monitor wait and re-try or rely on DexScreener.
                return None
    except Exception as e:
        logger.debug("RPC price fallback error: %s", e)
    return None

# -------------------- TELEGRAM ALERTS --------------------
async def send_alert(mint: str, token_info: Dict[str, Any], gain_percent: float, elapsed_min: float, extra: Dict[str, Any]):
    if not ENABLE_TELEGRAM:
        logger.info("Telegram disabled; skipping alert for %s", mint)
        return
    if not application:
        logger.warning("Telegram application not initialised.")
        return

    chat_id = TELEGRAM_CHAT_ID or CONFIG.get("telegram", {}).get("chat_id")
    if not chat_id:
        logger.warning("No TELEGRAM_CHAT_ID configured; cannot send alert.")
        return

    name = token_info.get("name", mint)
    symbol = token_info.get("symbol", "")

    market_cap = extra.get("market_cap") or extra.get("market_cap", 0) or 0
    price = extra.get("price_at_alert") or extra.get("initial_price") or 0.0

    message = (
        f"🚀 *ALERTA DE PUMP DETECTADA* 🚀\n\n"
        f"*Token:* {name} ({symbol})\n"
        f"*Ganancia:* +{gain_percent:.1f}%\n"
        f"*Tiempo desde mint:* {elapsed_min:.1f} min\n"
        f"*Market Cap:* ${market_cap:,.0f}\n"
        f"*Precio Actual:* {price:.8f} SOL\n\n"
        f"*Mint Address:* `{mint}`\n\n"
        f"🔗 Enlaces rápidos:"
    )

    keyboard = [
        [InlineKeyboardButton("🎯 Pump.fun", url=f"https://pump.fun/{mint}")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
        [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")]
    ]

    try:
        await application.bot.send_message(chat_id=chat_id,
                                           text=message,
                                           parse_mode=CONFIG.get("telegram", {}).get("parse_mode", "Markdown"),
                                           reply_markup=InlineKeyboardMarkup(keyboard),
                                           disable_web_page_preview=True)
        logger.info("Alert sent for %s", mint)
    except Exception as e:
        logger.error("Failed to send telegram alert for %s: %s", mint, e)

# -------------------- TELEGRAM COMMANDS --------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TELEGRAM_CHAT_ID
    TELEGRAM_CHAT_ID = update.message.chat_id
    await update.message.reply_text(
        "🚀 *Bot Pump.fun - Alerta de Pumps*\n\n"
        "Usa /cazar para iniciar monitoreo. /parar para detener.\n"
        "*/cazar* - Iniciar monitoreo\n"
        "*/parar* - Detener monitoreo\n"
        "*/status* - Estado actual\n"
        "*/tokens* - Tokens siendo monitoreados",
        parse_mode=CONFIG.get("telegram", {}).get("parse_mode", "Markdown")
    )

async def cazar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ws_task
    if ws_task and not ws_task.done():
        await update.message.reply_text("🤔 Ya está monitoreando Pump.fun")
        return
    await update.message.reply_text("🏹 *Iniciando Monitor Pump.fun...*", parse_mode=CONFIG.get("telegram", {}).get("parse_mode", "Markdown"))
    # init DB
    await init_db()
    ws_stop_event.clear()
    ws_task = asyncio.create_task(pumpportal_listener())
    await update.message.reply_text("✅ Monitor iniciado", parse_mode=CONFIG.get("telegram", {}).get("parse_mode", "Markdown"))

async def parar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ws_task
    if not ws_task or ws_task.done():
        await update.message.reply_text("🤔 No está monitoreando")
        return
    ws_stop_event.set()
    # cancel token monitors
    for mint in list(monitored_tokens.keys()):
        await cleanup_token(mint, "monitor_stopped")
    await update.message.reply_text("🛑 Monitor detenido")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 Monitor detenido"
    if ws_task and not ws_task.done():
        status_msg = (
            f"✅ *Monitor Pump.fun Activo*\n\n"
            f"🔍 *Tokens Monitoreados:* {len(monitored_tokens)}\n"
            f"🎯 *Targets:* {ROI_TARGETS}\n"
            f"🛑 *Dump threshold:* {DUMP_THRESHOLD_PERCENT}%\n"
            f"⏰ *Duración Máxima:* {MAX_MONITOR_TIME_MIN}min\n\n"
            f"📡 *Conectado a PumpPortal WebSocket*"
        )
    await update.message.reply_text(status_msg, parse_mode=CONFIG.get("telegram", {}).get("parse_mode", "Markdown"))

async def tokens_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not monitored_tokens:
        await update.message.reply_text("🔍 No hay tokens siendo monitoreados")
        return
    message = f"🔍 *Tokens en Monitoreo ({len(monitored_tokens)}):*\n\n"
    for i, (mint, data) in enumerate(monitored_tokens.items(), 1):
        elapsed = (now_utc() - data['start_time']).total_seconds() / 60.0
        message += f"{i}. `{mint}`\n   📛 {data.get('symbol')} | ⏰ {elapsed:.1f}min\n"
    await update.message.reply_text(message, parse_mode=CONFIG.get("telegram", {}).get("parse_mode", "Markdown"))

# -------------------- SHUTDOWN --------------------
def _shutdown():
    logger.info("Shutdown initiated. Cancelling tasks...")
    ws_stop_event.set()
    for mint, d in list(monitored_tokens.items()):
        task = d.get("monitoring_task")
        if task:
            try:
                task.cancel()
            except Exception:
                pass

async def _on_shutdown(app: Application):
    logger.info("Telegram app shutting down.")
    _shutdown()
    global db_pool
    if db_pool:
        await db_pool.close()
        logger.info("DB pool closed.")

# -------------------- MAIN --------------------
def main():
    global application
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not configured. Exiting.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    # register handlers
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("cazar", cazar_cmd))
    application.add_handler(CommandHandler("parar", parar_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("tokens", tokens_cmd))

    application.post_init(lambda app: logger.info("Telegram app initialized. Waiting for commands."))
    application.post_shutdown(_on_shutdown)

    logger.info("--- Bot Pump.fun - Ready. Use /cazar to start monitoring ---")
    application.run_polling()

if __name__ == "__main__":
    main()
