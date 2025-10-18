#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pump.fun Monitor - Versión final integrada
- Webhook Telegram (Railway)
- WSS listener (Helius/QuickNode/PumpPortal)
- On-chain bonding curve decoding (IDL/Anchor-aware)
- Redis TTL + dedupe (CACHE_TTL_SEC)
- PostgreSQL optional (asyncpg)
- Bot commands/buttons: /start (menu), /iniciar (start monitor), /detener, /status, /tokens, /config
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
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import deque

import aiohttp
import asyncpg
import websockets
import uvicorn
from fastapi import FastAPI, Request, Response

# Redis asyncio client
import redis.asyncio as aioredis

# Try solders first (preferred), fallback to solana.publickey
try:
    from solders.pubkey import Pubkey as PublicKey
    USING_SOLDERS = True
except Exception:
    try:
        from solana.publickey import PublicKey
        USING_SOLDERS = False
    except Exception:
        PublicKey = None
        USING_SOLDERS = False

# Telegram (we use python-telegram-bot Bot simple wrapper for send_message / webhook)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

# Optional anchorpy (for Anchor IDL-friendly parsing, optional but used if present)
try:
    import anchorpy
    HAVE_ANCHORPY = True
except Exception:
    HAVE_ANCHORPY = False

# ------------------------------------------------------------------
# Constants & embedded fallback IDL (minimal BondingCurve account info)
# ------------------------------------------------------------------
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Minimal IDL fragment (fallback) - we embed the fields we parse
PUMP_FUN_IDL_EMBEDDED = {
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
    "address": PUMP_FUN_PROGRAM_ID
  }
}

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
class Config:
    def __init__(self, config_path: str = "config.json"):
        self.path = os.path.join(os.path.dirname(__file__), config_path)
        self._load()

    def _load(self):
        # load user config.json if present
        data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}

        # TELEGRAM / WEBHOOK
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', data.get('telegram_chat_id', ''))
        self.DOMAIN_URL = os.getenv('DOMAIN_URL', data.get('domain_url', ''))  # used for webhook
        self.WEBHOOK_URL = os.getenv('WEBHOOK_URL', data.get('webhook_url', ''))
        self.TELEGRAM_WEBHOOK_PATH = os.getenv('TELEGRAM_WEBHOOK_PATH', '/telegram/webhook')

        # DB + Redis
        self.DATABASE_URL = os.getenv('DATABASE_URL', data.get('database_url', ''))
        self.ENABLE_DB = os.getenv('ENABLE_DB', str(data.get('enable_db', 'true'))).lower() == 'true'
        self.REDIS_URL = os.getenv('REDIS_URL', data.get('redis_url', os.getenv('REDIS_URL', 'redis://localhost:6379/0')))

        # RPC endpoints & WSS
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', data.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', data.get('helius_rpc_url', ''))
        self.QUICKNODE_WSS = os.getenv('QUICKNODE_WSS', data.get('quicknode_wss', ''))
        self.HELIUS_WSS = os.getenv('HELIUS_WSS', data.get('helius_wss', ''))
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', data.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))

        # pick rpc providers order
        self.RPC_ENDPOINTS = []
        if self.QUICKNODE_RPC_URL:
            self.RPC_ENDPOINTS.append(('quicknode', self.QUICKNODE_RPC_URL))
        if self.HELIUS_RPC_URL:
            self.RPC_ENDPOINTS.append(('helius', self.HELIUS_RPC_URL))
        # fallback public
        self.RPC_ENDPOINTS.append(('public', 'https://api.mainnet-beta.solana.com'))

        # WSS priority
        self.USE_PUMPPORTAL = os.getenv('USE_PUMPPORTAL', 'true').lower() == 'true'
        # choose WSS url preference
        self.WSS_URL = None
        self.WSS_TYPE = None
        # prefer pumpportal if set
        if self.USE_PUMPPORTAL and self.PUMPPORTAL_WSS:
            self.WSS_URL = self.PUMPPORTAL_WSS
            self.WSS_TYPE = 'pumpportal'
        elif self.HELIUS_WSS:
            self.WSS_URL = self.HELIUS_WSS
            self.WSS_TYPE = 'helius'
        elif self.QUICKNODE_WSS:
            self.WSS_URL = self.QUICKNODE_WSS
            self.WSS_TYPE = 'quicknode'

        # Alerts & monitoring
        self.ALERT_RULES = data.get('alert_rules', [
            {"name": "momentum_120_15", "alert_percent": 120.0, "time_window_min": 15}
        ])
        # these env vars override
        self.ALERT_PERCENT = float(os.getenv('ALERT_PERCENT', str(self.ALERT_RULES[0]['alert_percent'])))
        self.ALERT_TIME_WINDOW_MIN = float(os.getenv('ALERT_TIME_WINDOW_MIN', str(self.ALERT_RULES[0]['time_window_min'])))

        mon = data.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', str(mon.get('max_monitor_time_min', 30))))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', str(mon.get('dump_threshold_percent', -50))))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', str(mon.get('price_poll_interval_sec', 5))))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', str(mon.get('max_concurrent_monitors', 40))))
        self.MAX_TOKENS_MONITORED = int(os.getenv('MAX_TOKENS_MONITORED', '200'))

        # Redis TTL
        self.CACHE_TTL_SEC = int(os.getenv('CACHE_TTL_SEC', '60'))

        # Server & logging
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', str(data.get('health_port', 8080))))
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', data.get('log_level', 'INFO'))
        self.MODE = os.getenv('MODE', data.get('mode', 'production'))

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
def setup_logging(cfg: Config):
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# ------------------------------------------------------------------
# Data models
# ------------------------------------------------------------------
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

