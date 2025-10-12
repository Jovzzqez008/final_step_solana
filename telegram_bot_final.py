# telegram_bot_final.py
"""
Telegram Pump.fun detector (Helius + Moralis) -> Alerts via Telegram Webhook (FastAPI)
Solo Pump.fun (sin Jupiter). Basado en tu funcional_2.py y adaptado a Helius+Moralis.

Dependencias:
aiohttp
websockets
asyncpg
python-telegram-bot==20.*   # adaptado a la versión moderna
fastapi
uvicorn

Variables de entorno (configura en Railway):
- HELIUS_WSS_URL         e.g. wss://mainnet.helius-rpc.com/?api-key=TU_HELIUS_KEY
- MORALIS_API_KEY        tu_moralis_api_key
- MORALIS_BASE           opcional, default https://solana-gateway.moralis.io
- TELEGRAM_BOT_TOKEN     tu_bot_token
- TELEGRAM_CHAT_ID       chat_id por defecto (numeric)
- TELEGRAM_WEBHOOK_URL   URL pública de la app (ej: https://mi-app.up.railway.app)
- DATABASE_URL           postgres://user:pass@host:port/dbname
- OWNER_ID               tu id (numeric)
- UMBRAL_MCAP            ejemplo: 55000
- GRADUATION_MC_TARGET   ejemplo: 69000
- TOKEN_POLL_INTERVAL    (segundos) default 7
- PUMP_FUN_PROGRAM_ID    ID del programa Pump.fun (default en código)
"""

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import aiohttp
import websockets
import asyncpg
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------- config ----------------
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
MORALIS_API_KEY = os.getenv("MORALIS_API_KEY")
MORALIS_BASE = os.getenv("MORALIS_BASE", "https://solana-gateway.moralis.io")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
UMBRAL_MCAP = float(os.getenv("UMBRAL_MCAP", "55000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "69000"))
TOKEN_POLL_INTERVAL = int(os.getenv("TOKEN_POLL_INTERVAL", "7"))
PUMP_FUN_PROGRAM_ID = os.getenv("PUMP_FUN_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# Heurística para detectar mints en logs: base58-ish
MINT_PATTERN = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")

# webhook path (includes token for minimal security)
WEBHOOK_PATH = f"/webhook/{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else "/webhook/NO_TOKEN"

# ---------------- helpers ----------------
def now_ts():
    return datetime.now(timezone.utc)

def generate_links(mint: str) -> Dict[str, str]:
    return {
        "pumpfun": f"https://pump.fun/coin/{mint}",
        "dexscreener": f"https://dexscreener.com/solana/{mint}",
        "rugcheck": f"https://rugcheck.xyz/tokens/{mint}"
    }

# ---------------- Database ----------------
class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not self.dsn:
            raise RuntimeError("DATABASE_URL no definido")
        self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=6)
        logging.info("Conectado a Postgres")
        await self._ensure_tables()

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def _ensure_tables(self):
        q1 = """
        CREATE TABLE IF NOT EXISTS pump_notifications (
            id SERIAL PRIMARY KEY,
            mint TEXT UNIQUE NOT NULL,
            alert_type TEXT,
            symbol TEXT,
            market_cap DOUBLE PRECISION,
            detected_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
            graduated BOOLEAN DEFAULT FALSE,
            source TEXT DEFAULT 'pumpfun'
        );
        """
        q2 = """
        CREATE TABLE IF NOT EXISTS seen_tokens (
            mint TEXT PRIMARY KEY,
            last_seen TIMESTAMP WITH TIME ZONE DEFAULT now(),
            source TEXT DEFAULT 'pumpfun'
        );
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q1)
            await conn.execute(q2)
        logging.info("DB: tablas aseguradas (pump_notifications, seen_tokens)")

    async def was_notified(self, mint: str, alert_type: str = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint=$1 AND alert_type=$2 LIMIT 1", mint, alert_type)
            else:
                row = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint=$1 LIMIT 1", mint)
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str, symbol: Optional[str] = None, market_cap: Optional[float] = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_notifications (mint, alert_type, symbol, market_cap, detected_at)
                VALUES ($1,$2,$3,$4,NOW())
                ON CONFLICT (mint) DO UPDATE
                SET alert_type = EXCLUDED.alert_type,
                    symbol = COALESCE(EXCLUDED.symbol, pump_notifications.symbol),
                    market_cap = COALESCE(EXCLUDED.market_cap, pump_notifications.market_cap),
                    detected_at = NOW();
            """, mint, alert_type, symbol, market_cap)

    async def mark_graduated(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE pump_notifications SET graduated = TRUE WHERE mint = $1", mint)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT last_seen FROM seen_tokens WHERE mint=$1", mint)
            if not row:
                return False
            last_seen = row["last_seen"]
            return (now_ts() - last_seen).total_seconds() < minutes * 60

    async def mark_seen(self, mint: str, source: str = "pumpfun"):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint, last_seen, source) VALUES ($1, NOW(), $2)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW(), source = $2
            """, mint, source)

# ---------------- Moralis client ----------------
class MoralisClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base = MORALIS_BASE.rstrip("/")
        self.api_key = MORALIS_API_KEY

    async def _get(self, path: str, params: dict = None, timeout: int = 8):
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        url = f"{self.base}{path}"
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logging.debug(f"Moralis {resp.status} {url}")
        except asyncio.TimeoutError:
            logging.warning(f"Moralis timeout: {url}")
        except Exception as e:
            logging.error(f"Moralis error: {e}")
        return None

    async def get_token_bonding(self, mint: str):
        # Pump.fun specific bonding endpoint (as in your earlier design)
        path = f"/token/mainnet/exchange/pumpfun/{mint}/bonding"
        return await self._get(path)

    async def get_token_metadata(self, mint: str):
        path = f"/token/mainnet/{mint}/metadata"
        return await self._get(path)

# ---------------- Telegram notifier ----------------
class TelegramNotifier:
    def __init__(self, tg_app: Application):
        self.app = tg_app
        self.silent_mode = False

    async def send_pump_alert(self, symbol: str, mint: str, data: Dict[str, Any], alert_type: str, source: str = "pumpfun"):
        if self.silent_mode:
            logging.info("Silent mode ON - skipping alert")
            return

        # Compose message depending on alert_type
        if alert_type == "pump_early":
            title = "🚨 PUMP.FUN EARLY DETECTION"
            body = (
                f"<b>{symbol}</b>\n"
                f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
                f"• Price (approx): {data.get('price', 0)}\n"
                f"• Fuente: {source}\n\n"
                "⚡ <b>Oportunidad PRE-GRADUACIÓN</b>\n"
            )
        elif alert_type == "pre_graduation":
            title = "🎯 PRE-GRADUACIÓN INMINENTE"
            body = (
                f"<b>{symbol}</b>\n"
                f"• Market Cap: ${data.get('market_cap', 0):,.0f}\n"
                f"• Progreso: {data.get('graduation_percent', 0):.1f}%\n"
                f"• Fuente: {source}\n\n"
                "📈 <b>Cerca de graduarse</b>\n"
            )
        else:
            title = "ℹ️ PUMP.FUN ALERT"
            body = f"<b>{symbol}</b>\n• Fuente: {source}\n"

        text = f"{title}\n\n{body}\n<code>{mint}</code>"

        links = generate_links(mint)
        keyboard = [
            [InlineKeyboardButton("Pump.fun", url=links["pumpfun"]),
             InlineKeyboardButton("DexScreener", url=links["dexscreener"])],
            [InlineKeyboardButton("RugCheck", url=links["rugcheck"])]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=False
            )
            logging.info(f"Alerta enviada: {mint} ({alert_type})")
        except Exception as e:
            logging.error(f"Error enviando alerta: {e}")

# ---------------- Helius WebSocket listener (Pump.fun program) ----------------
class HeliusListener:
    def __init__(self, on_mint_callback):
        self.wss_url = HELIUS_WSS_URL
        self.program_id = PUMP_FUN_PROGRAM_ID
        self.on_mint = on_mint_callback
        self._running = False
        self._ws = None

    async def _subscribe(self, ws):
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [self.program_id]},
                {"commitment": "confirmed"}
            ]
        }
        await ws.send(json.dumps(msg))
        logging.info("Subscribed to Helius logs for Pump.fun program")

    async def _handle_payload(self, payload: dict):
        try:
            result = payload.get("params", {}).get("result", {})
            logs = result.get("logs", []) or []
            # Buscar mints en los logs con heurística
            for line in logs:
                m = MINT_PATTERN.search(line)
                if m:
                    mint = m.group(0)
                    # callback can be long-running; fire-and-forget but await to keep order
                    await self.on_mint(mint, source="helius")
        except Exception as e:
            logging.debug(f"Helius parse error: {e}")

    async def run(self):
        if not self.wss_url:
            logging.warning("HELIUS_WSS_URL no configurado; HeliusListener no iniciará.")
            return
        self._running = True
        backoff = 1
        while self._running:
            try:
                async with websockets.connect(self.wss_url, ping_interval=30, ping_timeout=10) as ws:
                    self._ws = ws
                    logging.info("Conectado a Helius WebSocket")
                    await self._subscribe(ws)
                    async for raw in ws:
                        try:
                            payload = json.loads(raw)
                        except Exception:
                            continue
                        await self._handle_payload(payload)
                    backoff = 1
            except Exception as e:
                logging.warning(f"Helius connection lost: {e} - reconectando en {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                self._ws = None

    async def stop(self):
        self._running = False
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass

# ---------------- Pump.fun Monitor ----------------
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier, session: aiohttp.ClientSession):
        self.db = db
        self.notifier = notifier
        self.http = session
        self.active_tokens: Dict[str, Dict[str, Any]] = {}
        self._running = False

    async def on_new_mint(self, mint: str, source: str = "helius"):
        # dedupe quick
        if await self.db.seen_recently(mint, minutes=10):
            return
        await self.db.mark_seen(mint, source)
        moralis = MoralisClient(self.http)
        bonding = await moralis.get_token_bonding(mint)
        meta = await moralis.get_token_metadata(mint)
        symbol = None
        market_cap = None
        price = None
        try:
            if bonding and isinstance(bonding, dict):
                market_cap = float(bonding.get("market_cap") or bonding.get("mcap") or 0)
                symbol = bonding.get("symbol") or (meta.get("symbol") if meta else None)
                price = float(bonding.get("price") or 0)
            elif meta and isinstance(meta, dict):
                symbol = meta.get("symbol")
                market_cap = float(meta.get("market_cap") or 0)
                price = float(meta.get("price") or 0)
        except Exception:
            logging.debug("Error parseando Moralis response")

        # Filter obviously irrelevant tokens
        if market_cap is not None and (market_cap < 500 or market_cap > 5_000_000):
            logging.info(f"Token {mint} filtrado por market_cap {market_cap}")
            return

        # Save and start monitoring
        await self.db.mark_seen(mint, source)
        self.active_tokens[mint] = {
            "symbol": symbol or "UNKNOWN",
            "market_cap": market_cap or 0,
            "price": price or 0,
            "last_checked": now_ts()
        }
        logging.info(f"Nuevo mint detectado {symbol or 'UNKNOWN'} {mint} MC={market_cap}")

        # Early alert strategy
        if market_cap and (market_cap >= 1000 and market_cap < (UMBRAL_MCAP * 0.5)):
            if not await self.db.was_notified(mint, "pump_early"):
                await self.notifier.send_pump_alert(symbol or "UNKNOWN", mint, {"market_cap": market_cap, "price": price}, "pump_early", source)
                await self.db.mark_notified(mint, "pump_early", symbol, market_cap)

    async def poll_active_tokens(self):
        self._running = True
        moralis = MoralisClient(self.http)
        while self._running:
            try:
                if not self.active_tokens:
                    await asyncio.sleep(TOKEN_POLL_INTERVAL)
                    continue
                for mint in list(self.active_tokens.keys()):
                    meta = self.active_tokens.get(mint, {})
                    data = await moralis.get_token_bonding(mint)
                    mc = None
                    price = None
                    if data and isinstance(data, dict):
                        try:
                            mc = float(data.get("market_cap") or data.get("mcap") or meta.get("market_cap") or 0)
                            price = float(data.get("price") or meta.get("price") or 0)
                        except Exception:
                            mc = meta.get("market_cap") or 0
                    else:
                        md = await moralis.get_token_metadata(mint)
                        if md and isinstance(md, dict):
                            try:
                                mc = float(md.get("market_cap") or 0)
                                price = float(md.get("price") or 0)
                            except Exception:
                                mc = meta.get("market_cap") or 0

                    meta["market_cap"] = mc or 0
                    meta["price"] = price or 0
                    meta["last_checked"] = now_ts()
                    await self.db.mark_seen(mint, "moralis_poll")

                    # Pre-graduation alert
                    if meta["market_cap"] >= UMBRAL_MCAP and not await self.db.was_notified(mint, "pre_graduation"):
                        graduation_percent = (meta["market_cap"] / GRADUATION_MC_TARGET) * 100
                        await self.notifier.send_pump_alert(meta.get("symbol","UNKNOWN"), mint, {"market_cap": meta["market_cap"], "graduation_percent": graduation_percent, "price": meta.get("price")}, "pre_graduation", "moralis_poll")
                        await self.db.mark_notified(mint, "pre_graduation", meta.get("symbol"), meta["market_cap"])

                    # Graduation reached
                    if meta["market_cap"] >= GRADUATION_MC_TARGET:
                        await self.db.mark_graduated(mint)
                        self.active_tokens.pop(mint, None)
                        logging.info(f"Token graduado {mint} -> removido de monitor")

            except Exception as e:
                logging.error(f"Error en poll_active_tokens: {e}")
            await asyncio.sleep(TOKEN_POLL_INTERVAL)

    async def stop(self):
        self._running = False

# ---------------- FastAPI + Telegram webhook ----------------
app = FastAPI(title="Pump.fun Detector (Helius+Moralis)")

# Globals
_db: Optional[Database] = None
_http_session: Optional[aiohttp.ClientSession] = None
_tg_app: Optional[Application] = None
_notifier: Optional[TelegramNotifier] = None
_monitor: Optional[PumpFunMonitor] = None
_helius: Optional[HeliusListener] = None
_poll_task: Optional[asyncio.Task] = None
_helius_task: Optional[asyncio.Task] = None

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    update = Update.de_json(data, _tg_app.bot)
    # Only process owner messages (safety)
    if update.effective_user and update.effective_user.id == OWNER_ID:
        await _tg_app.process_update(update)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok", "time": now_ts().isoformat()}

# ---------------- Telegram commands (use Application as in funcional_2.py) ----------------
async def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    await update.message.reply_text("Bot listo. Usa /iniciar para arrancar monitores.", parse_mode="HTML")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    # start monitor tasks
    if _monitor:
        await _monitor.stop()  # ensure clean
        asyncio.create_task(_monitor.poll_active_tokens())
    if _helius and (_helius_task is None or _helius_task.done()):
        logging.info("Iniciando Helius Listener")
        global _helius_task
        _helius_task = asyncio.create_task(_helius.run())
    await update.message.reply_text("✅ Monitores iniciados", parse_mode="HTML")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    if _monitor:
        await _monitor.stop()
    if _helius:
        await _helius.stop()
    await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    pump_status = "🟢" if _monitor and _monitor._running else "🔴"
    helius_status = "🟢" if _helius_task and not _helius_task.done() else "🔴"
    await update.message.reply_text(f"Pump monitor: {pump_status}\nHelius listener: {helius_status}", parse_mode="HTML")

async def silent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    args = context.args
    if not args or args[0] not in ("on","off"):
        await update.message.reply_text("Uso: /silent on|off")
        return
    if _notifier:
        _notifier.silent_mode = (args[0]=="on")
    await update.message.reply_text(f"Silent mode set to {args[0]}", parse_mode="HTML")

# ---------------- Startup / Shutdown ----------------
@app.on_event("startup")
async def startup():
    global _db, _http_session, _tg_app, _notifier, _monitor, _helius, _poll_task, _helius_task
    # Validate required envs
    missing = [k for k in ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","DATABASE_URL","OWNER_ID") if not os.getenv(k)]
    if missing:
        logging.error(f"Faltan variables: {missing}")
        raise RuntimeError(f"Faltan variables: {missing}")

    # DB
    _db = Database(DATABASE_URL)
    await _db.connect()

    # HTTP session
    _http_session = aiohttp.ClientSession()

    # Telegram app (used for sending and command handling)
    _tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    # register handlers
    _tg_app.add_handler(CommandHandler("start", start_command))
    _tg_app.add_handler(CommandHandler("iniciar", iniciar_command))
    _tg_app.add_handler(CommandHandler("detener", detener_command))
    _tg_app.add_handler(CommandHandler("status", status_command))
    _tg_app.add_handler(CommandHandler("silent", silent_command))
    await _tg_app.initialize()
    await _tg_app.start()  # start background tasks required by library

    # Notifier and monitor
    _notifier = TelegramNotifier(_tg_app)
    _monitor = PumpFunMonitor(_db, _notifier, _http_session)

    # Helius listener
    _helius = HeliusListener(_monitor.on_new_mint)
    _helius_task = asyncio.create_task(_helius.run())

    # Set Telegram webhook if TELEGRAM_WEBHOOK_URL provided
    if TELEGRAM_WEBHOOK_URL:
        webhook_url = TELEGRAM_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        try:
            async with _http_session.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook", data={"url": webhook_url}, timeout=10) as resp:
                res = await resp.json()
                logging.info(f"setWebhook response: {res}")
        except Exception as e:
            logging.warning(f"Failed to set webhook: {e}")
    else:
        logging.warning("TELEGRAM_WEBHOOK_URL not provided; webhook won't be set (telegram updates might not be received).")

    logging.info("Startup complete. Helius listener & monitor running.")

@app.on_event("shutdown")
async def shutdown():
    global _db, _http_session, _tg_app, _notifier, _monitor, _helius, _helius_task
    logging.info("Shutdown started...")
    try:
        if _helius:
            await _helius.stop()
        if _monitor:
            await _monitor.stop()
        if _helius_task:
            _helius_task.cancel()
        if _tg_app:
            await _tg_app.shutdown()
            await _tg_app.stop()
            await _tg_app.shutdown()
        if _db:
            await _db.close()
        if _http_session:
            await _http_session.close()
    except Exception as e:
        logging.error(f"Error during shutdown: {e}")
    logging.info("Shutdown complete.")

# ---------------- Entrypoint ----------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("telegram_bot_final:app", host="0.0.0.0", port=port, log_level="info")
