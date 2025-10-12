# telegram_pumpfun_listener.py
# Pump.fun listener -> detecta mints PRE-GRADUACIÓN y notifica por Telegram
# Uso: desplegar en Railway; requiere env vars documentadas abajo

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- Config ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Required env vars (must configurar en Railway)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
DOMAIN_URL = os.getenv("DOMAIN_URL")  # https://your-app.up.railway.app
SOLANA_RPC_WSS = os.getenv("SOLANA_RPC_WSS", "")  # wss://... (QuickNode, Helius, etc.)
SOLANA_RPC_HTTP = os.getenv("SOLANA_RPC_HTTP", "")  # https://... (RPC HTTP for getTransaction)
PUMP_PROGRAM_ID = os.getenv("PUMP_PROGRAM_ID")  # ID del programa pump.fun (REQUIRED)

# Tunables
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "10000"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "2"))
PORT = int(os.getenv("PORT", "8080"))

# Regex mint pattern
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ---------- Database (Postgres) ----------
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL no configurado")
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        logging.info("Postgres conectado")

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW()
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_candidates (
                    mint TEXT PRIMARY KEY,
                    symbol TEXT,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    status TEXT DEFAULT 'pregraduado',
                    graduated_at TIMESTAMP,
                    last_metric_at TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id SERIAL PRIMARY KEY,
                    mint TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT NOW(),
                    market_cap DECIMAL,
                    price DECIMAL,
                    extra JSONB
                )
            """)

    async def seen_recently(self, mint: str, minutes: int = 5) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint) VALUES ($1)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW()
            """, mint)

    async def was_notified(self, mint: str, alert_type: str = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow("SELECT 1 FROM notifications WHERE mint = $1 AND alert_type = $2", mint, alert_type)
            else:
                row = await conn.fetchrow("SELECT 1 FROM notifications WHERE mint = $1", mint)
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str, symbol: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO notifications (mint, alert_type, symbol)
                VALUES ($1, $2, $3)
                ON CONFLICT (mint) DO UPDATE SET alert_type = EXCLUDED.alert_type, symbol = EXCLUDED.symbol, created_at = NOW()
            """, mint, alert_type, symbol)

    async def upsert_pump_candidate(self, mint: str, symbol: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_candidates (mint, symbol) VALUES ($1, $2)
                ON CONFLICT (mint) DO UPDATE SET symbol = EXCLUDED.symbol
            """, mint, symbol)

    async def add_metric(self, mint: str, market_cap: float = None, price: float = None, extra: dict = None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO metrics (mint, market_cap, price, extra) VALUES ($1, $2, $3, $4)
            """, mint, market_cap, price, json.dumps(extra or {}))

# ---------- Telegram notifier ----------
class TelegramNotifier:
    def __init__(self, app: Application):
        self.app = app
        self.silent_mode = False

    async def send(self, title: str, mint: str, symbol: str = "UNK", data: dict = None, alert_type: str = "pump_fun"):
        if self.silent_mode:
            logging.info("Silent mode on, skipping alert")
            return
        data = data or {}
        message = f"🚀 <b>{title}</b>\n\n<b>{symbol}</b>\n"
        if 'market_cap' in data:
            message += f"• Market Cap: ${float(data.get('market_cap') or 0):,.0f}\n"
        if 'price' in data:
            message += f"• Precio: {data.get('price')}\n"
        message += f"\n<b>Mint:</b>\n<code>{mint}</code>\n"
        keyboard = [
            [InlineKeyboardButton("🔁 Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")]
        ]
        try:
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="HTML",
                                            reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
            logging.info(f"Alert sent for {mint}")
        except Exception as e:
            logging.error(f"Error sending telegram message: {e}")

# ---------- Pump.fun listener (logsSubscribe) ----------
class PumpFunListener:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.running = False
        self.ws = None

    async def subscribe_logs(self):
        if not SOLANA_RPC_WSS:
            logging.error("SOLANA_RPC_WSS no configurado")
            return

        # subscribe payload: logsSubscribe with mentions filter; PUMP_PROGRAM_ID required
        params = [{"mentions": [PUMP_PROGRAM_ID]}, {"commitment": "confirmed"}]
        req = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe", "params": params}

        reconnect_delay = 1
        while True:
            try:
                async with websockets.connect(SOLANA_RPC_WSS, ping_interval=30) as ws:
                    self.ws = ws
                    logging.info("Conectado a Solana WSS")
                    await ws.send(json.dumps(req))
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        # handle notifications
                        if "method" in msg and msg["method"] == "logsNotification":
                            await self.handle_log_notification(msg.get("params", {}))
            except Exception as e:
                logging.error(f"WSS error: {e}; reconnecting in {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def handle_log_notification(self, params: dict):
        try:
            result = params.get("result", {})
            logs = result.get("value", {}).get("logs", []) or []
            signature = result.get("value", {}).get("signature")
            # Quick heuristic: logs often include 'Create' or 'InitializeMint' lines with mint addr; extract with regex
            joined = "\n".join(logs)
            mints = set(MINT_PATTERN.findall(joined))
            # If no mints in logs, attempt to fetch transaction and scan accounts
            if not mints and signature:
                tx_mints = await self.extract_mints_from_tx(signature)
                mints.update(tx_mints)
            for mint in mints:
                if await self.db.seen_recently(mint, minutes=2):
                    continue
                await self.db.mark_seen(mint)
                logging.info(f"Detected mint: {mint} (signature {signature})")
                # Try to get extra details from transaction
                details = await self.fetch_token_details_from_tx(signature, mint)
                # store candidate and notify
                await self.db.upsert_pump_candidate(mint, details.get("symbol"))
                if not await self.db.was_notified(mint, "pump_fun"):
                    await self.notifier.send("PUMP.FUN PRE-GRADUACIÓN", mint, details.get("symbol", "UNKNOWN"),
                                             {"market_cap": details.get("market_cap"), "price": details.get("price")}, "pump_fun")
                    await self.db.mark_notified(mint, "pump_fun", details.get("symbol"))
                    await self.db.add_metric(mint, market_cap=details.get("market_cap"), price=details.get("price"), extra=details)
        except Exception as e:
            logging.error(f"Error handling log notification: {e}")

    async def extract_mints_from_tx(self, signature: str) -> List[str]:
        # fallback: fetch transaction and parse account keys for possible mints
        if not SOLANA_RPC_HTTP:
            return []
        try:
            async with aiohttp.ClientSession() as s:
                payload = {"jsonrpc":"2.0","id":1,"method":"getTransaction","params":[signature, {"encoding":"jsonParsed","commitment":"confirmed"}]}
                async with s.post(SOLANA_RPC_HTTP, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
                    result = data.get("result")
                    if not result:
                        return []
                    # search in accounts and instructions
                    mints = set()
                    meta = result.get("meta", {})
                    tx = result.get("transaction", {})
                    # inspect account keys
                    accounts = tx.get("message", {}).get("accountKeys", [])
                    for acc in accounts:
                        key = acc.get("pubkey") if isinstance(acc, dict) else acc
                        if key and MINT_PATTERN.fullmatch(key):
                            mints.add(key)
                    # inspect inner instructions parsed
                    instrs = result.get("transaction", {}).get("message", {}).get("instructions", []) or []
                    for ins in instrs:
                        # search strings in raw data
                        text = json.dumps(ins)
                        for found in MINT_PATTERN.findall(text):
                            mints.add(found)
                    return list(mints)
        except Exception as e:
            logging.debug(f"extract_mints_from_tx error: {e}")
            return []

    async def fetch_token_details_from_tx(self, signature: str, mint: str) -> Dict:
        details = {"symbol": None, "price": None, "market_cap": None}
        # Attempt to parse metadata URI from transaction or logs
        if not SOLANA_RPC_HTTP:
            return details
        try:
            async with aiohttp.ClientSession() as s:
                payload = {"jsonrpc":"2.0","id":1,"method":"getTransaction","params":[signature, {"encoding":"jsonParsed","commitment":"confirmed"}]}
                async with s.post(SOLANA_RPC_HTTP, json=payload, timeout=10) as resp:
                    if resp.status != 200:
                        return details
                    data = await resp.json()
                    result = data.get("result")
                    if not result:
                        return details
                    logs = (result.get("meta") or {}).get("logMessages") or []
                    joined = "\n".join(logs)
                    # Heuristics: pull symbol/name from logs
                    # Many pump.fun logs include name/symbol/text; search by regex patterns typical in pump logs
                    # Example patterns: "Name: TOKEN", "Symbol: TKN", "name:", "symbol:"
                    name_match = re.search(r'Name[:\s]+([A-Za-z0-9_\-]{1,32})', joined, re.IGNORECASE)
                    symbol_match = re.search(r'Symbol[:\s]+([A-Za-z0-9_\-]{1,12})', joined, re.IGNORECASE)
                    if symbol_match:
                        details["symbol"] = symbol_match.group(1)
                    elif name_match:
                        details["symbol"] = name_match.group(1)[:10]
                    # fallback: set symbol to first 6 chars of mint
                    if not details["symbol"]:
                        details["symbol"] = mint[:6]
                    # price/market_cap cannot be known pre-grad reliably; leave None
                    return details
        except Exception as e:
            logging.debug(f"fetch_token_details_from_tx error: {e}")
            return details

# ---------- FastAPI + Telegram integration ----------
app = FastAPI(title="PumpFun Listener")

db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
listener: Optional[PumpFunListener] = None

def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# Telegram commands
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = ("🤖 PumpFun Listener\n"
            "/iniciar - Iniciar listeners\n"
            "/detener - Detener listeners\n"
            "/status - Estado\n"
            "/silent on|off - silenciar\n")
    await update.message.reply_text(text)

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if listener:
        # start background task
        asyncio.create_task(listener.subscribe_logs())
        await update.message.reply_text("✅ Listener iniciado")
    else:
        await update.message.reply_text("Listener no inicializado")

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    # no hard stop impl; advise restart
    await update.message.reply_text("Detén la app en Railway para parar listener")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    stat = "RUNNING" if listener and listener.ws else "STOPPED"
    await update.message.reply_text(f"Estado listener: {stat}")

# webhook endpoint for Telegram
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="invalid token")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    if update.effective_user and update.effective_user.id == OWNER_ID:
        await telegram_app.process_update(update)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status":"ok","time": datetime.utcnow().isoformat()}

# ---------- initialize app ----------
async def initialize_app():
    global telegram_app, notifier, listener
    # validate critical envs
    needed = ["TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","OWNER_ID","DATABASE_URL","SOLANA_RPC_WSS","SOLANA_RPC_HTTP","PUMP_PROGRAM_ID","DOMAIN_URL"]
    missing = [k for k in needed if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Faltan env vars: {', '.join(missing)}")
    await db.connect()
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    # register handlers
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("iniciar", iniciar_command))
    telegram_app.add_handler(CommandHandler("detener", detener_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("silent", lambda u,c: None))  # silent handled in notifier if you extend
    notifier = TelegramNotifier(telegram_app)
    listener = PumpFunListener(db, notifier)

@app.on_event("startup")
async def startup():
    try:
        await initialize_app()
        await telegram_app.initialize()
        await telegram_app.start()
        # set webhook so Telegram posts updates to /webhook/<token>
        webhook_url = f"{DOMAIN_URL.rstrip('/')}/webhook/{TELEGRAM_BOT_TOKEN}"
        try:
            await telegram_app.bot.set_webhook(webhook_url)
            logging.info(f"Webhook registrado: {webhook_url}")
        except Exception as e:
            logging.error(f"Error registrando webhook: {e}")
        # start listener background
        asyncio.create_task(listener.subscribe_logs())
        logging.info("Startup completo")
    except Exception as e:
        logging.error(f"Startup error: {e}")
        raise

@app.on_event("shutdown")
async def shutdown():
    logging.info("Shutdown event")
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

# ---------- run local ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