# ------------------------------------------------------------------
# RPC Client with provider rotation & simple rate heuristics
# ------------------------------------------------------------------
class RPCClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.providers = cfg.RPC_ENDPOINTS
        self.idx = 0
        self.session: Optional[aiohttp.ClientSession] = None
        self.rate_limited = {}

    async def __aenter__(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=12, connect=3)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _current_provider(self) -> Tuple[str, str]:
        return self.providers[self.idx]

    async def _rotate(self):
        self.idx = (self.idx + 1) % len(self.providers)
        logging.info(f"RPC rotated to {self._current_provider()[0]}")

    async def rpc(self, method: str, params: list, timeout: int = 10) -> Optional[dict]:
        attempts = 0
        max_attempts = len(self.providers)
        while attempts < max_attempts:
            name, url = self._current_provider()
            payload = {"jsonrpc": "2.0", "id": int(time.time()*1000), "method": method, "params": params}
            try:
                async with self.session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('error'):
                            await self._rotate()
                            attempts += 1
                            continue
                        return data.get('result')
                    else:
                        await self._rotate()
                        attempts += 1
                        continue
            except asyncio.TimeoutError:
                await self._rotate()
                attempts += 1
                continue
            except Exception:
                await self._rotate()
                attempts += 1
                continue
        return None

    async def get_account_base64(self, address: str) -> Optional[dict]:
        return await self.rpc("getAccountInfo", [address, {"encoding": "base64"}])

# ------------------------------------------------------------------
# Database (Postgres) handler (optional)
# ------------------------------------------------------------------
class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not self.cfg.ENABLE_DB or not self.cfg.DATABASE_URL:
            logging.info("DB disabled or DATABASE_URL not set")
            return
        try:
            self.pool = await asyncpg.create_pool(self.cfg.DATABASE_URL, min_size=1, max_size=10)
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
                    )
                ''')
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_alerts (
                        id SERIAL PRIMARY KEY,
                        token_address TEXT,
                        alert_rule_name TEXT,
                        gain_percent NUMERIC,
                        time_elapsed_min NUMERIC,
                        price_at_alert NUMERIC,
                        market_cap_at_alert NUMERIC,
                        extra_data JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                ''')
            logging.info("Database connected and schema ensured")
        except Exception as e:
            logging.error(f"DB connect failed: {e}")
            self.pool = None

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def upsert_token(self, token: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_monitoring (token_address,symbol,name,bonding_curve,initial_price,initial_market_cap,
                      max_price,start_time,last_checked,status,metadata,priority)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (token_address) DO UPDATE SET
                      symbol=EXCLUDED.symbol, name=EXCLUDED.name, bonding_curve=EXCLUDED.bonding_curve,
                      initial_price=EXCLUDED.initial_price, initial_market_cap=EXCLUDED.initial_market_cap,
                      max_price=GREATEST(token_monitoring.max_price, EXCLUDED.max_price),
                      last_checked=EXCLUDED.last_checked, status=EXCLUDED.status, metadata=EXCLUDED.metadata, priority=EXCLUDED.priority
                ''', token.mint, token.symbol, token.name, token.bonding_curve, token.initial_price, token.initial_market_cap,
                     token.max_price, token.start_time, token.last_checked, token.status, json.dumps(token.metadata), token.priority)
        except Exception as e:
            logging.debug(f"DB upsert_token failed: {e}")

    async def record_alert(self, alert: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_alerts (token_address, alert_rule_name, gain_percent, time_elapsed_min, price_at_alert, market_cap_at_alert, extra_data)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                ''', alert.token_address, alert.rule_name, alert.gain_percent, alert.time_elapsed_min, alert.price_at_alert, alert.market_cap_at_alert, json.dumps(alert.extra_data))
        except Exception as e:
            logging.debug(f"DB record_alert failed: {e}")

