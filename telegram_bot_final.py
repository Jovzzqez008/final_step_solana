#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot_final.py
Versión final integrada:
- Pump.fun IDL decoding (BondingCurve)
- WebSocket PumpPortal listener
- On-chain bonding curve getAccountInfo decoding
- Redis TTL + dedupe
- Postgres storage (optional)
- FastAPI webhook for Telegram (/telegram/webhook)
- Commands: /start (menu) /iniciar (start monitoring) /status /tokens /rules
- Alerts when momentum rule triggered (default 120% en 15min)
- No automatic purchases
"""

import os
import sys
import json
import base64
import asyncio
import logging
import signal
import struct
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import aiohttp
import asyncpg
import websockets
import httpx

# Redis async
import redis.asyncio as aioredis

# Solana public key / PDA
from solana.publickey import PublicKey

# FastAPI + uvicorn
from fastapi import FastAPI, Request
import uvicorn

# Telegram (only for send & simple replies via webhook)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

# -----------------------
# Embedded minimal IDL for pump.fun (we use BondingCurve struct decode)
# -----------------------
PUMP_FUN_IDL = {
    "version": "0.1.0",
    "name": "pump",
    "accounts": [
        {
            "name": "BondingCurve",
            "type": {
                "kind": "struct",
                "fields": [
                    {"name": "virtualTokenReserves", "type": "u64"},
                    {"name": "virtualSolReserves", "type": "u64"},
                    {"name": "realTokenReserves", "type": "u64"},
                    {"name": "realSolReserves", "type": "u64"},
                    {"name": "tokenTotalSupply", "type": "u64"},
                    {"name": "complete", "type": "bool"}
                ]
            }
        }
    ],
    "metadata": {"address": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"}
}
PUMP_FUN_PROGRAM_ID = PublicKey(PUMP_FUN_IDL["metadata"]["address"])

# -----------------------
# Config (from config.json env override)
# -----------------------
class Config:
    def __init__(self, config_path: str = "config.json"):
        p = os.path.join(os.path.dirname(__file__), config_path)
        data = {}
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        # env overrides
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", data.get("telegram_bot_token", ""))
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", data.get("telegram_chat_id", ""))
        self.DATABASE_URL = os.getenv("DATABASE_URL", data.get("database_url", ""))
        self.QUICKNODE_RPC_URL = os.getenv("QUICKNODE_RPC_URL", data.get("quicknode_rpc_url", ""))
        self.HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", data.get("helius_rpc_url", ""))
        self.PUMPPORTAL_WSS = os.getenv("PUMPPORTAL_WSS", data.get("pumpportal_wss", "wss://pumpportal.fun/api/data"))
        # alerts
        self.ALERT_RULES = data.get("alert_rules", [
            {"name": "momentum_120_15", "alert_percent": 120.0, "time_window_min": 15, "description": "120% en 15 minutos"}
        ])
        mon = data.get("monitoring", {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv("MAX_MONITOR_TIME_MIN", mon.get("max_monitor_time_min", 35)))  # 35 min per your last ask
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv("DUMP_THRESHOLD_PERCENT", mon.get("dump_threshold_percent", -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv("PRICE_POLL_INTERVAL_SEC", mon.get("price_poll_interval_sec", 5)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv("MAX_CONCURRENT_MONITORS", mon.get("max_concurrent_monitors", 40)))
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", data.get("log_level", "INFO"))
        self.HEALTH_PORT = int(os.getenv("HEALTH_PORT", data.get("health_port", 8080)))
        self.REDIS_URL = os.getenv("REDIS_URL", os.getenv("REDIS_URL", None))
        self.ENABLE_DB = os.getenv("ENABLE_DB", str(data.get("enable_db", "true"))).lower() == "true"
        self.ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", str(data.get("enable_telegram", "true"))).lower() == "true"

# -----------------------
# Logging
# -----------------------
def setup_logging(cfg: Config):
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

# -----------------------
# Data models
# -----------------------
@dataclass
class TokenData:
    mint: str
    symbol: str
    name: str
    initial_price: float
    initial_market_cap: float
    max_price: float
    start_time: datetime
    status: str = "monitoring"
    bonding_curve: Optional[str] = None
    last_checked: Optional[datetime] = None
    metadata: Dict = None
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

@dataclass
class AlertData:
    token_address: str
    rule_name: str
    gain_percent: float
    time_elapsed_min: float
    price_at_alert: float
    market_cap_at_alert: float
    extra_data: Dict = None
    def __post_init__(self):
        if self.extra_data is None:
            self.extra_data = {}

# -----------------------
# RPC Client
# -----------------------
class RPCClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session: Optional[aiohttp.ClientSession] = None
        self.providers = []
        if cfg.QUICKNODE_RPC_URL:
            self.providers.append(("quicknode", cfg.QUICKNODE_RPC_URL))
        if cfg.HELIUS_RPC_URL:
            self.providers.append(("helius", cfg.HELIUS_RPC_URL))
        if not self.providers:
            self.providers.append(("public", "https://api.mainnet-beta.solana.com"))
        self.provider_idx = 0

    async def __aenter__(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
            self.session = None

    def _url(self):
        return self.providers[self.provider_idx][1]

    async def _rotate(self):
        self.provider_idx = (self.provider_idx + 1) % len(self.providers)
        logging.info(f"RPC provider rotated -> {self.providers[self.provider_idx][0]}")

    async def make_rpc(self, method: str, params: list, timeout: int = 10):
        payload = {"jsonrpc": "2.0", "id": int(time.time()), "method": method, "params": params}
        url = self._url()
        if not self.session:
            self.session = aiohttp.ClientSession()
        try:
            async with self.session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    raise Exception(f"RPC {resp.status}")
                d = await resp.json()
                if "error" in d:
                    raise Exception(d["error"])
                return d.get("result")
        except Exception as e:
            logging.warning(f"RPC call failed to {url}: {e}")
            # rotate and retry once
            await self._rotate()
            url2 = self._url()
            try:
                async with self.session.post(url2, json=payload, timeout=timeout) as resp:
                    d = await resp.json()
                    if "error" in d:
                        raise Exception(d["error"])
                    return d.get("result")
            except Exception as e2:
                logging.error(f"RPC retry failed to {url2}: {e2}")
                raise

    async def get_account_info_base64(self, address: str) -> Optional[Dict]:
        try:
            return await self.make_rpc("getAccountInfo", [address, {"encoding": "base64"}])
        except Exception as e:
            logging.debug(f"getAccountInfo fail {address}: {e}")
            return None

    async def dexscreener_price(self, mint: str) -> Optional[Dict]:
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self.session.get(url, timeout=6) as r:
                if r.status == 200:
                    js = await r.json()
                    if js.get("pairs"):
                        p = js["pairs"][0]
                        price = float(p.get("priceUsd", p.get("price", 0)) or 0)
                        mcap = float(p.get("marketCap", 0) or 0)
                        vol = float((p.get("volume") or {}).get("h24", 0) or 0)
                        return {"price": price, "market_cap": mcap, "volume_24h": vol, "source": "dexscreener"}
        except Exception as e:
            logging.debug(f"Dexscreener fail for {mint}: {e}")
        return None

    async def fetch_bonding_curve(self, bonding_curve_address: str) -> Optional[Dict]:
        """Decode Anchor BondingCurve account"""
        try:
            res = await self.get_account_info_base64(bonding_curve_address)
            if not res or not res.get("value") or not res["value"].get("data"):
                return None
            b64 = res["value"]["data"][0]
            raw = base64.b64decode(b64)
            # skip 8 byte anchor discriminator
            offset = 8
            needed = 8*5 + 1
            if len(raw) < offset + needed:
                logging.debug("bonding curve raw too small")
                return None
            try:
                virtualTokenReserves, virtualSolReserves, realTokenReserves, realSolReserves, tokenTotalSupply = struct.unpack_from("<QQQQQ", raw, offset)
                offset += 8*5
                complete = struct.unpack_from("<?", raw, offset)[0]
            except struct.error as e:
                logging.debug(f"bonding struct unpack error: {e}")
                return None
            price = 0.0
            mcap = 0.0
            try:
                if realTokenReserves and realSolReserves:
                    price = float(realSolReserves) / float(realTokenReserves)
                mcap = float(tokenTotalSupply) * price
            except Exception:
                pass
            return {
                "price": price,
                "market_cap": mcap,
                "complete": bool(complete),
                "virtualTokenReserves": virtualTokenReserves,
                "virtualSolReserves": virtualSolReserves,
                "realTokenReserves": realTokenReserves,
                "realSolReserves": realSolReserves,
                "tokenTotalSupply": tokenTotalSupply
            }
        except Exception as e:
            logging.debug(f"fetch_bonding_curve error: {e}")
            return None

    async def compute_associated_bonding_curve(self, mint: str) -> Optional[str]:
        """
        Try compute PDA(s) for bonding_curve / associatedBondingCurve using known seeds heuristics.
        We'll try a few plausible seeds based on community examples.
        Returns address as str or None.
        """
        try:
            mint_pk = PublicKey(mint)
        except Exception:
            return None

        # Heuristics: try seeds used in examples (this is heuristic and may require adaptation)
        tries = []
        # 1) compute PDA using program id + mint (common pattern)
        try:
            seeds = [b"bonding_curve", bytes(mint_pk)]
            pda, bump = PublicKey.find_program_address(seeds, PUMP_FUN_PROGRAM_ID)
            tries.append(str(pda))
        except Exception:
            pass
        # 2) try using mint only
        try:
            seeds2 = [bytes(mint_pk)]
            pda2, bump2 = PublicKey.find_program_address(seeds2, PUMP_FUN_PROGRAM_ID)
            tries.append(str(pda2))
        except Exception:
            pass
        # 3) try other orderings
        try:
            seeds3 = [b"bonding-curve", bytes(mint_pk)]
            pda3, _ = PublicKey.find_program_address(seeds3, PUMP_FUN_PROGRAM_ID)
            tries.append(str(pda3))
        except Exception:
            pass

        # filter duplicates
        tried = set()
        for addr in tries:
            if not addr or addr in tried:
                continue
            tried.add(addr)
            info = await self.get_account_info_base64(addr)
            if info and info.get("value") and info["value"].get("data"):
                # seems valid account — assume it's bonding curve
                return addr
        return None

# -----------------------
# Database handler
# -----------------------
class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not self.cfg.ENABLE_DB or not self.cfg.DATABASE_URL:
            logging.info("Database disabled or DATABASE_URL not set")
            return
        try:
            self.pool = await asyncpg.create_pool(self.cfg.DATABASE_URL, min_size=1, max_size=10)
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS token_monitoring (
                        token_address TEXT PRIMARY KEY,
                        symbol TEXT,
                        name TEXT,
                        bonding_curve TEXT,
                        initial_price NUMERIC,
                        initial_market_cap NUMERIC,
                        max_price NUMERIC,
                        start_time TIMESTAMPTZ,
                        last_checked TIMESTAMPTZ,
                        status TEXT,
                        metadata JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS token_alerts (
                        id SERIAL PRIMARY KEY,
                        token_address TEXT NOT NULL,
                        alert_rule_name TEXT,
                        gain_percent NUMERIC,
                        time_elapsed_min NUMERIC,
                        price_at_alert NUMERIC,
                        market_cap_at_alert NUMERIC,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        extra_data JSONB
                    )
                """)
            logging.info("Database connected and schema ensured")
        except Exception as e:
            logging.error(f"Database connect error: {e}")
            self.pool = None

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def add_or_update_token(self, token: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO token_monitoring (token_address, symbol, name, bonding_curve, initial_price, initial_market_cap, max_price, start_time, last_checked, status, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    ON CONFLICT (token_address) DO UPDATE SET
                        symbol = EXCLUDED.symbol,
                        name = EXCLUDED.name,
                        bonding_curve = EXCLUDED.bonding_curve,
                        initial_price = EXCLUDED.initial_price,
                        initial_market_cap = EXCLUDED.initial_market_cap,
                        max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price),
                        last_checked = EXCLUDED.last_checked,
                        status = EXCLUDED.status,
                        metadata = EXCLUDED.metadata
                """, token.mint, token.symbol, token.name, token.bonding_curve, token.initial_price, token.initial_market_cap, token.max_price, token.start_time, token.last_checked, token.status, json.dumps(token.metadata))
        except Exception as e:
            logging.debug(f"DB add/update token failed: {e}")

    async def record_alert(self, alert: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO token_alerts (token_address, alert_rule_name, gain_percent, time_elapsed_min, price_at_alert, market_cap_at_alert, extra_data)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                """, alert.token_address, alert.rule_name, alert.gain_percent, alert.time_elapsed_min, alert.price_at_alert, alert.market_cap_at_alert, json.dumps(alert.extra_data))
        except Exception as e:
            logging.debug(f"DB record_alert failed: {e}")

