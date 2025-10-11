# telegram_bot_final.py
# Solana Monitor Bot v6.1 Pro Optimized
# Webhook-compatible FastAPI app for Railway
# Requirements (add to requirements.txt): aiohttp asyncpg websockets python-telegram-bot[asyncio] uvicorn gunicorn apscheduler

import os
import json
import re
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# -----------------
# CONFIG from ENV
# -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL", "")  # <-- put your QuickNode WSS here
APP_URL = os.getenv("APP_URL", "")  # optional: https://your-app.railway.app

PORT = int(os.getenv("PORT", "8080"))

# thresholds and tuning (can be changed at runtime via commands)
PUMP_PRE_MIN = float(os.getenv("PUMP_PRE_MIN", "60000"))
PUMP_PRE_MAX = float(os.getenv("PUMP_PRE_MAX", "65000"))
PUMP_MIN_HOLDERS = int(os.getenv("PUMP_MIN_HOLDERS", "40"))

FLAT_MIN_AGE_H = int(os.getenv("FLAT_MIN_AGE_H", "72"))
FLAT_VOL_PCT = float(os.getenv("FLAT_VOL_PCT", "12.0"))

CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "5"))
PRE_BREAK_MULT = float(os.getenv("PRE_BREAK_MULT", "2.5"))

DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest")
RAYDIUM_API_BASE = os.getenv("RAYDIUM_API_BASE", "https://api.raydium.io/v2")

CACHE_DB = os.getenv("CACHE_DB", "/data/cache_v6.db")
os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)

# -----------------
# Utils
# -----------------
def now_ts() -> datetime:
    return datetime.utcnow()

def safe_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def json_log(event: str, **k):
    print(json.dumps({"ts": now_ts().isoformat(), "event": event, **k}, ensure_ascii=False))

