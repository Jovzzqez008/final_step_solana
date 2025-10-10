# telegram_bot_final_optimized.py
# Solana Monitor Bot v5_pro_optimized - FIXED Raydium API + Dual Objectives
# Objetivo 1: Pump.fun → Raydium graduation (tokens nuevos)
# Objetivo 2: Flat detection + breakout (tokens con tiempo en el mercado)
#
# Variables de entorno requeridas:
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL
# Opcionales: DEXSCREENER_API, HELIUS_WSS_URL

import os
import asyncio
import json
import re
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
# CONFIG / ENV
# ---------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL", "")

CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
RAYDIUM_GRAD_WINDOW_SECONDS = int(os.getenv("RAYDIUM_GRAD_WINDOW_SECONDS", "120"))

PUMP_PRE_GRADUATION_MIN = float(os.getenv("PUMP_PRE_GRADUATION_MIN", "55000"))
PUMP_PRE_GRADUATION_MAX = float(os.getenv("PUMP_PRE_GRADUATION_MAX", "68000"))
PUMP_MIN_HOLDERS = int(os.getenv("PUMP_MIN_HOLDERS", "40"))

PRE_BREAKOUT_VOLUME_MULTIPLIER = float(os.getenv("PRE_BREAKOUT_VOLUME_MULTIPLIER", "2.5"))
PRE_BREAKOUT_CANDLES_COUNT = int(os.getenv("PRE_BREAKOUT_CANDLES_COUNT", "3"))

CACHE_DB_PATH = os.getenv("CACHE_DB_PATH", "/data/cache_v5.db")

MAX_RETRIES = 6
BASE_BACKOFF = 1.0

# ---------------------------
# UTILITIES
# ---------------------------
def now_ts() -> datetime:
    return datetime.utcnow()

def iso_ts(ts: float) -> str:
    return datetime.utcfromtimestamp(ts).isoformat() + "Z"

def backoff_delay(attempt: int) -> float:
    return BASE_BACKOFF * (2 ** attempt) * (0.9 + 0.2 * (os.urandom(1)[0] / 255))

def format_number(num: float) -> str:
    try:
        num = float(num)
    except Exception:
        return "N/A"
    if num >= 1_000_000:
        return f"{num/1_000_000:.2f}M"
    elif num >= 1_000:
        return f"{num/1_000:.2f}K"
    else:
        return f"{num:.2f}"

def safe_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def json_log(event: str, **kw):
    log = {"ts": now_ts().isoformat(), "event": event, **kw}
    print(json.dumps(log, ensure_ascii=False))

# ---------------------------
# POSTGRES SCHEMA
# ---------------------------
DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS notified_tokens (
    mint TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    symbol TEXT,
    notified_at TIMESTAMP WITH TIME ZONE NOT NULL,
    meta JSONB
);
CREATE INDEX IF NOT EXISTS idx_notified_kind ON notified_tokens(kind);
CREATE INDEX IF NOT EXISTS idx_notified_time ON notified_tokens(notified_at);

CREATE TABLE IF NOT EXISTS flat_watchlist (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    detected_at TIMESTAMP WITH TIME ZONE NOT NULL,
    flat_low REAL,
    flat_high REAL,
    avg_volume REAL,
    notes TEXT,
    meta JSONB
);
CREATE INDEX IF NOT EXISTS idx_flat_detected_at ON flat_watchlist(detected_at);

CREATE TABLE IF NOT EXISTS smart_wallets (
    wallet TEXT PRIMARY KEY,
    first_seen TIMESTAMP WITH TIME ZONE NOT NULL,
    score REAL DEFAULT 0,
    meta JSONB
);

