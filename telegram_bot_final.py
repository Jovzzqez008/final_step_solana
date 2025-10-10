# telegram_bot_final.py
# Solana Monitor Bot v4.1 – Railway-ready
# - Pump.fun pre-grad detection (Helius WSS)
# - Flat scanner with stricter filters, Raydium + Jupiter candidates
# - Fallback candles via Birdeye
# - Security via RugCheck (LP lock, top holders)
# - SQLite cache for resilience + asyncio Queue for load control
# - Telegram UX: inline buttons, commands: start/iniciar/detener/status/config/ajustar_flat/ultimos/buscar
# - APScheduler: resumen automático cada 8h
#
# Start: python telegram_bot_final.py
# Webhook: POST https://DOMAIN/webhook/TELEGRAM_BOT_TOKEN

import os
import asyncio
import json
import random
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

import aiohttp
import asyncpg
import websockets

from fastapi import FastAPI, Request, HTTPException
from starlette.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes, CommandHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ---------------------------
# CONFIG
# ---------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")
JUPITER_API_BASE = os.getenv("JUPITER_API_BASE", "https://lite-api.jup.ag")
RAYDIUM_API = "https://api-v3.raydium.io"
BIRDEYE_API_BASE = os.getenv("BIRDEYE_API_BASE", "https://public-api.birdeye.so")
RUGCHECK_API_BASE = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz")

DOMAIN = os.getenv("DOMAIN", "")
PORT = int(os.getenv("PORT", "8080"))

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

# Pump.fun thresholds
PUMP_PRE_GRADUATION_MIN = float(os.getenv("PUMP_PRE_GRADUATION_MIN", "55000"))
PUMP_PRE_GRADUATION_MAX = float(os.getenv("PUMP_PRE_GRADUATION_MAX", "68000"))

# Flat scanner thresholds
FLAT_LIQUIDITY_MIN = float(os.getenv("FLAT_LIQUIDITY_MIN", "15000"))
FLAT_VOLUME_24H_MIN = float(os.getenv("FLAT_VOLUME_24H_MIN", "20000"))
FLAT_VOLATILITY_PCT = float(os.getenv("FLAT_VOLATILITY_PCT", "15.0"))
FLAT_VOLUME_AVG_PER_CANDLE_USD = float(os.getenv("FLAT_VOLUME_AVG_PER_CANDLE_USD", "200"))
FLAT_TOKEN_REPEAT_HOURS = int(os.getenv("FLAT_TOKEN_REPEAT_HOURS", "4"))
FLAT_MIN_ORGANIC_SCORE = float(os.getenv("FLAT_MIN_ORGANIC_SCORE", "15"))
MIN_HOLDER_COUNT = int(os.getenv("MIN_HOLDER_COUNT", "75"))
MIN_ORGANIC_VOLUME_RATIO = float(os.getenv("MIN_ORGANIC_VOLUME_RATIO", "0.2"))

# Pre-breakout
PRE_BREAKOUT_VOLUME_MULTIPLIER = float(os.getenv("PRE_BREAKOUT_VOLUME_MULTIPLIER", "2.5"))
PRE_BREAKOUT_CANDLES_COUNT = int(os.getenv("PRE_BREAKOUT_CANDLES_COUNT", "3"))

CANDLE_INTERVAL = "5m"
CANDLES_HOURS = int(os.getenv("CANDLES_HOURS", "3"))

MAX_RETRIES = 8
BASE_BACKOFF = 1.0

# ---------------------------
# UTILS
# ---------------------------
def now_ts() -> datetime:
    return datetime.utcnow()

def backoff_delay(attempt: int) -> float:
    return BASE_BACKOFF * (2 ** attempt) * (0.9 + 0.2 * (os.urandom(1)[0] / 255))

def format_number(num: float) -> str:
    if num >= 1_000_000:
        return f"{num/1_000_000:.2f}M"
    elif num >= 1_000:
        return f"{num/1_000:.2f}K"
    else:
        return f"{num:.2f}"

def json_log(event: str, **kw):
    log = {"ts": now_ts().isoformat(), "event": event, **kw}
    print(json.dumps(log, ensure_ascii=False))