# -----------------
# Simple sqlite cache for dedupe + marketcap caching
# -----------------
class Cache:
    def __init__(self, path=CACHE_DB):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS mcap(mint TEXT PRIMARY KEY, mc REAL, ts TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS seen(mint TEXT PRIMARY KEY, ts TEXT)")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS notified(mint TEXT PRIMARY KEY, kind TEXT, symbol TEXT, notified_at TEXT, meta TEXT)
        """)
        self.conn.commit()
    def get_mc(self, mint):
        cur = self.conn.execute("SELECT mc FROM mcap WHERE mint=?", (mint,))
        r = cur.fetchone()
        return r[0] if r else None
    def set_mc(self, mint, mc):
        self.conn.execute("REPLACE INTO mcap(mint,mc,ts) VALUES(?,?,?)", (mint, mc, now_ts().isoformat()))
        self.conn.commit()
    def seen_recent(self, mint):
        r = self.conn.execute("SELECT 1 FROM seen WHERE mint=?", (mint,)).fetchone()
        return bool(r)
    def mark_seen(self, mint):
        self.conn.execute("REPLACE INTO seen(mint,ts) VALUES(?,?)", (mint, now_ts().isoformat()))
        self.conn.commit()
    def was_notified(self, mint):
        r = self.conn.execute("SELECT 1 FROM notified WHERE mint=?", (mint,)).fetchone()
        return bool(r)
    def mark_notified(self, mint, kind, symbol, meta):
        self.conn.execute("REPLACE INTO notified(mint,kind,symbol,notified_at,meta) VALUES(?,?,?,?,?)",
                          (mint, kind, symbol, now_ts().isoformat(), json.dumps(meta)))
        self.conn.commit()

# -----------------
# Http client
# -----------------
class HttpClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
    async def get_json(self, url, params=None, headers=None, timeout=12):
        try:
            async with self.s.get(url, params=params, headers=headers, timeout=timeout) as r:
                text = await r.text()
                if r.status == 200:
                    try:
                        return json.loads(text)
                    except Exception:
                        return {}
                json_log("http_error", url=url, status=r.status, text=(text or "")[:300])
        except Exception as e:
            json_log("http_exc", url=url, err=str(e))
        return {}

# -----------------
# API wrappers
# -----------------
class DexScreenerClient:
    def __init__(self, http: HttpClient, base=DEXSCREENER_API):
        self.http = http
        self.base = base.rstrip("/")
    async def token_info(self, mint: str) -> Dict[str, Any]:
        # Dexscreener token endpoint
        return await self.http.get_json(f"{self.base}/tokens/{mint}")
    async def token_boosts(self) -> Dict[str,Any]:
        return await self.http.get_json(f"{self.base}/token-boosts/latest/v1")
    async def token_profiles(self) -> Dict[str,Any]:
        return await self.http.get_json(f"{self.base}/token-profiles/latest/v1")
    async def get_pairs_recent(self, limit=100) -> List[Dict[str,Any]]:
        # optional: list of pairs endpoint
        return await self.http.get_json(f"{self.base}/pairs")

class RaydiumClient:
    def __init__(self, http: HttpClient, base=RAYDIUM_API_BASE):
        self.http = http
        self.base = base.rstrip("/")
    async def get_new_pools(self) -> List[Dict[str,Any]]:
        # returns structured pools list (as in v5)
        r = await self.http.get_json(f"{self.base}/sdk/liquidity/mainnet.json")
        return r.get("official", []) if isinstance(r, dict) else []

# -----------------
# Notifier
# -----------------
class TelegramNotifier:
    def __init__(self, app):
        self.app = app
    async def send(self, text: str, buttons: Optional[List[Tuple[str,str]]] = None):
        if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == 0:
            json_log("no_chatid")
            return
        markup = None
        if buttons:
            kb = [[InlineKeyboardButton(lbl, url=url)] for lbl, url in buttons]
            markup = InlineKeyboardMarkup(kb)
        try:
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML",
                                            disable_web_page_preview=False, reply_markup=markup)
        except Exception as e:
            json_log("tg_send_err", error=str(e))

# -----------------
# Core logic
# -----------------
class Core:
    def __init__(self, http: HttpClient, dbpool, cache: Cache, notifier: TelegramNotifier):
        self.http = http
        self.db = dbpool
        self.cache = cache
        self.notifier = notifier
        self.dex = DexScreenerClient(http)
        self.raydium = RaydiumClient(http)

    async def get_marketcap(self, mint):
        mc = self.cache.get_mc(mint)
        if mc is not None:
            return mc
        try:
            r = await self.dex.token_info(mint)
            token = r.get("token") or {}
            mc = token.get("marketCapUsd") or token.get("marketCap")
            if mc:
                mc = float(mc)
                self.cache.set_mc(mint, mc)
                return mc
        except Exception:
            pass
        return None

    async def fetch_token_info(self, mint):
        info = {}
        try:
            info = await self.dex.token_info(mint) or {}
        except Exception:
            pass
        return info

# -----------------
# WSS Monitor for pump detection
# -----------------
class WSSPumpMonitor:
    def __init__(self, wss_url: str, core: Core):
        self.wss = wss_url
        self.core = core
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self._ws = None

    def extract_mints(self, msg: Dict[str,Any]) -> List[str]:
        txt = json.dumps(msg)
        matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{32,44}', txt)
        return list(dict.fromkeys(matches))

    async def handle_msg(self, msg: Dict[str,Any]):
        mints = self.extract_mints(msg)
        for mint in mints:
            if self.core.cache.seen_recent(mint):
                continue
            self.core.cache.mark_seen(mint)
            mc = await self.core.get_marketcap(mint)
            if mc is None:
                continue
            if PUMP_PRE_MIN <= mc <= PUMP_PRE_MAX:
                info = await self.core.fetch_token_info(mint)
                token = info.get("token") or {}
                holders = int(token.get("holderCount") or token.get("holders") or 0)
                if holders and holders < PUMP_MIN_HOLDERS:
                    continue
                if self.core.cache.was_notified(mint):
                    continue
                sym = token.get("symbol") or "UNK"
                text = (f"🚀 <b>PUMP.PRE-GRAD</b>\n\n"
                        f"<b>{safe_html(sym)}</b>\n"
                        f"• MC: ${mc:,.0f}\n"
                        f"• Holders: {holders}\n"
                        f"<b>Mint:</b>\n<code>{mint}</code>")
                buttons = [("DexScreener", f"https://dexscreener.com/solana/{mint}"),
                           ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
                           ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}")]
                await self.core.notifier.send(text, buttons)
                self.core.cache.mark_notified(mint, "pump_pre", sym, {"mc": mc})

    async def run(self):
        if not self.wss:
            json_log("no_wss_provided")
            return
        self.running = True
        backoff = 1
        while self.running:
            try:
                async with websockets.connect(self.wss, ping_interval=30, ping_timeout=10) as ws:
                    json_log("wss_connected")
                    backoff = 1
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self.handle_msg(msg)
                        except Exception as e:
                            json_log("wss_handle_err", err=str(e))
            except Exception as e:
                json_log("wss_error", err=str(e), backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

# -----------------
# Flat scanner (periodic)
# -----------------
class FlatScanner:
    def __init__(self, core: Core):
        self.core = core
        self.task: Optional[asyncio.Task] = None
        self.running = False

    def percent_abs(self, v):
        try:
            return abs(float(v))
        except Exception:
            return 9999.0

    async def check_candidate(self, mint):
        info = await self.core.fetch_token_info(mint)
        token = info.get("token") or {}
        # try to find 1h/24h changes
        ch1 = token.get("priceChange1h") or token.get("change1h") or token.get("priceChangeHour")
        ch24 = token.get("priceChange24h") or token.get("change24h") or token.get("priceChangeDay")
        ch1_abs = self.percent_abs(ch1) if ch1 is not None else 9999.0
        ch24_abs = self.percent_abs(ch24) if ch24 is not None else 9999.0
        is_flat = (ch1_abs <= FLAT_VOL_PCT) or (ch24_abs <= FLAT_VOL_PCT)
        mc = await self.core.get_marketcap(mint) or 0
        # exclude pump range
        if PUMP_PRE_MIN <= mc <= PUMP_PRE_MAX:
            return False
        # skip if not flat
        if not is_flat:
            return False
        # check age if possible (best-effort)
        # We'll mark flat if not recently seen and ch values small
        if self.core.cache.was_notified(mint):
            return False
        sym = token.get("symbol") or "UNK"
        text = (f"🟦 <b>TOKEN.FLAT</b>\n\n"
                f"<b>{safe_html(sym)}</b>\n"
                f"• MC: ${mc:,.0f}\n"
                f"• Cambios | 1h: {ch1 or 'N/A'}% - 24h: {ch24 or 'N/A'}%\n"
                f"<b>Mint:</b>\n<code>{mint}</code>")
        buttons = [("DexScreener", f"https://dexscreener.com/solana/{mint}")]
        await self.core.notifier.send(text, buttons)
        self.core.cache.mark_notified(mint, "flat", sym, {"mc": mc, "ch1": ch1, "ch24": ch24})
        return True

    async def run(self):
        self.running = True
        async with aiohttp.ClientSession() as session:
            http = HttpClient(session)
            dex = DexScreenerClient(http)
            while self.running:
                try:
                    boosts = await dex.token_boosts()
                    candidates = []
                    if isinstance(boosts, dict):
                        for t in boosts.get("tokens", []):
                            addr = t.get("address")
                            if addr:
                                candidates.append(addr)
                    profiles = await dex.token_profiles()
                    if isinstance(profiles, dict):
                        for k in (profiles.get("tokens") or {}).keys():
                            candidates.append(k)
                    candidates = list(dict.fromkeys([c for c in candidates if c]))
                    json_log("flat_scan_candidates", count=len(candidates))
                    for m in candidates:
                        try:
                            await self.check_candidate(m)
                        except Exception as e:
                            json_log("flat_check_err", err=str(e), mint=m)
                    await asyncio.sleep(max(30, CHECK_INTERVAL_MIN*60))
                except Exception as e:
                    json_log("flat_scan_loop_err", err=str(e))
                    await asyncio.sleep(30)

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

# -----------------
# Raydium graduation monitor (pump.fun -> Raydium)
# -----------------
class RaydiumGraduation:
    def __init__(self, core: Core, dbpool, notifier: TelegramNotifier):
        self.core = core
        self.db = dbpool
        self.notifier = notifier
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.seen = set()

    async def check_once(self):
        pools = await self.core.raydium.get_new_pools()
        new_found = 0
        for p in pools:
            try:
                pool_id = p.get("id")
                if not pool_id or pool_id in self.seen:
                    continue
                self.seen.add(pool_id)
                if len(self.seen) > 2000:
                    self.seen = set(list(self.seen)[-500:])
                mint = p.get("baseMint")
                if not mint:
                    continue
                # skip if already notified
                if self.core.cache.was_notified(mint):
                    continue
                mc = await self.core.get_marketcap(mint)
                if mc is None:
                    continue
                if not (PUMP_PRE_MIN <= mc <= PUMP_PRE_MAX):
                    continue
                info = await self.core.fetch_token_info(mint)
                token = info.get("token") or {}
                holders = int(token.get("holderCount") or token.get("holders") or 0)
                if holders and holders < PUMP_MIN_HOLDERS:
                    continue
                # Detect pump.fun origin if present in pairs
                from_pump = False
                for pair in info.get("pairs", []) if isinstance(info.get("pairs", []), list) else []:
                    url = (pair.get("url") or "").lower()
                    dex_id = (pair.get("dexId") or "").lower()
                    if "pump.fun" in url or "pump.fun" in dex_id:
                        from_pump = True
                        break
                if not from_pump:
                    continue
                sym = token.get("symbol") or "UNK"
                text = (f"🔥 <b>PUMP.FUN → RAYDIUM GRAD</b>\n\n"
                        f"<b>{safe_html(sym)}</b>\n"
                        f"• MC: ${mc:,.0f}\n"
                        f"• Holders: {holders}\n"
                        f"<b>Mint:</b>\n<code>{mint}</code>")
                buttons = [("DexScreener", f"https://dexscreener.com/solana/{mint}"),
                           ("Raydium", f"https://raydium.io/swap/?inputCurrency=sol&outputCurrency={mint}"),
                           ("Jupiter", f"https://jup.ag/swap/{mint}-SOL")]
                await self.notifier.send(text, buttons)
                self.core.cache.mark_notified(mint, "raydium_grad", sym, {"mc": mc})
                new_found += 1
            except Exception as e:
                json_log("raydium_process_err", err=str(e))
        if new_found:
            json_log("raydium_check", found=new_found)

    async def loop(self):
        self.running = True
        while self.running:
            try:
                await self.check_once()
            except Exception as e:
                json_log("raydium_loop_err", err=str(e))
            await asyncio.sleep(max(10, CHECK_INTERVAL_MIN*60//2))

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

# -----------------
# Telegram Commands & FastAPI wiring
# -----------------
app = FastAPI()
telegram_app: Optional[Application] = None
db_pool: Optional[asyncpg.pool.Pool] = None
http_session: Optional[aiohttp.ClientSession] = None
http_client: Optional[HttpClient] = None
cache = Cache()
notifier: Optional[TelegramNotifier] = None
core: Optional[Core] = None
wss_monitor: Optional[WSSPumpMonitor] = None
flat_scanner: Optional[FlatScanner] = None
raydium_grad: Optional[RaydiumGraduation] = None
scheduler: Optional[AsyncIOScheduler] = None

# Handlers
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    text = "🤖 Solana Monitor Bot v6.1\nComandos: /iniciar /detener /status /set_pump <min> <max> /set_flat <hours> <pct> /reconnect_wss"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    # start monitors
    if wss_monitor:
        await wss_monitor.start()
    if flat_scanner:
        await flat_scanner.start()
    if raydium_grad:
        await raydium_grad.start()
    await update.message.reply_text("✅ Monitores iniciados", parse_mode="HTML")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    if wss_monitor:
        await wss_monitor.stop()
    if flat_scanner:
        await flat_scanner.stop()
    if raydium_grad:
        await raydium_grad.stop()
    await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    wss_ok = (wss_monitor and wss_monitor.task and not wss_monitor.task.done())
    text = (f"<b>Estado</b>\nWSS: {'🟢' if wss_ok else '🔴'}\n"
            f"PUMP_PRE: {PUMP_PRE_MIN}-{PUMP_PRE_MAX}\n"
            f"FLAT_MIN_AGE_H: {FLAT_MIN_AGE_H}h\nFLAT_VOL_PCT: {FLAT_VOL_PCT}%")
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_set_pump(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global PUMP_PRE_MIN, PUMP_PRE_MAX
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    try:
        args = ctx.args
        if len(args) != 2:
            raise ValueError("Uso: /set_pump <min> <max>")
        PUMP_PRE_MIN = float(args[0])
        PUMP_PRE_MAX = float(args[1])
        await update.message.reply_text(f"Rango pump ajustado: {PUMP_PRE_MIN} - {PUMP_PRE_MAX}", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}", parse_mode="HTML")

async def cmd_set_flat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global FLAT_MIN_AGE_H, FLAT_VOL_PCT
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    try:
        args = ctx.args
        if len(args) != 2:
            raise ValueError("Uso: /set_flat <hours> <pct>")
        FLAT_MIN_AGE_H = int(args[0])
        FLAT_VOL_PCT = float(args[1])
        await update.message.reply_text(f"Flat params: age={FLAT_MIN_AGE_H}h pct={FLAT_VOL_PCT}%", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}", parse_mode="HTML")

async def cmd_reconnect_wss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    if not wss_monitor:
        await update.message.reply_text("WSS monitor no configurado", parse_mode="HTML")
        return
    await wss_monitor.stop()
    await asyncio.sleep(1)
    await wss_monitor.start()
    await update.message.reply_text("Reconectando WSS...", parse_mode="HTML")

# webhook endpoint
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, req: Request):
    try:
        if token != TELEGRAM_BOT_TOKEN:
            raise HTTPException(status_code=403, detail="invalid token")
        body = await req.json()
        update = Update.de_json(body, telegram_app.bot)
        # ignore others by user id
        if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
            return JSONResponse({"ok": True, "note": "ignored - not owner"})
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        json_log("webhook_error", error=str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/test")
async def test():
    return {"ok": True, "chat_id": TELEGRAM_CHAT_ID}

# -----------------
# Startup / Shutdown
# -----------------
async def init_app():
    global telegram_app, db_pool, http_session, http_client, notifier, core, wss_monitor, flat_scanner, raydium_grad, scheduler

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID not set")

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("set_pump", cmd_set_pump))
    telegram_app.add_handler(CommandHandler("set_flat", cmd_set_flat))
    telegram_app.add_handler(CommandHandler("reconnect_wss", cmd_reconnect_wss))

    # DB pool (Postgres preferred)
    db_pool = None
    if DATABASE_URL:
        try:
            db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=6)
            json_log("postgres_pool_ready")
            # ensure notified table exists (in case user uses Postgres)
            async with db_pool.acquire() as conn:
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS notified_tokens (
                    mint TEXT PRIMARY KEY,
                    kind TEXT,
                    symbol TEXT,
                    notified_at TIMESTAMPTZ,
                    meta JSONB
                );
                """)
        except Exception as e:
            json_log("postgres_err", err=str(e))
            db_pool = None

    http_session = aiohttp.ClientSession()
    http_client = HttpClient(http_session)
    notifier = TelegramNotifier(telegram_app)
    core = Core(http_client, db_pool, cache, notifier)

    # monitors
    wss_monitor = WSSPumpMonitor(HELIUS_WSS_URL, core) if HELIUS_WSS_URL else None
    flat_scanner = FlatScanner(core)
    raydium_grad = RaydiumGraduation(core, db_pool, notifier)

    # schedule a summary
    scheduler = AsyncIOScheduler()
    async def send_summary():
        try:
            cnt = 0
            if db_pool:
                async with db_pool.acquire() as conn:
                    cnt = await conn.fetchval("SELECT COUNT(*) FROM notified_tokens WHERE notified_at >= $1", now_ts() - timedelta(hours=24))
            else:
                # fallback to sqlite cache
                cur = cache.conn.execute("SELECT COUNT(*) FROM notified")
                cnt = cur.fetchone()[0] if cur else 0
            await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"📰 Resumen 24h: {cnt} alerts", parse_mode="HTML")
        except Exception as e:
            json_log("summary_err", err=str(e))
    scheduler.add_job(lambda: asyncio.create_task(send_summary()), "interval", hours=8)
    scheduler.start()