# ------------------------------------------------------------------
# IDL fetcher (try upstream repo raw, fallback to embedded)
# ------------------------------------------------------------------
async def fetch_idl() -> dict:
    # try to fetch from upstream Chainstack repo raw (common location)
    possible_urls = [
        "https://raw.githubusercontent.com/chainstacklabs/pump-fun-bot/main/idl/pump_fun_idl.json",
        "https://raw.githubusercontent.com/chainstacklabs/pump-fun-bot/main/idl/pump_idl.json",
    ]
    async with aiohttp.ClientSession() as session:
        for u in possible_urls:
            try:
                async with session.get(u, timeout=6) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logging.info("IDL downloaded from upstream repo")
                        return data
            except Exception:
                continue
    logging.info("Using embedded IDL fallback")
    return PUMP_FUN_IDL_EMBEDDED

# ------------------------------------------------------------------
# Bonding curve PDA compute & parser
# ------------------------------------------------------------------
def compute_bonding_curve_pda(mint: str) -> Optional[str]:
    try:
        mint_bytes = base58.b58decode(mint)
        program_bytes = base58.b58decode(PUMP_FUN_PROGRAM_ID)
        if USING_SOLDERS and PublicKey:
            program_pub = PublicKey(program_bytes)
            pda, bump = PublicKey.find_program_address([b"bonding_curve", mint_bytes], program_pub)
            return str(pda)
        elif PublicKey:
            # solana.publickey.PublicKey.find_program_address expects seeds list of bytes
            pda, bump = PublicKey.find_program_address([b"bonding_curve", mint_bytes], PublicKey(program_bytes))
            return str(pda)
    except Exception:
        return None
    return None

def parse_bonding_curve_account_b64(b64data: str) -> Optional[Dict]:
    try:
        raw = base64.b64decode(b64data)
        if len(raw) < 49:
            return None
        offset = 8
        vToken, vSol, rToken, rSol, supply = struct.unpack_from("<QQQQQ", raw, offset)
        offset += 40
        complete = struct.unpack_from("<?", raw, offset)[0]
        price = 0.0
        market_cap = 0.0
        # Many Pump tokens are 6 decimals; SOL has 9 decimals: we normalize price to SOL per token
        if rToken > 0 and rSol > 0:
            price = (float(rSol) / 1e9) / (float(rToken) / 1e6)
            market_cap = (float(supply) / 1e6) * price
        return {'price': price, 'market_cap': market_cap, 'complete': bool(complete), 'reserves': {'token': rToken, 'sol': rSol}}
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
        if not data or len(data) < 1:
            return False
        parsed = parse_bonding_curve_account_b64(data[0])
        if not parsed:
            return False
        if parsed['reserves']['token'] == 0 and parsed['reserves']['sol'] == 0:
            return False
        return True
    except Exception:
        return False

# ------------------------------------------------------------------
# Alert Engine (checks against initial_price within time window)
# ------------------------------------------------------------------
class AlertEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rules = cfg.ALERT_RULES

    def evaluate(self, token: TokenData, current_price: float, elapsed_min: float) -> List[AlertData]:
        if current_price <= 0 or token.initial_price <= 0:
            return []
        gain = ((current_price - token.initial_price) / token.initial_price) * 100.0
        triggered: List[AlertData] = []
        for r in self.rules:
            try:
                thresh = float(r.get('alert_percent', 0.0))
                tw = float(r.get('time_window_min', self.cfg.ALERT_TIME_WINDOW_MIN))
                if gain >= thresh and elapsed_min <= tw:
                    triggered.append(AlertData(
                        token_address=token.mint,
                        rule_name=r.get('name', 'rule'),
                        gain_percent=gain,
                        time_elapsed_min=elapsed_min,
                        price_at_alert=current_price,
                        market_cap_at_alert=current_price * (token.initial_market_cap / max(token.initial_price, 1e-12)),
                        extra_data={'symbol': token.symbol, 'name': token.name, 'initial_price': token.initial_price, 'max_price': token.max_price}
                    ))
            except Exception:
                continue
        return triggered