CREATE TABLE IF NOT EXISTS wallet_buys (
    id BIGSERIAL PRIMARY KEY,
    wallet TEXT,
    mint TEXT,
    amount REAL,
    price REAL,
    ts TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_wallet_buys_wallet ON wallet_buys(wallet);
CREATE INDEX IF NOT EXISTS idx_wallet_buys_mint ON wallet_buys(mint);
"""

# ---------------------------
# CACHE (sqlite) for small fast storage
# ---------------------------
class Cache:
    def __init__(self, path: str = CACHE_DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS marketcap(mint TEXT PRIMARY KEY, mc REAL, ts TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS recent_mints(mint TEXT PRIMARY KEY, ts TEXT)")
        self.conn.commit()

    def get_marketcap(self, mint: str) -> Optional[float]:
        cur = self.conn.execute("SELECT mc FROM marketcap WHERE mint=?", (mint,))
        r = cur.fetchone()
        return r[0] if r else None

    def set_marketcap(self, mint: str, mc: float):
        self.conn.execute("REPLACE INTO marketcap(mint,mc,ts) VALUES(?,?,?)", (mint, mc, now_ts().isoformat()))
        self.conn.commit()

    def recently_seen(self, mint: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM recent_mints WHERE mint=?", (mint,))
        return cur.fetchone() is not None

    def mark_seen(self, mint: str):
        self.conn.execute("REPLACE INTO recent_mints(mint,ts) VALUES(?,?)", (mint, now_ts().isoformat()))
        self.conn.commit()

# ---------------------------
# HTTP client with retries
# ---------------------------
class HttpClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_json(self, url: str, params: Dict[str, Any] = None, headers: Dict[str,str] = None) -> Any:
        attempt = 0
        while attempt < MAX_RETRIES:
            try:
                async with self.session.get(url, params=params, headers=headers, timeout=15) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status in (429, 502, 503):
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
# API Clients (Dexscreener, Raydium V2 FIXED, Pump.fun)
# ---------------------------
class DexScreenerClient:
    def __init__(self, http: HttpClient, base: str = DEXSCREENER_API):
        self.http = http
        self.base = base.rstrip("/")

    async def token_info(self, mint: str) -> Dict[str,Any]:
        url = f"{self.base}/tokens/{mint}"
        try:
            return await self.http.get_json(url)
        except Exception as e:
            json_log("dexs_token_error", mint=mint, error=str(e))
            return {}

    async def get_recent_pairs(self, limit: int = 50) -> List[Dict[str,Any]]:
        """Obtener pairs recientes de DexScreener para flat detection"""
        try:
            url = f"{self.base}/pairs"
            params = {
                "sort": "createdAt",
                "order": "desc", 
                "limit": limit,
                "chain": "solana"
            }
            data = await self.http.get_json(url, params=params)
            return data.get("pairs", [])
        except Exception as e:
            json_log("dexs_pairs_error", error=str(e))
            return []

class RaydiumClient:
    def __init__(self, http: HttpClient, base: str = "https://api.raydium.io/v2"):
        self.http = http
        self.base = base.rstrip("/")

    async def get_new_pools(self, limit: int = 200) -> List[Dict[str,Any]]:
        try:
            # NUEVO ENDPOINT FIXED - Raydium v2
            url = f"{self.base}/sdk/liquidity/mainnet.json"
            data = await self.http.get_json(url)
            
            pools = []
            
            # Procesar pools oficiales
            official_pools = data.get("official", [])
            for pool in official_pools:
                try:
                    pool_data = {
                        "id": pool.get("id"),
                        "baseMint": pool.get("baseMint"),
                        "quoteMint": pool.get("quoteMint"),
                        "lpMint": pool.get("lpMint"),
                        "baseDecimals": pool.get("baseDecimals"),
                        "quoteDecimals": pool.get("quoteDecimals"),
                        "lpDecimals": pool.get("lpDecimals"),
                        "version": pool.get("version", 4),
                        "programId": pool.get("programId"),
                        "authority": pool.get("authority"),
                        "openOrders": pool.get("openOrders"),
                        "targetOrders": pool.get("targetOrders"),
                        "baseVault": pool.get("baseVault"),
                        "quoteVault": pool.get("quoteVault"),
                        "withdrawQueue": pool.get("withdrawQueue"),
                        "lpVault": pool.get("lpVault"),
                        "marketVersion": 3,
                        "marketProgramId": pool.get("marketProgramId"),
                        "marketId": pool.get("marketId"),
                        "marketAuthority": pool.get("marketAuthority"),
                        "marketBaseVault": pool.get("marketBaseVault"),
                        "marketQuoteVault": pool.get("marketQuoteVault"),
                        "marketBids": pool.get("marketBids"),
                        "marketAsks": pool.get("marketAsks"),
                        "marketEventQueue": pool.get("marketEventQueue"),
                        "name": f"{pool.get('baseMint', '')[:4]}.../{pool.get('quoteMint', '')[:4]}..."
                    }
                    pools.append(pool_data)
                except Exception as e:
                    json_log("raydium_pool_parse_error", error=str(e))
                    continue
            
            json_log("raydium_pools_fetched", count=len(pools), official=len(official_pools))
            return pools[:limit]
            
        except Exception as e:
            json_log("raydium_pools_error", error=str(e))
            return []

# ---------------------------
# Notifier (Telegram) — HTML safe
# ---------------------------
class TelegramNotifier:
    def __init__(self, app):
        self.app = app

    async def send(self, text: str, buttons: Optional[List[Tuple[str,str]]] = None):
        if not TELEGRAM_CHAT_ID:
            json_log("tg_no_chat_id")
            return
        try:
            reply_markup = None
            if buttons:
                keyboard = [[InlineKeyboardButton(lbl, url=url)] for (lbl, url) in buttons]
                reply_markup = InlineKeyboardMarkup(keyboard)
            await self.app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=False, reply_markup=reply_markup)
        except Exception as e:
            json_log("tg_send_error", error=str(e))

# ---------------------------
# Core monitors
# ---------------------------
class MonitorCore:
    def __init__(self, http: HttpClient, db_pool, cache: Cache, notifier: TelegramNotifier):
        self.http = http
        self.db_pool = db_pool
        self.cache = cache
        self.notifier = notifier
        self.dexs = DexScreenerClient(http)
        self.raydium = RaydiumClient(http)

    async def get_marketcap(self, mint: str) -> Optional[float]:
        mc = self.cache.get_marketcap(mint)
        if mc is not None:
            return mc
        try:
            data = await self.dexs.token_info(mint)
            token = data.get("token") or {}
            mc = token.get("marketCapUsd") or token.get("marketCap")
            if mc:
                mc = float(mc)
                self.cache.set_marketcap(mint, mc)
                return mc
        except Exception:
            pass
        return None

    async def record_wallet_buy(self, wallet: str, mint: str, amount: float, price: float):
        q = "INSERT INTO wallet_buys(wallet,mint,amount,price,ts) VALUES($1,$2,$3,$4,$5)"
        async with self.db_pool.acquire() as conn:
            await conn.execute(q, wallet, mint, amount, price, now_ts())

    async def mark_smart_wallet(self, wallet: str, score: float = 1.0, meta: Dict = None):
        q = "INSERT INTO smart_wallets(wallet,first_seen,score,meta) VALUES($1,$2,$3,$4) ON CONFLICT (wallet) DO UPDATE SET score=smart_wallets.score + EXCLUDED.score"
        async with self.db_pool.acquire() as conn:
            await conn.execute(q, wallet, now_ts(), score, json.dumps(meta or {}))

    async def is_smart_wallet(self, wallet: str) -> bool:
        q = "SELECT score FROM smart_wallets WHERE wallet=$1"
        async with self.db_pool.acquire() as conn:
            r = await conn.fetchrow(q, wallet)
            return bool(r and r.get("score", 0) >= 1.0)

# ---------------------------
# Flat detector + Watchlist (OBJETIVO 2 - Tokens con tiempo)
# ---------------------------
class FlatDetector:
    def __init__(self, core: MonitorCore, db_pool):
        self.core = core
        self.db_pool = db_pool
        self.watch_check_task: Optional[asyncio.Task] = None
        self.running = False
        self.last_scan_time = None

    def candles_parse_dexs(self, dexs_resp: Dict[str,Any]) -> List[Dict[str,Any]]:
        candles = []
        if not dexs_resp:
            return candles
        pairs = dexs_resp.get("pairs", [])
        if not pairs:
            return candles
        chart = pairs[0].get("chart", {})
        for c in chart.get("candles", []):
            try:
                t = int(c.get("time", 0))//1000
                candles.append({
                    "time": t,
                    "open": float(c.get("open",0)),
                    "high": float(c.get("high",0)),
                    "low": float(c.get("low",0)),
                    "close": float(c.get("close",0)),
                    "volume_usd": float(c.get("volumeUsd",0) or c.get("volumeUsd",0))
                })
            except Exception:
                continue
        return candles

    def analyze_flat(self, candles: List[Dict[str,Any]]) -> Optional[Dict[str,Any]]:
        if not candles or len(candles) < 12:
            return None
        recent = candles[-12:]  # last ~1h of 5m candles
        highs = [c["high"] for c in recent]
        lows = [c["low"] for c in recent]
        vols = [c["volume_usd"] for c in recent]
        if not highs or not lows or min(lows) <= 0:
            return None
        volatility_pct = ((max(highs) - min(lows)) / min(lows)) * 100
        avg_vol = sum(vols)/len(vols)
        low_vol_ratio = sum(1 for v in vols if v < avg_vol*0.7) / len(vols)
        if volatility_pct < 12.0 and low_vol_ratio >= 0.6:
            flat_low = min(lows)
            flat_high = max(highs)
            return {"is_flat": True, "flat_low": flat_low, "flat_high": flat_high, "avg_vol": avg_vol, "volatility_pct": volatility_pct}
        return None

    async def add_to_watchlist(self, mint: str, symbol: str, flat: Dict[str,Any], meta: Dict = None):
        q = """
        INSERT INTO flat_watchlist(mint,symbol,detected_at,flat_low,flat_high,avg_volume,notes,meta)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (mint) DO UPDATE SET detected_at=EXCLUDED.detected_at, flat_low=EXCLUDED.flat_low, flat_high=EXCLUDED.flat_high, avg_volume=EXCLUDED.avg_volume, meta=EXCLUDED.meta
        """
        async with self.db_pool.acquire() as conn:
            await conn.execute(q, mint, symbol, now_ts(), flat["flat_low"], flat["flat_high"], flat["avg_vol"], "", json.dumps(meta or {}))

    async def remove_from_watchlist(self, mint: str):
        q = "DELETE FROM flat_watchlist WHERE mint=$1"
        async with self.db_pool.acquire() as conn:
            await conn.execute(q, mint)

    async def get_watchlist(self, limit:int=50) -> List[asyncpg.Record]:
        q = "SELECT * FROM flat_watchlist ORDER BY detected_at DESC LIMIT $1"
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(q, limit)
            return rows

    async def scan_for_flat_tokens(self):
        """Escanea tokens recientes para detectar patrones flat"""
        try:
            pairs = await self.core.dexs.get_recent_pairs(limit=100)
            flat_detected = 0
            
            for pair in pairs:
                try:
                    # Filtrar pairs con suficiente antigüedad (más de 4 horas)
                    created_at = pair.get("createdAt")
                    if created_at:
                        if isinstance(created_at, str):
                            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        else:
                            created_dt = datetime.fromtimestamp(created_at/1000)
                        
                        # Solo tokens con más de 4 horas de antigüedad
                        if (now_ts() - created_dt) < timedelta(hours=4):
                            continue
                    
                    base_token = pair.get("baseToken", {})
                    mint = base_token.get("address")
                    symbol = base_token.get("symbol", "UNK")
                    
                    if not mint:
                        continue
                    
                    # Verificar si ya está en watchlist
                    async with self.db_pool.acquire() as conn:
                        existing = await conn.fetchrow("SELECT 1 FROM flat_watchlist WHERE mint=$1", mint)
                        if existing:
                            continue
                    
                    # Obtener datos de velas
                    token_info = await self.core.dexs.token_info(mint)
                    candles = self.candles_parse_dexs(token_info)
                    
                    if not candles:
                        continue
                    
                    # Analizar patrón flat
                    flat_analysis = self.analyze_flat(candles)
                    if flat_analysis and flat_analysis["is_flat"]:
                        await self.add_to_watchlist(mint, symbol, flat_analysis, {
                            "liquidity": pair.get("liquidity", {}).get("usd"),
                            "volume_24h": pair.get("volume", {}).get("h24"),
                            "price": pair.get("priceUsd")
                        })
                        flat_detected += 1
                        json_log("flat_token_detected", mint=mint, symbol=symbol)
                        
                except Exception as e:
                    json_log("flat_scan_pair_error", error=str(e))
                    continue
            
            if flat_detected > 0:
                json_log("flat_scan_complete", detected=flat_detected)
                
        except Exception as e:
            json_log("flat_scan_error", error=str(e))

    async def watch_loop(self, notifier: TelegramNotifier):
        self.running = True
        while self.running:
            try:
                # Ejecutar scan cada 30 minutos
                if not self.last_scan_time or (now_ts() - self.last_scan_time) > timedelta(minutes=30):
                    await self.scan_for_flat_tokens()
                    self.last_scan_time = now_ts()
                
                # Monitorear watchlist existente
                rows = await self.get_watchlist(limit=200)
                for r in rows:
                    mint = r["mint"]
                    symbol = r["symbol"] or "UNK"
                    avg_vol = float(r["avg_volume"] or 0)
                    flat_low = float(r["flat_low"] or 0)
                    flat_high = float(r["flat_high"] or 0)
                    
                    dexs = await self.core.dexs.token_info(mint)
                    candles = self.candles_parse_dexs(dexs)
                    if not candles:
                        continue
                    
                    latest = candles[-1]
                    recent_vols = [c["volume_usd"] for c in candles[-PRE_BREAKOUT_CANDLES_COUNT:]] if PRE_BREAKOUT_CANDLES_COUNT <= len(candles) else [c["volume_usd"] for c in candles[-3:]]
                    recent_avg = sum(recent_vols)/len(recent_vols)
                    price = latest["close"]
                    
                    if price > flat_high and recent_avg > float(avg_vol) * PRE_BREAKOUT_VOLUME_MULTIPLIER:
                        smart_signal = False
                        async with self.core.db_pool.acquire() as conn:
                            buys = await conn.fetch("SELECT wallet,amount,price,ts FROM wallet_buys WHERE mint=$1 ORDER BY ts DESC LIMIT 10", mint)
                            for b in buys:
                                if await self.core.is_smart_wallet(b["wallet"]):
                                    smart_signal = True
                                    break
                        
                        pre = "⚡ <b>EXPLOSIÓN POST-FLAT</b> ⚡" if smart_signal else "⚡ <b>BREAKOUT POST-FLAT</b>"
                        text = (
                            f"{pre}\n\n"
                            f"<b>{safe_html(symbol)}</b>\n"
                            f"• Price: {price:.6f}\n"
                            f"• Flat range: {flat_low:.6f} - {flat_high:.6f}\n"
                            f"• Avg vol (flat): ${format_number(avg_vol)}\n"
                            f"• Vol recientes: ${format_number(recent_avg)}\n"
                            f"\n<b>Mint:</b>\n<code>{mint}</code>\n"
                        )
                        if smart_signal:
                            text += "\n<b>Smart money detected</b> — wallets with good history have bought.\n"
                        
                        buttons = [
                            ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
                            ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"),
                            ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
                        ]
                        await notifier.send(text, buttons)
                        
                        async with self.core.db_pool.acquire() as conn:
                            await conn.execute("INSERT INTO notified_tokens(mint,kind,symbol,notified_at,meta) VALUES($1,$2,$3,$4,$5) ON CONFLICT (mint) DO UPDATE SET notified_at=EXCLUDED.notified_at, kind=EXCLUDED.kind", 
                                             mint, "post_flat_breakout", symbol, now_ts(), json.dumps({"recent_avg": recent_avg}))
                        
                        await self.remove_from_watchlist(mint)
                
                await asyncio.sleep(max(10, CHECK_INTERVAL_MINUTES*60//2))
                
            except Exception as e:
                json_log("flat_watch_loop_error", error=str(e))
                await asyncio.sleep(5)

    async def start(self, notifier: TelegramNotifier):
        if self.watch_check_task and not self.watch_check_task.done():
            return
        self.watch_check_task = asyncio.create_task(self.watch_loop(notifier))

    async def stop(self):
        self.running = False
        if self.watch_check_task:
            self.watch_check_task.cancel()
            try:
                await self.watch_check_task
            except Exception:
                pass

# ---------------------------
# Raydium graduation monitor FIXED (OBJETIVO 1 - Tokens nuevos)
# ---------------------------
class RaydiumGraduationMonitor:
    def __init__(self, core: MonitorCore, db_pool, notifier: TelegramNotifier):
        self.core = core
        self.db_pool = db_pool
        self.notifier = notifier
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.last_checked_pools = set()

    async def check_once(self):
        pools = await self.core.raydium.get_new_pools(limit=300)
        now_ts_s = now_ts().timestamp()
        
        new_pools_found = 0
        notified_count = 0
        
        for pool in pools:
            try:
                pool_id = pool.get("id")
                if not pool_id:
                    continue
                    
                # Evitar revisar el mismo pool múltiples veces
                if pool_id in self.last_checked_pools:
                    continue
                    
                self.last_checked_pools.add(pool_id)
                if len(self.last_checked_pools) > 1000:
                    self.last_checked_pools = set(list(self.last_checked_pools)[-500:])
                
                # Identificar el token (baseMint es el token nuevo)
                mint = pool.get("baseMint")
                if not mint:
                    continue
                    
                # Verificar si ya notificamos este mint
                async with self.db_pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT 1 FROM notified_tokens WHERE mint=$1 AND kind=$2", mint, "raydium_grad")
                    if row:
                        continue
                
                new_pools_found += 1
                
                # Obtener info del token desde DexScreener
                mc = await self.core.get_marketcap(mint)
                if mc is None:
                    continue
                    
                # FILTRO PRINCIPAL: Market cap dentro del rango de pre-graduación
                if not (PUMP_PRE_GRADUATION_MIN <= mc <= PUMP_PRE_GRADUATION_MAX):
                    continue
                    
                token_info = await self.core.dexs.token_info(mint)
                token_obj = token_info.get("token") or {}
                holders = int(token_obj.get("holderCount") or token_obj.get("holders") or 0)
                
                # Verificar holders mínimos
                if holders < PUMP_MIN_HOLDERS:
                    continue
                    
                symbol = token_obj.get("symbol") or "UNK"
                name = token_obj.get("name") or ""
                
                # VERIFICAR SI VIENE DE PUMP.FUN (OBJETIVO PRINCIPAL)
                is_from_pump = False
                pairs_info = token_info.get("pairs", [])
                for p in pairs_info:
                    url = p.get("url", "").lower()
                    dex_id = p.get("dexId", "").lower()
                    if "pump.fun" in url or "pump.fun" in dex_id:
                        is_from_pump = True
                        break
                
                # SOLO NOTIFICAR SI ES DE PUMP.FUN (OBJETIVO 1)
                if not is_from_pump:
                    continue
                
                text = (
                    f"🔥 <b>PUMP.FUN → RAYDIUM - GRADUACIÓN DETECTADA</b>\n\n"
                    f"<b>{safe_html(symbol)}</b> {safe_html(name)}\n"
                    f"<b>MC:</b> ${mc:,.0f}\n"
                    f"• Holders: {holders}\n"
                    f"• Ventana: {RAYDIUM_GRAD_WINDOW_SECONDS}s\n\n"
                    f"<b>Mint:</b>\n<code>{mint}</code>\n"
                    f"• <b>Recién graduado de Pump.fun a Raydium</b>"
                )
                buttons = [
                    ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
                    ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"),
                    ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
                    ("Raydium", f"https://raydium.io/swap/?inputCurrency=sol&outputCurrency={mint}"),
                ]
                await self.notifier.send(text, buttons)
                notified_count += 1
                
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO notified_tokens(mint,kind,symbol,notified_at,meta) VALUES($1,$2,$3,$4,$5) ON CONFLICT(mint) DO UPDATE SET notified_at=EXCLUDED.notified_at, kind=EXCLUDED.kind", 
                        mint, "raydium_grad", symbol, now_ts(), json.dumps({"mc": mc, "from_pump": True})
                    )
                
                await asyncio.sleep(0.2)  # Rate limiting
                
            except Exception as e:
                json_log("raydium_pool_process_error", error=str(e))
                continue
        
        if new_pools_found > 0:
            json_log("raydium_check_complete", new_pools=new_pools_found, notified=notified_count)

    async def loop(self):
        self.running = True
        while self.running:
            try:
                await self.check_once()
            except Exception as e:
                json_log("raydium_grad_loop_error", error=str(e))
            await asyncio.sleep(max(10, CHECK_INTERVAL_MINUTES * 60 // 3))

    async def start(self):
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except Exception:
                pass

# ---------------------------
# Helius pump monitor (optional) — listens to WSS for mints
# ---------------------------
class HeliusPumpMonitor:
    def __init__(self, wss_url: str, core: MonitorCore, db_pool, notifier: TelegramNotifier):
        self.wss_url = wss_url
        self.core = core
        self.db_pool = db_pool
        self.notifier = notifier
        self.running = False
        self.task: Optional[asyncio.Task] = None

    def extract_mint_from_msg(self, msg: Dict[str,Any]) -> Optional[str]:
        if not msg:
            return None
        mint = msg.get("mint") or msg.get("token")
        if mint and 32 <= len(mint) <= 44:
            return mint
        logs = []
        if isinstance(msg.get("params"), dict):
            logs = msg["params"].get("result", {}).get("value", {}).get("logs", []) or []
        if logs:
            combined = " ".join(logs)
            matches = re.findall(r'[1-9A-HJ-NP-Za-km-z]{32,44}', combined)
            for m in matches:
                if 32 <= len(m) <= 44:
                    return m
        return None

    async def start(self):
        if not self.wss_url:
            return
        if self.task and not self.task.done():
            return
        self.running = True
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except Exception:
                pass

    async def _loop(self):
        attempt = 0
        while self.running:
            try:
                async with websockets.connect(self.wss_url, ping_interval=30, ping_timeout=10) as ws:
                    attempt = 0
                    json_log("helius_wss_connected")
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            msg = json.loads(raw)
                            mint = self.extract_mint_from_msg(msg)
                            if not mint:
                                continue
                            if self.core.cache.recently_seen(mint):
                                continue
                            self.core.cache.mark_seen(mint)
                            mc = await self.core.get_marketcap(mint)
                            if mc is None:
                                continue
                            if PUMP_PRE_GRADUATION_MIN <= mc <= PUMP_PRE_GRADUATION_MAX:
                                token_info = await self.core.dexs.token_info(mint)
                                token_obj = token_info.get("token") or {}
                                symbol = token_obj.get("symbol") or "UNK"
                                holders = int(token_obj.get("holderCount") or token_obj.get("holders") or 0)
                                if holders < PUMP_MIN_HOLDERS:
                                    continue
                                text = (
                                    f"🚀 <b>PUMP.FUN - PRE-GRAD</b>\n\n"
                                    f"<b>{safe_html(symbol)}</b>\n"
                                    f"<b>MC:</b> ${mc:,.0f}\n"
                                    f"• Holders: {holders}\n\n"
                                    f"<b>Mint:</b>\n<code>{mint}</code>"
                                )
                                buttons = [
                                    ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
                                    ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"),
                                    ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
                                ]
                                await self.notifier.send(text, buttons)
                                async with self.db_pool.acquire() as conn:
                                    await conn.execute("INSERT INTO notified_tokens(mint,kind,symbol,notified_at,meta) VALUES($1,$2,$3,$4,$5) ON CONFLICT (mint) DO UPDATE SET notified_at=EXCLUDED.notified_at, kind=EXCLUDED.kind", mint, "pump_pregrad", symbol, now_ts(), json.dumps({"mc": mc}))
                        except Exception as e:
                            json_log("helius_msg_error", error=str(e))
                            continue
            except Exception as e:
                attempt += 1
                json_log("helius_connect_error", attempt=attempt, error=str(e))
                await asyncio.sleep(backoff_delay(attempt))

# ---------------------------
# Commands & App wiring
# ---------------------------
app = FastAPI()
telegram_app: Optional[Application] = None
db_pool: Optional[asyncpg.pool.Pool] = None
http_session: Optional[aiohttp.ClientSession] = None
http_client: Optional[HttpClient] = None
cache: Optional[Cache] = None
notifier: Optional[TelegramNotifier] = None
core: Optional[MonitorCore] = None
flat_detector: Optional[FlatDetector] = None
raydium_grad: Optional[RaydiumGraduationMonitor] = None
helius_monitor: Optional[HeliusPumpMonitor] = None
scheduler: Optional[AsyncIOScheduler] = None

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    text = (
        "🤖 <b>Solana Monitor Bot v5_pro_optimized</b>\n\n"
        "<b>OBJETIVO 1:</b> Pump.fun → Raydium graduation (tokens nuevos)\n"
        "<b>OBJETIVO 2:</b> Flat detection + breakout (tokens con tiempo)\n\n"
        "Commands: /iniciar /detener /status /mints_list /watchlist /rank /buscar <mint>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_iniciar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    if helius_monitor:
        await helius_monitor.start()
    if raydium_grad:
        await raydium_grad.start()
    if flat_detector:
        await flat_detector.start(notifier)
    await update.message.reply_text("✅ Monitores iniciados (v5_pro_optimized)", parse_mode="HTML")

async def cmd_detener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    if helius_monitor:
        await helius_monitor.stop()
    if raydium_grad:
        await raydium_grad.stop()
    if flat_detector:
        await flat_detector.stop()
    await update.message.reply_text("⛔ Monitores detenidos", parse_mode="HTML")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    running = (raydium_grad.running if raydium_grad else False)
    cnt = 0
    if db_pool:
        async with db_pool.acquire() as conn:
            v = await conn.fetchval("SELECT COUNT(*) FROM notified_tokens WHERE notified_at >= $1", now_ts() - timedelta(hours=24))
            cnt = int(v or 0)
    text = (
        f"<b>📊 ESTADO v5_pro_optimized</b>\n\n"
        f"• Raydium grad monitor: {'🟢' if running else '🔴'}\n"
        f"• Alerts 24h: {cnt}\n"
        f"• Flat watchlist: see /watchlist\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_mints_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    if not db_pool:
        await update.message.reply_text("DB not ready", parse_mode="HTML")
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT mint, kind, symbol, notified_at FROM notified_tokens ORDER BY notified_at DESC LIMIT 20")
    if not rows:
        await update.message.reply_text("No hay tokens notificados.", parse_mode="HTML")
        return
    text = "<b>🔗 Últimos 20 mints</b>\n\n"
    keyboard = []
    for r in rows:
        sym = r["symbol"] or "UNK"
        mint = r["mint"]
        text += f"• <b>{safe_html(sym)}</b> <code>{mint[:8]}...{mint[-8:]}</code>\n"
        keyboard.append([
            InlineKeyboardButton("Dex", url=f"https://dexscreener.com/solana/{mint}"),
            InlineKeyboardButton("Rug", url=f"https://rugcheck.xyz/tokens/{mint}"),
            InlineKeyboardButton("Jup", url=f"https://jup.ag/swap/{mint}-SOL"),
        ])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", reply_markup=reply_markup)

async def cmd_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    rows = await flat_detector.get_watchlist(limit=50)
    if not rows:
        await update.message.reply_text("Watchlist vacía.", parse_mode="HTML")
        return
    text = "<b>👀 Watchlist (Flat tokens)</b>\n\n"
    keyboard = []
    for r in rows:
        mint = r["mint"]
        sym = r["symbol"] or "UNK"
        det = r["detected_at"]
        text += f"• <b>{safe_html(sym)}</b> <code>{mint[:8]}...{mint[-8:]}</code> detected {det.strftime('%Y-%m-%d %H:%M')}\n"
        keyboard.append([
            InlineKeyboardButton("Dex", url=f"https://dexscreener.com/solana/{mint}"),
        ])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML", reply_markup=reply_markup)

async def cmd_rank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    if not db_pool:
        await update.message.reply_text("DB not ready", parse_mode="HTML")
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT mint, symbol, meta FROM flat_watchlist")
    scored = []
    for r in rows:
        meta = r["meta"] or {}
        try:
            avg = float(meta.get("avg_vol", 1))
            recent = float(meta.get("recent_avg", avg))
            score = recent / (avg+1e-9)
        except Exception:
            score = 0.0
        scored.append((r["mint"], r["symbol"] or "UNK", score))
    scored.sort(key=lambda x: x[2], reverse=True)
    text = "<b>🏆 Ranking probable breakouts</b>\n\n"
    for mint, sym, score in scored[:20]:
        text += f"• <b>{safe_html(sym)}</b> score {score:.2f}\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
        return
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Uso: /buscar <mint>", parse_mode="HTML")
        return
    mint = args[0].strip()
    dexs = await core.dexs.token_info(mint)
    token = dexs.get("token") or {}
    symbol = token.get("symbol") or "UNK"
    name = token.get("name") or ""
    mc = token.get("marketCapUsd") or token.get("marketCap") or 0
    liq = token.get("liquidity", {}).get("usd") if isinstance(token.get("liquidity"), dict) else token.get("liquidity", 0)
    text = (
        f"🔎 <b>Consulta token</b>\n\n"
        f"<b>{safe_html(symbol)}</b> {safe_html(name)}\n"
        f"• MC: ${format_number(mc)}\n"
        f"• Liq: ${format_number(liq)}\n"
        f"<b>Mint:</b>\n<code>{mint}</code>"
    )
    buttons = [
        ("DexScreener", f"https://dexscreener.com/solana/{mint}"),
        ("RugCheck", f"https://rugcheck.xyz/tokens/{mint}"),
        ("Jupiter", f"https://jup.ag/swap/{mint}-SOL"),
    ]
    await notifier.send(text, buttons)

# ---------------------------
# Initialization & startup/shutdown
# ---------------------------
async def init_app():
    global telegram_app, db_pool, http_session, http_client, cache, notifier, core, flat_detector, raydium_grad, helius_monitor, scheduler

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
    telegram_app.add_handler(CommandHandler("mints_list", cmd_mints_list))
    telegram_app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    telegram_app.add_handler(CommandHandler("rank", cmd_rank))
    telegram_app.add_handler(CommandHandler("buscar", cmd_buscar))

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8)
    async with db_pool.acquire() as conn:
        await conn.execute(DB_SCHEMA)

    http_session = aiohttp.ClientSession()
    http_client = HttpClient(http_session)

    cache = Cache(path=CACHE_DB_PATH)
    notifier = TelegramNotifier(telegram_app)
    core = MonitorCore(http_client, db_pool, cache, notifier)

    flat_detector = FlatDetector(core, db_pool)
    raydium_grad = RaydiumGraduationMonitor(core, db_pool, notifier)
    helius_monitor = HeliusPumpMonitor(HELIUS_WSS_URL, core, db_pool, notifier) if HELIUS_WSS_URL else None

    scheduler = AsyncIOScheduler()
    async def send_summary():
        async with db_pool.acquire() as conn:
            cnt = await conn.fetchval("SELECT COUNT(*) FROM notified_tokens WHERE notified_at >= $1", now_ts() - timedelta(hours=24))
        text = f"📰 <b>Resumen (24h)</b>\n• Alerts: {cnt}"
        await telegram_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML")
    scheduler.add_job(lambda: asyncio.create_task(send_summary()), "interval", hours=8)
    scheduler.start()

@app.on_event("startup")
async def on_startup():
    json_log("startup_init")
    await init_app()
    await telegram_app.initialize()
    await telegram_app.start()
    if helius_monitor:
        await helius_monitor.start()
    if raydium_grad:
        await raydium_grad.start()
    if flat_detector:
        await flat_detector.start(notifier)
    json_log("monitors_started_v5_pro_optimized")

@app.on_event("shutdown")
async def on_shutdown():
    json_log("shutdown_start")
    if helius_monitor:
        await helius_monitor.stop()
    if raydium_grad:
        await raydium_grad.stop()
    if flat_detector:
        await flat_detector.stop()
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

@app.get("/test")
async def test_endpoint():
    return {"status": "v5_pro_optimized ok", "chat_id": TELEGRAM_CHAT_ID}

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, req: Request):
    try:
        if token != TELEGRAM_BOT_TOKEN:
            raise HTTPException(status_code=403, detail="invalid token")
        body = await req.json()
        update = Update.de_json(body, telegram_app.bot)
        if update.effective_user and update.effective_user.id != TELEGRAM_CHAT_ID:
            return JSONResponse({"ok": True, "note": "ignored - private bot"})
        await telegram_app.process_update(update)
        return JSONResponse({"ok": True})
    except Exception as e:
        json_log("webhook_error", error=str(e))
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

def run_uvicorn():
    import uvicorn
    uvicorn.run("telegram_bot_final_optimized:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))

if __name__ == "__main__":
    asyncio.run(init_app())
    run_uvicorn()
