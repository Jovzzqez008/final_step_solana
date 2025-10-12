# telegram_bot_pumpfun_sniper.py
# Pump.fun SNIPER Bot - Detecta tokens al momento de graduación (GraphQL)
# Polling: 22 segundos, User-Agent: PC (Chrome)
# Requiere env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OWNER_ID, DATABASE_URL

import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
from fastapi import FastAPI, Request, HTTPException, Query
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- CONFIG ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

POLL_SECONDS = 22  # polling cada 22 segundos (solicitado)
PUMP_FUN_GRAPHQL = "https://pump.fun/api/graphql"

# detect mints like "...pump" or solana mint pattern
MINT_RE = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')
PUMP_SUFFIX = "pump"

# ---------- DB (asyncpg) ----------
class DB:
    def __init__(self):
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL not set")
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=6)
        await self._init()
        logging.info("✅ Postgres connected")

    async def _init(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pump_notifications (
                    mint TEXT PRIMARY KEY,
                    symbol TEXT,
                    alert_type TEXT,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    meta JSONB
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW()
                );
            """)

    async def was_notified(self, mint: str) -> bool:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint=$1", mint)
            return bool(r)

    async def mark_notified(self, mint: str, symbol: str, alert_type: str, meta: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_notifications (mint, symbol, alert_type, meta)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (mint) DO UPDATE SET symbol=EXCLUDED.symbol, alert_type=EXCLUDED.alert_type, meta=EXCLUDED.meta, detected_at=NOW()
            """, mint, symbol, alert_type, json.dumps(meta))

    async def seen_recently(self, mint: str, minutes: int = 2) -> bool:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT 1 FROM seen_tokens WHERE mint=$1 AND last_seen >= NOW() - ($2::int || ' minutes')::interval",
                mint, minutes
            )
            return bool(r)

    async def mark_seen(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen_tokens (mint, last_seen) VALUES ($1, NOW())
                ON CONFLICT (mint) DO UPDATE SET last_seen = NOW()
            """, mint)

    async def list_recent_notifications(self, limit: int = 10) -> List[Dict]:
        """Devuelve los últimos N registros de pump_notifications"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT mint, symbol, alert_type, detected_at, meta FROM pump_notifications ORDER BY detected_at DESC LIMIT $1",
                limit
            )
            results = []
            for r in rows:
                results.append({
                    "mint": r["mint"],
                    "symbol": r["symbol"],
                    "alert_type": r["alert_type"],
                    "detected_at": r["detected_at"].isoformat() if r["detected_at"] else None,
                    "meta": r["meta"]
                })
            return results

db = DB()

# ---------- TELEGRAM notifier ----------
class Notifier:
    def __init__(self, tg_app: Application):
        self.app = tg_app
        self.silent = False

    async def send_sniper_alert(self, symbol: str, mint: str, meta: dict):
        if self.silent:
            logging.info("Silent on - skipping message")
            return
        text = (
            f"🚨 <b>PUMP.FUN → GRADUADO</b> 🚨\n\n"
            f"<b>{symbol}</b>\n"
            f"• Mint: <code>{mint}</code>\n"
            f"• Detected: {meta.get('reason','graduated')}\n"
            f"• Market Cap (if provided): ${meta.get('market_cap','N/A')}\n"
            f"\nRevisa rápido con los botones:"
        )
        keyboard = [
            [InlineKeyboardButton("⚡ Swap Jupiter", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
            [InlineKeyboardButton("📈 Pump.fun", url=f"https://pump.fun/coin?mint={mint}")],
            [InlineKeyboardButton("🌊 Raydium", url=f"https://raydium.io/pair/{mint}-SOL")]
        ]
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
                disable_web_page_preview=True
            )
            logging.info("✅ Telegram alert sent for %s", mint)
        except Exception as e:
            logging.exception("Error sending telegram message: %s", e)

notifier: Optional[Notifier] = None

# ---------- GraphQL helper (pump.fun) ----------
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Connection": "keep-alive",
}

