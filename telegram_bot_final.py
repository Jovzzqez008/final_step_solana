# telegram_bot_final_v7_pro.py
# SOLANA MONITOR - Pump.fun + Pre-Explosion (Jupiter boosted)
# PostgreSQL (asyncpg) + QuickNode WSS + Jupiter Token API V2 + webhook-ready
# Despliegue: Railway (Procfile -> web: gunicorn telegram_bot_final_v7_pro:app --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT)

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- CONFIG ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL", "")
JUPITER_API_URL = os.getenv("JUPITER_API_URL", "https://lite-api.jup.ag/tokens/v2")
PORT = int(os.getenv("PORT", "8080"))

# Parameters (tune if needed)
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "10000"))        # alert new mint if market cap >=
PRE_EXPLOSION_MC_MIN = float(os.getenv("PRE_EXPLOSION_MC_MIN", "100000"))
PRE_WINDOW_MIN = int(os.getenv("PRE_WINDOW_MIN", "10"))      # window for pre-explosion heuristics
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))  # 60s as requested

# mint regex for solana
MINT_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

# ---------- UTIL ----------
def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def safe_html(s: Optional[str]) -> str:
    if not s:
        return ""
    return s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

# ---------- DB (asyncpg) ----------
class PG:
    def __init__(self):
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self, dsn: str):
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
        await self._init_tables()
        logging.info("✅ Postgres connected")

    async def _init_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    mint TEXT PRIMARY KEY,
                    alert_type TEXT,
                    symbol TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_tokens (
                    mint TEXT PRIMARY KEY,
                    last_seen TIMESTAMP DEFAULT NOW()
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id SERIAL PRIMARY KEY,
                    mint TEXT,
                    ts TIMESTAMP DEFAULT NOW(),
                    price NUMERIC,
                    market_cap NUMERIC,
                    volume_24h NUMERIC
                );
            """)

    async def was_notified(self, mint: str, alert_type: Optional[str]=None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                r = await conn.fetchrow("SELECT 1 FROM notifications WHERE mint=$1 AND alert_type=$2", mint, alert_type)
            else:
                r = await conn.fetchrow("SELECT 1 FROM notifications WHERE mint=$1", mint)
            return bool(r)

    async def mark_notified(self, mint: str, alert_type: str, symbol: Optional[str]=None):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO notifications(mint, alert_type, symbol)
                VALUES($1,$2,$3)
                ON CONFLICT (mint) DO UPDATE SET alert_type=EXCLUDED.alert_type, symbol=EXCLUDED.symbol, created_at=NOW()
            """, mint, alert_type, symbol)

    async def seen_recent(self, mint: str, minutes: int = 5) -> bool:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow("""SELECT 1 FROM seen_tokens WHERE mint=$1 AND last_seen >= NOW() - INTERVAL '1 minute' * $2""", mint, minutes)
            return bool(r)

    async def mark_seen(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""INSERT INTO seen_tokens(mint) VALUES($1) ON CONFLICT (mint) DO UPDATE SET last_seen=NOW()""", mint)

    async def add_metric(self, mint: str, price: Optional[float], mc: Optional[float], vol: Optional[float]):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO metrics(mint, price, market_cap, volume_24h) VALUES($1,$2,$3,$4)", mint, price, mc, vol)

# ---------- HTTP client ----------
class HTTP:
    def __init__(self):
        self.session = aiohttp.ClientSession()

    async def get(self, url: str, params: dict = None, timeout: int = 6):
        try:
            async with self.session.get(url, params=params, timeout=timeout) as r:
                text = await r.text()
                if r.status == 200:
                    try:
                        return json.loads(text)
                    except Exception:
                        return text
                logging.warning("HTTP %s -> %s", r.status, (text or "")[:300])
        except Exception as e:
            logging.exception("HTTP GET error %s %s", url, e)
        return None

    async def close(self):
        await self.session.close()

# ---------- Jupiter helpers (robust) ----------
class Jupiter:
    def __init__(self, http: HTTP, base: str = None):
        self.http = http
        self.base = (base or JUPITER_API_URL).rstrip("/")

    async def search(self, query: str):
        url = f"{self.base}/search"
        return await self.http.get(url, params={"query": query})

    async def recent(self, limit: int = 30):
        url = f"{self.base}/recent"
        return await self.http.get(url, params={"limit": limit})

    async def toptrending(self, interval: str = "5m", limit: int = 20):
        url = f"{self.base}/toptrending/{interval}"
        return await self.http.get(url, params={"limit": limit})

    async def boosted(self, limit: int = 50):
        # endpoint requested: price/jup-boosted (may be available on lite/pro)
        url = f"{self.base}/price/jup-boosted"
        return await self.http.get(url, params={"limit": limit})

# ---------- Telegram notifier ----------
class Notifier:
    def __init__(self, app: Application):
        self.app = app
        self.silent = False

    async def send(self, text: str, buttons: Optional[List[tuple]] = None):
        if self.silent:
            logging.info("Silent enabled - skipping message")
            return
        if not TELEGRAM_CHAT_ID:
            logging.warning("TELEGRAM_CHAT_ID not set")
            return
        markup = None
        if buttons:
            kb = [[InlineKeyboardButton(lbl, url=url)] for lbl,url in buttons]
            markup = InlineKeyboardMarkup(kb)
        try:
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            logging.exception("Telegram send failed %s", e)

def build_buttons(mint: str):
    return [
        ("Swap Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
        ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
        ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"),
        ("Go+ Security", f"https://gopluslabs.io/token-security/{mint}")
    ]

# ---------- MONITORS: PumpFun (WSS) + PreExplosion (Jupiter boosted + recent/trending) ----------
class PumpFun:
    def __init__(self, db: PG, http: HTTP, notifier: Notifier):
        self.db = db
        self.http = http
        self.notifier = notifier
        self.running = False
        self.task = None

    async def fetch_token_info(self, mint: str):
        j = Jupiter(self.http)
        data = await j.search(mint)
        # normalize possible shapes
        if isinstance(data, dict):
            tokens = data.get("tokens") or data.get("data") or data.get("token") or []
        elif isinstance(data, list):
            tokens = data
        else:
            tokens = []
        if tokens and isinstance(tokens, list) and len(tokens) > 0:
            t = tokens[0]
            # parse common fields conservatively
            price = t.get("usdPrice") or t.get("price") or t.get("usd_price")
            mc = t.get("mcap") or t.get("mcapUsd") or t.get("marketCap")
            vol = t.get("volume24h") or t.get("volume24hUsd") or t.get("liquidity")
            sym = t.get("symbol") or t.get("name") or "UNK"
            organic = t.get("organicScore") or t.get("organic_score")
            return {"symbol": sym, "price": price, "market_cap": mc, "volume_24h": vol, "organic_score": organic, "raw": t}
        return None

    async def handle_ws_raw(self, raw):
        # extract mint-like strings
        text = raw if isinstance(raw, str) else json.dumps(raw)
        mints = MINT_RE.findall(text)
        for mint in set(mints):
            if await self.db.seen_recent(mint):
                continue
            await self.db.mark_seen(mint)
            logging.info("New mint detected (WSS): %s", mint)
            info = await self.fetch_token_info(mint)
            if not info:
                continue
            price, mc, vol = info.get("price"), info.get("market_cap"), info.get("volume_24h")
            try:
                mc_val = float(mc) if mc is not None else None
            except Exception:
                mc_val = None
            # store metric
            try:
                await self.db.add_metric(mint, price=float(price) if price else None, mc=mc_val, vol=float(vol) if vol else None)
            except Exception:
                # ignore storage errors
                pass
            # alert early if MC threshold reached (no holders check)
            if mc_val and mc_val >= PUMP_MC_MIN and not await self.db.was_notified(mint, "pump_fun"):
                sym = info.get("symbol")
                text_msg = (f"🚀 <b>PUMP.FUN - Nuevo pool detectado (WSS)</b>\n\n"
                            f"<b>{safe_html(sym)}</b>\n"
                            f"• Precio: {price or 'N/A'}\n"
                            f"• Market Cap: ${mc_val:,.0f}\n\n"
                            f"<b>Mint:</b>\n<code>{mint}</code>\n\n"
                            f"• Enlaces para ver rápido y decidir:")
                buttons = build_buttons(mint)
                await self.notifier.send(text_msg, buttons)
                await self.db.mark_notified(mint, "pump_fun", sym)

    async def run(self):
        if not QUICKNODE_WSS_URL:
            logging.warning("QUICKNODE_WSS_URL no configurada, omitiendo PumpFun WSS monitor")
            return
        self.running = True
        backoff = 1
        while self.running:
            try:
                logging.info("Connecting to QuickNode WSS...")
                async with websockets.connect(QUICKNODE_WSS_URL, ping_interval=30, ping_timeout=10) as ws:
                    logging.info("WSS connected")
                    backoff = 1
                    async for msg in ws:
                        if not self.running:
                            break
                        try:
                            await self.handle_ws_raw(msg)
                        except Exception as e:
                            logging.exception("Error handling ws raw %s", e)
            except Exception as e:
                logging.exception("WSS error %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("PumpFun monitor started")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("PumpFun monitor stopped")

class PreExplosion:
    def __init__(self, db: PG, http: HTTP, notifier: Notifier):
        self.db = db
        self.http = http
        self.notifier = notifier
        self.running = False
        self.task = None

    async def analyze_token_obj(self, t: dict):
        # t is a Jupiter token object (various names)
        mint = t.get("id") or t.get("mint") or t.get("address")
        if not mint:
            return
        if await self.db.was_notified(mint, "pre_explosion"):
            return
        mc = t.get("mcap") or t.get("marketCap") or t.get("mcapUsd")
        try:
            mc_val = float(mc) if mc is not None else None
        except Exception:
            mc_val = None
        if not mc_val or mc_val < PRE_EXPLOSION_MC_MIN:
            return

        stats5 = t.get("stats5m") or t.get("stats_5m") or {}
        # parse robustly
        volume_change = stats5.get("volumeChange") or stats5.get("volume_change") or stats5.get("volumeChangePercent") or 0
        num_traders = stats5.get("numTraders") or stats5.get("num_traders") or 0
        price_chg = stats5.get("priceChange") or stats5.get("price_change") or 0
        # heuristic thresholds (tunable)
        try:
            vc = float(volume_change)
        except Exception:
            vc = 0.0
        try:
            nt = float(num_traders)
        except Exception:
            nt = 0.0
        try:
            pc = float(price_chg)
        except Exception:
            pc = 0.0

        # Main heuristic: strong 5m volume spike + rising traders + compressed price (small move)
        if vc > 100 and nt > 10 and abs(pc) < 20:
            # final check: also consult boosted endpoint to confirm heat if available
            boosted_ok = False
            try:
                j = Jupiter(self.http)
                boosted = await j.boosted(limit=200)
                # boosted can be dict or list or nested; search for mint
                if isinstance(boosted, dict):
                    items = boosted.get("tokens") or boosted.get("data") or boosted.get("items") or boosted
                else:
                    items = boosted
                if isinstance(items, list):
                    for it in items:
                        mid = it.get("id") or it.get("mint") or it.get("address")
                        if mid and mid == mint:
                            boosted_ok = True
                            break
            except Exception:
                boosted_ok = False

            # Prepare message and send
            sym = t.get("symbol") or t.get("name") or "UNK"
            txt = (f"🔥 <b>PRE-EXPLOSIÓN</b>\n\n"
                   f"<b>{safe_html(sym)}</b>\n"
                   f"• MC: ${mc_val:,.0f}\n"
                   f"• Vol5m: {vc}\n"
                   f"• Traders5m: {int(nt)}\n"
                   f"• PriceChg5m: {pc:.2f}%\n"
                   f"{'• Boosted: Sí\n' if boosted_ok else ''}"
                   f"\n<b>Mint:</b>\n<code>{mint}</code>\n\n"
                   f"• Revisa rápidamente con los botones y decide manualmente.")
            buttons = build_buttons(mint)
            await self.notifier.send(txt, buttons)
            await self.db.mark_notified(mint, "pre_explosion", sym)
            await self.db.add_metric(mint, price=None, mc=mc_val, vol=None)

    async def scan_once(self):
        j = Jupiter(self.http)
        # gather candidates: recent + toptrending + boosted
        res = []
        try:
            recent = await j.recent(limit=40)
            if isinstance(recent, dict):
                res += (recent.get("tokens") or recent.get("data") or [])
            elif isinstance(recent, list):
                res += recent
        except Exception:
            pass
        try:
            trending = await j.toptrending(interval="5m", limit=40)
            if isinstance(trending, dict):
                res += (trending.get("tokens") or trending.get("data") or [])
            elif isinstance(trending, list):
                res += trending
        except Exception:
            pass
        # boosted - prioritized
        try:
            boosted = await j.boosted(limit=200)
            if isinstance(boosted, dict):
                res += (boosted.get("tokens") or boosted.get("data") or boosted.get("items") or [])
            elif isinstance(boosted, list):
                res += boosted
        except Exception:
            pass

        # unify by mint
        uniq = {}
        for t in res:
            if not isinstance(t, dict):
                continue
            mid = t.get("id") or t.get("mint") or t.get("address")
            if mid:
                uniq[mid] = t

        logging.info("PreExplosion scan candidates: %d", len(uniq))
        for t in uniq.values():
            try:
                await self.analyze_token_obj(t)
            except Exception as e:
                logging.exception("Error analyzing token %s", e)

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self.scan_once()
            except Exception as e:
                logging.exception("PreExplosion run error %s", e)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())
        logging.info("PreExplosion scanner started")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logging.info("PreExplosion scanner stopped")

# ---------- APP wiring (FastAPI + Telegram) ----------
app = FastAPI()
pg = PG()
http = HTTP()
telegram_app: Optional[Application] = None
notifier: Optional[Notifier] = None
pump: Optional[PumpFun] = None
prex: Optional[PreExplosion] = None

def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)

# Telegram command handlers
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    msg = ("🤖 <b>SOLANA MONITOR - Pump.fun + Pre-Explosión</b>\n\n"
           "Comandos:\n/iniciar - Iniciar monitores\n/detener - Detener monitores\n/status - Estado\n/silent on|off - Silenciar\n")
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text("✅ Iniciando monitores...", parse_mode="HTML")
    if pump:
        await pump.start()
    if prex:
        await prex.start()
    await update.message.reply_text("✅ Monitores iniciados", parse_mode="HTML")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if pump:
        await pump.stop()
    if prex:
        await prex.stop()
    await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    status = (f"WSS Pump: {'🟢' if pump and pump.running else '🔴'}\n"
              f"Pre-Explosion: {'🟢' if prex and prex.running else '🔴'}\n"
              f"PUMP_MC_MIN: ${PUMP_MC_MIN:,.0f}\n"
              f"PRE_EXPLOSION_MC_MIN: ${PRE_EXPLOSION_MC_MIN:,.0f}\n"
              f"CHECK_INTERVAL_S: {CHECK_INTERVAL_SECONDS}\n")
    await update.message.reply_text(status, parse_mode="HTML")

async def cmd_silent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    args = ctx.args or []
    if not args or args[0] not in ("on","off"):
        await update.message.reply_text("Uso: /silent on|off")
        return
    notifier.silent = (args[0] == "on")
    await update.message.reply_text(f"Silent: {'ON' if notifier.silent else 'OFF'}")

# webhook endpoint
@app.post("/webhook/{token}")
async def webhook(token: str, req: Request):
    if token != TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=403, detail="invalid token")
    body = await req.json()
    update = Update.de_json(body, telegram_app.bot)
    # only owner interactions processed
    if update.effective_user and update.effective_user.id != OWNER_ID:
        return JSONResponse({"ok": True, "note": "ignored - not owner"})
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok", "ts": now_iso()}

# startup/shutdown
@app.on_event("startup")
async def startup():
    global telegram_app, notifier, pump, prex
    # validate envs
    missing = []
    for k in ("TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","OWNER_ID","DATABASE_URL"):
        if not globals().get(k):
            missing.append(k)
    if missing:
        raise RuntimeError("Missing env: " + ", ".join(missing))

    # connect pg
    await pg.connect(DATABASE_URL)

    # init telegram app (used to send messages and process webhook updates)
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("silent", cmd_silent))
    # initialize notifier & monitors
    notifier = Notifier(telegram_app)
    pump = PumpFun(pg, http, notifier)
    prex = PreExplosion(pg, http, notifier)

    # initialize telegram app runtime (so it can send messages)
    await telegram_app.initialize()
    await telegram_app.start()
    logging.info("Application startup complete - ready. Use /iniciar to start monitors.")

@app.on_event("shutdown")
async def shutdown():
    logging.info("Shutting down monitors...")
    if pump:
        await pump.stop()
    if prex:
        await prex.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    await http.close()
    if pg.pool:
        await pg.pool.close()
    logging.info("Shutdown complete")

# run local dev
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("telegram_bot_final_v7_pro:app", host="0.0.0.0", port=PORT, log_level="info")