# -----------------------
# Alert Engine
# -----------------------
class AlertEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rules = cfg.ALERT_RULES

    def evaluate(self, token: TokenData, current_price: float, elapsed_min: float) -> List[AlertData]:
        if current_price <= 0:
            return []
        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100 if token.initial_price > 0 else 0.0
        triggered = []
        for r in self.rules:
            if gain_percent >= float(r["alert_percent"]) and elapsed_min <= float(r["time_window_min"]):
                triggered.append(AlertData(
                    token_address=token.mint,
                    rule_name=r["name"],
                    gain_percent=gain_percent,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=0.0,
                    extra_data={"symbol": token.symbol, "name": token.name, "initial_price": token.initial_price, "max_price": token.max_price}
                ))
        return triggered

# -----------------------
# Notification (Telegram) - uses Bot only for send; webhook endpoint handles incoming updates
# -----------------------
class Notification:
    bot: Optional[Bot] = None
    cfg: Optional[Config] = None

    @classmethod
    def init(cls, cfg: Config):
        cls.cfg = cfg
        if cfg.ENABLE_TELEGRAM and cfg.TELEGRAM_BOT_TOKEN:
            cls.bot = Bot(token=cfg.TELEGRAM_BOT_TOKEN)

    @classmethod
    async def send_token_alert(cls, token: TokenData, alert: AlertData):
        if not cls.bot or not cls.cfg or not cls.cfg.TELEGRAM_CHAT_ID:
            logging.info(f"ALERT (local): {token.symbol} +{alert.gain_percent:.1f}%")
            return
        try:
            msg = cls._format_message(token, alert)
            kb = cls._keyboard(token.mint)
            # Bot.send_message is synchronous in telegram, but Bot in PTB supports async via aiohttp; Bot has .send_message coroutine
            await cls.bot.send_message(chat_id=cls.cfg.TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown", reply_markup=kb, disable_web_page_preview=True)
            logging.info(f"Telegram alert sent for {token.symbol} {token.mint}")
        except Exception as e:
            logging.error(f"Telegram send failed: {e}")

    @staticmethod
    def _format_message(token: TokenData, alert: AlertData) -> str:
        mint = token.mint
        return (
            f"🚀 *ALERTA DE MOMENTUM* 🚀\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Mint:* `{mint}`\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f} min\n"
            f"*Precio al alert:* {alert.price_at_alert:.8f}\n"
            f"*Market Cap aprox:* ${alert.market_cap_at_alert:,.0f}\n\n"
            f"🔗 *Enlaces rápidos*\n"
            f"• Pump.fun: https://pump.fun/{mint}\n"
            f"• DexScreener: https://dexscreener.com/solana/{mint}\n"
            f"• RugCheck: https://rugcheck.xyz/tokens/{mint}\n"
            f"• Birdeye: https://birdeye.so/token/{mint}?chain=solana\n\n"
            f"🕒 Tiempo desde creación: {alert.time_elapsed_min:.1f} minutos\n"
        )

    @staticmethod
    def _keyboard(mint: str) -> InlineKeyboardMarkup:
        kb = [
            [InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"), InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"), InlineKeyboardButton("Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")]
        ]
        return InlineKeyboardMarkup(kb)

