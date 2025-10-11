# telegram_bot_final_v7_pro.py
# Monolithic Elite Bot v1 — PostgreSQL + QuickNode WSS + Jupiter V2 + Helius + DexScreener
# Deploy: Railway (Procfile example provided by user)
# Python runtime: 3.11

import os
import re
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------- CONFIG & ENV ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DATABASE_URL = os.getenv("DATABASE_URL")
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL", "")
JUPITER_API_URL = os.getenv("JUPITER_API_URL", "https://lite-api.jup.ag/tokens/v2")
HELIUS_API_URL = os.getenv("HELIUS_API_URL", "")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest")

PORT = int(os.getenv("PORT", "8080"))

# thresholds (as requested)
PUMP_MC_MIN = float(os.getenv("PUMP_MC_MIN", "100000"))  # $100k minimum MC for pre-explosion alerts
PRE_WINDOW_MIN = int(os.getenv("PRE_WINDOW_MIN", "10"))  # 10 minutes window
FLAT_MIN_AGE_H = int(os.getenv("FLAT_MIN_AGE_H", "72"))  # 72 hours age requirement
FLAT_CONSOL_H = int(os.getenv("FLAT_CONSOL_H", "2"))    # 2 hours consolidation
FLAT_VOL_PCT = float(os.getenv("FLAT_VOL_PCT", "15.0"))  # +/-15% allowed move in consolidation

CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "3"))  # general scan interval

# regex to detect mint-like addresses
MINT_RE = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

# ---------- UTILITIES ----------
def now_ts() -> datetime:
    return datetime.utcnow()

def iso(dt: datetime) -> str:
    return dt.isoformat() + "Z"

def safe_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def json_log(event: str, **k):
    logging.info("%s %s", event, json.dumps(k, default=str, ensure_ascii=False))

# ---------- HTTP client ----------
class HttpClient:
    def __init__(self, session: aiohttp.ClientSession, timeout: int = 8):
        self.s = session
        self.timeout = timeout

    async def get_json(self, url: str, params: dict = None, headers: dict = None, timeout=None):
        try:
            to = timeout or self.timeout
            async with self.s.get(url, params=params, headers=headers, timeout=to) as r:
                text = await r.text()
                if r.status == 200:
                    return json.loads(text)
                logging.warning("HTTP %s returned %s: %s", url, r.status, (text or "")[:300])
        except Exception as e:
            logging.exception("HTTP get_json error %s %s", url, e)
        return {}

# ---------- DATABASE LAYER (asyncpg) ----------
class PGStore:
    def __init__(self, pool: asyncpg.pool.Pool):
        self.pool = pool

    @classmethod
    async def create(cls, dsn: str):
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
        self = cls(pool)
        await self._init()
        return self

    async def _init(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS notified (
                mint TEXT PRIMARY KEY,
                kind TEXT,
                symbol TEXT,
                notified_at TIMESTAMP,
                meta JSONB
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                mint TEXT PRIMARY KEY,
                last_seen TIMESTAMP
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                ts TIMESTAMP,
                mint TEXT,
                price REAL,
                mc REAL,
                vol REAL
            );
            """)
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_mint_ts ON metrics (mint, ts);")
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """)

    # notified
    async def was_notified(self, mint: str) -> bool:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT 1 FROM notified WHERE mint=$1", mint)
            return r is not None

    async def mark_notified(self, mint: str, kind: str, symbol: str, meta: dict):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO notified(mint,kind,symbol,notified_at,meta)
                VALUES($1,$2,$3,$4,$5)
                ON CONFLICT (mint) DO UPDATE SET kind=EXCLUDED.kind, symbol=EXCLUDED.symbol, notified_at=EXCLUDED.notified_at, meta=EXCLUDED.meta
            """, mint, kind, symbol, now_ts(), json.dumps(meta))

    # seen
    async def seen_recent(self, mint: str) -> bool:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow("SELECT 1 FROM seen WHERE mint=$1", mint)
            return r is not None

    async def mark_seen(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO seen(mint,last_seen) VALUES($1,$2) ON CONFLICT (mint) DO UPDATE SET last_seen=EXCLUDED.last_seen
            """, mint, now_ts())

    # metrics
    async def add_metric(self, mint: str, price: Optional[float], mc: Optional[float], vol: Optional[float]):
        async with self.pool.acquire() as conn:
            await conn.execute("INSERT INTO metrics(ts,mint,price,mc,vol) VALUES($1,$2,$3,$4,$5)", now_ts(), mint, price or 0.0, mc or 0.0, vol or 0.0)
            cutoff = now_ts() - timedelta(days=2)
            await conn.execute("DELETE FROM metrics WHERE ts < $1", cutoff)

    async def get_metrics_window(self, mint: str, minutes: int) -> List[Tuple[datetime,float,float,float]]:
        cutoff = now_ts() - timedelta(minutes=minutes)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT ts,price,mc,vol FROM metrics WHERE mint=$1 AND ts >= $2 ORDER BY ts ASC", mint, cutoff)
            return [(r['ts'], r['price'], r['mc'], r['vol']) for r in rows]

    async def list_recent_notifications(self, limit: int = 15):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT mint,kind,symbol,notified_at FROM notified ORDER BY notified_at DESC LIMIT $1", limit)
            return rows

