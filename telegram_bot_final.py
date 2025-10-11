# telegram_bot_final_v7_pro.py
# Solana Monitor Bot v7 PRO
# - Webhook FastAPI (Railway)
# - Manual start (/iniciar) to run monitors:
#   - WSS Pump Monitor (uses HELIUS_WSS_URL / QuickNode WSS)
#   - Flat Scanner
#   - Pre-Explosion Scanner (25 minutes window, default factor 2.5)
#   - Raydium Graduation Scanner
# - Reconexión WSS with backoff
# - SQLite cache for dedupe + metrics
# - All notifications to same TELEGRAM_CHAT_ID
#
# Requirements: same as your requirements.txt (aiohttp, asyncpg optional, python-telegram-bot[asyncio], fastapi, uvicorn, gunicorn, websockets, apscheduler)

import os
import json
import re
import asyncio
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import websockets
from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# -----------------
# CONFIG (ENV)
# -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "")  # optional Postgres
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL", "")  # use your QuickNode WSS here
APP_URL = os.getenv("APP_URL", "")  # optional for webhook auto-set

PORT = 8080  # fixed as requested

# Thresholds & tuning (defaults, change via commands)
PUMP_PRE_MIN = float(os.getenv("PUMP_PRE_MIN", "60000"))
PUMP_PRE_MAX = float(os.getenv("PUMP_PRE_MAX", "65000"))
PUMP_MIN_HOLDERS = int(os.getenv("PUMP_MIN_HOLDERS", "40"))

FLAT_MIN_AGE_H = int(os.getenv("FLAT_MIN_AGE_H", "72"))
FLAT_VOL_PCT = float(os.getenv("FLAT_VOL_PCT", "12.0"))

# PRE-EXPLOSION parameters
PRE_WINDOW_MIN = int(os.getenv("PRE_WINDOW_MIN", "25"))    # minutes window
PRE_BREAK_MULT = float(os.getenv("PRE_BREAK_MULT", "2.5"))  # multiplier

CHECK_INTERVAL_MIN = int(os.getenv("CHECK_INTERVAL_MIN", "5"))  # periodic scans

DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest")

CACHE_DB = os.getenv("CACHE_DB", "/data/cache_v7_pro.db")
os.makedirs(os.path.dirname(CACHE_DB), exist_ok=True)

# -----------------
# Utilities
# -----------------
def now_ts() -> datetime:
    return datetime.utcnow()

def iso(ts: datetime) -> str:
    return ts.isoformat() + "Z"

def safe_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def json_log(event: str, **k):
    print(json.dumps({"ts": now_ts().isoformat(), "event": event, **k}, ensure_ascii=False))

