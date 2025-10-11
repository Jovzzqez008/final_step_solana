# telegram_bot_pump_fun_multisource_fixed_final.py
# SOLANA PUMP.FUN BOT - FUENTES CENTRADAS EN PUMP.FUN (Versión final solicitada)
# PostgreSQL + Pump.fun primary + DexScreener fallback + Telegram webhook/polling

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== CONFIGURACIÓN ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Variables de entorno críticas
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")

# ENDPOINT PRINCIPAL: pump.fun (editable por env)
PUMP_FUN_BASE = os.getenv("PUMP_FUN_BASE", "https://pump.fun")

# Fallbacks
DEXSCREENER_API = "https://api.dexscreener.com/latest"

# Parámetros de trading optimizados
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "3000"))
PUMP_MC_MAX = float(os.getenv("PUMP_MC_MAX", "50000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "65000"))

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "1"))  # minutes
PORT = int(os.getenv("PORT", "8080"))

# Regex para detectar mints de Solana
MINT_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ========== BASE DE DATOS POSTGRESQL ==========
class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL not set")
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self._create_tables()
        await self._update_tables()
        logging.info("✅ PostgreSQL conectado y tablas verificadas")

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_notifications (
                    id SERIAL PRIMARY KEY,
                    mint TEXT UNIQUE NOT NULL,
                    alert_type TEXT NOT NULL,
                    symbol TEXT,
                    market_cap DECIMAL,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    graduated BOOLEAN DEFAULT FALSE,
                    source TEXT DEFAULT 'unknown'
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    source TEXT DEFAULT 'unknown'
                )
            """)

    async def _update_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("ALTER TABLE pump_notifications ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'unknown'")
            await conn.execute("ALTER TABLE pump_notifications ADD COLUMN IF NOT EXISTS market_cap DECIMAL")
            await conn.execute("ALTER TABLE pump_notifications ADD COLUMN IF NOT EXISTS graduated BOOLEAN DEFAULT FALSE")
            await conn.execute("ALTER TABLE seen_tokens ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'unknown'")

    async def was_notified(self, mint: str, alert_type: str = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint = $1 AND alert_type = $2", mint, alert_type)
            else:
                row = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint = $1", mint)
            return bool(row)

    async def mark_notified(self, mint: str, alert_type: str, symbol: str = None, market_cap: float = None, source: str = "unknown"):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_notifications (mint, alert_type, symbol, market_cap, source)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (mint) DO UPDATE SET
                    alert_type = EXCLUDED.alert_type,
                    symbol = EXCLUDED.symbol,
                    market_cap = EXCLUDED.market_cap,
                    source = EXCLUDED.source,
                    detected_at = NOW()
            """, mint, alert_type, symbol, market_cap, source)

    async def mark_graduated(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE pump_notifications SET graduated = TRUE WHERE mint = $1", mint)

    async def seen_recently(self, mint: str, minutes: int = 3) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 1 FROM seen_tokens 
                WHERE mint = $1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2
            """, mint, minutes)
            return bool(row)

    async def mark_seen(self, mint: str, source: str = "unknown"):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint, source) VALUES ($1, $2)
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW(), source = $2
            """, mint, source)

    async def get_pre_graduation_tokens(self) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT mint, symbol, market_cap, source
                FROM pump_notifications 
                WHERE graduated = FALSE AND market_cap BETWEEN $1 AND $2
                ORDER BY detected_at DESC
                LIMIT 50
            """, PUMP_MC_MIN, GRADUATION_MC_TARGET)
            return [dict(r) for r in rows]

    async def list_recent_notifications(self, limit: int = 10) -> List[Dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT mint, symbol, alert_type, market_cap, detected_at FROM pump_notifications ORDER BY detected_at DESC LIMIT $1", limit)
            return [dict(r) for r in rows]

# ========== CLIENTE HTTP ==========
class APIClient:
    def __init__(self):
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def fetch_json(self, url: str, params: dict = None, headers: dict = None, timeout: int = 8) -> Any:
        try:
            async with self.session.get(url, params=params, headers=headers, timeout=timeout) as response:
                text = await response.text()
                if response.status == 200:
                    try:
                        return json.loads(text)
                    except Exception:
                        # fallback: if html, return raw text
                        return text
                else:
                    logging.warning("HTTP %s returned %s", url, response.status)
        except asyncio.TimeoutError:
            logging.warning("Timeout fetching %s", url)
        except Exception as e:
            logging.error("Error fetching %s: %s", url, str(e))
        return None

# ========== PUMPFUN API (PRINCIPAL) ==========
class PumpFunAPI:
    """
    Cliente flexible para pump.fun.
    Intenta múltiples endpoints comunes y hace parsing tolerante.
    Si tu despliegue de pump.fun tiene rutas específicas, actualiza PUMP_FUN_BASE o
    los paths dentro de estos métodos.
    """
    def __init__(self, client: APIClient):
        self.client = client
        self.base = PUMP_FUN_BASE.rstrip('/')

    async def get_recent(self, limit: int = 50) -> List[Dict]:
        # Intentos por orden de probabilidad
        attempts = [
            f"{self.base}/api/coins/recent",
            f"{self.base}/api/coins",
            f"{self.base}/coins/recent",
            f"{self.base}/recent"
        ]
        for url in attempts:
            data = await self.client.fetch_json(url, params={"limit": limit})
            if not data:
                continue
            # aceptar list o dict con key 'coins' o 'data' o 'tokens'
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for k in ("coins","data","tokens","items"):
                    if k in data and isinstance(data[k], list):
                        return data[k]
                # maybe the API returns single-object wrapped
                if "coin" in data and isinstance(data["coin"], dict):
                    return [data["coin"]]
        return []

    async def get_coin_by_mint(self, mint: str) -> Optional[Dict]:
        # Intenta endpoints directos por mint
        attempts = [
            f"{self.base}/api/coins/{mint}",
            f"{self.base}/api/coin/{mint}",
            f"{self.base}/coins/{mint}",
            f"{self.base}/coin/{mint}",
            f"{self.base}/coin?mint={mint}"
        ]
        for url in attempts:
            data = await self.client.fetch_json(url)
            if not data:
                continue
            if isinstance(data, dict):
                # flexible: if data has 'id' or 'mint' etc
                if data.get("id") == mint or data.get("mint") == mint or "symbol" in data:
                    return data
                # maybe wrapped
                for k in ("coin","data","result"):
                    if k in data and isinstance(data[k], dict):
                        if data[k].get("id") == mint or data[k].get("mint") == mint or "symbol" in data[k]:
                            return data[k]
        return None

# ========== DexScreener (fallback) ==========
class DexScreenerAPI:
    def __init__(self, client: APIClient):
        self.client = client

    async def get_token_info(self, mint: str) -> Dict:
        url = f"{DEXSCREENER_API}/tokens/{mint}"
        return await self.client.fetch_json(url)

# ========== NOTIFICADOR TELEGRAM ==========
def build_buttons_for_mint(mint: str) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("⚡ Swap (Jupiter)", url=f"https://jup.ag/swap/{mint}-SOL")],
        [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
        [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
        [InlineKeyboardButton("📈 Pump.Fun", url=f"{PUMP_FUN_BASE.rstrip('/')}/coin/{mint}")]
    ]
    return InlineKeyboardMarkup(kb)

class TelegramNotifier:
    def __init__(self, bot_app: Application):
        self.app = bot_app
        self.silent_mode = False

    async def send_pump_alert(self, title: str, symbol: str, mint: str, data: Dict, alert_type: str, source: str = "pump.fun"):
        if self.silent_mode:
            logging.info("Silent mode ON - skipping alert for %s", symbol)
            return

        # Construir mensaje seguro (evita backslashes problemáticos)
        msg_lines = []
        msg_lines.append(f"🚨 <b>{title}</b> 🚨")
        msg_lines.append("")
        msg_lines.append(f"<b>{symbol}</b>")
        if 'market_cap' in data and data['market_cap'] is not None:
            try:
                mc_str = f"• Market Cap: ${float(data['market_cap']):,.0f}"
            except Exception:
                mc_str = f"• Market Cap: {data.get('market_cap')}"
            msg_lines.append(mc_str)
        if 'price' in data and data['price'] is not None:
            msg_lines.append(f"• Precio: {data.get('price')}")
        if alert_type == "pump_early":
            msg_lines.append("• ⚡ <b>OPORTUNIDAD PRE-GRADUACIÓN</b>")
        elif alert_type == "pre_graduation":
            msg_lines.append("• 📈 <b>PRE-GRADUACIÓN INMINENTE</b>")
            if 'graduation_percent' in data:
                msg_lines.append(f"• Progreso: {data['graduation_percent']:.1f}%")
        elif alert_type == "post_graduation_pump":
            msg_lines.append("• 🔥 <b>EXPLOSIÓN POST-GRADUACIÓN</b>")
            if 'price_change_5m' in data:
                msg_lines.append(f"• Cambio 5m: {data['price_change_5m']:.2f}%")
        msg_lines.append(f"• Fuente: {source}")
        msg_lines.append("")
        msg_lines.append("<b>Mint:</b>")
        msg_lines.append(f"<code>{mint}</code>")

        message = "\n".join(msg_lines)

        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
                reply_markup=build_buttons_for_mint(mint),
                disable_web_page_preview=True
            )
            logging.info("✅ Alerta enviada: %s (%s) via %s", symbol, alert_type, source)
        except Exception as e:
            logging.error("Error sending Telegram message: %s", str(e))

# ========== MONITOR PRINCIPAL CENTRADO EN PUMP.FUN ==========
class PumpFunMonitor:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False
        self.round = 0

    async def scan(self):
        self.round += 1
        logging.info("🔄 PumpFun Scan round %d", self.round)
        processed = 0
        async with APIClient() as client:
            pump = PumpFunAPI(client)
            ds = DexScreenerAPI(client)

            # 1) Intentar obtener recent desde pump.fun
            tokens = await pump.get_recent(limit=50)
            if not tokens:
                logging.warning("⚠ pump.fun recent returned no tokens; will try DexScreener recent fallback")
                # fallback: try to extract recent new pairs from DexScreener (not perfect but helps)
                try:
                    ds_raw = await client.fetch_json("https://api.dexscreener.com/latest/dex/pairs/recent")
                    if isinstance(ds_raw, dict):
                        tokens = ds_raw.get("pairs", [])[:50]
                except Exception:
                    tokens = []

            # process tokens
            for item in tokens:
                # Normalize flexible shapes:
                # pump.fun likely returns dicts with keys: id / mint / symbol / mcap / usdPrice / stats5m ...
                mint = None
                symbol = "UNKNOWN"
                market_cap = 0
                price = None

                if isinstance(item, dict):
                    mint = item.get("id") or item.get("mint") or item.get("address")
                    symbol = item.get("symbol") or item.get("name") or symbol
                    market_cap = item.get("mcap") or item.get("marketCap") or item.get("market_cap") or 0
                    price = item.get("usdPrice") or item.get("price") or item.get("priceUsd")

                if not mint:
                    # try nested structures (DexScreener pairs shape)
                    if 'pair' in item and isinstance(item['pair'], dict):
                        pair = item['pair']
                        # try identify token mint in pair data
                        mint = pair.get('tokenAddress') or pair.get('baseTokenAddress') or pair.get('tokenAddress')
                        symbol = pair.get('tokenSymbol') or pair.get('symbol') or symbol
                        # attempt to parse market cap / price if present
                        market_cap = pair.get('marketCap') or market_cap
                        price = pair.get('priceUsd') or price

                if not mint:
                    continue

                # ignore if seen very recently
                try:
                    if await self.db.seen_recently(mint, minutes=2):
                        continue
                except Exception:
                    pass

                await self.db.mark_seen(mint, source="pump.fun")

                # ensure numeric market cap
                try:
                    market_cap = float(market_cap) if market_cap else 0.0
                except Exception:
                    market_cap = 0.0

                # filter by reasonable MC
                if market_cap < PUMP_MC_MIN or market_cap > PUMP_MC_MAX:
                    # still might be interesting if very small MC -> leave out as user requested
                    continue

                # send early alert if not already notified
                if not await self.db.was_notified(mint, "pump_early"):
                    await self.notifier.send_pump_alert(
                        title="PUMP.FUN EARLY DETECTION",
                        symbol=symbol,
                        mint=mint,
                        data={"market_cap": market_cap, "price": price},
                        alert_type="pump_early",
                        source="pump.fun"
                    )
                    await self.db.mark_notified(mint, "pump_early", symbol, market_cap, "pump.fun")
                    processed += 1
                    logging.info("🚨 EARLY alert for %s (%s) mc=%s", symbol, mint, market_cap)

        logging.info("✅ Scan round %d done, %d alerts sent", self.round, processed)

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self.scan()
            except Exception as e:
                logging.error("PumpFunMonitor error: %s", str(e))
            await asyncio.sleep(max(30, CHECK_INTERVAL * 60))

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ PumpFunMonitor started")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PumpFunMonitor stopped")

# ========== POST-GRADUATION SCANNER ==========
class PostGraduationScanner:
    def __init__(self, db: Database, notifier: TelegramNotifier):
        self.db = db
        self.notifier = notifier
        self.task = None
        self.running = False

    async def scan(self):
        try:
            tokens = await self.db.get_pre_graduation_tokens()
            async with APIClient() as client:
                pump = PumpFunAPI(client)
                for t in tokens:
                    mint = t['mint']
                    symbol = t.get('symbol') or "UNKNOWN"
                    coin = await pump.get_coin_by_mint(mint)
                    if not coin:
                        continue
                    mc = coin.get('mcap') or coin.get('marketCap') or 0
                    try:
                        mc = float(mc)
                    except Exception:
                        mc = 0
                    # mark graduated if over target
                    if mc > GRADUATION_MC_TARGET and not await self.db.was_notified(mint, "graduated"):
                        await self.db.mark_graduated(mint)
                        await self.db.mark_notified(mint, "graduated")
                        logging.info("🎓 Marked graduated: %s (%s)", symbol, mint)
                    # detect post-graduation pump: try find stats5m priceChange
                    stats5 = coin.get("stats5m") or {}
                    price_change_5m = stats5.get("priceChange") or stats5.get("price_change") or 0
                    try:
                        pc5 = float(price_change_5m)
                    except Exception:
                        pc5 = 0
                    if mc > GRADUATION_MC_TARGET and pc5 > 15 and not await self.db.was_notified(mint, "post_graduation_pump"):
                        await self.notifier.send_pump_alert(
                            title="EXPLOSIÓN POST-GRADUACIÓN",
                            symbol=symbol,
                            mint=mint,
                            data={"market_cap": mc, "price_change_5m": pc5},
                            alert_type="post_graduation_pump",
                            source="pump.fun"
                        )
                        await self.db.mark_notified(mint, "post_graduation_pump")
        except Exception as e:
            logging.error("PostGraduationScanner error: %s", str(e))

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self.scan()
            except Exception as e:
                logging.error("PostGraduationScanner failed: %s", str(e))
            await asyncio.sleep(2 * 60)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("✅ PostGraduationScanner started")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("⛔ PostGraduationScanner stopped")

# ========== FASTAPI + TELEGRAM APP ==========
app = FastAPI(title="Solana Pump.fun Bot - Final")
db = Database()
telegram_app: Optional[Application] = None
notifier: Optional[TelegramNotifier] = None
pump_monitor: Optional[PumpFunMonitor] = None
post_graduation_scanner: Optional[PostGraduationScanner] = None

def is_authorized(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# ========== TELEGRAM COMMANDS ==========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("❌ No autorizado")
        return
    welcome = (
        "🎯 <b>SOLANA PUMP.FUN BOT (Final)</b>\n\n"
        "Comandos:\n"
        "/iniciar - Iniciar monitores\n"
        "/detener - Detener monitores\n"
        "/status - Estado\n"
        "/silent on|off - Modo silencioso\n"
        "/lista - Últimos tokens detectados\n\n"
        "Fuente principal: pump.fun"
    )
    await update.message.reply_text(welcome, parse_mode="HTML")

async def cmd_iniciar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if pump_monitor:
        await pump_monitor.start()
    if post_graduation_scanner:
        await post_graduation_scanner.start()
    await update.message.reply_text("✅ Monitores iniciados", parse_mode="HTML")

async def cmd_detener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if pump_monitor:
        await pump_monitor.stop()
    if post_graduation_scanner:
        await post_graduation_scanner.stop()
    await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    pump_status = "🟢" if pump_monitor and pump_monitor.running else "🔴"
    post_status = "🟢" if post_graduation_scanner and post_graduation_scanner.running else "🔴"
    silent = "🔇" if notifier and notifier.silent_mode else "🔔"
    text = (
        f"<b>Estado</b>\nPumpFun Monitor: {pump_status}\n"
        f"PostGraduation: {post_status}\n"
        f"Silent: {silent}\n\n"
        f"PUMP_MC_MIN: ${PUMP_MC_MIN:,.0f}\n"
        f"GRAD_TARGET: ${GRADUATION_MC_TARGET:,.0f}\n"
        f"Intervalo: {CHECK_INTERVAL} min"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_silent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    args = context.args or []
    if not args or args[0] not in ("on","off"):
        await update.message.reply_text("Uso: /silent on|off")
        return
    if notifier:
        notifier.silent_mode = (args[0] == "on")
    await update.message.reply_text(f"Silent mode {'ON' if notifier and notifier.silent_mode else 'OFF'}")

async def cmd_lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    rows = await db.list_recent_notifications(limit=10)
    if not rows:
        await update.message.reply_text("No hay notificaciones.", parse_mode="HTML")
        return
    lines = ["<b>Últimas notificaciones:</b>\n"]
    for r in rows:
        sym = r.get('symbol') or "UNK"
        mint = r.get('mint')
        mc = r.get('market_cap') or 0
        at = r.get('detected_at')
        # construct links
        links = []
        links.append(f"<a href='https://dexscreener.com/solana/{mint}'>DexScreener</a>")
        links.append(f"<a href='{PUMP_FUN_BASE.rstrip('/')}/coin/{mint}'>Pump.Fun</a>")
        links.append(f"<a href='https://rugcheck.xyz/tokens/{mint}'>RugCheck</a>")
        links_txt = " | ".join(links)
        lines.append(f"• <b>{sym}</b> • ${float(mc):,.0f}\n<code>{mint}</code>\n{links_txt}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

# ========== WEBHOOK ==========
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, telegram_app.bot)
        if update.effective_user and update.effective_user.id == OWNER_ID:
            await telegram_app.process_update(update)
        return JSONResponse({"status":"ok"})
    except Exception as e:
        logging.error("Webhook error: %s", str(e))
        return JSONResponse({"status":"error","error":str(e)}, status_code=500)

@app.get("/health")
async def health():
    return {"status":"ok", "timestamp": datetime.utcnow().isoformat(), "pump_monitor": pump_monitor.running if pump_monitor else False}

@app.get("/")
async def root():
    return {"message":"Solana Pump.fun Bot - Final", "status":"operational"}

# ========== INICIALIZACIÓN ==========
async def initialize_app():
    global telegram_app, notifier, pump_monitor, post_graduation_scanner

    required = {"TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN, "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID, "OWNER_ID": OWNER_ID, "DATABASE_URL": DATABASE_URL}
    missing = [k for k,v in required.items() if not v]
    if missing:
        raise RuntimeError("Faltan variables: " + ", ".join(missing))

    await db.connect()

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("silent", cmd_silent))
    telegram_app.add_handler(CommandHandler("lista", cmd_lista))

    notifier = TelegramNotifier(telegram_app)
    pump_monitor = PumpFunMonitor(db, notifier)
    post_graduation_scanner = PostGraduationScanner(db, notifier)

    logging.info("✅ Aplicación inicializada")

@app.on_event("startup")
async def startup_event():
    try:
        await initialize_app()
        await telegram_app.initialize()
        # set webhook if environment indicates production url
        RAILWAY_URL = os.getenv("RAILWAY_STATIC_URL") or os.getenv("APP_URL") or os.getenv("WEBHOOK_URL")
        if RAILWAY_URL:
            webhook_url = f"{RAILWAY_URL.rstrip('/')}/webhook"
            await telegram_app.bot.set_webhook(webhook_url)
            logging.info("✅ Webhook configured: %s", webhook_url)
        else:
            await telegram_app.start()
            logging.info("✅ Telegram polling started (dev mode)")
        logging.info("🚀 Bot listo - usa /iniciar")
    except Exception as e:
        logging.error("Startup error: %s", str(e))
        raise

@app.on_event("shutdown")
async def shutdown_event():
    logging.info("🛑 Shutting down...")
    if pump_monitor:
        await pump_monitor.stop()
    if post_graduation_scanner:
        await post_graduation_scanner.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    if db and db.pool:
        await db.pool.close()
    logging.info("✅ Shutdown complete")

# ========== RUN LOCAL ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