# ---------------------------
# DB: Postgres for alerts
# ---------------------------
DB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notified_tokens (
    mint TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    notified_at TIMESTAMP WITH TIME ZONE NOT NULL,
    symbol TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notified_tokens_kind ON notified_tokens(kind);
CREATE INDEX IF NOT EXISTS idx_notified_tokens_notified_at ON notified_tokens(notified_at);
"""

class DB:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        if self.pool:
            return
        if not self.database_url:
            raise RuntimeError("DATABASE_URL no configurada")
        self.pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=10)
        async with self.pool.acquire() as conn:
            await conn.execute(DB_SCHEMA_SQL)
            json_log("db_schema_ready")

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def was_notified_recently(self, mint: str, kind: str, hours: int) -> bool:
        q = "SELECT notified_at FROM notified_tokens WHERE mint=$1 AND kind=$2"
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(q, mint, kind)
            if not row:
                return False
            return (now_ts() - row["notified_at"]) < timedelta(hours=hours)

    async def mark_notified(self, mint: str, kind: str, symbol: str = None):
        q = """
        INSERT INTO notified_tokens(mint, kind, notified_at, symbol)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (mint) DO UPDATE
          SET notified_at = EXCLUDED.notified_at, kind = EXCLUDED.kind, symbol = EXCLUDED.symbol;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(q, mint, kind, now_ts(), symbol)

    async def count_alerts_last_24h(self) -> int:
        q = "SELECT COUNT(*) FROM notified_tokens WHERE notified_at >= $1"
        async with self.pool.acquire() as conn:
            try:
                r = await conn.fetchval(q, now_ts() - timedelta(hours=24))
                return int(r or 0)
            except Exception as e:
                json_log("db_count_error", error=str(e))
                return 0

    async def last_notifications(self, limit: int = 5) -> List[asyncpg.Record]:
        q = """SELECT mint, kind, symbol, notified_at
               FROM notified_tokens
               ORDER BY notified_at DESC
               LIMIT $1"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(q, limit)
        return rows

# ---------------------------
# SQLite cache for resilience
# ---------------------------
class Cache:
    def __init__(self, path: str = "/data/cache.db"):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("""
        CREATE TABLE IF NOT EXISTS marketcap_cache (
            mint TEXT PRIMARY KEY,
            marketcap REAL,
            updated_at TEXT
        )""")
        self._conn.execute("""
        CREATE TABLE IF NOT EXISTS recent_mints (
            mint TEXT PRIMARY KEY,
            added_at TEXT
        )""")
        self._conn.commit()

    def get_marketcap(self, mint: str) -> Optional[float]:
        cur = self._conn.execute("SELECT marketcap FROM marketcap_cache WHERE mint=?", (mint,))
        row = cur.fetchone()
        return row[0] if row else None

    def set_marketcap(self, mint: str, mc: float):
        self._conn.execute("REPLACE INTO marketcap_cache(mint, marketcap, updated_at) VALUES(?,?,?)",
                           (mint, mc, now_ts().isoformat()))
        self._conn.commit()

    def recently_seen(self, mint: str) -> bool:
        cur = self._conn.execute("SELECT added_at FROM recent_mints WHERE mint=?", (mint,))
        return cur.fetchone() is not None

    def mark_seen(self, mint: str):
        self._conn.execute("REPLACE INTO recent_mints(mint, added_at) VALUES(?,?)",
                           (mint, now_ts().isoformat()))
        self._conn.commit()

# ---------------------------
# HTTP client with retries
# ---------------------------
class HttpClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_json(self, url: str, params: Dict[str, Any] = None, headers: Dict[str, str] = None) -> Any:
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                async with self.session.get(url, params=params, headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status in (429, 503, 502):
                        await asyncio.sleep(backoff_delay(attempt))
                        attempt += 1
                        continue
                    text = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {text}")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(backoff_delay(attempt))
                attempt += 1
                if attempt >= MAX_RETRIES:
                    raise
        raise RuntimeError("Max retries exceeded")

# ---------------------------
# Jupiter client
# ---------------------------
class JupiterClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def get_token_info(self, mint: str) -> Optional[Dict[str, Any]]:
        url = f"{JUPITER_API_BASE}/tokens/v2/search?query={mint}"
        try:
            data = await self.http.get_json(url)
            if isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception as e:
            json_log("jup_token_error", mint=mint, error=str(e))
        return None

    async def get_high_organic_tokens(self) -> List[Dict[str, Any]]:
        endpoints = [
            f"{JUPITER_API_BASE}/tokens/v2/toporganicscore/1h?limit=100",
            f"{JUPITER_API_BASE}/tokens/v2/toptraded/1h?limit=50",
            f"{JUPITER_API_BASE}/tokens/v2/recent?limit=30"
        ]
        all_tokens = []
        for endpoint in endpoints:
            try:
                tokens = await self.http.get_json(endpoint)
                if isinstance(tokens, list):
                    all_tokens.extend(tokens)
            except Exception as e:
                json_log("jup_endpoint_error", endpoint=endpoint, error=str(e))
                continue
        filtered = []
        for token in all_tokens:
            try:
                organic_score = token.get('organicScore', 0)
                liquidity = float(token.get('liquidity', 0))
                volume_24h = float(token.get('stats24h', {}).get('buyVolume', 0) +
                                   token.get('stats24h', {}).get('sellVolume', 0))
                if (organic_score >= FLAT_MIN_ORGANIC_SCORE and
                    liquidity >= FLAT_LIQUIDITY_MIN and
                    volume_24h >= FLAT_VOLUME_24H_MIN):
                    filtered.append(token)
            except (ValueError, TypeError):
                continue
        return filtered

    async def get_candidates(self) -> List[Dict[str, Any]]:
        candidates = await self.get_high_organic_tokens()
        unique_tokens = {}
        for token in candidates:
            mint = token.get('id') or token.get('mint') or token.get('address')
            if mint:
                unique_tokens[mint] = token
        return list(unique_tokens.values())

# ---------------------------
# DexScreener + Birdeye fallback
# ---------------------------
class DexScreenerClient:
    def __init__(self, http: HttpClient, base: str):
        self.http = http
        self.base = base.rstrip("/")

    async def get_token(self, mint: str) -> Dict[str, Any]:
        url = f"{self.base}/tokens/{mint}"
        try:
            return await self.http.get_json(url)
        except Exception as e:
            json_log("dexs_token_error", mint=mint, error=str(e))
            return {}

    async def get_token_pairs(self, mint: str) -> List[Dict[str, Any]]:
        url = f"{self.base}/tokens/{mint}"
        try:
            data = await self.http.get_json(url)
            return data.get('pairs', [])
        except Exception:
            return []

# ---------------------------
# Birdeye client (parcheado)
# ---------------------------
class BirdeyeClient:
    def __init__(self, http: HttpClient, base: str):
        self.http = http
        self.base = base.rstrip("/")

    async def get_candles(self, mint: str, interval: str = "5m", limit: int = 72) -> List[Dict[str, Any]]:
        # Evitar SOL nativo (no soportado en Birdeye)
        if mint == "So11111111111111111111111111111111111111112":
            return []
        url = f"{self.base}/public/market/candles"
        params = {"address": mint, "interval": interval, "limit": limit}
        headers = {"accept": "application/json"}
        try:
            data = await self.http.get_json(url, params=params, headers=headers)
            return data.get("data", []) if isinstance(data, dict) else []
        except Exception as e:
            json_log("birdeye_candles_error", mint=mint, error=str(e))
            return []

# ---------------------------
# ---------------------------
# Raydium client (parcheado)
# ---------------------------
class RaydiumClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def get_new_pools(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Obtiene pools recién creados en Raydium con fallback"""
        try:
            # Endpoint estable
            url = f"{RAYDIUM_API}/pool/list"
            params = {"pageSize": limit, "page": 1, "sortField": "createdAt", "sortType": "desc"}
            data = await self.http.get_json(url, params=params)
            return data.get("data", [])
        except Exception as e:
            json_log("raydium_pools_error", error=str(e))
            # Fallback a AMM v3
            try:
                url = f"{RAYDIUM_API}/ammV3/pools"
                data = await self.http.get_json(url, params={"limit": limit})
                return data.get("data", [])
            except Exception as e2:
                json_log("raydium_pools_fallback_error", error=str(e2))
                return []

# ---------------------------
# RugCheck: LP lock & security
# ---------------------------
class RugCheckClient:
    def __init__(self, http: HttpClient, base: str):
        self.http = http
        self.base = base.rstrip("/")

    async def get_security(self, mint: str) -> Dict[str, Any]:
        url = f"{self.base}/tokens/{mint}"
        try:
            data = await self.http.get_json(url)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            json_log("rugcheck_error", mint=mint, error=str(e))
            return {}

# ---------------------------
# Notifier (Telegram)
# ---------------------------
class TelegramNotifier:
    def __init__(self, app: Application):
        self.app = app

    async def send_message(self, text: str, buttons: Optional[List[Tuple[str,str]]] = None):
        if not TELEGRAM_CHAT_ID:
            json_log("tg_no_chat_id")
            return
        try:
            reply_markup = None
            if buttons:
                keyboard = [[InlineKeyboardButton(lbl, url=url)] for (lbl, url) in buttons]
                reply_markup = InlineKeyboardMarkup(keyboard)
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
                reply_markup=reply_markup
            )
        except Exception as e:
            json_log("tg_send_error", error=str(e))