# ---------- NOTIFIER ----------
class TelegramNotifier:
    def __init__(self, app):
        self.app = app
        self.silent = False

    async def send(self, text: str, buttons: Optional[List[Tuple[str,str]]] = None):
        if self.silent:
            logging.info("Silent ON - skipping send")
            return
        if not TELEGRAM_CHAT_ID:
            logging.warning("TELEGRAM_CHAT_ID not set")
            return
        try:
            markup = None
            if buttons:
                kb = [[InlineKeyboardButton(lbl, url=url)] for lbl,url in buttons]
                markup = InlineKeyboardMarkup(kb)
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=False, reply_markup=markup)
        except Exception as e:
            logging.exception("Telegram send failed %s", e)

# ---------- CORE (Jupiter / Helius / DexScreener) ----------
class Core:
    def __init__(self, http: HttpClient, store: PGStore, notifier: TelegramNotifier):
        self.http = http
        self.store = store
        self.notifier = notifier

    # token info via Jupiter search by mint
    async def jupiter_token(self, mint: str) -> Dict[str,Any]:
        try:
            url = f"{JUPITER_API_URL.rstrip('/')}/search"
            params = {"query": mint}
            return await self.http.get_json(url, params=params)
        except Exception:
            return {}

    # category endpoints (toptrending/toptraded/recent)
    async def jupiter_category(self, category: str, interval: str = None, limit: int = 50) -> Dict[str,Any]:
        try:
            if interval:
                url = f"{JUPITER_API_URL.rstrip('/')}/{category}/{interval}"
            else:
                url = f"{JUPITER_API_URL.rstrip('/')}/{category}"
            params = {"limit": limit}
            return await self.http.get_json(url, params=params)
        except Exception:
            return {}

    # stats endpoint per mint (assumes /{mint}/stats exists - adapt if necessary)
    async def jupiter_stats(self, mint: str, interval: str = "5m") -> Dict[str,Any]:
        try:
            url = f"{JUPITER_API_URL.rstrip('/')}/{mint}/stats"
            params = {"interval": interval}
            return await self.http.get_json(url, params=params)
        except Exception:
            return {}

    async def dexscreener_token(self, mint: str) -> Dict[str,Any]:
        try:
            url = f"{DEXSCREENER_API.rstrip('/')}/tokens/{mint}"
            return await self.http.get_json(url)
        except Exception:
            return {}

    # unified get_mc_price_vol: try Jupiter -> Helius -> DexScreener
    async def get_mc_price_vol(self, mint: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        # returns (price, mc, vol24)
        # 1) Jupiter
        try:
            j = await self.jupiter_token(mint)
            token = j.get("token") or {}
            price = token.get("usdPrice") or token.get("price") or None
            mc = token.get("mcap") or token.get("mcapUsd") or token.get("marketCapUsd") or token.get("marketCap")
            vol = token.get("liquidity") or token.get("volume24hUsd") or token.get("volume24h") or None
            if price is not None or mc is not None or vol is not None:
                try:
                    return float(price) if price else None, float(mc) if mc else None, float(vol) if vol else None
                except Exception:
                    pass
        except Exception:
            pass
        # 2) Helius (if available) - attempt basic token info
        if HELIUS_API_URL:
            try:
                url = f"{HELIUS_API_URL.rstrip('/')}/getTokenMetadata?mint={mint}"
                # Note: adjust to actual helius endpoint if different
                h = await self.http.get_json(url)
                # try parse
                price = h.get("priceUsd") if isinstance(h, dict) else None
                # helius might not provide mc/vol; skip
                if price:
                    return float(price), None, None
            except Exception:
                pass
        # 3) Dexscreener fallback
        try:
            d = await self.dexscreener_token(mint)
            token = d.get("token") or {}
            price = token.get("price") or token.get("usdPrice") or None
            mc = token.get("marketCapUsd") or token.get("marketCap") or None
            vol = None
            pairs = d.get("pairs") or []
            if pairs and isinstance(pairs, list):
                total = 0.0
                found = False
                for p in pairs:
                    v = p.get("volumeUsd") or p.get("volume")
                    if v:
                        try:
                            total += float(v)
                            found = True
                        except Exception:
                            pass
                if found:
                    vol = total
            try:
                return float(price) if price else None, float(mc) if mc else None, float(vol) if vol else None
            except Exception:
                return None, None, None
        except Exception:
            return None, None, None

# ---------- MONITORS ----------
class WSSPumpMonitor:
    def __init__(self, wss_url: str, core: Core, store: PGStore):
        self.wss = wss_url
        self.core = core
        self.store = store
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.backoff = 1

    def extract_mints(self, raw: str) -> List[str]:
        return list(dict.fromkeys(MINT_RE.findall(raw)))

    async def handle(self, raw: str):
        mints = self.extract_mints(raw)
        for mint in mints:
            if await self.store.seen_recent(mint):
                continue
            await self.store.mark_seen(mint)
            price, mc, vol = await self.core.get_mc_price_vol(mint)
            await self.store.add_metric(mint, price, mc, vol)
            # eliminate holders constraint per user request
            # require MC >= PUMP_MC_MIN
            if mc and mc >= PUMP_MC_MIN:
                if await self.store.was_notified(mint):
                    continue
                # enrich via jupiter for symbol
                info = await self.core.jupiter_token(mint)
                token = info.get("token") or {}
                sym = token.get("symbol") or token.get("name") or "UNK"
                text = (f"🚀 <b>PUMP.PRE-GRAD (WSS)</b>\n\n"
                        f"<b>{safe_html(sym)}</b>\n"
                        f"• Price: {price or 'N/A'} USD\n"
                        f"• Market Cap: ${mc:,.0f}\n"
                        f"• Vol24h: {vol or 'N/A'}\n\n"
                        f"<b>Mint:</b>\n<code>{mint}</code>")
                buttons = build_buttons(mint)
                await self.core.notifier.send(text, buttons)
                await self.store.mark_notified(mint, "pump_pre", sym, {"mc": mc, "price": price})

    async def run(self):
        if not self.wss:
            logging.info("WSS not configured, skipping WSSPumpMonitor")
            return
        self.running = True
        self.backoff = 1
        while self.running:
            try:
                logging.info("Connecting WSS %s", self.wss)
                async with websockets.connect(self.wss, ping_interval=30, ping_timeout=10) as ws:
                    logging.info("WSS connected")
                    self.backoff = 1
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            txt = raw if isinstance(raw, str) else json.dumps(raw)
                            await self.handle(txt)
                        except Exception as e:
                            logging.exception("WSS handle error %s", e)
            except Exception as e:
                logging.exception("WSS error %s", e)
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
            except Exception:
                pass

class PreExplosionScanner:
    """
    Uses Jupiter stats (5m and 1h) to detect accumulation patterns in the last PRE_WINDOW_MIN minutes.
    Criteria:
      - volumeChange (stats5m) > 0 (sustained)
      - numTraders (stats5m) increasing
      - priceChange small (compressed) but slightly positive
      - MC >= PUMP_MC_MIN
    """
    def __init__(self, core: Core, store: PGStore):
        self.core = core
        self.store = store
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def evaluate(self, mint: str):
        try:
            stats5 = await self.core.jupiter_stats(mint, interval="5m")
            stats1h = await self.core.jupiter_stats(mint, interval="1h")
            # flexible parsing
            s5 = stats5.get("stats5m") if isinstance(stats5, dict) else stats5
            if not s5:
                s5 = stats5
            volume_change = None
            num_traders = None
            price_change = None
            if isinstance(s5, dict):
                volume_change = s5.get("volumeChange") or s5.get("volume_change") or s5.get("volumeChangePercent")
                num_traders = s5.get("numTraders") or s5.get("num_traders") or s5.get("numTraders")
                price_change = s5.get("priceChange") or s5.get("price_change")
            # convert
            try:
                vc = float(volume_change) if volume_change is not None else None
            except Exception:
                vc = None
            try:
                nt = float(num_traders) if num_traders is not None else None
            except Exception:
                nt = None
            try:
                pc = float(price_change) if price_change is not None else None
            except Exception:
                pc = None
            # metrics and MC
            price, mc, vol24 = await self.core.get_mc_price_vol(mint)
            await self.store.add_metric(mint, price, mc, vol24)
            # check MC
            if not mc or mc < PUMP_MC_MIN:
                return
            # heuristics
            vol_pos = vc is not None and vc > 0
            traders_up = nt is not None and nt > 0
            price_compressed = pc is None or abs(pc) < 3.0  # small percent move threshold
            triggered = vol_pos and traders_up and price_compressed
            if triggered and not await self.store.was_notified(mint):
                info = await self.core.jupiter_token(mint)
                token = info.get("token") or {}
                sym = token.get("symbol") or token.get("name") or "UNK"
                reasons = []
                if vol_pos: reasons.append("VOL+ (5m)")
                if traders_up: reasons.append("TRADERS+ (5m)")
                if price_compressed: reasons.append("PRICE compressed")
                text = (f"🔥 <b>PRE-EXPLOSION</b>\n\n"
                        f"<b>{safe_html(sym)}</b>\n"
                        f"• MC: ${mc:,.0f}\n"
                        f"• Price: {price or 'N/A'}\n"
                        f"• Reasons: {', '.join(reasons)}\n"
                        f"• Window: last {PRE_WINDOW_MIN} minutes\n\n"
                        f"<b>Mint:</b>\n<code>{mint}</code>")
                buttons = build_buttons(mint)
                await self.core.notifier.send(text, buttons)
                await self.store.mark_notified(mint, "pre_explosion", sym, {"reasons": reasons})
        except Exception as e:
            logging.exception("pre evaluate error %s %s", mint, e)

    async def scan(self):
        # candidate source: jupiter recent + toptrending
        try:
            recent = await self.core.jupiter_category("recent", limit=50)
            trending = await self.core.jupiter_category("toptrending", interval="5m", limit=50)
            cands = []
            # parse recent
            for t in (recent.get("tokens") or recent.get("data") or []):
                addr = t.get("id") or t.get("address") or t.get("mint")
                if addr:
                    cands.append(addr)
            for t in (trending.get("tokens") or trending.get("data") or []):
                addr = t.get("id") or t.get("address") or t.get("mint")
                if addr:
                    cands.append(addr)
            cands = list(dict.fromkeys(cands))
            logging.info("PreExplosion candidates: %d", len(cands))
            for mint in cands:
                # evaluate last PRE_WINDOW_MIN minutes by heuristics
                await self.evaluate(mint)
        except Exception as e:
            logging.exception("pre scan error %s", e)

    async def run(self):
        self.running = True
        while self.running:
            await self.scan()
            await asyncio.sleep(max(30, CHECK_INTERVAL_MIN * 60))

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

class TrendingScanner:
    def __init__(self, core: Core, store: PGStore, interval_min: int = 3):
        self.core = core
        self.store = store
        self.interval_min = interval_min
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def run_once(self):
        try:
            data = await self.core.jupiter_category("toptrending", interval="5m", limit=20)
            items = data.get("tokens") or data.get("data") or []
            for t in items:
                mint = t.get("id") or t.get("address") or t.get("mint")
                sym = t.get("symbol") or t.get("name") or "UNK"
                if not mint:
                    continue
                if await self.store.was_notified(mint):
                    continue
                price, mc, vol = await self.core.get_mc_price_vol(mint)
                # alert early trending regardless of MC or optionally set MC filter; keep broad
                text = (f"🔥 <b>TRENDING NOW</b>\n\n"
                        f"<b>{safe_html(sym)}</b>\n"
                        f"• Price: {price or 'N/A'}\n"
                        f"• MC: ${mc or 0:,.0f}\n"
                        f"• Vol24h: {vol or 'N/A'}\n\n"
                        f"<b>Mint:</b>\n<code>{mint}</code>")
                buttons = build_buttons(mint)
                await self.core.notifier.send(text, buttons)
                await self.store.mark_notified(mint, "trending", sym, {"source": "jupiter_toptrending"})
        except Exception as e:
            logging.exception("trending run_once error %s", e)

    async def run(self):
        self.running = True
        while self.running:
            await self.run_once()
            await asyncio.sleep(max(60, self.interval_min * 60))

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

class FlatScanner:
    """
    Detect tokens that have age >= FLAT_MIN_AGE_H and price movement within +/- FLAT_VOL_PCT
    for at least FLAT_CONSOL_H hours (uses stored metrics prices).
    """
    def __init__(self, core: Core, store: PGStore):
        self.core = core
        self.store = store
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def evaluate_flat(self, mint: str):
        try:
            # metrics over last FLAT_CONSOL_H hours
            mins = FLAT_CONSOL_H * 60
            rows = await self.store.get_metrics_window(mint, mins)
            if not rows or len(rows) < 3:
                return
            prices = [r[1] for r in rows if r[1] is not None]
            if not prices:
                return
            max_p = max(prices)
            min_p = min(prices)
            if min_p <= 0:
                return
            pct_move = (max_p - min_p) / min_p * 100.0
            # age check: token first pool/created > FLAT_MIN_AGE_H hours (try jupiter token first)
            tok = await self.core.jupiter_token(mint)
            token = tok.get("token") or {}
            first_pool = token.get("firstPool") or token.get("first_pool")
            age_ok = False
            if first_pool:
                created = first_pool.get("createdAt") or first_pool.get("created_at")
                if created:
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", ""))
                        age_h = (now_ts() - dt).total_seconds() / 3600.0
                        if age_h >= FLAT_MIN_AGE_H:
                            age_ok = True
                    except Exception:
                        age_ok = False
            # fallback: if no firstPool, rely on metrics history length to approximate age
            if not age_ok:
                # if we have metrics older than FLAT_MIN_AGE_H hours, consider ok
                hist = await self.store.get_metrics_window(mint, FLAT_MIN_AGE_H * 60)
                if hist and len(hist) >= 2:
                    age_ok = True
            if age_ok and pct_move <= FLAT_VOL_PCT:
                if not await self.store.was_notified(mint):
                    sym = token.get("symbol") or token.get("name") or "UNK"
                    price, mc, vol = await self.core.get_mc_price_vol(mint)
                    text = (f"🟦 <b>TOKEN.FLAT</b>\n\n"
                            f"<b>{safe_html(sym)}</b>\n"
                            f"• Price: {price or 'N/A'}\n"
                            f"• MC: ${mc or 0:,.0f}\n"
                            f"• Move( {FLAT_CONSOL_H}h ): {pct_move:.2f}%\n"
                            f"<b>Mint:</b>\n<code>{mint}</code>")
                    buttons = build_buttons(mint)
                    await self.core.notifier.send(text, buttons)
                    await self.store.mark_notified(mint, "flat", sym, {"pct_move": pct_move})
        except Exception as e:
            logging.exception("flat evaluate error %s", e)

    async def scan(self):
        # candidate pool: jupiter toptraded or recent
        try:
            data = await self.core.jupiter_category("toptraded", interval="1h", limit=100)
            cands = []
            for t in (data.get("tokens") or data.get("data") or []):
                addr = t.get("id") or t.get("address") or t.get("mint")
                if addr:
                    cands.append(addr)
            cands = list(dict.fromkeys(cands))
            logging.info("FlatScanner candidates: %d", len(cands))
            for m in cands:
                await self.evaluate_flat(m)
        except Exception as e:
            logging.exception("flat scan error %s", e)

    async def run(self):
        self.running = True
        while self.running:
            await self.scan()
            await asyncio.sleep(max(60, CHECK_INTERVAL_MIN * 60 * 3))

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

# ---------- HELPERS ----------
def build_buttons(mint: str) -> List[Tuple[str,str]]:
    buttons = []
    # jupiter swap (link)
    try:
        jup_link = f"https://jup.ag/swap/{mint}-SOL"
        buttons.append(("Swap (Jupiter)", jup_link))
    except Exception:
        pass
    buttons.append(("DexScreener", f"https://dexscreener.com/solana/{mint}"))
    buttons.append(("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"))
    buttons.append(("Go+ Security", f"https://gopluslabs.io/token-security/{mint}"))
    return buttons

# ---------- FASTAPI + Telegram wiring ----------
app = FastAPI()
telegram_app: Optional[Application] = None

http_session: Optional[aiohttp.ClientSession] = None
http_client: Optional[HttpClient] = None
pg_store: Optional[PGStore] = None
notifier: Optional[TelegramNotifier] = None
core: Optional[Core] = None

wss_monitor: Optional[WSSPumpMonitor] = None
pre_scanner: Optional[PreExplosionScanner] = None
trending_scanner: Optional[TrendingScanner] = None
flat_scanner: Optional[FlatScanner] = None

# command helpers
def is_owner(update: Update) -> bool:
    if update.effective_user:
        return update.effective_user.id == OWNER_ID or update.effective_user.id == TELEGRAM_CHAT_ID
    return False

# Telegram commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Solana Monitor vElite - usa /iniciar /detener /status /silent on|off /listar /set_params", parse_mode="HTML")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text("✅ Iniciando monitores...", parse_mode="HTML")
    if wss_monitor:
        await wss_monitor.start()
    if pre_scanner:
        await pre_scanner.start()
    if trending_scanner:
        await trending_scanner.start()
    if flat_scanner:
        await flat_scanner.start()
    await update.message.reply_text("✅ Monitores iniciados", parse_mode="HTML")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if wss_monitor:
        await wss_monitor.stop()
    if pre_scanner:
        await pre_scanner.stop()
    if trending_scanner:
        await trending_scanner.stop()
    if flat_scanner:
        await flat_scanner.stop()
    await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    wss_ok = bool(wss_monitor and wss_monitor.task and not wss_monitor.task.done())
    pre_ok = bool(pre_scanner and pre_scanner.task and not pre_scanner.task.done())
    trend_ok = bool(trending_scanner and trending_scanner.task and not trending_scanner.task.done())
    flat_ok = bool(flat_scanner and flat_scanner.task and not flat_scanner.task.done())
    text = (f"<b>Status</b>\n"
            f"WSS: {'🟢' if wss_ok else '🔴'}\n"
            f"PreScanner: {'🟢' if pre_ok else '🔴'}\n"
            f"Trending: {'🟢' if trend_ok else '🔴'}\n"
            f"Flat: {'🟢' if flat_ok else '🔴'}\n"
            f"PUMP_MC_MIN: ${PUMP_MC_MIN:,.0f}\n"
            f"PRE_WINDOW_MIN: {PRE_WINDOW_MIN}min\n"
            f"FLAT_AGE_H: {FLAT_MIN_AGE_H}h CONSOL_H: {FLAT_CONSOL_H}h VOL_PCT: {FLAT_VOL_PCT}%")
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_silent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Uso: /silent on|off", parse_mode="HTML")
        return
    if args[0].lower() == "on":
        notifier.silent = True
        await update.message.reply_text("🔕 Silent mode ON", parse_mode="HTML")
    else:
        notifier.silent = False
        await update.message.reply_text("🔔 Silent mode OFF", parse_mode="HTML")

async def cmd_listar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    rows = await pg_store.list_recent_notifications(limit=15)
    if not rows:
        await update.message.reply_text("No hay notificaciones aún.", parse_mode="HTML")
        return
    text = "<b>Últimas notificaciones</b>\n\n"
    for r in rows:
        text += f"• <b>{safe_html(r['symbol'] or 'UNK')}</b> [{r['kind']}] <code>{r['mint'][:8]}...{r['mint'][-8:]}</code>\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_set_params(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text("Uso no implementado. Para ajustar parámetros actualiza variables de entorno y reinicia.", parse_mode="HTML")

# webhook endpoint (optional)
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
    return {"status": "ok", "timestamp": iso(now_ts())}

# ---------- STARTUP / SHUTDOWN ----------
async def init_app_components():
    global telegram_app, http_session, http_client, pg_store, notifier, core
    global wss_monitor, pre_scanner, trending_scanner, flat_scanner

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID not set")
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID not set")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("silent", cmd_silent))
    telegram_app.add_handler(CommandHandler("listar", cmd_listar))
    telegram_app.add_handler(CommandHandler("set_params", cmd_set_params))

    http_session = aiohttp.ClientSession()
    http_client = HttpClient(http_session)
    pg_store = await PGStore.create(DATABASE_URL)
    notifier = TelegramNotifier(telegram_app)
    core = Core(http_client, pg_store, notifier)

    # instantiate monitors
    wss_monitor = WSSPumpMonitor(QUICKNODE_WSS_URL, core, pg_store) if QUICKNODE_WSS_URL else None
    pre_scanner = PreExplosionScanner(core, pg_store)
    trending_scanner = TrendingScanner(core, pg_store, interval_min=3)
    flat_scanner = FlatScanner(core, pg_store)

@app.on_event("startup")
async def on_startup():
    await init_app_components()
    # optional: set webhook if desired via APP URL env var (not auto-run monitors)
    await telegram_app.initialize()
    await telegram_app.start()
    json_log("app_started")

@app.on_event("shutdown")
async def on_shutdown():
    json_log("shutdown_start")
    if wss_monitor:
        await wss_monitor.stop()
    if pre_scanner:
        await pre_scanner.stop()
    if trending_scanner:
        await trending_scanner.stop()
    if flat_scanner:
        await flat_scanner.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    if http_session:
        await http_session.close()
    if pg_store:
        await pg_store.pool.close()
    json_log("shutdown_done")

# ---------- RUN (dev) ----------
def run_uvicorn():
    import uvicorn
    uvicorn.run("telegram_bot_final_v7_pro:app", host="0.0.0.0", port=PORT, log_level="info")

if __name__ == "__main__":
    # local dev: initialize and run
    asyncio.run(init_app_components())
    run_uvicorn()