# -----------------------
# Token Manager (monitors tokens concurrently with a semaphore)
# -----------------------
class TokenManager:
    def __init__(self, cfg: Config, db: Database, rpc: RPCClient, alert_engine: AlertEngine, redis_client: Optional[aioredis.Redis]):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.alert_engine = alert_engine
        self.redis = redis_client
        self.monitored: Dict[str, TokenData] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.alerted: set = set()
        self.sem = asyncio.Semaphore(self.cfg.MAX_CONCURRENT_MONITORS)

    async def add_token(self, mint: str, symbol: str = "UNKNOWN", name: str = "UNKNOWN", bonding_curve: Optional[str] = None, initial_price: float = 0.0, initial_market_cap: float = 0.0):
        # Deduplicate via Redis if available (short TTL)
        key = f"token_seen:{mint}"
        if self.redis:
            try:
                ok = await self.redis.setnx(key, "1")
                if ok:
                    # set TTL to slightly larger than monitor window
                    await self.redis.expire(key, int(self.cfg.MAX_MONITOR_TIME_MIN * 60 + 30))
                else:
                    logging.debug(f"Redis dedupe: {mint} already seen")
                    return
            except Exception as e:
                logging.debug(f"Redis error dedupe: {e}")

        if mint in self.monitored:
            logging.debug(f"{mint} already monitored in memory")
            return

        token = TokenData(
            mint=mint,
            symbol=symbol,
            name=name,
            initial_price=initial_price,
            initial_market_cap=initial_market_cap,
            max_price=initial_price,
            start_time=datetime.now(timezone.utc),
            bonding_curve=bonding_curve
        )
        self.monitored[mint] = token
        token.last_checked = datetime.now(timezone.utc)
        await self.db.add_or_update_token(token)
        # spawn monitor
        t = asyncio.create_task(self._monitor_token(mint))
        self.tasks[mint] = t
        logging.info(f"Started monitoring {symbol} ({mint[:8]}...)")

    async def _monitor_token(self, mint: str):
        async with self.sem:
            token = self.monitored.get(mint)
            if not token:
                return
            try:
                start = token.start_time
                # ensure rpc session
                await self.rpc.__aenter__()
                while mint in self.monitored:
                    elapsed_min = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
                    if elapsed_min >= self.cfg.MAX_MONITOR_TIME_MIN:
                        logging.info(f"Token {token.symbol} {mint} timed out after {elapsed_min:.1f} min")
                        await self._remove_token(mint, "timeout")
                        return
                    price_data = None
                    # try dexscreener
                    try:
                        price_data = await self.rpc.dexscreener_price(mint)
                    except Exception:
                        price_data = None
                    # if not available, try bonding curve (on-chain). If token.bonding_curve missing, try compute
                    if not price_data:
                        bc_addr = token.bonding_curve
                        if not bc_addr:
                            bc_addr = await self.rpc.compute_associated_bonding_curve(mint)
                            if bc_addr:
                                token.bonding_curve = bc_addr
                        if bc_addr:
                            try:
                                bc = await self.rpc.fetch_bonding_curve(bc_addr)
                                if bc:
                                    price_data = {"price": bc["price"], "market_cap": bc["market_cap"], "source": "bonding_curve", "complete": bc.get("complete", False)}
                            except Exception:
                                price_data = None
                    if not price_data:
                        # no price yet
                        await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
                        continue
                    current_price = float(price_data.get("price", 0.0) or 0.0)
                    current_mcap = float(price_data.get("market_cap", 0.0) or 0.0)
                    token.last_checked = datetime.now(timezone.utc)
                    if current_price > token.max_price:
                        token.max_price = current_price
                    # persist quick
                    await self.db.add_or_update_token(token)
                    # detect dump from max
                    if token.max_price > 0:
                        loss_from_max = ((current_price - token.max_price) / token.max_price) * 100
                        if loss_from_max <= self.cfg.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"Dump detected for {token.symbol} {mint}: {loss_from_max:.1f}% -> removing")
                            await self._remove_token(mint, "dumped")
                            return
                    # evaluate alerts
                    alerts = self.alert_engine.evaluate(token, current_price, elapsed_min)
                    for a in alerts:
                        if mint in self.alerted:
                            continue
                        a.market_cap_at_alert = current_mcap
                        # record and notify
                        await self.db.record_alert(a)
                        self.alerted.add(mint)
                        await Notification.send_token_alert(token, a)
                        # remove after alert (as per design)
                        await self._remove_token(mint, "alert_sent")
                        return
                    await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                logging.info(f"Monitoring cancelled {mint}")
            except Exception as e:
                logging.error(f"Error monitoring {mint}: {e}")
                await self._remove_token(mint, "error")

    async def _remove_token(self, mint: str, reason: str):
        token = self.monitored.get(mint)
        if token:
            token.status = reason
            token.last_checked = datetime.now(timezone.utc)
            try:
                await self.db.add_or_update_token(token)
            except Exception:
                pass
            del self.monitored[mint]
        if mint in self.tasks:
            t = self.tasks[mint]
            t.cancel()
            del self.tasks[mint]
        logging.info(f"Removed {mint}: {reason}")

    def get_status(self):
        return {"monitored_tokens": len(self.monitored), "active_tasks": len(self.tasks), "tokens": list(self.monitored.keys())}