# ------------------------------------------------------------------
# Notifications (Telegram)
# ------------------------------------------------------------------
class Notification:
    bot: Optional[Bot] = None
    cfg: Optional[Config] = None

    @classmethod
    def init(cls, cfg: Config):
        cls.cfg = cfg
        if cfg.TELEGRAM_BOT_TOKEN:
            cls.bot = Bot(token=cfg.TELEGRAM_BOT_TOKEN)

    @classmethod
    async def send_alert(cls, token: TokenData, alert: AlertData):
        if not cls.bot or not cls.cfg or not cls.cfg.TELEGRAM_CHAT_ID:
            logging.info(f"ALERT (no-telegram): {token.symbol} +{alert.gain_percent:.1f}%")
            return
        try:
            message = cls._format(token, alert)
            keyboard = cls._keyboard(token.mint)
            # send in threadpool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: cls.bot.send_message(chat_id=cls.cfg.TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown', reply_markup=keyboard, disable_web_page_preview=True))
            logging.info(f"Telegram alert sent for {token.symbol}")
        except Exception as e:
            logging.error(f"Telegram send failed: {e}")

    @staticmethod
    def _format(token: TokenData, alert: AlertData) -> str:
        pr = "🔥" if token.priority else "🚀"
        return (
            f"{pr} *ALERTA DE MOMENTUM* {pr}\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Mint:* `{token.mint}`\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f} min\n"
            f"*Precio inicial:* {token.initial_price:.8f}\n"
            f"*Precio actual:* {alert.price_at_alert:.8f}\n"
            f"*Market Cap aprox:* ${alert.market_cap_at_alert:,.0f}\n\n"
            f"🔗 Enlaces:\n"
            f"• Pump.fun: https://pump.fun/{token.mint}\n"
            f"• DexScreener: https://dexscreener.com/solana/{token.mint}\n"
            f"• RugCheck: https://rugcheck.xyz/tokens/{token.mint}\n"
            f"\n🕒 Tiempo desde creación: {alert.time_elapsed_min:.1f} minutos\n"
        )

    @staticmethod
    def _keyboard(mint: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"), InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}")]
        ])