GRAPHQL_CANDIDATES = [
    ("""
    query RecentCoins($limit:Int){ 
      recent(limit:$limit) {
        id
        mint
        name
        symbol
        bondingCurve { complete }
        pool { id }
        stats5m { priceChange }
      }
    }
    """, {"limit": 50}),
    ("""
    query Recent($limit:Int){
      recentCoins(limit:$limit){
        id
        mint
        name
        symbol
        firstPool { createdAt }
        bondingCurve { complete }
        pool { id }
        stats5m { priceChange, numTraders }
      }
    }
    """, {"limit": 50}),
    ("""
    query Top($limit:Int){
      coins(limit:$limit){
        id
        mint
        name
        symbol
        bonded { complete }
        pool { id }
        stats5m { priceChange }
      }
    }
    """, {"limit": 50}),
    ("""
    query CoinsRecent($limit:Int){
      coinsRecent(limit:$limit){
        id
        mint
        name
        symbol
        bondingCurve { complete }
        pool { id }
      }
    }
    """, {"limit": 50}),
    ("""
    query Search($query:String){
      search(query:$query) {
        id
        mint
        name
        symbol
        bondingCurve { complete }
        pool { id }
      }
    }
    """, {"query": ""})
]

async def post_graphql(session: aiohttp.ClientSession, query: str, variables: dict):
    payload = {"query": query, "variables": variables}
    try:
        async with session.post(PUMP_FUN_GRAPHQL, json=payload, headers=DEFAULT_HEADERS, timeout=10) as resp:
            text = await resp.text()
            if resp.status != 200:
                logging.warning("HTTP %s %s returned %s", PUMP_FUN_GRAPHQL, json.dumps(variables), resp.status)
                return None
            try:
                return json.loads(text)
            except Exception:
                return None
    except Exception as e:
        logging.debug("GraphQL request error: %s", e)
        return None

def find_token_dicts(obj: Any) -> List[Dict]:
    found = []
    if isinstance(obj, dict):
        if ("mint" in obj or "id" in obj) and (isinstance(obj.get("mint"), str) or isinstance(obj.get("id"), str)):
            found.append(obj)
        for v in obj.values():
            found.extend(find_token_dicts(v))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_token_dicts(item))
    return found

def looks_graduated(token: Dict) -> Optional[str]:
    try:
        bc = token.get("bondingCurve") or token.get("bonded") or token.get("bonding")
        if isinstance(bc, dict):
            if bc.get("complete") is True or str(bc.get("complete")).lower() == "true":
                return "bondingCurve.complete"
        if token.get("pool"):
            return "pool_exists"
        for k in ("graduated", "isGraduated", "is_graduated", "status"):
            if k in token:
                v = token.get(k)
                if v is True or (isinstance(v, str) and v.lower() == "graduated"):
                    return k
        stats5 = token.get("stats5m") or token.get("stats_5m")
        if isinstance(stats5, dict):
            price_change = stats5.get("priceChange") or stats5.get("price_change") or 0
            try:
                if abs(float(price_change)) > 30:
                    return "big_5m_move"
            except Exception:
                pass
    except Exception:
        pass
    return None

# ---------- PumpFun Sniper ----------
class PumpFunSniper:
    def __init__(self, db: DB, notifier: Notifier):
        self.db = db
        self.notifier = notifier
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.session = None
        self.backoff = 1

    async def start(self):
        if self.task and not self.task.done():
            return
        self.running = True
        self.task = asyncio.create_task(self._run())
        logging.info("✅ Pump.fun Sniper started")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.session:
            await self.session.close()
        logging.info("⛔ Pump.fun Sniper stopped")

    async def _run(self):
        self.session = aiohttp.ClientSession()
        try:
            while self.running:
                try:
                    await self.scan_once()
                    self.backoff = 1
                    await asyncio.sleep(POLL_SECONDS)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logging.exception("Scan loop error: %s", e)
                    await asyncio.sleep(self.backoff)
                    self.backoff = min(self.backoff * 2, 60)
        finally:
            if self.session:
                await self.session.close()

    async def scan_once(self):
        found_any = False
        for query, vars_template in GRAPHQL_CANDIDATES:
            vars_copy = dict(vars_template)
            data = await post_graphql(self.session, query, vars_copy)
            if not data:
                continue
            payload = data.get("data") or data
            token_dicts = find_token_dicts(payload)
            if not token_dicts:
                continue
            found_any = True
            for t in token_dicts:
                mint = t.get("mint") or t.get("id")
                if not mint or not isinstance(mint, str):
                    continue
                if not (MINT_RE.search(mint) or mint.endswith(PUMP_SUFFIX)):
                    m = MINT_RE.search(mint)
                    if m:
                        mint = m.group(0)
                    else:
                        continue
                reason = looks_graduated(t)
                if not reason:
                    if mint.endswith(PUMP_SUFFIX) and (t.get("pool") or t.get("firstPool")):
                        reason = "suffix_and_pool"
                if not reason:
                    continue
                if await self.db.was_notified(mint):
                    continue
                symbol = t.get("symbol") or t.get("name") or mint[:8]
                meta = {"reason": reason}
                if "mcap" in t:
                    meta["market_cap"] = t.get("mcap")
                await self.db.mark_notified(mint, symbol, "graduated", meta)
                await self.db.mark_seen(mint)
                await self.notifier.send_sniper_alert(symbol, mint, meta)
                logging.info("Detected graduated mint %s (%s) reason=%s", mint, symbol, reason)
        if not found_any:
            logging.debug("No usable GraphQL response on this iteration")