@app.on_event("startup")
async def on_startup():
    json_log("startup")
    await init_app()
    # register webhook with Telegram if APP_URL provided
    if APP_URL:
        webhook_url = f"{APP_URL.rstrip('/')}/webhook/{TELEGRAM_BOT_TOKEN}"
        try:
            await telegram_app.bot.set_webhook(webhook_url)
            json_log("webhook_set", url=webhook_url)
        except Exception as e:
            json_log("webhook_set_err", err=str(e))
    await telegram_app.initialize()
    await telegram_app.start()
    # start monitors
    if wss_monitor:
        await wss_monitor.start()
    if flat_scanner:
        await flat_scanner.start()
    if raydium_grad:
        await raydium_grad.start()
    json_log("monitors_started")

@app.on_event("shutdown")
async def on_shutdown():
    json_log("shutdown_start")
    if wss_monitor:
        await wss_monitor.stop()
    if flat_scanner:
        await flat_scanner.stop()
    if raydium_grad:
        await raydium_grad.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    if http_session:
        await http_session.close()
    if db_pool:
        await db_pool.close()
    if scheduler:
        scheduler.shutdown(wait=False)
    json_log("shutdown_done")

# -----------------
# If run directly (useful local dev)
# -----------------
def run_uvicorn():
    import uvicorn
    uvicorn.run("telegram_bot_final:app", host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    # local run (dev)
    asyncio.run(init_app())
    run_uvicorn()