# ------------------------------------------------------------------
# Token Manager (monitoring + Redis dedupe)
# ------------------------------------------------------------------
class TokenManager:
    def __init__(self, cfg: Config, db: Database, rpc: RPCClient, redis_client):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.redis = redis_client
        self.alert_engine = AlertEngine(cfg)
        self.monitored: Dict[str, TokenData] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.alerted = set()
        self.semaphore = asyncio.Semaphore(cfg.MAX_CONCURRENT_MONITORS)

    async def add_token(self, mint: str, symbol: str = "UNKNOWN", name: str = "UNKNOWN", bonding_curve: Optional[str] = None, initial_price: float = 0.0, initial_market_cap: float = 0.0):
        if mint in self.monitored:
            return
        if len(self.monitored) >= self.cfg.MAX_TOKENS_MONITORED:
            logging.warning("Max tokens monitored reached")
            return
        if not bonding_curve:
            bonding_curve = compute_bonding_curve_pda(mint)
        if not bonding_curve:
            logging.debug("No bonding curve PDA for mint")
            return
        valid = await validate_pump_fun_token(self.rpc, mint, bonding_curve)
        if not valid:
            logging.debug("Bonding curve validation failed")
            return
        # fetch initial price if not provided
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

        priority = initial_market_cap >= 50000
        token = TokenData(mint=mint, symbol=symbol, name=name, initial_price=initial_price, initial_market_cap=initial_market_cap, max_price=initial_price, start_time=datetime.now(timezone.utc), bonding_curve=bonding_curve, priority=priority)
        token.last_checked = datetime.now(timezone.utc)
        self.monitored[mint] = token
        await self.db.upsert_token(token)
        self.tasks[mint] = asyncio.create_task(self._monitor(mint))
        logging.info(f"Started monitoring {token.symbol} ({mint[:8]}...) {'PRIORITY' if priority else ''}")

    async def _monitor(self, mint: str):
        async with self.semaphore:
            token = self.monitored.get(mint)
            if not token:
                return
            try:
                while mint in self.monitored:
                    elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                    if elapsed_min >= self.cfg.MAX_MONITOR_TIME_MIN:
                        logging.info(f"Token {token.symbol} {mint} timed out after {elapsed_min:.1f}min")
                        await self._remove(mint, "timeout")
                        return

                    current_price = 0.0
                    # fetch on-chain bonding curve each poll
                    try:
                        acc = await self.rpc.get_account_base64(token.bonding_curve)
                        if acc and acc.get('value') and acc['value'].get('data'):
                            parsed = parse_bonding_curve_account_b64(acc['value']['data'][0])
                            if parsed and parsed['price'] > 0:
                                current_price = parsed['price']
                                if token.initial_price == 0.0:
                                    token.initial_price = current_price
                                    token.initial_market_cap = parsed['market_cap']
                                    token.max_price = current_price
                    except Exception:
                        pass

                    if current_price == 0.0:
                        await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
                        continue

                    token.last_checked = datetime.now(timezone.utc)
                    if current_price > token.max_price:
                        token.max_price = current_price

                    # save periodically
                    try:
                        # keep DB updates light; update every few iterations
                        await self.db.upsert_token(token)
                    except Exception:
                        pass

                    # detect dump
                    if token.max_price > 0:
                        loss = ((current_price - token.max_price) / token.max_price) * 100.0
                        if loss <= self.cfg.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"Dump detected for {token.symbol} {loss:.1f}% -> removing")
                            await self._remove(mint, "dumped")
                            return

                    # evaluate alerts
                    alerts = self.alert_engine.evaluate(token, current_price, elapsed_min)
                    for alert in alerts:
                        key = f"alert:{mint}"
                        locked = False
                        try:
                            if await self.redis.get(key):
                                locked = True
                        except Exception:
                            locked = False
                        if locked:
                            continue
                        try:
                            await self.redis.set(key, "1", ex=self.cfg.CACHE_TTL_SEC)
                        except Exception:
                            pass
                        await self.db.record_alert(alert)
                        await Notification.send_alert(token, alert)
                        self.alerted.add(mint)
                        logging.info(f"ALERT queued: {token.symbol} +{alert.gain_percent:.1f}%")
                        await self._remove(mint, "alert_sent")
                        return

                    await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)

            except asyncio.CancelledError:
                logging.debug("Monitor cancelled")
            except Exception as e:
                logging.error(f"Monitor error for {mint}: {e}")
                await self._remove(mint, "error")

    async def _remove(self, mint: str, reason: str):
        token = self.monitored.get(mint)
        if token:
            token.status = reason
            token.last_checked = datetime.now(timezone.utc)
            try:
                await self.db.upsert_token(token)
            except Exception:
                pass
            del self.monitored[mint]
        task = self.tasks.get(mint)
        if task:
            task.cancel()
            del self.tasks[mint]
        logging.info(f"Removed {mint}: {reason}")