# -----------------------
# PumpPortal WSS client
# -----------------------
class PumpPortalClient:
    def __init__(self, cfg: Config, token_manager: TokenManager):
        self.cfg = cfg
        self.tm = token_manager
        self.running = False
        self.ws = None
        self.reconnect = 5
        self.max_reconnect = 60

    async def connect(self):
        uri = self.cfg.PUMPPORTAL_WSS
        self.running = True
        while self.running:
            try:
                logging.info(f"Connecting to PumpPortal WSS: {uri}")
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                    self.ws = websocket
                    self.reconnect = 5
                    # subscribe if needed
                    try:
                        await websocket.send(json.dumps({"method": "subscribeNewToken"}))
                    except Exception:
                        pass
                    async for raw in websocket:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            logging.debug("Invalid WSS data")
                            continue
                        await self._handle(data)
            except Exception as e:
                logging.error(f"WSS error: {e}")
                await asyncio.sleep(self.reconnect)
                self.reconnect = min(self.reconnect * 2, self.max_reconnect)
            finally:
                self.ws = None

    async def _handle(self, data: Dict):
        # normalize
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        mint = payload.get("mint") or payload.get("token") or payload.get("tokenMint")
        symbol = payload.get("symbol") or payload.get("tokenSymbol") or payload.get("symbol") or "UNKNOWN"
        name = payload.get("name") or payload.get("tokenName") or symbol
        bonding_curve = payload.get("bondingCurve") or payload.get("bonding_curve") or payload.get("curve") or None
        initial_price = 0.0
        initial_mcap = 0.0
        try:
            if isinstance(payload.get("pairs"), list) and payload.get("pairs"):
                p = payload["pairs"][0]
                initial_price = float(p.get("priceUsd", p.get("price", 0)) or 0)
                initial_mcap = float(p.get("marketCap", 0) or 0)
        except Exception:
            pass
        if not mint:
            logging.debug("WSS message without mint - skipping")
            return
        mint = str(mint)
        # add token to monitor
        try:
            await self.tm.add_token(mint=mint, symbol=symbol, name=name, bonding_curve=bonding_curve, initial_price=initial_price, initial_market_cap=initial_mcap)
        except Exception as e:
            logging.error(f"Add token error: {e}")

    async def stop(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

# -----------------------
# FastAPI app (health/metrics + Telegram webhook)
# -----------------------
def create_app(botref):
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok", "monitored": len(botref.token_manager.monitored), "ws_connected": bool(botref.portal_client and botref.portal_client.ws)}

    @app.get("/metrics")
    async def metrics():
        s = botref.token_manager.get_status()
        return {"monitored_tokens": s["monitored_tokens"], "active_tasks": s["active_tasks"]}

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        """
        Telegram will POST updates here.
        We implement minimal command handling for /start and /iniciar and /status /tokens /rules
        """
        try:
            data = await request.json()
        except Exception:
            return {"ok": False}
        # handle only message updates
        message = data.get("message") or data.get("edited_message") or {}
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if not chat_id or not text:
            return {"ok": True}
        # simple command parsing
        cmd = text.strip().split()[0].lower()
        # respond accordingly
        if cmd == "/start":
            kb = [
                [InlineKeyboardButton("Iniciar Monitor", callback_data="iniciar"), InlineKeyboardButton("Status", callback_data="status")],
                [InlineKeyboardButton("Tokens", callback_data="tokens"), InlineKeyboardButton("Rules", callback_data="rules")]
            ]
            try:
                await Notification.bot.send_message(chat_id=chat_id, text="🤖 *BOT PUMP.FUN* \nUsa /iniciar para empezar a monitorear o usa botones.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
            except Exception as e:
                logging.error(f"telegram send /start error: {e}")
        elif cmd == "/iniciar":
            # start monitoring loop if not already started
            if not botref.running:
                asyncio.create_task(botref.start_monitoring())
                await Notification.bot.send_message(chat_id=chat_id, text="▶️ Monitor iniciado", parse_mode="Markdown")
            else:
                await Notification.bot.send_message(chat_id=chat_id, text="ℹ️ Monitor ya en ejecución", parse_mode="Markdown")
        elif cmd == "/status":
            s = botref.token_manager.get_status()
            txt = f"📊 Tokens monitoreados: {s['monitored_tokens']}\nTareas: {s['active_tasks']}\nWS conectado: {bool(botref.portal_client and botref.portal_client.ws)}"
            await Notification.bot.send_message(chat_id=chat_id, text=txt)
        elif cmd == "/tokens":
            s = botref.token_manager.get_status()
            if s["monitored_tokens"] == 0:
                await Notification.bot.send_message(chat_id=chat_id, text="🔍 No hay tokens en monitoreo")
            else:
                lines = []
                for i, m in enumerate(s["tokens"][:10], 1):
                    td = botref.token_manager.monitored.get(m)
                    elapsed = (datetime.now(timezone.utc) - td.start_time).total_seconds() / 60 if td else 0
                    lines.append(f"{i}. {td.symbol if td else 'UNK'} - {elapsed:.1f}min - `{m[:16]}...`")
                txt = "🔍 TOKENS:\n" + "\n".join(lines)
                await Notification.bot.send_message(chat_id=chat_id, text=txt, parse_mode="Markdown")
        elif cmd == "/rules":
            txt = "🎯 Reglas activas:\n"
            for r in botref.cfg.ALERT_RULES:
                txt += f"• {r['name']}: +{r['alert_percent']}% en {r['time_window_min']}min — {r.get('description','')}\n"
            await Notification.bot.send_message(chat_id=chat_id, text=txt, parse_mode="Markdown")
        else:
            # unknown - ignore or echo
            pass
        return {"ok": True}

    return app

# -----------------------
# Main Bot container
# -----------------------
class PumpFunBot:
    def __init__(self):
        self.cfg = Config()
        setup_logging(self.cfg)
        self.db = Database(self.cfg)
        self.rpc = RPCClient(self.cfg)
        self.redis: Optional[aioredis.Redis] = None
        self.alert_engine = AlertEngine(self.cfg)
        self.token_manager: Optional[TokenManager] = None
        self.portal_client: Optional[PumpPortalClient] = None
        self.fastapi_app: Optional[FastAPI] = None
        self.http_task: Optional[asyncio.Task] = None
        self.wss_task: Optional[asyncio.Task] = None
        self.running = False
        Notification.init(self.cfg)

    async def init_services(self):
        # DB
        await self.db.connect()
        # Redis
        if self.cfg.REDIS_URL:
            try:
                self.redis = aioredis.from_url(self.cfg.REDIS_URL)
                # quick ping
                await self.redis.ping()
                logging.info("Redis connected")
            except Exception as e:
                logging.error(f"Redis connect failed: {e}")
                self.redis = None
        else:
            logging.info("REDIS_URL not set; skipping Redis (dedupe disabled)")
            self.redis = None
        # Token Manager
        self.token_manager = TokenManager(self.cfg, self.db, self.rpc, self.alert_engine, self.redis)
        self.portal_client = PumpPortalClient(self.cfg, self.token_manager)
        # FastAPI
        self.fastapi_app = create_app(self)
        # set Notification bot if not set
        Notification.init(self.cfg)

    async def start_monitoring(self):
        if self.running:
            logging.info("Monitor already running")
            return
        await self.init_services()
        # start FastAPI server as background task
        self.http_task = asyncio.create_task(self._run_http())
        # set Telegram webhook if configured
        await self._configure_telegram_webhook()
        # start WSS client
        self.wss_task = asyncio.create_task(self.portal_client.connect())
        self.running = True
        logging.info("Monitoring started")

    async def stop_monitoring(self):
        logging.info("Stopping monitoring")
        if self.portal_client:
            await self.portal_client.stop()
        if self.wss_task:
            self.wss_task.cancel()
        if self.http_task:
            self.http_task.cancel()
        await self.db.disconnect()
        if self.redis:
            try:
                await self.redis.close()
            except Exception:
                pass
        self.running = False

    async def _configure_telegram_webhook(self):
        # set webhook to YOUR RAILWAY domain env variable DOMAIN_URL or fallback to env TELEGRAM_WEBHOOK_URL
        webhook_base = os.getenv("DOMAIN_URL", os.getenv("TELEGRAM_WEBHOOK_URL", None))
        if not webhook_base:
            logging.info("No DOMAIN_URL or TELEGRAM_WEBHOOK_URL env var — skipping webhook setup")
            return
        webhook_url = webhook_base.rstrip("/") + "/telegram/webhook"
        if Notification.bot:
            try:
                res = await Notification.bot.set_webhook(url=webhook_url, drop_pending_updates=True)
                logging.info(f"✅ Telegram webhook set to {webhook_url}")
            except Exception as e:
                logging.error(f"Setting webhook failed: {e}")

    async def _run_http(self):
        port = self.cfg.HEALTH_PORT
        cfg = uvicorn.Config(self.fastapi_app, host="0.0.0.0", port=port, log_level="info")
        server = uvicorn.Server(cfg)
        await server.serve()

# -----------------------
# Entrypoint
# -----------------------
async def main():
    bot = PumpFunBot()
    # trap signals
    loop = asyncio.get_event_loop()
    def _sig(sig, frame):
        logging.info(f"Signal {sig} received, shutting down")
        asyncio.create_task(bot.stop_monitoring())
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    # Start monitoring automatically if TELEGRAM webhook will be used and you want auto-start:
    # but per your request, we wait for /iniciar from Telegram (webhook).
    logging.info("PumpFunBot ready - waiting for /iniciar via Telegram webhook to start monitoring")
    # keep the loop alive
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