# ---------- FastAPI + Telegram wiring ----------
app = FastAPI(title="PumpFun Sniper Bot")

telegram_app: Optional[Application] = None
sniper: Optional[PumpFunSniper] = None

def is_owner(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# Telegram commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = (
        "🚀 Pump.fun Sniper Bot\n\n"
        "Comandos:\n"
        "/iniciar - comenzar a monitorear graduaciones (GraphQL)\n"
        "/detener - detener monitoreo\n"
        "/status - estado\n"
        "/silent on|off - modo silencioso\n\n"
        f"Polling cada {POLL_SECONDS} segundos. User-Agent: PC Chrome."
    )
    await update.message.reply_text(text)

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text("✅ Iniciando sniper (Pump.fun GraphQL)...")
    await sniper.start()
    await update.message.reply_text("✅ Sniper iniciado")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text("⛔ Deteniendo sniper...")
    await sniper.stop()
    await update.message.reply_text("⛔ Sniper detenido")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    running = sniper.running if sniper else False
    s = "🟢 RUNNING" if running else "🔴 STOPPED"
    await update.message.reply_text(f"Pump.fun Sniper status: {s}\nPolling every {POLL_SECONDS}s\nSilent: {notifier.silent}")

async def cmd_silent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    args = ctx.args or []
    if not args or args[0] not in ("on", "off"):
        await update.message.reply_text("Uso: /silent on|off")
        return
    notifier.silent = (args[0] == "on")
    await update.message.reply_text("Silent %s" % ("ON" if notifier.silent else "OFF"))

# ========== NEW ENDPOINT: /last ==========
@app.get("/last")
async def last_notifications(limit: int = Query(10, ge=1, le=200)):
    """
    Devuelve los últimos N mints detectados en JSON.
    ?limit=10  (por defecto 10, máximo 200)
    """
    if not db.pool:
        raise HTTPException(status_code=503, detail="Database not connected")
    try:
        rows = await db.list_recent_notifications(limit=limit)
        return JSONResponse({"ok": True, "count": len(rows), "items": rows})
    except Exception as e:
        logging.exception("Error fetching last notifications: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/webhook/{token}")
async def webhook(token: str, req: Request):
    try:
        if token != TELEGRAM_BOT_TOKEN:
            raise HTTPException(status_code=403, detail="invalid token")
        body = await req.json()
        update = Update.de_json(body, telegram_app.bot)
        if update.effective_user and not is_owner(update):
            return JSONResponse({"ok": True, "note": "ignored - not owner"})
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        logging.exception("webhook err %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

# ---------- startup/shutdown ----------
async def initialize():
    global telegram_app, notifier, sniper
    missing = [k for k in ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","OWNER_ID","DATABASE_URL") if not os.getenv(k)]
    if missing:
        raise RuntimeError("Missing env vars: " + ", ".join(missing))
    await db.connect()
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("silent", cmd_silent))
    await telegram_app.initialize()
    await telegram_app.start()
    notifier = Notifier(telegram_app)
    sniper = PumpFunSniper(db, notifier)
    logging.info("App initialized")

@app.on_event("startup")
async def on_startup():
    try:
        await initialize()
        logging.info("✅ App startup complete - Use /iniciar to start sniper")
    except Exception as e:
        logging.exception("Startup failed: %s", e)
        raise

@app.on_event("shutdown")
async def on_shutdown():
    logging.info("Shutting down...")
    if sniper:
        await sniper.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    if db.pool:
        await db.pool.close()
    logging.info("Shutdown complete")

# ---------- local run ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("telegram_bot_pumpfun_sniper:app", host="0.0.0.0", port=int(os.getenv("PORT","8080")), log_level="info")