# ------------------------------------------------------------------
# WebSocket Listener (PumpPortal or logsSubscribe)
# ------------------------------------------------------------------
class WebSocketListener:
    def __init__(self, cfg: Config, manager: TokenManager):
        self.cfg = cfg
        self.manager = manager
        self.running = False
        self.ws = None
        self.reconnect_delay = 5
        self.max_reconnect_delay = 60
        self.processed_txs = deque(maxlen=2000)
        self.wss_type = cfg.WSS_TYPE

    async def connect(self):
        if not self.cfg.WSS_URL:
            logging.error("No WSS URL configured")
            return
        self.running = True
        uri = self.cfg.WSS_URL
        while self.running:
            try:
                logging.info(f"Connecting to WSS ({self.wss_type}): {uri}")
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                    self.ws = websocket
                    self.reconnect_delay = 5
                    # subscribe based on type
                    if self.wss_type in ('helius', 'quicknode'):
                        sub = {"jsonrpc":"2.0","id":1,"method":"logsSubscribe","params":[{"mentions":[PUMP_FUN_PROGRAM_ID]},{"commitment":"confirmed"}]}
                        await websocket.send(json.dumps(sub))
                        logging.info("Subscribed logsSubscribe")
                    elif self.wss_type == 'pumpportal':
                        try:
                            await websocket.send(json.dumps({"method":"subscribeNewToken"}))
                        except Exception:
                            pass
                        logging.info("Subscribed pumpportal")
                    async for raw in websocket:
                        try:
                            data = json.loads(raw)
                            await self._handle(data)
                        except Exception as e:
                            logging.debug(f"WSS msg error: {e}")
            except Exception as e:
                logging.error(f"WSS connection error: {e}")
            finally:
                self.ws = None
                if self.running:
                    logging.info(f"Reconnect in {self.reconnect_delay}s")
                    await asyncio.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def _handle(self, payload: Dict):
        try:
            # PumpPortal direct event format
            if payload.get('mint') and payload.get('signature'):
                mint = payload.get('mint')
                sig = payload.get('signature')
                if sig in self.processed_txs:
                    return
                self.processed_txs.append(sig)
                symbol = payload.get('symbol', 'UNKNOWN')
                name = payload.get('name', symbol)
                await self.manager.add_token(mint=mint, symbol=symbol, name=name)
                return

            # logsSubscribe format
            if payload.get('method') != 'logsNotification':
                return
            params = payload.get('params', {})
            result = params.get('result', {})
            value = result.get('value', {})
            logs = value.get('logs', [])
            signature = value.get('signature', '')
            if signature in self.processed_txs:
                return
            # detect create instruction text to reduce noise
            is_create = any('Instruction: Create' in l or 'create' in l.lower() for l in logs)
            if not is_create:
                return
            self.processed_txs.append(signature)
            # extract mint by inspecting the transaction (multi-method)
            mint = await self._extract_mint_from_tx(signature)
            if mint:
                await self.manager.add_token(mint=mint)
            else:
                logging.debug("Could not extract mint from tx")
        except Exception as e:
            logging.debug(f"Handler error: {e}")

    async def _extract_mint_from_tx(self, signature: str) -> Optional[str]:
        try:
            # ensure rpc session
            if not self.manager.rpc.session:
                await self.manager.rpc.__aenter__()
            tx = await self.manager.rpc.rpc("getTransaction", [signature, {"encoding":"jsonParsed","commitment":"confirmed"}], timeout=10)
            if not tx:
                return None
            meta = tx.get('meta', {})
            transaction = tx.get('transaction', {})
            message = transaction.get('message', {})
            post = meta.get('postTokenBalances', [])
            if post:
                for b in post:
                    mint = b.get('mint')
                    ui = b.get('uiTokenAmount', {})
                    amt = ui.get('uiAmount')
                    if mint and amt and float(amt) > 0 and mint != 'So11111111111111111111111111111111111111112':
                        return mint
            # balance diff
            pre = set([b.get('mint') for b in meta.get('preTokenBalances', []) if b.get('mint')])
            postm = set([b.get('mint') for b in post if b.get('mint')])
            new = postm - pre
            if new:
                for m in new:
                    return m
            # instructions/account keys fallback
            instructions = message.get('instructions', [])
            for inst in instructions:
                if inst.get('programId') != PUMP_FUN_PROGRAM_ID:
                    continue
                # accounts may contain mint addresses
                accounts = inst.get('accounts', [])
                for a in accounts[:4]:
                    if isinstance(a, str) and len(a) == 44 and a not in (PUMP_FUN_PROGRAM_ID,):
                        return a
                # sometimes parsed instruction contains inner data
            # accountKeys last attempt
            keys = message.get('accountKeys', [])
            for k in keys[:8]:
                pubkey = k.get('pubkey') if isinstance(k, dict) else (k if isinstance(k, str) else None)
                if pubkey and isinstance(pubkey, str) and len(pubkey) == 44:
                    if pubkey not in (PUMP_FUN_PROGRAM_ID, '11111111111111111111111111111111', 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'):
                        return pubkey
            return None
        except Exception as e:
            logging.debug(f"Extract mint error: {e}")
            return None

    async def stop(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

# ------------------------------------------------------------------
# FastAPI app (webhook endpoints + health + metrics)
# ------------------------------------------------------------------
def create_app(bot_instance):
    app = FastAPI(title="Pump.fun Monitor")

    @app.get("/")
    async def root():
        return {"status":"ok","service":"pump.fun monitor"}

    @app.get("/health")
    async def health():
        w = bot_instance.wss_listener
        return {"status":"ok","wss_connected": bool(w.ws) if w else False, "monitored": len(bot_instance.token_manager.monitored) if bot_instance.token_manager else 0}

    @app.get("/metrics")
    async def metrics():
        # small metrics dump
        tokens = []
        for mint, token in list(bot_instance.token_manager.monitored.items())[:30]:
            elapsed = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
            gain = 0.0
            if token.initial_price > 0:
                gain = ((token.max_price - token.initial_price) / token.initial_price) * 100.0
            tokens.append({"mint": mint, "symbol": token.symbol, "gain": round(gain,2), "elapsed_min": round(elapsed,2)})
        return {"monitored": len(bot_instance.token_manager.monitored), "alerts": len(bot_instance.token_manager.alerted), "tokens": tokens}

    @app.post(bot_instance.config.TELEGRAM_WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        try:
            data = await request.json()
        except Exception:
            return Response(status_code=400)
        # handle message or callback_query
        if 'message' in data or 'edited_message' in data:
            message = data.get('message') or data.get('edited_message')
            chat = message.get('chat', {})
            text = message.get('text', '')
            # simple auth: only accept configured chat id
            if bot_instance.config.TELEGRAM_CHAT_ID and str(chat.get('id')) != str(bot_instance.config.TELEGRAM_CHAT_ID):
                return {"ok": True}
            if text and text.startswith('/'):
                await handle_command(text, chat, bot_instance)
        elif 'callback_query' in data:
            await handle_callback_query(data['callback_query'], bot_instance)
        return {"ok": True}

    return app

# ------------------------------------------------------------------
# Telegram command handlers (menu/buttons)
# ------------------------------------------------------------------
async def handle_command(text: str, chat: Dict, bot_instance):
    command = text.split()[0]
    chat_id = chat.get('id')
    if command == "/start":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Iniciar", callback_data="cmd:start"), InlineKeyboardButton("⏸️ Detener", callback_data="cmd:stop")],
            [InlineKeyboardButton("📊 Status", callback_data="cmd:status"), InlineKeyboardButton("🔍 Tokens", callback_data="cmd:tokens")],
            [InlineKeyboardButton("⚙️ Config", callback_data="cmd:config")]
        ])
        await bot_instance.bot.send_message(chat_id=chat_id, text=(
            "🤖 Pump.fun Monitor\n\n"
            "• /iniciar - Iniciar monitoreo\n"
            "• /detener - Detener\n"
            "• /status - Estado\n"
            "• /tokens - Tokens activos\n"
            "• /config - Configuración"
        ), parse_mode="Markdown", reply_markup=kb)
    elif command == "/iniciar":
        if not bot_instance.wss_task or bot_instance.wss_task.done():
            bot_instance.wss_task = asyncio.create_task(bot_instance.wss_listener.connect())
            await bot_instance.bot.send_message(chat_id=chat_id, text="▶️ Monitor INICIADO", parse_mode="Markdown")
        else:
            await bot_instance.bot.send_message(chat_id=chat_id, text="⚠️ Monitor ya corriendo")
    elif command == "/detener":
        if bot_instance.wss_listener and bot_instance.wss_listener.running:
            await bot_instance.wss_listener.stop()
            await bot_instance.bot.send_message(chat_id=chat_id, text="⏸️ Monitor DETENIDO")
        else:
            await bot_instance.bot.send_message(chat_id=chat_id, text="⚠️ No está corriendo")
    elif command == "/status":
        w = bot_instance.wss_listener
        wss_status = "Conectado" if w and w.ws else "Desconectado"
        running_status = "Activo" if w and w.running else "Detenido"
        rpc_provider = bot_instance.rpc._current_provider()[0]
        await bot_instance.bot.send_message(chat_id=chat_id, text=(
            f"🔌 WSS: {wss_status}\n▶️ Monitor: {running_status}\n🌐 RPC: {rpc_provider}\n"
            f"📈 Tokens: {len(bot_instance.token_manager.monitored)}\nAlerts: {len(bot_instance.token_manager.alerted)}"
        ), parse_mode="Markdown")
    elif command == "/tokens":
        tokens = list(bot_instance.token_manager.monitored.items())[:20]
        if not tokens:
            await bot_instance.bot.send_message(chat_id=chat_id, text="No hay tokens monitoreados")
            return
        msg = "Tokens monitoreados (Top 20):\n\n"
        for m, t in tokens:
            elapsed = (datetime.now(timezone.utc) - t.start_time).total_seconds() / 60.0
            gain = 0.0
            if t.initial_price > 0:
                gain = ((t.max_price - t.initial_price) / t.initial_price) * 100.0
            msg += f"{t.symbol} | {gain:+.1f}% | {elapsed:.1f}min\n`{m[:20]}...`\n\n"
        await bot_instance.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
    elif command == "/config":
        cfg = bot_instance.config
        msg = (f"Alert: {cfg.ALERT_PERCENT}% in {cfg.ALERT_TIME_WINDOW_MIN}min\nMax monitor: {cfg.MAX_MONITOR_TIME_MIN}min\nDump threshold: {cfg.DUMP_THRESHOLD_PERCENT}%\nPoll: {cfg.PRICE_POLL_INTERVAL_SEC}s\nCache TTL: {cfg.CACHE_TTL_SEC}s")
        await bot_instance.bot.send_message(chat_id=chat_id, text=msg)

async def handle_callback_query(cq: Dict, bot_instance):
    cd = cq.get('data', '')
    chat_id = cq['message']['chat']['id']
    if cd == "cmd:start":
        if not bot_instance.wss_task or bot_instance.wss_task.done():
            bot_instance.wss_task = asyncio.create_task(bot_instance.wss_listener.connect())
            await bot_instance.bot.send_message(chat_id=chat_id, text="▶️ Monitor iniciado")
        else:
            await bot_instance.bot.send_message(chat_id=chat_id, text="⚠️ Ya está corriendo")
    elif cd == "cmd:stop":
        if bot_instance.wss_listener and bot_instance.wss_listener.running:
            await bot_instance.wss_listener.stop()
            await bot_instance.bot.send_message(chat_id=chat_id, text="⏸️ Monitor detenido")
        else:
            await bot_instance.bot.send_message(chat_id=chat_id, text="⚠️ No está corriendo")
    elif cd == "cmd:status":
        await handle_command("/status", {"id": chat_id}, bot_instance)
    elif cd == "cmd:tokens":
        await handle_command("/tokens", {"id": chat_id}, bot_instance)
    elif cd == "cmd:config":
        await handle_command("/config", {"id": chat_id}, bot_instance)

# ------------------------------------------------------------------
# PumpFunService - orchestrates everything
# ------------------------------------------------------------------
class PumpFunService:
    def __init__(self):
        self.config = Config()
        setup_logging(self.config)
        self.bot = None
        if self.config.TELEGRAM_BOT_TOKEN:
            self.bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
        Notification.init(self.config)
        self.db = Database(self.config)
        self.rpc = RPCClient(self.config)
        self.redis = None
        self.token_manager: Optional[TokenManager] = None
        self.wss_listener: Optional[WebSocketListener] = None
        self.wss_task: Optional[asyncio.Task] = None
        self.fastapi_app = None

    async def start(self):
        logging.info("Starting Pump.fun Monitor service")
        await self.db.connect()
        # Redis connect
        try:
            self.redis = aioredis.from_url(self.config.REDIS_URL, decode_responses=True)
            await self.redis.ping()
            logging.info("Redis connected")
        except Exception as e:
            logging.error(f"Redis connection failed: {e}")
            raise

        # prepare rpc
        await self.rpc.__aenter__()

        # token manager
        self.token_manager = TokenManager(self.config, self.db, self.rpc, self.redis)

        # wss listener
        self.wss_listener = WebSocketListener(self.config, self.token_manager)

        # set webhook if domain set
        if self.bot and (self.config.DOMAIN_URL or self.config.WEBHOOK_URL):
            webhook_url = (self.config.DOMAIN_URL or self.config.WEBHOOK_URL).rstrip("/") + self.config.TELEGRAM_WEBHOOK_PATH
            try:
                await self.bot.set_webhook(url=webhook_url)
                logging.info(f"Telegram webhook set to {webhook_url}")
            except Exception as e:
                logging.warning(f"Set webhook failed: {e}")

        # fastapi app
        self.fastapi_app = create_app(self)
        logging.info("Service ready. Use /iniciar to start monitoring.")

    async def stop(self):
        logging.info("Stopping service")
        if self.wss_listener:
            await self.wss_listener.stop()
        if self.wss_task:
            self.wss_task.cancel()
        await self.db.disconnect()
        if self.redis:
            await self.redis.close()
        await self.rpc.__aexit__(None, None, None)
        logging.info("Stopped")

# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------
async def _main():
    svc = PumpFunService()
    await svc.start()
    # run uvicorn
    config = uvicorn.Config(svc.fastapi_app, host="0.0.0.0", port=svc.config.HEALTH_PORT, log_level="info")
    server = uvicorn.Server(config)
    logging.info("Bot ready - control via Telegram")
    await server.serve()

def run_main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user")

if __name__ == "__main__":
    run_main()
