# telegram_bot_final.py
# Solana Pump.fun sniper-alert focused: detect graduated tokens (pump.fun GraphQL)
# Postgres (Railway) + Telegram webhook control + /last endpoint
# Deploy: Procfile (web: gunicorn telegram_bot_final:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT)

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ------------- CONFIG -------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")
DOMAIN_URL = os.getenv("DOMAIN_URL")  # webhook base URL (Railway)
# Pump.fun GraphQL endpoint (internal)
PUMPFUN_GRAPHQL = "https://pump.fun/api/graphql"

# tuning
CHECK_INTERVAL_SECS = int(os.getenv("CHECK_INTERVAL_SECS", "22"))  # 22s as requested
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "3000"))
GRADUATION_MC_TARGET = float(os.getenv("GRADUATION_MC_TARGET", "65000"))
LAST_LIMIT_DEFAULT = int(os.getenv("LAST_LIMIT_DEFAULT", "25"))

# regex mint (solana)
MINT_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

PORT = int(os.getenv("PORT", "8080"))

# ------------- DB helper -------------
class DB:
    def __init__(self):
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self, dsn: str):
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
        await self._init()

    async def _init(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS pump_notifications (
                id SERIAL PRIMARY KEY,
                mint TEXT UNIQUE NOT NULL,
                symbol TEXT,
                market_cap NUMERIC,
                alert_type TEXT,
                source TEXT,
                detected_at TIMESTAMP DEFAULT NOW()
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_tokens (
                mint TEXT PRIMARY KEY,
                last_seen TIMESTAMP DEFAULT NOW()
            );
            """)

    async def seen_recent(self, mint: str, secs: int = 30) -> bool:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT 1 FROM seen_tokens WHERE mint=$1 AND last_seen >= NOW() - $2::interval", mint, f"{secs} seconds")
            return bool(r)

    async def mark_seen(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO seen_tokens(mint,last_seen) VALUES($1,NOW()) ON CONFLICT (mint) DO UPDATE SET last_seen=EXCLUDED.last_seen", mint)

    async def was_notified(self, mint: str, alert_type: Optional[str] = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                r = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint=$1 AND alert_type=$2", mint, alert_type)
            else:
                r = await conn.fetchrow("SELECT 1 FROM pump_notifications WHERE mint=$1", mint)
            return bool(r)

    async def add_notification(self, mint: str, symbol: str, market_cap: float, alert_type: str, source: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO pump_notifications(mint,symbol,market_cap,alert_type,source,detected_at)
                VALUES($1,$2,$3,$4,$5,NOW())
                ON CONFLICT (mint) DO UPDATE SET symbol=EXCLUDED.symbol, market_cap=EXCLUDED.market_cap, alert_type=EXCLUDED.alert_type, source=EXCLUDED.source, detected_at=EXCLUDED.detected_at
            """, mint, symbol, market_cap, alert_type, source)

    async def last_notifications(self, limit: int = 25):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT mint,symbol,market_cap,alert_type,source,detected_at FROM pump_notifications ORDER BY detected_at DESC LIMIT $1", limit)
            return [dict(r) for r in rows]

db = DB()

# ------------- Telegram notifier -------------
class Notifier:
    def __init__(self, app: Application):
        self.app = app
        self.silent = False

    async def send(self, title: str, symbol: str, mint: str, market_cap: float, source: str, extra: Dict = None):
        if self.silent:
            logging.info("Silent ON - skip send")
            return
        text = f"🚨 <b>{title}</b>\n\n<b>{symbol}</b>\n• Market Cap: ${market_cap:,.0f}\n• Fuente: {source}\n\n<b>Mint:</b>\n<code>{mint}</code>"
        if extra:
            if "price" in extra:
                text = text.replace("</code>", f"\n• Price: {extra['price']}</code>")
        kb = [
            [InlineKeyboardButton("⚡ Swap (Jupiter)", url=f"https://jup.ag/swap/{mint}-SOL")],
            [InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("🛡 RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")],
            [InlineKeyboardButton("📈 Pump.fun", url=f"https://pump.fun/coin?mint={mint}")]
        ]
        try:
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb), disable_web_page_preview=True)
            logging.info("Sent alert for %s", mint)
        except Exception as e:
            logging.exception("telegram send error: %s", e)

notifier: Optional[Notifier] = None

