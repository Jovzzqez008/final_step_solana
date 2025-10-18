#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot_final.py
Pump.fun monitor (no purchases). Full-featured single-file deliverable.
- WSS primary + secondary fallback
- Watchdog (3 minutes no tokens -> switch/reconnect)
- Bonding curve on-chain decode (IDL embedded)
- Compute PDA fallback for bonding_curve
- Redis TTL dedupe (60s)
- Postgres optional persistence (asyncpg)
- FastAPI health/metrics + Telegram webhook path
- Telegram buttons & commands via webhook (no polling)
"""

import os
import sys
import json
import base64
import asyncio
import logging
import struct
import time
import base58
import signal
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from collections import deque

import aiohttp
import asyncpg
import websockets
import uvicorn
from fastapi import FastAPI, Request, Response

# redis asyncio client
import redis.asyncio as aioredis

# Telegram Bot (synchronous methods will be run in executor)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

# Prefer solders Pubkey if available (faster), fallback to solana.publickey
try:
    from solders.pubkey import Pubkey as PublicKeySolders
    USING_SOLDERS = True
except Exception:
    USING_SOLDERS = False
    try:
        from solana.publickey import PublicKey as PublicKeySolana
    except Exception:
        PublicKeySolana = None

# --------------------------
# Embedded minimal IDL portion (bonding curve struct)
# --------------------------
PUMP_FUN_IDL = {
  "version": "0.1.0",
  "name": "pump",
  "accounts": [
    {
      "name": "BondingCurve",
      "type": {
        "kind": "struct",
        "fields": [
          { "name": "virtualTokenReserves", "type": "u64" },
          { "name": "virtualSolReserves", "type": "u64" },
          { "name": "realTokenReserves", "type": "u64" },
          { "name": "realSolReserves", "type": "u64" },
          { "name": "tokenTotalSupply", "type": "u64" },
          { "name": "complete", "type": "bool" }
        ]
      }
    }
  ],
  "metadata": {
    "address": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
  }
}
PUMP_FUN_PROGRAM_ID = PUMP_FUN_IDL['metadata']['address']

# --------------------------
# Config loader (env first, fallback to config.json)
# --------------------------
class Config:
    def __init__(self, path: str = "config.json"):
        self.path = os.path.join(os.path.dirname(__file__), path)
        self._load()

    def _load(self):
        cfg = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}

        # Telegram
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', cfg.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', cfg.get('telegram_chat_id', ''))
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', str(cfg.get('enable_telegram', 'true'))).lower() == 'true'

        # RPC & WSS
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', cfg.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', cfg.get('helius_rpc_url', ''))
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', cfg.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))
        self.SECONDARY_WSS = os.getenv('SECONDARY_WSS', cfg.get('secondary_wss', ''))
        self.WSS_PRIORITY = os.getenv('WSS_PRIORITY', cfg.get('wss_priority', 'pumpportal')).lower()

        # pick WSS primary by priority
        # if HELIUS/QUICKNODE provided via env, prefer them depending on WSS_PRIORITY
        self.WSS_URL = None
        self.WSS_TYPE = None
        if self.WSS_PRIORITY == 'helius' and self.HELIUS_RPC_URL:
            # auto-derive helius websocket if api-key present
            if 'api-key=' in self.HELIUS_RPC_URL and 'helius' in self.HELIUS_RPC_URL:
                # example: helius RPC URL may include api-key parameter; user should pass proper WSS in env often
                self.WSS_URL = os.getenv('HELIUS_WSS', None)
                self.WSS_TYPE = 'helius' if self.WSS_URL else None
        if not self.WSS_URL:
            # default to pumpportal if provided
            if self.PUMPPORTAL_WSS:
                self.WSS_URL = self.PUMPPORTAL_WSS
                self.WSS_TYPE = 'pumpportal'
        if not self.WSS_URL and self.SECONDARY_WSS:
            self.WSS_URL = self.SECONDARY_WSS
            self.WSS_TYPE = 'secondary'

        # Database & Redis
        self.DATABASE_URL = os.getenv('DATABASE_URL', cfg.get('database_url', ''))
        self.REDIS_URL = os.getenv('REDIS_URL', cfg.get('redis_url', 'redis://localhost:6379/0'))
        self.ENABLE_DB = os.getenv('ENABLE_DB', str(cfg.get('enable_db', 'true'))).lower() == 'true'

        # Alerts & monitoring
        self.ALERT_RULES = cfg.get('alert_rules', [
            {"name": "momentum_120_15", "alert_percent": 120.0, "time_window_min": 15}
        ])
        monitoring = cfg.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', monitoring.get('max_monitor_time_min', 30)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', monitoring.get('dump_threshold_percent', -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', monitoring.get('price_poll_interval_sec', 5)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', monitoring.get('max_concurrent_monitors', 40)))
        self.MAX_TOKENS_MONITORED = int(os.getenv('MAX_TOKENS_MONITORED', monitoring.get('max_tokens_monitored', 150)))
        self.CACHE_TTL_SEC = int(os.getenv('CACHE_TTL_SEC', '60'))  # Redis TTL for dedupe

        # Watchdog (3 minutes per user's choice)
        self.WATCHDOG_NO_TOKEN_SEC = int(os.getenv('WATCHDOG_NO_TOKEN_SEC', str(3 * 60)))  # 3 minutes

        # server
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', cfg.get('health_port', 8080)))
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', cfg.get('log_level', 'INFO'))
        self.DOMAIN_URL = os.getenv('DOMAIN_URL', cfg.get('domain_url', ''))
        self.TELEGRAM_WEBHOOK_PATH = os.getenv('TELEGRAM_WEBHOOK_PATH', '/telegram/webhook')

# --------------------------
# Logging
# --------------------------
def setup_logging(cfg: Config):
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# --------------------------
# Models
# --------------------------
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
    priority: bool = False

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

# --------------------------
# RPC client with failover
# --------------------------
class RPCClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session: Optional[aiohttp.ClientSession] = None
        # providers list - user can set QUICKNODE_RPC_URL / HELIUS_RPC_URL env if desired
        self.providers: List[Tuple[str, str]] = []
        if cfg.QUICKNODE_RPC_URL:
            self.providers.append(('quicknode', cfg.QUICKNODE_RPC_URL))
        if cfg.HELIUS_RPC_URL:
            self.providers.append(('helius', cfg.HELIUS_RPC_URL))
        # fallback public if none
        if not self.providers:
            self.providers.append(('public', 'https://api.mainnet-beta.solana.com'))
        self.idx = 0
        self.session = None

    async def __aenter__(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=10, connect=3)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _current(self) -> Tuple[str, str]:
        return self.providers[self.idx]

    async def _rotate(self):
        self.idx = (self.idx + 1) % len(self.providers)
        logging.info(f"RPC rotated to provider {self.providers[self.idx][0]}")

    async def rpc(self, method: str, params: list, timeout: int = 8) -> Optional[dict]:
        name, url = self._current()
        payload = {"jsonrpc": "2.0", "id": int(time.time()*1000), "method": method, "params": params}
        attempts = 0
        max_attempts = len(self.providers)
        while attempts < max_attempts:
            attempts += 1
            try:
                if not self.session:
                    await self.__aenter__()
                async with self.session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if 'error' in data:
                            logging.debug(f"RPC {name} error {data['error']}")
                            await self._rotate()
                            continue
                        return data.get('result')
                    else:
                        logging.debug(f"RPC {name} status {resp.status}")
                        await self._rotate()
            except Exception as e:
                logging.debug(f"RPC {name} exception: {e}")
                await self._rotate()
                continue
        return None

    async def get_account_base64(self, addr: str) -> Optional[dict]:
        return await self.rpc("getAccountInfo", [addr, {"encoding": "base64"}])

    async def get_transaction(self, signature: str) -> Optional[dict]:
        return await self.rpc("getTransaction", [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}], timeout=10)

# --------------------------
# Database (asyncpg)
# --------------------------
class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not self.cfg.ENABLE_DB or not self.cfg.DATABASE_URL:
            logging.info("DB disabled or DATABASE_URL not set")
            return
        try:
            self.pool = await asyncpg.create_pool(self.cfg.DATABASE_URL, min_size=1, max_size=8)
            async with self.pool.acquire() as conn:
                await conn.execute('''
                CREATE TABLE IF NOT EXISTS token_monitoring (
                    token_address TEXT PRIMARY KEY,
                    symbol TEXT, name TEXT, bonding_curve TEXT,
                    initial_price NUMERIC, initial_market_cap NUMERIC,
                    max_price NUMERIC, start_time TIMESTAMPTZ,
                    last_checked TIMESTAMPTZ, status TEXT,
                    metadata JSONB, priority BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )''')
                await conn.execute('''
                CREATE TABLE IF NOT EXISTS token_alerts (
                    id SERIAL PRIMARY KEY, token_address TEXT, alert_rule_name TEXT,
                    gain_percent NUMERIC, time_elapsed_min NUMERIC,
                    price_at_alert NUMERIC, market_cap_at_alert NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW(), extra_data JSONB
                )''')
            logging.info("Database connected and schema ensured")
        except Exception as e:
            logging.error(f"Database connect failed: {e}")
            self.pool = None

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def upsert_token(self, t: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                INSERT INTO token_monitoring (token_address,symbol,name,bonding_curve,initial_price,initial_market_cap,max_price,start_time,last_checked,status,metadata,priority)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (token_address) DO UPDATE SET
                    symbol=EXCLUDED.symbol, name=EXCLUDED.name, bonding_curve=EXCLUDED.bonding_curve,
                    initial_price=EXCLUDED.initial_price, initial_market_cap=EXCLUDED.initial_market_cap,
                    max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price),
                    last_checked=EXCLUDED.last_checked, status=EXCLUDED.status, metadata=EXCLUDED.metadata, priority=EXCLUDED.priority
                ''', t.mint, t.symbol, t.name, t.bonding_curve, t.initial_price, t.initial_market_cap, t.max_price, t.start_time, t.last_checked, t.status, json.dumps(t.metadata), t.priority)
        except Exception as e:
            logging.debug(f"DB upsert_token failed: {e}")

    async def record_alert(self, a: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                INSERT INTO token_alerts (token_address, alert_rule_name, gain_percent, time_elapsed_min, price_at_alert, market_cap_at_alert, extra_data)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ''', a.token_address, a.rule_name, a.gain_percent, a.time_elapsed_min, a.price_at_alert, a.market_cap_at_alert, json.dumps(a.extra_data))
        except Exception as e:
            logging.debug(f"DB record_alert failed: {e}")

# --------------------------
# Bonding curve decoding + PDA compute
# --------------------------
def compute_bonding_curve_pda(mint: str) -> Optional[str]:
    try:
        mint_bytes = base58.b58decode(mint)
        program_bytes = base58.b58decode(PUMP_FUN_PROGRAM_ID)
        if len(mint_bytes) != 32 or len(program_bytes) != 32:
            return None
        if USING_SOLDERS:
            from solders.pubkey import Pubkey
            program_pub = Pubkey.from_string(PUMP_FUN_PROGRAM_ID)
            pda, bump = Pubkey.find_program_address([b"bonding_curve", mint_bytes], program_pub)
            return str(pda)
        else:
            if PublicKeySolana is None:
                return None
            pda, bump = PublicKeySolana.find_program_address([b"bonding_curve", mint_bytes], PublicKeySolana(PUMP_FUN_PROGRAM_ID))
            return str(pda)
    except Exception:
        return None

def parse_bonding_curve_account_b64(b64data: str) -> Optional[Dict]:
    try:
        raw = base64.b64decode(b64data)
        # anchor discriminator (8) + 5*u64 + bool (1) = 8 + 40 +1 = 49
        if len(raw) < 49:
            return None
        offset = 8
        vToken, vSol, rToken, rSol, supply = struct.unpack_from("<QQQQQ", raw, offset)
        offset += 40
        complete = struct.unpack_from("<?", raw, offset)[0]
        # price: realSol/rescaled divided by realToken/rescaled. Pump.fun tokens typically 6 decimals, SOL 9 decimals.
        price = 0.0
        market_cap = 0.0
        try:
            if rToken > 0 and rSol > 0:
                # convert to decimals: token 1e6, SOL 1e9
                price = (float(rSol) / 1e9) / (float(rToken) / 1e6)
                market_cap = (float(supply) / 1e6) * price
        except Exception:
            price = 0.0
            market_cap = 0.0
        return {
            'price': price,
            'market_cap': market_cap,
            'complete': bool(complete),
            'virtualTokenReserves': vToken,
            'virtualSolReserves': vSol,
            'realTokenReserves': rToken,
            'realSolReserves': rSol,
            'tokenTotalSupply': supply
        }
    except Exception:
        return None

async def validate_pump_fun_token(rpc: RPCClient, mint: str, bonding_curve: str) -> bool:
    try:
        acc = await rpc.get_account_base64(bonding_curve)
        if not acc or not acc.get('value'):
            return False
        owner = acc['value'].get('owner', '')
        if owner != PUMP_FUN_PROGRAM_ID:
            return False
        data = acc['value'].get('data')
        if not data or not data[0]:
            return False
        parsed = parse_bonding_curve_account_b64(data[0])
        if not parsed:
            return False
        if parsed['realTokenReserves'] == 0 and parsed['realSolReserves'] == 0:
            return False
        return True
    except Exception:
        return False

# --------------------------
# Alert engine
# --------------------------
class AlertEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rules = self.cfg.ALERT_RULES

    def evaluate(self, token: TokenData, current_price: float, elapsed_min: float) -> List[AlertData]:
        out = []
        if current_price <= 0 or token.initial_price <= 0:
            return out
        gain = ((current_price - token.initial_price) / token.initial_price) * 100.0
        for r in self.rules:
            try:
                if gain >= float(r['alert_percent']) and elapsed_min <= float(r['time_window_min']):
                    out.append(AlertData(token_address=token.mint, rule_name=r['name'], gain_percent=gain, time_elapsed_min=elapsed_min, price_at_alert=current_price, market_cap_at_alert=current_price * (token.initial_market_cap / max(token.initial_price, 1e-12)), extra_data={'symbol': token.symbol, 'name': token.name}))
            except Exception:
                continue
        return out

# --------------------------
# Notification (Telegram) - synchronous Bot calls run in executor
# --------------------------
class Notification:
    bot: Optional[Bot] = None
    cfg: Optional[Config] = None

    @classmethod
    def init(cls, cfg: Config):
        cls.cfg = cfg
        if cfg.ENABLE_TELEGRAM and cfg.TELEGRAM_BOT_TOKEN:
            cls.bot = Bot(token=cfg.TELEGRAM_BOT_TOKEN)

    @classmethod
    async def send_alert(cls, token: TokenData, alert: AlertData):
        if not cls.bot or not cls.cfg or not cls.cfg.TELEGRAM_CHAT_ID:
            logging.info(f"ALERT (no telegram) {token.symbol} +{alert.gain_percent:.1f}%")
            return
        try:
            msg = cls._format(token, alert)
            kb = cls._keyboard(token.mint)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: cls.bot.send_message(chat_id=cls.cfg.TELEGRAM_CHAT_ID, text=msg, parse_mode='Markdown', reply_markup=kb, disable_web_page_preview=True))
            logging.info(f"Telegram alert sent for {token.symbol}")
        except Exception as e:
            logging.error(f"Telegram send failed: {e}")

    @staticmethod
    def _format(token: TokenData, alert: AlertData) -> str:
        prefix = "🔥" if token.priority else "🚀"
        return (
            f"{prefix} *ALERTA DE MOMENTUM* {prefix}\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Mint:* `{token.mint}`\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f} min\n"
            f"*Precio al alert:* {alert.price_at_alert:.8f}\n"
            f"*Market Cap aprox:* ${alert.market_cap_at_alert:,.0f}\n\n"
            f"🔗 Enlaces:\n"
            f"• [Pump.fun](https://pump.fun/{token.mint})\n"
            f"• [DexScreener](https://dexscreener.com/solana/{token.mint})\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{token.mint})\n\n"
            f"🕒 Tiempo desde creación: {alert.time_elapsed_min:.1f} min\n"
        )

    @staticmethod
    def _keyboard(mint: str) -> InlineKeyboardMarkup:
        kb = [
            [InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"),
             InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")]
        ]
        return InlineKeyboardMarkup(kb)

# --------------------------
# Token manager
# --------------------------
class TokenManager:
    def __init__(self, cfg: Config, db: Database, rpc: RPCClient, redis_client):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.redis = redis_client
        self.alert_engine = AlertEngine(cfg)
        self.monitored: Dict[str, TokenData] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.alerted: set = set()
        self.semaphore = asyncio.Semaphore(cfg.MAX_CONCURRENT_MONITORS)

    async def add_token(self, mint: str, symbol: str = "UNKNOWN", name: str = "UNKNOWN", bonding_curve: Optional[str] = None, initial_price: float = 0.0, initial_market_cap: float = 0.0):
        if not mint:
            return
        if mint in self.monitored:
            return
        if len(self.monitored) >= self.cfg.MAX_TOKENS_MONITORED:
            logging.warning("Max tokens monitored reached; skipping new token")
            return
        # determine bonding_curve
        if not bonding_curve:
            bonding_curve = compute_bonding_curve_pda(mint)
        if not bonding_curve:
            logging.debug("Could not compute bonding_curve PDA for mint %s", mint)
            return
        # validate on-chain
        try:
            is_valid = await validate_pump_fun_token(self.rpc, mint, bonding_curve)
        except Exception:
            is_valid = False
        if not is_valid:
            logging.debug("Invalid or incomplete bonding curve for %s", mint)
            return
        # try fetch initial price on-chain
        if initial_price == 0.0:
            try:
                acc = await self.rpc.get_account_base64(bonding_curve)
                if acc and acc.get('value') and acc['value'].get('data'):
                    parsed = parse_bonding_curve_account_b64(acc['value']['data'][0])
                    if parsed:
                        initial_price = parsed['price']
                        initial_market_cap = parsed['market_cap']
            except Exception:
                pass
        priority = initial_market_cap >= 69000  # or other threshold
        token = TokenData(mint=mint, symbol=symbol, name=name, initial_price=initial_price, initial_market_cap=initial_market_cap, max_price=initial_price, start_time=datetime.now(timezone.utc), bonding_curve=bonding_curve, priority=priority)
        token.last_checked = datetime.now(timezone.utc)
        self.monitored[mint] = token
        await self.db.upsert_token(token)
        self.tasks[mint] = asyncio.create_task(self._monitor_token(mint))
        logging.info(f"Started monitoring {symbol} ({mint[:8]}...)")

    async def _monitor_token(self, mint: str):
        async with self.semaphore:
            token = self.monitored.get(mint)
            if not token:
                return
            try:
                start_time = token.start_time
                while mint in self.monitored:
                    elapsed_min = (datetime.now(timezone.utc) - start_time).total_seconds() / 60.0
                    if elapsed_min >= self.cfg.MAX_MONITOR_TIME_MIN:
                        logging.info(f"Token {token.symbol} {mint} timed out after {elapsed_min:.1f} min")
                        await self._remove_token(mint, "timeout")
                        return
                    current_price = 0.0
                    # Fetch bonding curve account
                    try:
                        acc = await self.rpc.get_account_base64(token.bonding_curve)
                        if acc and acc.get('value') and acc['value'].get('data'):
                            parsed = parse_bonding_curve_account_b64(acc['value']['data'][0])
                            if parsed and parsed['price'] > 0:
                                current_price = parsed['price']
                                if token.initial_price == 0.0:
                                    token.initial_price = current_price
                                    token.max_price = current_price
                                    token.initial_market_cap = parsed['market_cap']
                    except Exception:
                        pass
                    if current_price == 0.0:
                        await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
                        continue
                    token.last_checked = datetime.now(timezone.utc)
                    if current_price > token.max_price:
                        token.max_price = current_price
                    # persist occasionally
                    if int(time.time()) % 5 == 0:
                        await self.db.upsert_token(token)
                    # detect dump
                    if token.max_price > 0:
                        loss = ((current_price - token.max_price) / token.max_price) * 100.0
                        if loss <= self.cfg.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"Dump detected for {token.symbol} {mint}: {loss:.1f}% -> removing")
                            await self._remove_token(mint, "dumped")
                            return
                    # evaluate alerts
                    alerts = self.alert_engine.evaluate(token, current_price, elapsed_min)
                    for alert in alerts:
                        key = f"alert:{mint}"
                        already = await self.redis.get(key)
                        if already:
                            continue
                        await self.redis.set(key, "1", ex=self.cfg.CACHE_TTL_SEC)
                        await self.db.record_alert(alert)
                        await Notification.send_alert(token, alert)
                        self.alerted.add(mint)
                        logging.info(f"ALERT triggered for {mint}: +{alert.gain_percent:.1f}%")
                        await self._remove_token(mint, "alert_sent")
                        return
                    await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                logging.info(f"Monitor cancelled for {mint}")
            except Exception as e:
                logging.error(f"Error in monitor for {mint}: {e}")
                await self._remove_token(mint, "error")

    async def _remove_token(self, mint: str, reason: str):
        tok = self.monitored.get(mint)
        if tok:
            tok.status = reason
            tok.last_checked = datetime.now(timezone.utc)
            try:
                await self.db.upsert_token(tok)
            except Exception:
                pass
            del self.monitored[mint]
        t = self.tasks.get(mint)
        if t:
            t.cancel()
            del self.tasks[mint]
        logging.info(f"Removed token {mint}: {reason}")

# --------------------------
# WebSocket listener with watchdog & fallback
# --------------------------
class WebSocketListener:
    def __init__(self, cfg: Config, manager: TokenManager, rpc: RPCClient, redis_client):
        self.cfg = cfg
        self.manager = manager
        self.rpc = rpc
        self.redis = redis_client
        self.ws = None
        self.running = False
        self.reconnect_delay = 5
        self.max_reconnect = 60
        self.processed_sigs = deque(maxlen=2000)
        self.last_token_seen = datetime.now(timezone.utc)
        # primary/secondary
        self.primary_wss = cfg.WSS_URL
        self.primary_type = cfg.WSS_TYPE if hasattr(cfg, 'WSS_TYPE') else 'pumpportal'
        self.secondary_wss = cfg.SECONDARY_WSS or None
        self.current_uri = self.primary_wss

    async def connect(self):
        if not self.primary_wss:
            logging.error("No WSS configured")
            return
        self.running = True
        self.current_uri = self.primary_wss
        while self.running:
            try:
                logging.info(f"Connecting to WSS: {self.current_uri}")
                async with websockets.connect(self.current_uri, ping_interval=20, ping_timeout=10) as ws:
                    self.ws = ws
                    # subscribe message depending on provider
                    try:
                        if self.primary_type in ('helius', 'quicknode'):
                            sub = {"jsonrpc":"2.0","id":1,"method":"logsSubscribe","params":[{"mentions":[PUMP_FUN_PROGRAM_ID]},{"commitment":"confirmed"}]}
                            await ws.send(json.dumps(sub))
                            logging.info("Subscribed to logs (pump.fun)")
                        else:
                            # pumpportal-like
                            await ws.send(json.dumps({"method":"subscribeNewToken"}))
                            logging.info("Subscribed pumpportal")
                    except Exception:
                        logging.debug("Subscription send failed; continuing")
                    # start watchdog watcher task
                    watchdog_task = asyncio.create_task(self._watchdog_loop())
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        # normalize and handle
                        try:
                            await self._handle_msg(data)
                        except Exception as e:
                            logging.debug(f"Message handle failed: {e}")
                    watchdog_task.cancel()
            except Exception as e:
                logging.error(f"WSS connection error: {e}")
            finally:
                self.ws = None
                if not self.running:
                    break
                logging.info(f"Reconnecting in {self.reconnect_delay}s...")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect)

    async def stop(self):
        self.running = False
        try:
            if self.ws:
                await self.ws.close()
        except Exception:
            pass

    async def _watchdog_loop(self):
        # if no tokens seen in WATCHDOG_NO_TOKEN_SEC, attempt fallback
        try:
            while True:
                await asyncio.sleep(5)
                elapsed = (datetime.now(timezone.utc) - self.last_token_seen).total_seconds()
                if elapsed > self.cfg.WATCHDOG_NO_TOKEN_SEC:
                    logging.warning("Watchdog: no tokens seen in %s seconds", elapsed)
                    # switch to secondary if available
                    if self.secondary_wss and self.current_uri != self.secondary_wss:
                        logging.info("Switching to secondary WSS: %s", self.secondary_wss)
                        # notify via Telegram
                        if Notification.bot and self.cfg.ENABLE_TELEGRAM:
                            loop = asyncio.get_event_loop()
                            await loop.run_in_executor(None, lambda: Notification.bot.send_message(chat_id=self.cfg.TELEGRAM_CHAT_ID, text=f"Watchdog: switching to secondary WSS", parse_mode='Markdown'))
                        # set current and break to reconnect outer loop
                        self.current_uri = self.secondary_wss
                        await self.stop()
                        return
                    else:
                        # try reconnecting same
                        logging.info("Watchdog: reconnecting current WSS")
                        await self.stop()
                        return
        except asyncio.CancelledError:
            return

    async def _handle_msg(self, payload: dict):
        # update last seen if relevant
        # pumpportal has top-level 'mint' and 'signature'
        if isinstance(payload, dict) and ('mint' in payload or ('data' in payload and isinstance(payload['data'], dict) and 'mint' in payload['data'])):
            p = payload.get('data') if 'data' in payload and isinstance(payload['data'], dict) else payload
            mint = p.get('mint') or p.get('token') or None
            signature = p.get('signature') or p.get('tx') or None
            if signature and signature in self.processed_sigs:
                return
            if signature:
                self.processed_sigs.append(signature)
            if mint:
                # mark last seen
                self.last_token_seen = datetime.now(timezone.utc)
                logging.info(f"Detected new mint (portal): {mint}")
                symbol = p.get('symbol','UNKNOWN')
                name = p.get('name', symbol)
                await self.manager.add_token(mint=mint, symbol=symbol, name=name, bonding_curve=p.get('bondingCurve') or p.get('bonding_curve'))
            return
        # if logsNotification (helius/quicknode)
        if payload.get('method') != 'logsNotification':
            return
        params = payload.get('params', {})
        result = params.get('result', {}) if params else {}
        value = result.get('value', {})
        signature = result.get('signature', '') or result.get('signature')
        if signature in self.processed_sigs:
            return
        logs = value.get('logs', [])
        # detect create instruction in logs
        is_create = any('Instruction: Create' in l or 'create' in l.lower() for l in logs)
        if not is_create:
            return
        self.processed_sigs.append(signature)
        # fetch transaction to extract mint
        tx = await self.rpc.get_transaction(signature)
        mint = None
        if tx:
            try:
                meta = tx.get('meta', {})
                post_balances = meta.get('postTokenBalances', [])
                # method: find mint in postTokenBalances with uiAmount > 0
                for b in post_balances:
                    m = b.get('mint')
                    amt = b.get('uiTokenAmount', {}).get('uiAmount')
                    if m and amt and amt > 0:
                        if m != 'So11111111111111111111111111111111111111112':
                            mint = m
                            break
                if not mint:
                    # attempt accountKey heuristics
                    message = tx.get('transaction', {}).get('message', {})
                    account_keys = message.get('accountKeys', [])
                    for key in account_keys[:6]:
                        pub = key.get('pubkey') if isinstance(key, dict) else key
                        if pub and isinstance(pub, str) and len(pub) == 44 and pub not in (PUMP_FUN_PROGRAM_ID, '11111111111111111111111111111111', 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'):
                            mint = pub
                            break
            except Exception:
                pass
        if not mint:
            # fallback: try decode instruction accounts in tx message.instructions
            try:
                message = tx.get('transaction', {}).get('message', {})
                instructions = message.get('instructions', [])
                for inst in instructions:
                    if inst.get('programId') == PUMP_FUN_PROGRAM_ID or inst.get('programIdIndex') == PUMP_FUN_PROGRAM_ID:
                        accounts = inst.get('accounts', [])
                        for acct in accounts[:4]:
                            if isinstance(acct, str) and len(acct) == 44:
                                mint = acct
                                break
                        if mint:
                            break
            except Exception:
                pass
        if mint:
            self.last_token_seen = datetime.now(timezone.utc)
            logging.info(f"Detected new mint from logs: {mint}")
            await self.manager.add_token(mint=mint)
        else:
            logging.warning("Could not extract mint from transaction %s", signature)

# --------------------------
# FastAPI app & Telegram webhook handling
# --------------------------
def create_app(service):
    app = FastAPI(title="PumpFun Monitor", version="1.0")

    @app.get("/")
    async def root():
        return {"status":"ok","service":"pump.fun monitor"}

    @app.get("/health")
    async def health():
        return {"status":"healthy","tokens": len(service.token_manager.monitored), "alerts": len(service.token_manager.alerted)}

    @app.get("/metrics")
    async def metrics():
        tokens = []
        for mint, t in list(service.token_manager.monitored.items())[:30]:
            elapsed = (datetime.now(timezone.utc) - t.start_time).total_seconds() / 60.0
            gain = 0.0
            if t.initial_price and t.max_price:
                gain = ((t.max_price - t.initial_price) / (t.initial_price or 1)) * 100.0
            tokens.append({"mint": mint, "symbol": t.symbol, "gain": round(gain,2), "elapsed_min": round(elapsed,2)})
        return {"monitored": len(service.token_manager.monitored), "alerts": len(service.token_manager.alerted), "tokens": tokens}

    @app.post(service.config.TELEGRAM_WEBHOOK_PATH)
    async def telegram_webhook(req: Request):
        # Telegram webhook will POST updates here
        try:
            body = await req.json()
        except Exception:
            return Response(status_code=400)
        # handle callback_query
        if "callback_query" in body:
            cq = body['callback_query']
            await handle_callback_query(cq, service)
            return {"ok": True}
        message = body.get("message") or body.get("edited_message") or {}
        if not message:
            return {"ok": True}
        chat = message.get("chat", {})
        text = message.get("text", "") or ""
        # security: only accept configured chat id if set
        if service.config.TELEGRAM_CHAT_ID:
            if str(chat.get("id")) != str(service.config.TELEGRAM_CHAT_ID):
                return {"ok": True}
        if text.startswith("/"):
            await handle_command(text, chat, service)
        return {"ok": True}

    return app

# --------------------------
# Telegram commands & callbacks (webhook-driven)
# --------------------------
async def handle_command(text: str, chat: Dict, svc):
    cmd = text.split()[0]
    chat_id = chat.get("id")
    if cmd == "/start":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Iniciar", callback_data="cmd:start")],
            [InlineKeyboardButton("⏸️ Detener", callback_data="cmd:stop")],
            [InlineKeyboardButton("📊 Status", callback_data="cmd:status"), InlineKeyboardButton("🔍 Tokens", callback_data="cmd:tokens")],
            [InlineKeyboardButton("⚙️ Config", callback_data="cmd:config")]
        ])
        text_msg = ("🤖 *Pump.fun Monitor*\n\n"
                    "Bot listo. Usa los botones o los comandos:\n"
                    "/iniciar - Iniciar monitoreo\n"
                    "/detener - Detener monitoreo\n"
                    "/status - Estado\n"
                    "/tokens - Tokens activos\n"
                    "/config - Configuración")
        await send_telegram_message(svc, chat_id, text_msg, reply_markup=kb)

    elif cmd == "/iniciar":
        if not svc.wss_task or svc.wss_task.done():
            svc.wss_task = asyncio.create_task(svc.wss_listener.connect())
            await send_telegram_message(svc, chat_id, "▶️ Monitor INICIADO\nEscuchando tokens...")
        else:
            await send_telegram_message(svc, chat_id, "⚠️ Monitor ya está corriendo")

    elif cmd == "/detener":
        if svc.wss_listener and svc.wss_listener.running:
            await svc.wss_listener.stop()
            await send_telegram_message(svc, chat_id, "⏸️ Monitor DETENIDO")
        else:
            await send_telegram_message(svc, chat_id, "⚠️ El monitor no está corriendo")

    elif cmd == "/status":
        wss = svc.wss_listener
        wss_status = "✅ Conectado" if (wss and wss.ws) else "❌ Desconectado"
        running = "✅ Activo" if (wss and wss.running) else "⏸️ Detenido"
        rpc_provider = svc.rpc._current()[0]
        msg = (f"📊 *Estado del Bot*\n\n"
               f"🔌 WSS: {wss_status}\n"
               f"▶️ Monitoreo: {running}\n"
               f"🌐 RPC: {rpc_provider}\n\n"
               f"📈 Tokens: {len(svc.token_manager.monitored)}\n"
               f"⚠️ Alertas: {len(svc.token_manager.alerted)}\n"
               f"🧠 Redis: {'✅' if svc.redis else '❌'}\n"
               f"🗄️ DB: {'✅' if svc.db.pool else '❌'}")
        await send_telegram_message(svc, chat_id, msg)

    elif cmd == "/tokens":
        tokens = list(svc.token_manager.monitored.items())[:20]
        if tokens:
            m = "🔍 Tokens monitoreados (Top 20):\n\n"
            for mint, t in tokens:
                elapsed = (datetime.now(timezone.utc) - t.start_time).total_seconds() / 60.0
                gain = 0.0
                if t.initial_price and t.max_price:
                    gain = ((t.max_price - t.initial_price) / (t.initial_price or 1)) * 100.0
                emoji = "🔥" if t.priority else "•"
                m += f"{emoji} {t.symbol} | {gain:+.1f}% | {elapsed:.1f}m\n`{mint}`\n"
        else:
            m = "🔭 No hay tokens monitoreados"
        await send_telegram_message(svc, chat_id, m)

    elif cmd == "/config":
        cfg = svc.config
        m = (f"⚙️ Configuración\n\n"
             f"• Alert rules: {cfg.ALERT_RULES}\n"
             f"• Max monitor min: {cfg.MAX_MONITOR_TIME_MIN}\n"
             f"• Dump threshold: {cfg.DUMP_THRESHOLD_PERCENT}%\n"
             f"• Price poll: {cfg.PRICE_POLL_INTERVAL_SEC}s\n"
             f"• Cache TTL: {cfg.CACHE_TTL_SEC}s")
        await send_telegram_message(svc, chat_id, m)

async def handle_callback_query(cq: Dict, svc):
    data = cq.get("data")
    chat_id = cq.get("message", {}).get("chat", {}).get("id")
    if data == "cmd:start":
        if not svc.wss_task or svc.wss_task.done():
            svc.wss_task = asyncio.create_task(svc.wss_listener.connect())
            await send_telegram_message(svc, chat_id, "▶️ Monitor iniciado (botón)")
        else:
            await send_telegram_message(svc, chat_id, "⚠️ Ya está corriendo")
    elif data == "cmd:stop":
        if svc.wss_listener and svc.wss_listener.running:
            await svc.wss_listener.stop()
            await send_telegram_message(svc, chat_id, "⏸️ Monitor detenido (botón)")
        else:
            await send_telegram_message(svc, chat_id, "⚠️ No está corriendo")
    elif data == "cmd:status":
        await handle_command("/status", {"id": chat_id}, svc)
    elif data == "cmd:tokens":
        await handle_command("/tokens", {"id": chat_id}, svc)
    elif data == "cmd:config":
        await handle_command("/config", {"id": chat_id}, svc)

async def send_telegram_message(svc, chat_id, text, reply_markup=None):
    if not svc.bot:
        return
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, lambda: svc.bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown', reply_markup=reply_markup))
    except Exception as e:
        logging.error(f"Failed send message: {e}")

# --------------------------
# Main service orchestration
# --------------------------
class PumpFunService:
    def __init__(self):
        self.config = Config()
        setup_logging(self.config)
        self.bot = None
        if self.config.ENABLE_TELEGRAM and self.config.TELEGRAM_BOT_TOKEN:
            self.bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
        Notification.init(self.config)
        self.db = Database(self.config)
        self.rpc = RPCClient(self.config)
        self.redis = None
        self.token_manager = None
        self.wss_listener = None
        self.wss_task: Optional[asyncio.Task] = None
        self.fastapi_app = None

    async def start(self):
        logging.info("Starting Pump.fun Monitor service")
        # DB
        await self.db.connect()
        # Redis
        try:
            self.redis = aioredis.from_url(self.config.REDIS_URL, decode_responses=True)
            await self.redis.ping()
            logging.info("Redis connected")
        except Exception as e:
            logging.error(f"Redis connect failed: {e}")
            raise
        # RPC
        await self.rpc.__aenter__()
        # Token manager
        self.token_manager = TokenManager(self.config, self.db, self.rpc, self.redis)
        # ws listener
        self.wss_listener = WebSocketListener(self.config, self.token_manager, self.rpc, self.redis)
        # webhook set if domain provided
        if self.bot and (self.config.DOMAIN_URL or os.getenv('WEBHOOK_URL')):
            webhook_target = (self.config.DOMAIN_URL or os.getenv('WEBHOOK_URL')).rstrip("/") + self.config.TELEGRAM_WEBHOOK_PATH
            try:
                await asyncio.get_event_loop().run_in_executor(None, lambda: self.bot.set_webhook(webhook_target))
                logging.info(f"Telegram webhook set to {webhook_target}")
            except Exception as e:
                logging.warning(f"Failed set webhook: {e}")
        # FastAPI app
        self.fastapi_app = create_app(self)
        logging.info("Service ready. Use /iniciar to start monitoring")

    async def stop(self):
        logging.info("Stopping service")
        if self.wss_listener:
            await self.wss_listener.stop()
        if self.wss_task:
            self.wss_task.cancel()
        if self.db:
            await self.db.disconnect()
        if self.redis:
            await self.redis.close()
        if self.rpc:
            await self.rpc.__aexit__(None, None, None)
        logging.info("Stopped")

# --------------------------
# Entrypoint
# --------------------------
async def _main():
    svc = PumpFunService()
    await svc.start()
    # run uvicorn server
    config = uvicorn.Config(svc.fastapi_app, host="0.0.0.0", port=svc.config.HEALTH_PORT, log_level="info")
    server = uvicorn.Server(config)
    logging.info("Bot ready - control via Telegram")
    await server.serve()

def run_main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logging.info("Interrupted")

if __name__ == "__main__":
    run_main()