# ---------------------------
# Queue for analysis
# ---------------------------
class AnalysisQueue:
    def __init__(self, maxsize: int = 300):
        self.queue = asyncio.Queue(maxsize=maxsize)

    async def push(self, item: Dict[str, Any]):
        try:
            await self.queue.put(item)
        except asyncio.QueueFull:
            json_log("queue_full")

    async def pop(self) -> Dict[str, Any]:
        item = await self.queue.get()
        self.queue.task_done()
        return item

# ---------------------------
# Pump.fun monitor
# ---------------------------
class HeliusPumpMonitor:
    def __init__(self, wss_url: str, http: HttpClient, notifier: TelegramNotifier, db: DB, jup: JupiterClient, cache: Cache):
        self.wss_url = wss_url
        self.http = http
        self.notifier = notifier
        self.db = db
        self.jup = jup
        self.cache = cache
        self.running = False
        self.task: Optional[asyncio.Task] = None

    async def start(self):
        if self.running or not self.wss_url:
            return
        self.running = True
        self.task = asyncio.create_task(self._loop())
        json_log("pump_monitor_started")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        json_log("pump_monitor_stopped")

    async def _loop(self):
        attempt = 0
        while self.running:
            try:
                async with websockets.connect(self.wss_url, ping_interval=30, ping_timeout=10) as ws:
                    attempt = 0
                    json_log("wss_connected")
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._process(msg)
                        except Exception as e:
                            json_log("wss_msg_error", error=str(e))
                            continue
            except Exception as e:
                attempt += 1
                json_log("wss_connect_error", attempt=attempt, error=str(e))
                await asyncio.sleep(backoff_delay(attempt))

    async def _process(self, msg: Dict[str, Any]):
        program = msg.get("program") or msg.get("programId")
        if program != "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":
            return
        mint = self._extract_mint_from_message(msg)
        if not mint:
            return
        if self.cache.recently_seen(mint):
            return
        self.cache.mark_seen(mint)

        marketcap = await self._get_marketcap(mint)
        if marketcap is None:
            return

        if PUMP_PRE_GRADUATION_MIN <= marketcap <= PUMP_PRE_GRADUATION_MAX:
            if await self.db.was_notified_recently(mint, "pump_pregrad", 8):
                return
            token_info = await self.jup.get_token_info(mint)
            symbol = token_info.get('symbol', 'UNK') if token_info else msg.get('symbol', 'UNK')
            name = token_info.get('name', '') if token_info else msg.get('name', '')
            if await self._is_fake_token(mint, token_info):
                json_log("pump_fake_filtered", mint=mint, symbol=symbol)
                return
            await self._send_pump_alert(mint, symbol, name, marketcap, token_info)

    def _extract_mint_from_message(self, msg: Dict[str, Any]) -> Optional[str]:
        mint = msg.get("mint") or msg.get("token")
        if mint and 32 <= len(mint) <= 44:
            return mint
        params = msg.get("params") or {}
        result = params.get("result", {}) if isinstance(params, dict) else {}
        logs = result.get("value", {}).get("logs", []) if isinstance(result, dict) else []
        if logs:
            import re
            combined = " ".join(logs)
            matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{32,44}', combined)
            for m in matches:
                if 32 <= len(m) <= 44:
                    return m
        return None

    async def _get_marketcap(self, mint: str) -> Optional[float]:
        cached = self.cache.get_marketcap(mint)
        if cached is not None:
            return cached
        try:
            url = f"{DEXSCREENER_API.rstrip('/')}/tokens/{mint}"
            data = await self.http.get_json(url)
            token = data.get("token") or {}
            mc = token.get("marketCapUsd")
            if mc:
                mc_val = float(mc)
                self.cache.set_marketcap(mint, mc_val)
                return mc_val
        except Exception:
            pass
        return None

    async def _is_fake_token(self, mint: str, token_info: Optional[Dict]) -> bool:
        if not token_info:
            return False
        audit = token_info.get('audit', {})
        mint_disabled = audit.get('mintAuthorityDisabled', False)
        freeze_disabled = audit.get('freezeAuthorityDisabled', False)
        holder_count = token_info.get('holderCount', 0)
        liquidity = token_info.get('liquidity', 0)
        if not mint_disabled:
            return True
        if holder_count < 50:
            return True
        if liquidity < 5000:
            return True
        return False

    async def _send_pump_alert(self, mint: str, symbol: str, name: str, marketcap: float, token_info: Optional[Dict]):
        organic_score = token_info.get('organicScore', 'N/A') if token_info else 'N/A'
        holder_count = token_info.get('holderCount', 'N/A') if token_info else 'N/A'
        liquidity = token_info.get('liquidity', 0) if token_info else 0
        volume_24h = 0
        if token_info and token_info.get('stats24h'):
            volume_24h = token_info['stats24h'].get('buyVolume', 0) + token_info['stats24h'].get('sellVolume', 0)
        audit = token_info.get('audit', {}) if token_info else {}
        mint_disabled = audit.get('mintAuthorityDisabled', False)
        freeze_disabled = audit.get('freezeAuthorityDisabled', False)
        top_holders_pct = audit.get('topHoldersPercentage', 'N/A')

        text = (
            f"🚀 <b>PUMP.FUN - PRE-GRAD</b>\n\n"
            f"<b>{symbol}</b> {name}\n"
            f"<b>MC:</b> ${marketcap:,.0f} / ${PUMP_PRE_GRADUATION_MAX:,.0f}\n\n"
            f"<b>📊 Métricas:</b>\n"
            f"• Organic: {organic_score}\n"
            f"• Holders: {holder_count}\n"
            f"• Liq: ${format_number(liquidity)}\n"
            f"• Vol 24h: ${format_number(volume_24h)}\n\n"
            f"<b>🛡️ Seguridad:</b>\n"
            f"• Mint Revocada: {'✅' if mint_disabled else '❌'}\n"
            f"• Freeze Revocado: {'✅' if freeze_disabled else '❌'}\n"
            f"• Top Holders: {top_holders_pct}%\n\n"
            f"<b>Mint:</b>\n<code>{mint}</code>"
        )
        buttons = [
            ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
            ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"),
            ("Birdeye", f"https://birdeye.so/token/{mint}"),
            ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
        ]
        await self.notifier.send_message(text, buttons)
        await self.db.mark_notified(mint, "pump_pregrad", symbol)