# ------------- Pump.fun GraphQL client -------------
async def fetch_pumpfun_graphql(session: aiohttp.ClientSession, query: str, variables: dict = None, timeout: int = 8):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://pump.fun/",
        "Origin": "https://pump.fun",
        "Content-Type": "application/json"
    }
    payload = {"query": query, "variables": variables or {}}
    try:
        async with session.post(PUMPFUN_GRAPHQL, json=payload, headers=headers, timeout=timeout) as resp:
            text = await resp.text()
            if resp.status != 200:
                logging.warning("HTTP %s %s returned %s", PUMPFUN_GRAPHQL, query[:80], resp.status)
                return None
            try:
                return json.loads(text)
            except Exception:
                logging.warning("Invalid JSON from pump.fun")
                return None
    except Exception as e:
        logging.warning("pump.fun graphql fetch error: %s", e)
        return None

# GraphQL query attempt (flexible). We try typical shapes many pump.fun builds use.
GRAPHQL_RECENT_GRADUATED = """
query RecentCoins($limit: Int!) {
  coins(limit: $limit, filter: {graduated: true}, sort: "detectedAt:desc") {
    id
    symbol
    usdPrice
    mcap
    status
    firstPool {
      createdAt
    }
    updatedAt
  }
}
"""

# fallback query if schema differs
GRAPHQL_RECENT_GENERIC = """
query {
  recentCoins(limit: 50) {
    id
    symbol
    usdPrice
    mcap
    status
  }
}
"""

# ------------- Monitor: poll pump.fun and detect newly graduated -------------
class GraduationMonitor:
    def __init__(self, session: aiohttp.ClientSession, db: DB, notifier: Notifier):
        self.session = session
        self.db = db
        self.notifier = notifier
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.backoff = 1

    async def poll_once(self):
        # 1) try main GraphQL query
        data = await fetch_pumpfun_graphql(self.session, GRAPHQL_RECENT_GRADUATED, {"limit": 50})
        coins = None
        if data:
            # try different shapes
            if isinstance(data, dict):
                # either data['data']['coins'] or data.get('coins')
                coins = (data.get("data") or {}).get("coins") or data.get("coins") or data.get("recentCoins")
        if not coins:
            # try a fallback
            data2 = await fetch_pumpfun_graphql(self.session, GRAPHQL_RECENT_GENERIC)
            if data2:
                coins = (data2.get("data") or {}).get("recentCoins") or data2.get("recentCoins")

        if not coins:
            logging.debug("No coins returned from pump.fun this round")
            return 0

        found = 0
        for c in coins:
            if not isinstance(c, dict):
                continue
            mint = c.get("id") or c.get("mint") or ""
            # some pump.fun examples include 'pump' appended by other scripts; extract actual mint using regex
            m = MINT_RE.search(mint)
            if m:
                mint_clean = m.group(0)
            else:
                # skip if no valid mint
                continue

            # Check if graduated: status flag or presence of firstPool or mcap >= target
            status = c.get("status") or ""
            first_pool = c.get("firstPool") or {}
            mcap = c.get("mcap") or c.get("marketCap") or c.get("fdv") or 0
            try:
                mcap_val = float(mcap) if mcap not in (None, "") else 0.0
            except Exception:
                mcap_val = 0.0

            # heuristics for graduation: status contains 'graduated' or firstPool exists or mcap > GRADUATION_MC_TARGET
            is_graduated = False
            if isinstance(status, str) and "gradu" in status.lower():
                is_graduated = True
            elif first_pool and first_pool.get("createdAt"):
                is_graduated = True
            elif mcap_val >= GRADUATION_MC_TARGET:
                is_graduated = True

            if not is_graduated:
                # skip; we only alert on graduation stage
                continue

            # skip recently seen
            if await self.db.seen_recent(mint_clean, secs=60):
                continue
            await self.db.mark_seen(mint_clean)

            # only notify once per mint
            if await self.db.was_notified(mint_clean, "graduated"):
                continue

            symbol = c.get("symbol") or "UNK"
            price = c.get("usdPrice") or c.get("price") or None

            # store and notify
            await self.db.add_notification(mint_clean, symbol, mcap_val, "graduated", "pump.fun_graphql")
            await self.notifier.send("PUMP.FUN -> GRADUADO (RAYDIUM POOL)", symbol, mint_clean, mcap_val, "pump.fun_graphql", extra={"price": price})
            found += 1
            logging.info("Graduated detected: %s %s mc=%s", symbol, mint_clean, mcap_val)

        return found

    async def run(self):
        self.running = True
        self.backoff = 1
        while self.running:
            try:
                found = await self.poll_once()
                # reset backoff on success
                self.backoff = 1
                await asyncio.sleep(CHECK_INTERVAL_SECS)
            except Exception as e:
                logging.exception("Monitor error: %s", e)
                await asyncio.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, 60)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