# -----------------
# SQLite cache & metrics
# -----------------
class Cache:
    def __init__(self, path=CACHE_DB):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notified (
                mint TEXT PRIMARY KEY,
                kind TEXT,
                symbol TEXT,
                notified_at TEXT,
                meta TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                mint TEXT PRIMARY KEY,
                last_seen TEXT
            )
        """)
        # store metrics history: timestamp, mint, marketcap, volume_usd
        cur.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                ts TEXT,
                mint TEXT,
                mc REAL,
                vol REAL
            )
        """)
        # persisted settings (so set_pump persists)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        self.conn.commit()

    # notified
    def was_notified(self, mint: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM notified WHERE mint=?", (mint,))
        return cur.fetchone() is not None

    def mark_notified(self, mint: str, kind: str, symbol: str, meta: dict):
        self.conn.execute("REPLACE INTO notified(mint,kind,symbol,notified_at,meta) VALUES(?,?,?,?,?)",
                          (mint, kind, symbol, now_ts().isoformat(), json.dumps(meta)))
        self.conn.commit()

    # seen
    def seen_recent(self, mint: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE mint=?", (mint,))
        return cur.fetchone() is not None

    def mark_seen(self, mint: str):
        self.conn.execute("REPLACE INTO seen(mint,last_seen) VALUES(?,?)", (mint, now_ts().isoformat()))
        self.conn.commit()

    # metrics
    def add_metric(self, mint: str, mc: Optional[float], vol: Optional[float]):
        self.conn.execute("INSERT INTO metrics(ts,mint,mc,vol) VALUES(?,?,?,?)", (now_ts().isoformat(), mint, mc or 0.0, vol or 0.0))
        self.conn.commit()
        # prune old metrics > 2 days
        cutoff = (now_ts() - timedelta(days=2)).isoformat()
        self.conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        self.conn.commit()

    def get_metrics_window(self, mint: str, minutes: int) -> List[Tuple[str,float,float]]:
        cutoff = (now_ts() - timedelta(minutes=minutes)).isoformat()
        cur = self.conn.execute("SELECT ts,mc,vol FROM metrics WHERE mint=? AND ts >= ? ORDER BY ts", (mint,cutoff))
        return cur.fetchall()

    # settings
    def set_setting(self, key: str, value: str):
        self.conn.execute("REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        self.conn.commit()

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        cur = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        r = cur.fetchone()
        return r[0] if r else default

cache = Cache()

# load persisted settings (if any)
PUMP_PRE_MIN = float(cache.get_setting("PUMP_PRE_MIN") or PUMP_PRE_MIN)
PUMP_PRE_MAX = float(cache.get_setting("PUMP_PRE_MAX") or PUMP_PRE_MAX)
FLAT_MIN_AGE_H = int(cache.get_setting("FLAT_MIN_AGE_H") or FLAT_MIN_AGE_H)
FLAT_VOL_PCT = float(cache.get_setting("FLAT_VOL_PCT") or FLAT_VOL_PCT)
PRE_WINDOW_MIN = int(cache.get_setting("PRE_WINDOW_MIN") or PRE_WINDOW_MIN)
PRE_BREAK_MULT = float(cache.get_setting("PRE_BREAK_MULT") or PRE_BREAK_MULT)

# -----------------
# HTTP client (aiohttp)
# -----------------
class HttpClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
    async def get_json(self, url, params=None, headers=None, timeout=12):
        try:
            async with self.s.get(url, params=params, headers=headers, timeout=timeout) as r:
                text = await r.text()
                if r.status == 200:
                    return json.loads(text)
                json_log("http_error", url=url, status=r.status, text=(text or "")[:300])
        except Exception as e:
            json_log("http_exc", url=url, err=str(e))
        return {}

# -----------------
# DexScreener wrapper (used for marketcap, volume, changes)
# -----------------
class DexScreener:
    def __init__(self, http: HttpClient, base=DEXSCREENER_API):
        self.http = http
        self.base = base.rstrip("/")

    async def token_info(self, mint: str) -> Dict[str,Any]:
        # token endpoint
        return await self.http.get_json(f"{self.base}/tokens/{mint}")

    async def token_boosts(self) -> Dict[str,Any]:
        return await self.http.get_json(f"{self.base}/token-boosts/latest/v1")

    async def token_profiles(self) -> Dict[str,Any]:
        return await self.http.get_json(f"{self.base}/token-profiles/latest/v1")

# -----------------
# Notifier (Telegram)
# -----------------
class TelegramNotifier:
    def __init__(self, app: Application):
        self.app = app
        self.silent = False

    async def send(self, text: str, buttons: Optional[List[Tuple[str,str]]] = None):
        if self.silent:
            json_log("silent_mode_on", text=text[:200])
            return
        if not TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID == 0:
            json_log("no_chatid", text=text[:200])
            return
        try:
            markup = None
            if buttons:
                kb = [[InlineKeyboardButton(lbl, url=url)] for lbl,url in buttons]
                markup = InlineKeyboardMarkup(kb)
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=False, reply_markup=markup)
        except Exception as e:
            json_log("tg_send_err", error=str(e))

# -----------------
# Core logic
# -----------------
class Core:
    def __init__(self, http: HttpClient, notifier: TelegramNotifier):
        self.http = http
        self.dex = DexScreener(http)
        self.notifier = notifier

    async def get_mc_and_vol(self, mint: str) -> Tuple[Optional[float], Optional[float]]:
        # attempt via DexScreener token endpoint
        try:
            info = await self.dex.token_info(mint)
            token = info.get("token") or {}
            mc = token.get("marketCapUsd") or token.get("marketCap")
            # try pairs/volume
            vol = None
            # some responses include pairs array with volumeUsd or volume
            pairs = info.get("pairs") or []
            if pairs and isinstance(pairs, list):
                # sum 24h volumes if present
                total = 0.0
                found = False
                for p in pairs:
                    v = p.get("volume", {})
                    if isinstance(v, dict):
                        h24 = v.get("h24") or v.get("24h")
                    else:
                        h24 = p.get("volumeUsd") or p.get("volumeUsd24H")
                    try:
                        if h24:
                            total += float(h24)
                            found = True
                    except Exception:
                        pass
                if found:
                    vol = total
            # fallback simplest
            if mc is not None:
                try:
                    mc = float(mc)
                except Exception:
                    mc = None
            if vol is not None:
                try:
                    vol = float(vol)
                except Exception:
                    vol = None
            return mc, vol
        except Exception as e:
            json_log("get_mc_vol_err", err=str(e), mint=mint)
            return None, None

# -----------------
# WSS Pump Monitor (connects to HELIUS_WSS_URL/QuickNode WSS)
# -----------------
class WSSPumpMonitor:
    def __init__(self, wss_url: str, core: Core):
        self.wss = wss_url
        self.core = core
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.backoff = 1

    def extract_mints(self, msg: Dict[str,Any]) -> List[str]:
        txt = json.dumps(msg)
        matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{32,44}', txt)
        return list(dict.fromkeys(matches))

    async def handle(self, msg: Dict[str,Any]):
        mints = self.extract_mints(msg)
        for mint in mints:
            if cache.seen_recent(mint):
                continue
            cache.mark_seen(mint)
            mc, vol = await self.core.get_mc_and_vol(mint)
            # store metrics for pre-explosion evaluation
            cache.add_metric(mint, mc, vol)
            if mc is None:
                continue
            # pump pre-grad detection by marketcap range
            if PUMP_PRE_MIN <= mc <= PUMP_PRE_MAX:
                # get info to estimate holders
                info = await self.core.dex.token_info(mint)
                token = info.get("token") or {}
                holders = int(token.get("holderCount") or token.get("holders") or 0)
                if holders and holders < PUMP_MIN_HOLDERS:
                    # skip if too few holders
                    continue
                if cache.was_notified(mint):
                    continue
                sym = token.get("symbol") or "UNK"
                text = (f"🚀 <b>PUMP.PRE-GRAD</b>\n\n"
                        f"<b>{safe_html(sym)}</b>\n"
                        f"• MC: ${mc:,.0f}\n"
                        f"• Holders: {holders}\n\n"
                        f"<b>Mint:</b>\n<code>{mint}</code>")
                buttons = [
                    ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
                    ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
                    ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}")
                ]
                await self.core.notifier.send(text, buttons)
                cache.mark_notified(mint, "pump_pre", sym, {"mc": mc, "holders": holders})

    async def run(self):
        if not self.wss:
            json_log("wss_monitor_not_configured")
            return
        self.running = True
        self.backoff = 1
        while self.running:
            try:
                async with websockets.connect(self.wss, ping_interval=30, ping_timeout=10) as ws:
                    json_log("wss_connected")
                    self.backoff = 1
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self.handle(msg)
                        except Exception as e:
                            json_log("wss_handle_err", err=str(e))
            except Exception as e:
                json_log("wss_error", err=str(e), backoff=self.backoff)
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

# -----------------
# Pre-Explosion Scanner (periodic)
#   - scans candidate tokens (from token_boosts / token_profiles)
#   - records metrics; if current (mc or vol) >= PRE_BREAK_MULT * earliest_in_window -> alert
# -----------------
class PreExplosionScanner:
    def __init__(self, core: Core):
        self.core = core
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def scan_and_detect(self):
        async with aiohttp.ClientSession() as session:
            http = HttpClient(session)
            dex = DexScreener(http)
            # Get candidate tokens from boosts + profiles
            boosts = await dex.token_boosts()
            candidates = []
            if isinstance(boosts, dict):
                for t in boosts.get("tokens", []):
                    a = t.get("address')
                    if a:
                        candidates.append(a)
            profiles = await dex.token_profiles()
            if isinstance(profiles, dict):
                for k in (profiles.get("tokens") or {}).keys():
                    candidates.append(k)
            # dedupe
            candidates = list(dict.fromkeys([c for c in candidates if c]))
            json_log("pre_candidates", count=len(candidates))
            for mint in candidates:
                try:
                    mc, vol = await self.core.get_mc_and_vol(mint)
                    if mc is None and vol is None:
                        continue
                    cache.add_metric(mint, mc, vol)
                    # evaluate window
                    rows = cache.get_metrics_window(mint, PRE_WINDOW_MIN)
                    # need at least 2 points: earliest and latest
                    if len(rows) < 2:
                        continue
                    # rows are ordered by ts
                    earliest_ts, earliest_mc, earliest_vol = rows[0]
                    latest_ts, latest_mc, latest_vol = rows[-1]
                    # compare both MC and vol; trigger if either increases >= PRE_BREAK_MULT
                    triggered = False
                    reasons = []
                    try:
                        if earliest_mc and latest_mc and earliest_mc > 0:
                            if (latest_mc / earliest_mc) >= PRE_BREAK_MULT:
                                triggered = True
                                reasons.append(f"MC x{latest_mc/earliest_mc:.2f}")
                    except Exception:
                        pass
                    try:
                        if earliest_vol and latest_vol and earliest_vol > 0:
                            if (latest_vol / earliest_vol) >= PRE_BREAK_MULT:
                                triggered = True
                                reasons.append(f"VOL x{latest_vol/earliest_vol:.2f}")
                    except Exception:
                        pass
                    if triggered and not cache.was_notified(mint):
                        # fetch simple token info for symbol
                        info = await dex.token_info(mint)
                        token = info.get("token") or {}
                        sym = token.get("symbol") or "UNK"
                        text = (
                            f"🔥 <b>PRE-EXPLOSION ALERT</b> 🔥\n\n"
                            f"<b>{safe_html(sym)}</b>\n"
                            f"• Reasons: {', '.join(reasons)}\n"
                            f"• Window: last {PRE_WINDOW_MIN} minutes\n"
                            f"<b>Mint:</b>\n<code>{mint}</code>"
                        )
                        buttons = [
                            ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
                            ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
                            ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}")
                        ]
                        await self.core.notifier.send(text, buttons)
                        cache.mark_notified(mint, "pre_explosion", sym, {"reasons": reasons})
                except Exception as e:
                    json_log("pre_scan_err", err=str(e), mint=mint)

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self.scan_and_detect()
            except Exception as e:
                json_log("pre_run_err", err=str(e))
            await asyncio.sleep(max(30, CHECK_INTERVAL_MIN * 60))

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

# -----------------
# Flat Scanner (periodic) - simplified: checks token_changes and age
# -----------------
class FlatScanner:
    def __init__(self, core: Core):
        self.core = core
        self.task: Optional[asyncio.Task] = None
        self.running = False

    async def scan(self):
        async with aiohttp.ClientSession() as session:
            http = HttpClient(session)
            dex = DexScreener(http)
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
            json_log("flat_candidates", count=len(candidates))
            for mint in candidates:
                try:
                    info = await dex.token_info(mint)
                    token = info.get("token") or {}
                    # skip pump.fun tokens when you want? user asked both types covered earlier; but for flat we exclude pump-only tokens
                    pairs = info.get("pairs") or []
                    is_pump = False
                    for p in pairs:
                        url = (p.get("url") or "").lower()
                        dexid = (p.get("dexId") or "").lower()
                        if "pump.fun" in url or "pump.fun" in dexid:
                            is_pump = True
                            break
                    # calculate 1h and 24h change if present
                    ch1 = token.get("priceChange1h") or token.get("change1h") or token.get("1h")
                    ch24 = token.get("priceChange24h") or token.get("change24h") or token.get("24h")
                    try:
                        ch1_abs = abs(float(ch1)) if ch1 is not None else 9999.0
                    except Exception:
                        ch1_abs = 9999.0
                    try:
                        ch24_abs = abs(float(ch24)) if ch24 is not None else 9999.0
                    except Exception:
                        ch24_abs = 9999.0
                    mc = token.get("marketCapUsd") or token.get("marketCap") or 0
                    try:
                        mc_val = float(mc) if mc else 0.0
                    except Exception:
                        mc_val = 0.0
                    # check age from metrics history - require > FLAT_MIN_AGE_H (best-effort)
                    age_hours = None
                    rows = cache.get_metrics_window(mint, FLAT_MIN_AGE_H * 60)
                    if rows:
                        # if we've stored metrics earlier, check earliest
                        earliest_ts = rows[0][0]
                        try:
                            earliest_dt = datetime.fromisoformat(earliest_ts)
                            age_hours = (now_ts() - earliest_dt).total_seconds() / 3600.0
                        except Exception:
                            age_hours = None
                    # decide flat: small ch1 or ch24 and age >= threshold
                    is_flat = (ch1_abs <= FLAT_VOL_PCT) or (ch24_abs <= FLAT_VOL_PCT)
                    if is_flat and (age_hours is None or age_hours >= FLAT_MIN_AGE_H):
                        # avoid pump range tokens if you want to keep separate; user asked both types to be covered elsewhere
                        if not cache.was_notified(mint):
                            sym = token.get("symbol") or "UNK"
                            text = (f"🟦 <b>TOKEN.FLAT</b>\n\n"
                                    f"<b>{safe_html(sym)}</b>\n"
                                    f"• MC: ${mc_val:,.0f}\n"
                                    f"• Cambios | 1h: {ch1 or 'N/A'}% - 24h: {ch24 or 'N/A'}%\n"
                                    f"<b>Mint:</b>\n<code>{mint}</code>")
                            buttons = [("DexScreener", f"https://dexscreener.com/solana/{mint}")]
                            await self.core.notifier.send(text, buttons)
                            cache.mark_notified(mint, "flat", sym, {"mc": mc_val})
                except Exception as e:
                    json_log("flat_scan_err", err=str(e), mint=mint)

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self.scan()
            except Exception as e:
                json_log("flat_run_err", err=str(e))
            await asyncio.sleep(max(60, CHECK_INTERVAL_MIN * 60 * 3))  # less frequent

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

# -----------------
# Raydium Graduation monitor (periodic scan)
# -----------------
class RaydiumGraduation:
    def __init__(self, core: Core):
        self.core = core
        self.task: Optional[asyncio.Task] = None
        self.running = False
        self.seen = set()

    async def check(self):
        # fetch Raydium pools list via earlier-known endpoint
        try:
            async with aiohttp.ClientSession() as session:
                hc = HttpClient(session)
                url = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"
                data = await hc.get_json(url)
                pools = data.get("official", []) if isinstance(data, dict) else []
                for p in pools:
                    pool_id = p.get("id")
                    if not pool_id or pool_id in self.seen:
                        continue
                    self.seen.add(pool_id)
                    if len(self.seen) > 2000:
                        self.seen = set(list(self.seen)[-500:])
                    mint = p.get("baseMint")
                    if not mint:
                        continue
                    if cache.was_notified(mint):
                        continue
                    mc, vol = await self.core.get_mc_and_vol(mint)
                    if mc is None:
                        continue
                    if not (PUMP_PRE_MIN <= mc <= PUMP_PRE_MAX):
                        continue
                    # verify pump origin
                    try:
                        info = await self.core.dex.token_info(mint)
                        pairs = info.get("pairs") or []
                        from_pump = False
                        for pair in pairs:
                            urlp = (pair.get("url") or "").lower()
                            dexid = (pair.get("dexId") or "").lower()
                            if "pump.fun" in urlp or "pump.fun" in dexid:
                                from_pump = True
                                break
                        if not from_pump:
                            continue
                        token = info.get("token") or {}
                        sym = token.get("symbol") or "UNK"
                        holders = int(token.get("holderCount") or token.get("holders") or 0)
                        if holders and holders < PUMP_MIN_HOLDERS:
                            continue
                        text = (f"🔥 <b>PUMP.FUN → RAYDIUM GRAD</b>\n\n"
                                f"<b>{safe_html(sym)}</b>\n"
                                f"• MC: ${mc:,.0f}\n"
                                f"• Holders: {holders}\n"
                                f"<b>Mint:</b>\n<code>{mint}</code>")
                        buttons = [
                            ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
                            ("Raydium", f"https://raydium.io/swap/?inputCurrency=sol&outputCurrency={mint}"),
                            ("Jupiter", f"https://jup.ag/swap/{mint}-SOL")
                        ]
                        await self.core.notifier.send(text, buttons)
                        cache.mark_notified(mint, "raydium_grad", sym, {"mc": mc})
                    except Exception as e:
                        json_log("raydium_check_err", err=str(e), pool=p.get("id"))
        except Exception as e:
            json_log("raydium_outer_err", err=str(e))

    async def run(self):
        self.running = True
        while self.running:
            try:
                await self.check()
            except Exception as e:
                json_log("raydium_run_err", err=str(e))
            await asyncio.sleep(max(30, CHECK_INTERVAL_MIN * 60 // 2))

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

# -----------------
# Telegram command handlers & FastAPI wiring
# -----------------
app = FastAPI()
telegram_app: Optional[Application] = None
http_session: Optional[aiohttp.ClientSession] = None
http_client: Optional[HttpClient] = None
notifier: Optional[TelegramNotifier] = None
core: Optional[Core] = None

wss_monitor: Optional[WSSPumpMonitor] = None
pre_scanner: Optional[PreExplosionScanner] = None
flat_scanner: Optional[FlatScanner] = None
raydium_grad: Optional[RaydiumGraduation] = None

scheduler = AsyncIOScheduler()

# commands
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    text = ("🤖 <b>Solana Monitor v7 PRO</b>\n\n"
            "Comandos:\n"
            "/iniciar - iniciar todos los monitores\n"
            "/detener - detener todos los monitores\n"
            "/status - ver estado\n"
            "/set_pump <min> <max> - ajustar rango pump pre\n"
            "/set_flat <hours> <pct> - ajustar flat params\n"
            "/set_pre <minutes> <factor> - ajustar pre-explosion window y factor\n"
            "/listar - mostrar últimos tokens notificados\n"
            "/reconnect_wss - forzar reconexión WSS\n"
            "/silent on|off - activar/desactivar alertas\n"
            "/summary - mostrar alertas 24h")
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    await update.message.reply_text("✅ Iniciando monitores...", parse_mode="HTML")
    # start monitors
    if wss_monitor:
        await wss_monitor.start()
    if pre_scanner:
        await pre_scanner.start()
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
    if pre_scanner:
        await pre_scanner.stop()
    if flat_scanner:
        await flat_scanner.stop()
    if raydium_grad:
        await raydium_grad.stop()
    await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    wss_ok = (wss_monitor and wss_monitor.task and not wss_monitor.task.done())
    pre_ok = (pre_scanner and pre_scanner.task and not pre_scanner.task.done())
    flat_ok = (flat_scanner and flat_scanner.task and not flat_scanner.task.done())
    ray_ok = (raydium_grad and raydium_grad.task and not raydium_grad.task.done())
    text = (f"<b>Status</b>\n"
            f"WSS: {'🟢' if wss_ok else '🔴'}\n"
            f"PreScanner: {'🟢' if pre_ok else '🔴'}\n"
            f"FlatScanner: {'🟢' if flat_ok else '🔴'}\n"
            f"RaydiumGrad: {'🟢' if ray_ok else '🔴'}\n"
            f"PUMP_PRE: ${PUMP_PRE_MIN:,.0f} - ${PUMP_PRE_MAX:,.0f}\n"
            f"PRE_WINDOW: {PRE_WINDOW_MIN}min factor {PRE_BREAK_MULT}\n"
            f"FLAT_MIN_AGE_H: {FLAT_MIN_AGE_H}h FLAT_VOL_PCT: {FLAT_VOL_PCT}%")
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_set_pump(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    try:
        args = ctx.args
        if len(args) != 2:
            raise ValueError("Uso: /set_pump <min> <max>")
        global PUMP_PRE_MIN, PUMP_PRE_MAX
        PUMP_PRE_MIN = float(args[0])
        PUMP_PRE_MAX = float(args[1])
        cache.set_setting("PUMP_PRE_MIN", str(PUMP_PRE_MIN))
        cache.set_setting("PUMP_PRE_MAX", str(PUMP_PRE_MAX))
        await update.message.reply_text(f"Rango pump ajustado: ${PUMP_PRE_MIN:,.0f} - ${PUMP_PRE_MAX:,.0f}", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}", parse_mode="HTML")

async def cmd_set_flat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    try:
        args = ctx.args
        if len(args) != 2:
            raise ValueError("Uso: /set_flat <hours> <pct>")
        global FLAT_MIN_AGE_H, FLAT_VOL_PCT
        FLAT_MIN_AGE_H = int(args[0])
        FLAT_VOL_PCT = float(args[1])
        cache.set_setting("FLAT_MIN_AGE_H", str(FLAT_MIN_AGE_H))
        cache.set_setting("FLAT_VOL_PCT", str(FLAT_VOL_PCT))
        await update.message.reply_text(f"Flat params actualizados: age={FLAT_MIN_AGE_H}h pct={FLAT_VOL_PCT}%", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}", parse_mode="HTML")

async def cmd_set_pre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    try:
        args = ctx.args
        if len(args) != 2:
            raise ValueError("Uso: /set_pre <minutes> <factor>")
        global PRE_WINDOW_MIN, PRE_BREAK_MULT
        PRE_WINDOW_MIN = int(args[0])
        PRE_BREAK_MULT = float(args[1])
        cache.set_setting("PRE_WINDOW_MIN", str(PRE_WINDOW_MIN))
        cache.set_setting("PRE_BREAK_MULT", str(PRE_BREAK_MULT))
        await update.message.reply_text(f"Pre-explosion params: window={PRE_WINDOW_MIN}min factor={PRE_BREAK_MULT}", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}", parse_mode="HTML")

async def cmd_listar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    cur = cache.conn.execute("SELECT mint,kind,symbol,notified_at FROM notified ORDER BY notified_at DESC LIMIT 15")
    rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("No hay notificaciones aún.", parse_mode="HTML")
        return
    text = "<b>Últimas notificaciones</b>\n\n"
    for r in rows:
        mint, kind, sym, ts = r
        text += f"• <b>{safe_html(sym or 'UNK')}</b> [{kind}] <code>{mint[:8]}...{mint[-8:]}</code>\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_reconnect_wss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    if not wss_monitor:
        await update.message.reply_text("WSS monitor no configurado.", parse_mode="HTML")
        return
    await wss_monitor.stop()
    await asyncio.sleep(1)
    await wss_monitor.start()
    await update.message.reply_text("Reconectando WSS...", parse_mode="HTML")

async def cmd_silent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
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

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    # count notifications in last 24h
    cutoff = (now_ts() - timedelta(hours=24)).isoformat()
    cur = cache.conn.execute("SELECT COUNT(*) FROM notified WHERE notified_at >= ?", (cutoff,))
    n = cur.fetchone()[0] if cur else 0
    await update.message.reply_text(f"📰 Alerts (24h): {n}", parse_mode="HTML")

# webhook endpoint
@app.post("/webhook/{token}")
async def telegram_webhook(token: str, req: Request):
    try:
        if token != TELEGRAM_BOT_TOKEN:
            raise HTTPException(status_code=403, detail="invalid token")
        body = await req.json()
        update = Update.de_json(body, telegram_app.bot)
        # only owner allowed
        if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
            return JSONResponse({"ok": True, "note": "ignored - not owner"})
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        json_log("webhook_err", err=str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": now_ts().isoformat()}

# -----------------
# Startup / Shutdown
# -----------------
async def init_app():
    global telegram_app, http_session, http_client, notifier, core
    global wss_monitor, pre_scanner, flat_scanner, raydium_grad

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID not set")

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    # register commands
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("set_pump", cmd_set_pump))
    telegram_app.add_handler(CommandHandler("set_flat", cmd_set_flat))
    telegram_app.add_handler(CommandHandler("set_pre", cmd_set_pre))
    telegram_app.add_handler(CommandHandler("listar", cmd_listar))
    telegram_app.add_handler(CommandHandler("reconnect_wss", cmd_reconnect_wss))
    telegram_app.add_handler(CommandHandler("silent", cmd_silent))
    telegram_app.add_handler(CommandHandler("summary", cmd_summary))

    http_session = aiohttp.ClientSession()
    http_client = HttpClient(http_session)
    notifier = TelegramNotifier(telegram_app)
    core = Core(http_client, notifier)

    # instantiate monitors (but DO NOT start until /iniciar)
    wss_monitor = WSSPumpMonitor(HELIUS_WSS_URL, core) if HELIUS_WSS_URL else None
    pre_scanner = PreExplosionScanner(core)
    flat_scanner = FlatScanner(core)
    raydium_grad = RaydiumGraduation(core)

@app.on_event("startup")
async def on_startup():
    await init_app()
    # if APP_URL provided, try set webhook
    if APP_URL:
        try:
            webhook_url = f"{APP_URL.rstrip('/')}/webhook/{TELEGRAM_BOT_TOKEN}"
            await telegram_app.bot.set_webhook(webhook_url)
            json_log("webhook_set", url=webhook_url)
        except Exception as e:
            json_log("webhook_set_err", err=str(e))
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
    if flat_scanner:
        await flat_scanner.stop()
    if raydium_grad:
        await raydium_grad.stop()
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    if http_session:
        await http_session.close()
    json_log("shutdown_done")

# -----------------
# Local run helper (dev) - port fixed 8080
# -----------------
def run_uvicorn():
    import uvicorn
    uvicorn.run("telegram_bot_final_v7_pro:app", host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    # local dev invocation
    asyncio.run(init_app())
    run_uvicorn()