# ---------------------------
# Flat scanner with queue + filters + RugCheck + Birdeye fallback
# ---------------------------
class FlatScanner:
    def __init__(self, jup: JupiterClient, dexs: DexScreenerClient, notifier: TelegramNotifier, db: DB,
                 raydium: RaydiumClient, birdeye: BirdeyeClient, rugcheck: RugCheckClient, analysis_queue: AnalysisQueue):
        self.jup = jup
        self.dexs = dexs
        self.notifier = notifier
        self.db = db
        self.raydium = raydium
        self.birdeye = birdeye
        self.rugcheck = rugcheck
        self.q = analysis_queue
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.worker_task: Optional[asyncio.Task] = None

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._producer_loop())
        self.worker_task = asyncio.create_task(self._worker_loop())
        json_log("flat_scanner_started")

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
        if self.worker_task:
            self.worker_task.cancel()
        try:
            if self.task:
                await self.task
            if self.worker_task:
                await self.worker_task
        except asyncio.CancelledError:
            pass
        json_log("flat_scanner_stopped")

    async def _producer_loop(self):
        while self.running:
            try:
                await self.scan_once()
            except Exception as e:
                json_log("flat_scan_error", error=str(e))
            await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)

    async def _worker_loop(self):
        sem = asyncio.Semaphore(8)
        while self.running:
            item = await self.q.pop()
            async with sem:
                try:
                    await self._analyze(item)
                except Exception as e:
                    json_log("flat_analyze_error", error=str(e))

    async def scan_once(self):
        json_log("scan_start")
        jup_candidates = await self.jup.get_candidates()
        raydium_pools = await self.raydium.get_new_pools()
        raydium_candidates = []
        for pool in raydium_pools:
            mint = pool.get('mintA', {}).get('address')
            if mint:
                token_info = await self.jup.get_token_info(mint)
                if token_info:
                    raydium_candidates.append(token_info)
        all_candidates = {}
        for token in jup_candidates + raydium_candidates:
            token_mint = token.get('id') or token.get('mint') or token.get('address')
            if token_mint:
                all_candidates[token_mint] = token
        candidates = list(all_candidates.values())

        filtered = []
        for token in candidates:
            if await self._is_promising_token(token):
                filtered.append(token)
        json_log("scan_filtered", count=len(filtered))
        for token in filtered:
            await self.q.push(token)

    async def _is_promising_token(self, token: Dict[str, Any]) -> bool:
        """Filtros mejorados para tokens flat"""
        try:
            mint = token.get('id') or token.get('mint') or token.get('address')
            if not mint:
                return False

            if await self.db.was_notified_recently(mint, "flat", FLAT_TOKEN_REPEAT_HOURS):
                return False

            liquidity = float(token.get('liquidity', 0))
            volume_24h = float(token.get('stats24h', {}).get('buyVolume', 0) +
                               token.get('stats24h', {}).get('sellVolume', 0))
            organic_score = token.get('organicScore', 0)
            holder_count = token.get('holderCount', 0)

            # Ratio buys/sells
            buys = token.get('stats24h', {}).get('buys', 0)
            sells = token.get('stats24h', {}).get('sells', 0)
            total_trades = buys + sells
            if total_trades > 0:
                buy_ratio = buys / total_trades
                if buy_ratio < 0.35 or buy_ratio > 0.65:
                    return False

            # Seguridad
            audit = token.get('audit', {})
            mint_disabled = audit.get('mintAuthorityDisabled', False)
            top_holders_pct = audit.get('topHoldersPercentage', 100)

            return (
                liquidity >= FLAT_LIQUIDITY_MIN and
                volume_24h >= FLAT_VOLUME_24H_MIN and
                organic_score >= FLAT_MIN_ORGANIC_SCORE and
                holder_count >= MIN_HOLDER_COUNT and
                mint_disabled and
                top_holders_pct < 25
            )
        except (ValueError, TypeError):
            return False

    def _parse_candles_dexs(self, resp: Dict[str, Any]) -> List[Dict[str, Any]]:
        candles = []
        if not resp:
            return candles
        if "pairs" in resp and resp["pairs"]:
            pair = resp["pairs"][0]
            chart = pair.get("chart", {})
            for candle in chart.get("candles", []):
                candles.append({
                    "time": int(candle.get("time", 0)) // 1000,
                    "open": float(candle.get("open", 0)),
                    "high": float(candle.get("high", 0)),
                    "low": float(candle.get("low", 0)),
                    "close": float(candle.get("close", 0)),
                    "volume_usd": float(candle.get("volumeUsd", 0)),
                })
        return candles

    def _is_flat_improved(self, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Flat con duración mínima y pre‑ruptura"""
        res = {"is_flat": False, "avg_vol": 0.0, "volatility_pct": 100.0,
               "flat_period_hours": CANDLES_HOURS, "pre_breakout": False, "recent_volume_avg": 0.0}
        if not candles:
            return res

        cutoff = now_ts().timestamp() - (CANDLES_HOURS * 3600)
        recent_candles = [c for c in candles if c["time"] >= cutoff]
        if len(recent_candles) < 12:  # al menos 1h de velas de 5m
            return res

        highs = [c["high"] for c in recent_candles]
        lows = [c["low"] for c in recent_candles]
        vols = [c["volume_usd"] for c in recent_candles]
        if not highs or not lows or min(lows) <= 0:
            return res

        volatility_pct = ((max(highs) - min(lows)) / min(lows)) * 100
        avg_volume = sum(vols) / len(vols)
        low_volume_candles = sum(1 for v in vols if v < FLAT_VOLUME_AVG_PER_CANDLE_USD)
        low_volume_ratio = low_volume_candles / len(vols)
        high_volume_spikes = sum(1 for v in vols if v > FLAT_VOLUME_AVG_PER_CANDLE_USD * 3)

        recent = vols[-min(PRE_BREAKOUT_CANDLES_COUNT, len(vols)):]
        recent_volume_avg = sum(recent) / len(recent) if recent else 0

        is_flat = (volatility_pct < FLAT_VOLATILITY_PCT and
                   low_volume_ratio >= 0.7 and
                   high_volume_spikes <= 2)

        pre_breakout = (is_flat and
                        recent_volume_avg > avg_volume * PRE_BREAKOUT_VOLUME_MULTIPLIER and
                        recent_volume_avg > FLAT_VOLUME_AVG_PER_CANDLE_USD * 2)

        res.update({"avg_vol": avg_volume, "volatility_pct": volatility_pct,
                    "is_flat": is_flat, "pre_breakout": pre_breakout,
                    "recent_volume_avg": recent_volume_avg})
        return res

    async def _analyze(self, token: Dict[str, Any]):
        mint = token.get('id') or token.get('mint') or token.get('address')
        if not mint:
            return
        # DexScreener candles
        resp = await self.dexs.get_token(mint)
        candles = self._parse_candles_dexs(resp)
        # Fallback Birdeye if no candles
        if not candles:
            be_c = await self.birdeye.get_candles(mint, interval="5m", limit=72)
            candles = [{
                "time": int(c["unixTime"]),
                "open": float(c.get("open", 0)), "high": float(c.get("high", 0)),
                "low": float(c.get("low", 0)), "close": float(c.get("close", 0)),
                "volume_usd": float(c.get("volume", 0))
            } for c in be_c] if be_c else []
        if not candles:
            return

        # Security: RugCheck LP lock & holder concentration (if provided)
        rc = await self.rugcheck.get_security(mint)
        lp_locked = rc.get("lpLocked", None)  # boolean or %
        lock_info = rc.get("lockInfo", "")
        top_holders_pct = rc.get("topHoldersPercentage", None)

        analysis = self._is_flat_improved(candles)
        if analysis["is_flat"]:
            await self._send_flat_alert(token, analysis, mint, lp_locked, lock_info, top_holders_pct)

    async def _send_flat_alert(self, token: Dict[str, Any], analysis: Dict[str, Any],
                               mint: str, lp_locked: Optional[Any], lock_info: str, top_holders_pct: Optional[float]):
        symbol = token.get('symbol', 'UNK')
        name = token.get('name', '')
        liquidity = token.get('liquidity', 0)
        market_cap = token.get('marketCap', token.get('fdv', 0))
        organic_score = token.get('organicScore', 'N/A')
        holder_count = token.get('holderCount', 'N/A')
        audit = token.get('audit', {})
        mint_disabled = audit.get('mintAuthorityDisabled', False)
        freeze_disabled = audit.get('freezeAuthorityDisabled', False)
        rc_lp = f"{lp_locked} (lock: {lock_info})" if lp_locked is not None else "N/A"
        rc_top = f"{top_holders_pct}%" if top_holders_pct is not None else audit.get('topHoldersPercentage', 'N/A')

        pre_breakout_emoji = "⚡" if analysis.get('pre_breakout', False) else ""
        text = (
            f"🎯 {pre_breakout_emoji} <b>FLAT DETECTADO</b> {pre_breakout_emoji}\n\n"
            f"<b>{symbol}</b> {name}\n"
            f"• Duración: ~{analysis['flat_period_hours']}h\n"
            f"• Volatilidad: {analysis['volatility_pct']:.2f}%\n"
            f"• Vol Prom: ${analysis['avg_vol']:.2f}\n"
            f"• Vol Reciente: ${analysis['recent_volume_avg']:.2f}\n"
            f"• Liq: ${format_number(liquidity)} | MC: ${format_number(market_cap)}\n\n"
            f"<b>Calidad:</b> Organic {organic_score} | Holders {holder_count}\n"
            f"<b>Seguridad:</b> Mint {'✅' if mint_disabled else '❌'} | Freeze {'✅' if freeze_disabled else '❌'}\n"
            f"• LP Lock (RugCheck): {rc_lp}\n"
            f"• Top Holders: {rc_top}\n\n"
            f"<b>Mint:</b>\n<code>{mint}</code>\n"
        )

        if analysis.get('pre_breakout', False):
            text += (
                f"\n<b>Estrategia:</b> Pre-ruptura posible\n"
                f"• Vol reciente {PRE_BREAKOUT_VOLUME_MULTIPLIER}x\n"
                f"• Entrada escalonada | stop cerca del rango\n"
                f"• Objetivo: 2–3x del rango\n"
            )
        else:
            text += (
                f"\n<b>Estrategia:</b> Consolidación\n"
                f"• Esperar ruptura con volumen\n"
                f"• Tamaño pequeño | stops prudentes\n"
            )

        buttons = [
            ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
            ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"),
            ("Birdeye", f"https://birdeye.so/token/{mint}"),
            ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
        ]
        await self.notifier.send_message(text, buttons)
        await self.db.mark_notified(mint, "flat", symbol)

# ---------------------------
# FastAPI + Telegram
# ---------------------------
app = FastAPI()
telegram_app: Optional[Application] = None
db: Optional[DB] = None
pump_monitor: Optional[HeliusPumpMonitor] = None
flat_scanner: Optional[FlatScanner] = None
http_session: Optional[aiohttp.ClientSession] = None
cache: Optional[Cache] = None
analysis_queue: Optional[AnalysisQueue] = None
scheduler: Optional[AsyncIOScheduler] = None

# ---------------------------
# Commands
# ---------------------------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    text = (
        "🤖 <b>Solana Monitor Bot v4.1</b>\n\n"
        "• Pump.fun pre-grad + filtros seguridad\n"
        "• Raydium pools + Jupiter candidatos\n"
        "• Flat + pre-ruptura, duración mínima\n"
        "• Cache persistente, cola de análisis\n"
        "• Botones y resumen cada 8h\n\n"
        "Comandos: /iniciar /detener /status /config /ajustar_flat /ultimos /buscar <mint>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    await pump_monitor.start()
    await flat_scanner.start()
    await update.message.reply_text("✅ Monitores v4.1 iniciados", parse_mode="HTML")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    await pump_monitor.stop()
    await flat_scanner.stop()
    await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    running = (pump_monitor.running if pump_monitor else False) or (flat_scanner.running if flat_scanner else False)
    cnt = await db.count_alerts_last_24h()
    text = (
        f"<b>📊 ESTADO v4.1</b>\n\n"
        f"• Estado: {'🟢 Activos' if running else '🔴 Inactivos'}\n"
        f"• Alertas 24h: {cnt}\n"
        f"• Intervalo: {CHECK_INTERVAL_MINUTES}m\n"
        f"• Cache: SQLite\n"
        f"• Cola: activa\n"
        f"• Resumen 8h: {'🟢' if scheduler else '🔴'}\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    text = (
        "<b>⚙️ Config v4.1</b>\n\n"
        f"Pump: ${PUMP_PRE_GRADUATION_MIN:,.0f}–${PUMP_PRE_GRADUATION_MAX:,.0f}\n"
        f"Flat: Liq ${FLAT_LIQUIDITY_MIN:,.0f}, Vol24h ${FLAT_VOLUME_24H_MIN:,.0f}, "
        f"Volatilidad {FLAT_VOLATILITY_PCT}%, Vol/candle ${FLAT_VOLUME_AVG_PER_CANDLE_USD}\n"
        f"Organic {FLAT_MIN_ORGANIC_SCORE}+ | Holders {MIN_HOLDER_COUNT}+ | No repetir {FLAT_TOKEN_REPEAT_HOURS}h\n"
        f"Pre-ruptura mult: {PRE_BREAKOUT_VOLUME_MULTIPLIER}x candles {PRE_BREAKOUT_CANDLES_COUNT}\n"
        f"Velas: {CANDLES_HOURS}h ({CANDLE_INTERVAL})\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_ajustar_flat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    args = ctx.args or []
    if len(args) < 2:
        text = (
            "<b>Uso:</b> /ajustar_flat <param> <valor>\n"
            "Params: liquidez, vol24h, volatilidad, volumen_promedio, organic_score, holders, intervalo, horas_sin_repetir, pre_ruptura_mult"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        return
    param = args[0].lower()
    val = args[1]
    global FLAT_VOLATILITY_PCT, FLAT_VOLUME_AVG_PER_CANDLE_USD, FLAT_LIQUIDITY_MIN, FLAT_VOLUME_24H_MIN
    global FLAT_MIN_ORGANIC_SCORE, MIN_HOLDER_COUNT, CHECK_INTERVAL_MINUTES, FLAT_TOKEN_REPEAT_HOURS
    global PRE_BREAKOUT_VOLUME_MULTIPLIER
    try:
        if param in ("volatilidad", "volatility"):
            FLAT_VOLATILITY_PCT = float(val)
        elif param in ("volumen_promedio", "avg_vol"):
            FLAT_VOLUME_AVG_PER_CANDLE_USD = float(val)
        elif param in ("liquidez", "liquidity"):
            FLAT_LIQUIDITY_MIN = float(val)
        elif param in ("vol24h", "volume24h"):
            FLAT_VOLUME_24H_MIN = float(val)
        elif param in ("organic_score", "organic"):
            FLAT_MIN_ORGANIC_SCORE = float(val)
        elif param in ("holders", "holder_count"):
            MIN_HOLDER_COUNT = int(val)
        elif param in ("intervalo", "interval"):
            CHECK_INTERVAL_MINUTES = int(val)
        elif param in ("horas_sin_repetir", "repeat_hours"):
            FLAT_TOKEN_REPEAT_HOURS = int(val)
        elif param in ("pre_ruptura_mult", "pre_breakout_mult"):
            PRE_BREAKOUT_VOLUME_MULTIPLIER = float(val)
        else:
            await update.message.reply_text("❌ Parámetro no reconocido.", parse_mode="HTML")
            return
        await update.message.reply_text(f"✅ <code>{param}</code> = <code>{val}</code>", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("❌ Valor inválido.", parse_mode="HTML")

async def cmd_ultimos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    rows = await db.last_notifications(limit=5)
    if not rows:
        await update.message.reply_text("No hay tokens recientes.", parse_mode="HTML")
        return
    text = "<b>🕒 Últimos tokens notificados:</b>\n\n"
    for r in rows:
        text += f"• <b>{r['symbol'] or 'UNK'}</b> ({r['kind']})\n"
        text += f"  {r['notified_at'].strftime('%Y-%m-%d %H:%M')} UTC\n"
        text += f"  <code>{r['mint']}</code>\n\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    args = ctx.args or []
    if len(args) < 1:
        await update.message.reply_text("Uso: /buscar <mint>", parse_mode="HTML")
        return
    mint = args[0].strip()
    # Jupiter info
    token_info = None
    try:
        token_info = await jup_client.get_token_info(mint)
    except Exception:
        token_info = None
    # DexScreener basic
    dexs_resp = await dexs_client.get_token(mint)
    pairs = dexs_resp.get("pairs", [])
    price = None
    liquidity = None
    volume24 = None
    if pairs:
        p0 = pairs[0]
        price = p0.get("priceUsd") or p0.get("priceNative")
        liquidity = p0.get("liquidity", {}).get("usd")
        volume24 = p0.get("volume", {}).get("h24")
    # RugCheck security
    rc = await rugcheck_client.get_security(mint)
    lp_locked = rc.get("lpLocked", "N/A")
    top_holders_pct = rc.get("topHoldersPercentage", "N/A")

    symbol = (token_info or {}).get("symbol", "UNK")
    name = (token_info or {}).get("name", "")
    organic_score = (token_info or {}).get("organicScore", "N/A")
    holders = (token_info or {}).get("holderCount", "N/A")
    audit = (token_info or {}).get("audit", {})
    mint_disabled = audit.get("mintAuthorityDisabled", False)
    freeze_disabled = audit.get("freezeAuthorityDisabled", False)

    text = (
        f"🔎 <b>Consulta de token</b>\n\n"
        f"<b>{symbol}</b> {name}\n"
        f"• Precio: {price or 'N/A'}\n"
        f"• Liquidez: ${format_number(float(liquidity or 0))}\n"
        f"• Vol 24h: ${format_number(float(volume24 or 0))}\n"
        f"• Organic: {organic_score} | Holders: {holders}\n"
        f"• Mint Revocada: {'✅' if mint_disabled else '❌'} | Freeze: {'✅' if freeze_disabled else '❌'}\n"
        f"• LP Lock: {lp_locked} | Top holders: {top_holders_pct}%\n\n"
        f"<b>Mint:</b>\n<code>{mint}</code>"
    )
    buttons = [
        ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
        ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"),
        ("Birdeye", f"https://birdeye.so/token/{mint}"),
        ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
    ]
    await notifier.send_message(text, buttons)

# ---------------------------
# Resumen automático cada 8h
# ---------------------------
async def send_summary():
    cnt = await db.count_alerts_last_24h()
    text = (
        f"📰 <b>Resumen automático (8h)</b>\n\n"
        f"• Alertas últimas 24h: {cnt}\n"
        f"• Pump monitor: {'🟢' if pump_monitor.running else '🔴'}\n"
        f"• Flat scanner: {'🟢' if flat_scanner.running else '🔴'}\n"
        f"• Intervalo: {CHECK_INTERVAL_MINUTES}m\n"
        f"• Filtros: Liq ≥ {FLAT_LIQUIDITY_MIN}, Vol24h ≥ {FLAT_VOLUME_24H_MIN}, "
        f"Organic ≥ {FLAT_MIN_ORGANIC_SCORE}, Holders ≥ {MIN_HOLDER_COUNT}\n"
    )
    await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML")

# ---------------------------
# Initialization
# ---------------------------
async def init_bot():
    global telegram_app, db, pump_monitor, flat_scanner, http_session, cache, analysis_queue
    global jup_client, dexs_client, birdeye_client, raydium_client, rugcheck_client, notifier, scheduler

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no configurado")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no configurado")
    if not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID no configurado")

    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("iniciar", cmd_iniciar))
    telegram_app.add_handler(CommandHandler("detener", cmd_detener))
    telegram_app.add_handler(CommandHandler("status", cmd_status))
    telegram_app.add_handler(CommandHandler("ajustar_flat", cmd_ajustar_flat))
    telegram_app.add_handler(CommandHandler("help", cmd_start))
    telegram_app.add_handler(CommandHandler("config", cmd_config))
    telegram_app.add_handler(CommandHandler("ultimos", cmd_ultimos))
    telegram_app.add_handler(CommandHandler("buscar", cmd_buscar))

    db = DB(DATABASE_URL)
    await db.connect()

    cache = Cache(path="/data/cache.db")
    http_session = aiohttp.ClientSession()
    http_client = HttpClient(http_session)

    jup_client = JupiterClient(http_client)
    dexs_client = DexScreenerClient(http_client, DEXSCREENER_API)
    birdeye_client = BirdeyeClient(http_client, BIRDEYE_API_BASE)
    raydium_client = RaydiumClient(http_client)
    rugcheck_client = RugCheckClient(http_client, RUGCHECK_API_BASE)
    notifier = TelegramNotifier(telegram_app)

    analysis_queue = AnalysisQueue(maxsize=300)

    pump_monitor = HeliusPumpMonitor(HELIUS_WSS_URL, http_client, notifier, db, jup_client, cache)
    flat_scanner = FlatScanner(jup_client, dexs_client, notifier, db, raydium_client, birdeye_client, rugcheck_client, analysis_queue)

    # Scheduler cada 8 horas
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_summary, "interval", hours=8)
    scheduler.start()

@app.on_event("startup")
async def on_startup():
    json_log("startup", port=PORT)
    await init_bot()
    await telegram_app.initialize()
    await telegram_app.start()
    await pump_monitor.start()
    await flat_scanner.start()
    json_log("monitors_started")

@app.on_event("shutdown")
async def on_shutdown():
    json_log("shutdown")
    await pump_monitor.stop()
    await flat_scanner.stop()
    if scheduler:
        scheduler.shutdown(wait=False)
    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()
    if http_session:
        await http_session.close()
    if db:
        await db.close()
    json_log("shutdown_done")

@app.get("/test")
async def test_endpoint():
    return {
        "status": "ok",
        "bot_running": telegram_app is not None,
        "chat_id": TELEGRAM_CHAT_ID,
        "version": "4.1",
        "features": [
            "Raydium + Jupiter candidates",
            "Pre-Breakout Detection",
            "Advanced Security (RugCheck)",
            "SQLite Cache + Queue",
            "Fallback Birdeye",
            "Resumen cada 8h",
            "Ultimos + Buscar comandos"
        ]
    }

@app.get("/")
async def root():
    return {"status": "Bot v4.1 funcionando", "version": "4.1", "webhook": "active"}

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, req: Request):
    try:
        if token != TELEGRAM_BOT_TOKEN:
            raise HTTPException(status_code=403, detail="Invalid token")
        body = await req.json()
        update = Update.de_json(body, telegram_app.bot)
        if update.effective_user:
            if update.effective_user.id != TELEGRAM_CHAT_ID:
                return JSONResponse({"ok": True, "note": "ignored - private bot"})
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        json_log("webhook_error", error=str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

def run_uvicorn():
    import uvicorn
    uvicorn.run("telegram_bot_v4_1:app", host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    asyncio.run(init_bot())
    run_uvicorn()