# ------------- FASTAPI + Telegram wiring -------------
app = FastAPI(title="Pump.fun Graduation Sniper")

telegram_app: Optional[Application] = None
monitor: Optional[GraduationMonitor] = None
http_session: Optional[aiohttp.ClientSession] = None
notifier: Optional[Notifier] = None

def is_owner(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID

# Telegram commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = ("🤖 <b>PUMP.FUN GRADUATION SNIPER</b>\n\n"
            "Comandos:\n/iniciar - iniciar monitor\n/detener - detener monitor\n/status - estado\n/silent on|off\n\n"
            "Este bot alerta cuando un token se registra como GRADUADO en pump.fun (pool en Raydium).")
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if monitor:
        await monitor.start()
    await update.message.reply_text("✅ Monitor iniciado", parse_mode="HTML")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if monitor:
        await monitor.stop()
    await update.message.reply_text("⛔ Monitor detenido", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    running = bool(monitor and monitor.running)
    text = (f"<b>Status</b>\nMonitor running: {'✅' if running else '❌'}\n"
            f"Interval: {CHECK_INTERVAL_SECS}s\n"
            f"PUMP_MC_MIN: ${PUMP_MC_MIN:,.0f}\nGRAD_TARGET: ${GRADUATION_MC_TARGET:,.0f}")
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_silent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: /silent on|off")
        return
    if args[0].lower() == "on":
        notifier.silent = True
        await update.message.reply_text("🔕 Silent ON")
    else:
        notifier.silent = False
        await update.message.reply_text("🔔 Silent OFF")

# webhook: Telegram will post updates here
@app.post("/webhook/{token}")
async def webhook(token: str, req: Request):
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="invalid token")
    body = await req.json()
    update = Update.de_json(body, telegram_app.bot)
    if update.effective_user and not is_owner(update):
        return JSONResponse({"ok": True, "note": "ignored - not owner"})
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})

# last endpoint
@app.get("/last")
async def last(limit: int = LAST_LIMIT_DEFAULT):
    rows = await db.last_notifications(limit)
    # convert datetimes
    for r in rows:
        if isinstance(r.get("detected_at"), datetime):
            r["detected_at"] = r["detected_at"].isoformat()
    return JSONResponse({"last": rows})

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

# ------------- startup / shutdown -------------
@app.on_event("startup")
async def on_startup():
    global telegram_app, http_session, notifier, monitor
    # check env
    missing = [k for k,v in {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "OWNER_ID": OWNER_ID,
        "DATABASE_URL": DATABASE_URL,
        "DOMAIN_URL": DOMAIN_URL
    }.items() if not v]
    if missing:
        logging.error("Missing env vars: %s", missing)
        raise RuntimeError("Missing env vars: " + ", ".join(missing))

    # db
    await db.connect(DATABASE_URL)

    # telegram app
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("silent", cmd_silent))

    # http session + notifier + monitor
    http_session = aiohttp.ClientSession()
    notifier = Notifier(telegram_app)
    monitor = GraduationMonitor(http_session, db, notifier)

    # initialize telegram (webhook)
    await telegram_app.initialize()
    await telegram_app.start()

    # set webhook if DOMAIN_URL present
    if DOMAIN_URL:
        webhook_url = f"{DOMAIN_URL.rstrip('/')}/webhook/{TELEGRAM_BOT_TOKEN}"
        try:
            await telegram_app.bot.set_webhook(webhook_url)
            logging.info("Webhook set to %s", webhook_url)
        except Exception as e:
            logging.warning("Could not set webhook: %s", e)

    logging.info("Startup complete. Use /iniciar to start the monitor.")

@app.on_event("shutdown")
async def on_shutdown():
    # stop monitor
    if monitor:
        await monitor.stop()
    if telegram_app:
        try:
            await telegram_app.bot.delete_webhook()
        except Exception:
            pass
        await telegram_app.stop()
        await telegram_app.shutdown()
    if http_session:
        await http_session.close()
    if db.pool:
        await db.pool.close()
    logging.info("Shutdown complete.")

# ------------- run for local dev -------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("telegram_bot_final:app", host="0.0.0.0", port=PORT, log_level="info")
