#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot_final.py
Bot de monitorización Pump.fun (sin compras automáticas).
- Webhook Telegram (FastAPI) -> evita problemas de event loop / polling en Railway
- Redis para dedupe + TTL
- PostgreSQL opcional para persistencia
- Bonding curve on-chain parsing (getAccountInfo base64 + struct) + Anchor IDL support
- PDA / associated bonding curve computation (best-effort)
- DexScreener fallback for price
- Alert rules: momentum % + time window (default 120% / 15min)
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
import uvicorn
from fastapi import FastAPI, Request, Response, BackgroundTasks

# Redis asyncio client (redis-py >=5 supports asyncio)
import redis.asyncio as aioredis

# Solana / Anchor
from solders.pubkey import Pubkey  # en lugar de solana.publickey.PublicKey
from solana.rpc.async_api import AsyncClient as SolanaAsyncClient

# Anchorpy (optional decoding / program usage if available)
try:
    from anchorpy import Program, Provider, Wallet, Idl
    ANCHORPY_AVAILABLE = True
except Exception:
    ANCHORPY_AVAILABLE = False

# Telegram (only for outgoing bot actions)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

# -----------------------
# IDL (pump.fun) embedded (you provided it in the convo)
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

# -----------------------
# Config loader
# -----------------------
class Config:
    def __init__(self, config_path: str = "config.json"):
        self.path = os.path.join(os.path.dirname(__file__), config_path)
        self._load()

    def _load(self):
        data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        # env vars override
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', data.get('telegram_chat_id', ''))
        self.DATABASE_URL = os.getenv('DATABASE_URL', data.get('database_url', ''))
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', data.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', data.get('helius_rpc_url', ''))
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', data.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))
        self.REDIS_URL = os.getenv('REDIS_URL', data.get('redis_url', 'redis://localhost:6379/0'))

        # alert rules (allow override)
        self.ALERT_RULES = data.get('alert_rules', [
            {
                "name": "momentum_120_15",
                "alert_percent": 120.0,
                "time_window_min": 15,
                "description": "120% en 15 minutos"
            }
        ])

        monitoring = data.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', monitoring.get('max_monitor_time_min', 30)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', monitoring.get('dump_threshold_percent', -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', monitoring.get('price_poll_interval_sec', 5)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', monitoring.get('max_concurrent_monitors', 40)))

        self.LOG_LEVEL = os.getenv('LOG_LEVEL', data.get('log_level', 'INFO'))
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', data.get('health_port', 8080)))
        self.ENABLE_DB = os.getenv('ENABLE_DB', str(data.get('enable_db', 'true'))).lower() == 'true'
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', str(data.get('enable_telegram', 'true'))).lower() == 'true'
        self.TELEGRAM_WEBHOOK_PATH = os.getenv('TELEGRAM_WEBHOOK_PATH', '/telegram/webhook')

# -----------------------
# Logging setup
# -----------------------
def setup_logging(cfg: Config):
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
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
# RPC Client (rotate providers)
# -----------------------
class RPCClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session: Optional[aiohttp.ClientSession] = None
        self.providers = []
        if self.cfg.QUICKNODE_RPC_URL:
            self.providers.append(('quicknode', self.cfg.QUICKNODE_RPC_URL))
        if self.cfg.HELIUS_RPC_URL:
            self.providers.append(('helius', self.cfg.HELIUS_RPC_URL))
        if not self.providers:
            self.providers.append(('public','https://api.mainnet-beta.solana.com'))
        self.idx = 0

    async def __aenter__(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _url(self):
        return self.providers[self.idx][1]

    async def _rotate(self):
        self.idx = (self.idx + 1) % len(self.providers)
        logging.info(f"Switched RPC to {self.providers[self.idx][0]}")

    async def rpc(self, method: str, params: list, timeout: int = 8):
        payload = {"jsonrpc":"2.0","id":int(time.time()),"method":method,"params":params}
        url = self._url()
        try:
            async with self.session.post(url, json=payload, timeout=timeout) as resp:
                data = await resp.json()
                if resp.status != 200 or 'error' in data:
                    raise Exception(f"RPC error {resp.status} {data.get('error')}")
                return data.get('result')
        except Exception as e:
            logging.debug(f"RPC {method} failed at {url}: {e}")
            await self._rotate()
            # try once more
            url2 = self._url()
            try:
                async with self.session.post(url2, json=payload, timeout=timeout) as resp:
                    data = await resp.json()
                    if resp.status != 200 or 'error' in data:
                        raise Exception(f"RPC error {resp.status} {data.get('error')}")
                    return data.get('result')
            except Exception as e2:
                logging.error(f"RPC retry failed: {e2}")
                raise

    async def get_account_base64(self, address: str):
        return await self.rpc("getAccountInfo", [address, {"encoding":"base64"}])

# -----------------------
# DB (Postgres) handler (optional)
# -----------------------
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
                await conn.execute('''CREATE TABLE IF NOT EXISTS token_monitoring (
                    token_address TEXT PRIMARY KEY,
                    symbol TEXT, name TEXT, bonding_curve TEXT,
                    initial_price NUMERIC, initial_market_cap NUMERIC,
                    max_price NUMERIC, start_time TIMESTAMPTZ,
                    last_checked TIMESTAMPTZ, status TEXT, metadata JSONB, created_at TIMESTAMPTZ DEFAULT NOW()
                )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS token_alerts (
                    id SERIAL PRIMARY KEY, token_address TEXT, alert_rule_name TEXT,
                    gain_percent NUMERIC, time_elapsed_min NUMERIC,
                    price_at_alert NUMERIC, market_cap_at_alert NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW(), extra_data JSONB
                )''')
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
                    INSERT INTO token_monitoring (token_address,symbol,name,bonding_curve,initial_price,initial_market_cap,max_price,start_time,last_checked,status,metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    ON CONFLICT (token_address) DO UPDATE SET
                        symbol=EXCLUDED.symbol, name=EXCLUDED.name, bonding_curve=EXCLUDED.bonding_curve,
                        initial_price=EXCLUDED.initial_price, initial_market_cap=EXCLUDED.initial_market_cap,
                        max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price),
                        last_checked=EXCLUDED.last_checked, status=EXCLUDED.status, metadata=EXCLUDED.metadata
                ''', token.mint, token.symbol, token.name, token.bonding_curve,
                      token.initial_price, token.initial_market_cap, token.max_price,
                      token.start_time, token.last_checked, token.status, json.dumps(token.metadata))
        except Exception as e:
            logging.debug(f"DB upsert failed: {e}")

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

# -----------------------
# Alert Engine
# -----------------------
class AlertEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rules = cfg.ALERT_RULES

    def evaluate(self, token: TokenData, current_price: float, elapsed_min: float) -> List[AlertData]:
        if current_price <= 0 or token.initial_price <= 0:
            return []
        gain = ((current_price - token.initial_price) / token.initial_price) * 100.0
        triggered = []
        for r in self.rules:
            if gain >= float(r['alert_percent']) and elapsed_min <= float(r['time_window_min']):
                triggered.append(AlertData(
                    token_address=token.mint,
                    rule_name=r['name'],
                    gain_percent=gain,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=0.0,
                    extra_data={'symbol': token.symbol, 'name': token.name, 'initial_price': token.initial_price, 'max_price': token.max_price}
                ))
        return triggered

# -----------------------
# Notification (Telegram outgoing)
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
    async def send_alert(cls, token: TokenData, alert: AlertData):
        if not cls.bot or not cls.cfg or not cls.cfg.TELEGRAM_CHAT_ID:
            logging.info(f"ALERT (no telegram): {token.symbol} +{alert.gain_percent:.1f}%")
            return
        try:
            message = cls._format(token, alert)
            keyboard = cls._keyboard(token.mint)
            # Bot.send_message is synchronous but wrapped in asyncio by aiohttp default - use run_in_executor
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, cls.bot.send_message, cls.cfg.TELEGRAM_CHAT_ID, message, 'Markdown', False, None, None, None, json.loads(json.dumps({"reply_markup": keyboard.to_dict()})))
            logging.info(f"Telegram alert sent for {token.mint}")
        except Exception as e:
            logging.error(f"Telegram send failed: {e}")

    @staticmethod
    def _format(token: TokenData, alert: AlertData) -> str:
        return (
            f"🚀 *ALERTA DE MOMENTUM* 🚀\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Mint:* `{token.mint}`\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f} min\n"
            f"*Precio al alert:* {alert.price_at_alert:.8f}\n"
            f"*Market Cap aprox:* ${alert.market_cap_at_alert:,.0f}\n\n"
            f"🔗 *Enlaces rápidos*\n"
            f"• Pump.fun: https://pump.fun/{token.mint}\n"
            f"• DexScreener: https://dexscreener.com/solana/{token.mint}\n"
            f"• RugCheck: https://rugcheck.xyz/tokens/{token.mint}\n"
            f"• Birdeye: https://birdeye.so/token/{token.mint}?chain=solana\n\n"
            f"🕒 Tiempo desde creación: {alert.time_elapsed_min:.1f} minutos\n"
        )

    @staticmethod
    def _keyboard(mint: str) -> InlineKeyboardMarkup:
        kb = [
            [InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"),
             InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
             InlineKeyboardButton("Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")]
        ]
        return InlineKeyboardMarkup(kb)

# -----------------------
# Compute associated bonding curve PDA (best-effort)
# -----------------------
def compute_associated_bonding_curve_pda(bonding_curve_pubkey: PublicKey, mint_pubkey: PublicKey) -> PublicKey:
    """
    Best-effort: Many have used seeds [bonding_curve, token_program_id, mint] and associated token program.
    We try several common PDA derivations used by pump.fun ecosystem.
    """
    PROGRAM_PUBKEY = PublicKey(PUMP_FUN_IDL['metadata']['address'])
    tokenprog = PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    atoken_program = PublicKey("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

    # Try 1: associated bonding curve (bondingCurve + tokenprog + mint) with atoken program
    seeds1 = [bytes(bonding_curve_pubkey), bytes(tokenprog), bytes(mint_pubkey)]
    try:
        pda1, bump1 = PublicKey.find_program_address(seeds1, atoken_program)
        return pda1
    except Exception:
        pass
    # Try 2: seeds with program id
    seeds2 = [b"bonding_curve", bytes(bonding_curve_pubkey)]
    try:
        pda2, bump2 = PublicKey.find_program_address(seeds2, PROGRAM_PUBKEY)
        return pda2
    except Exception:
        pass
    # Fallback: return bonding_curve_pubkey itself (caller must handle)
    return bonding_curve_pubkey

# -----------------------
# Parse bonding curve account (on-chain) via getAccountInfo base64
# Anchor layout: skip 8 byte discriminator then u64 x5 + bool
# -----------------------
def parse_bonding_curve_account_b64(b64data: str) -> Optional[Dict]:
    try:
        raw = base64.b64decode(b64data)
        # check size
        expected = 8 + (8*5 + 1)
        if len(raw) < expected:
            return None
        offset = 8
        vToken, vSol, rToken, rSol, supply = struct.unpack_from("<QQQQQ", raw, offset)
        offset += 8*5
        complete = struct.unpack_from("<?", raw, offset)[0]
        price = 0.0
        market_cap = 0.0
        if rToken and rSol:
            try:
                price = float(rSol) / float(rToken)
            except Exception:
                price = 0.0
        try:
            market_cap = float(supply) * price
        except Exception:
            market_cap = 0.0
        return {
            'virtualTokenReserves': vToken,
            'virtualSolReserves': vSol,
            'realTokenReserves': rToken,
            'realSolReserves': rSol,
            'tokenTotalSupply': supply,
            'complete': bool(complete),
            'price': price,
            'market_cap': market_cap
        }
    except Exception as e:
        logging.debug(f"parse bond curve failed: {e}")
        return None

# -----------------------
# Token Manager
# -----------------------
class TokenManager:
    def __init__(self, cfg: Config, db: Database, rpc: RPCClient, redis_client):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.alert_engine = AlertEngine(cfg)
        self.monitored: Dict[str, TokenData] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.alerted = set()
        self.semaphore = asyncio.Semaphore(self.cfg.MAX_CONCURRENT_MONITORS)
        self.redis = redis_client

    async def add_token(self, mint: str, symbol: str="UNKNOWN", name: str="UNKNOWN", bonding_curve: Optional[str]=None, initial_price: float=0.0, initial_market_cap: float=0.0):
        if mint in self.monitored:
            return
        token = TokenData(mint=mint, symbol=symbol, name=name,
                          initial_price=initial_price,
                          initial_market_cap=initial_market_cap,
                          max_price=initial_price,
                          start_time=datetime.now(timezone.utc),
                          bonding_curve=bonding_curve)
        self.monitored[mint] = token
        token.last_checked = datetime.now(timezone.utc)
        await self.db.upsert_token(token)
        self.tasks[mint] = asyncio.create_task(self._monitor(mint))
        logging.info(f"Started monitoring {symbol} ({mint[:8]}...)")

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

                    # price fetch: DexScreener first
                    price_data = None
                    # ensure rpc session open
                    if not self.rpc.session:
                        await self.rpc.__aenter__()
                    try:
                        price_data = await self._price_from_dexscreener(mint)
                    except Exception:
                        price_data = None

                    # fallback to on-chain bonding curve if available
                    if not price_data:
                        if token.bonding_curve:
                            try:
                                acc = await self.rpc.get_account_base64(token.bonding_curve)
                                if acc and acc.get('value') and acc['value'].get('data'):
                                    parsed = parse_bonding_curve_account_b64(acc['value']['data'][0])
                                    if parsed:
                                        price_data = {'price': parsed['price'], 'market_cap': parsed['market_cap'], 'source': 'bonding_curve', 'complete': parsed['complete']}
                            except Exception:
                                price_data = None
                        else:
                            # try computing bonding curve PDA heuristics (best-effort)
                            try:
                                # attempt to compute bonding curve PDA from mint (some heuristics)
                                # This is expensive: requires on-chain data exploration. We'll try a common PDA derivation:
                                # not implemented full scan here; skip for now
                                pass
                            except Exception:
                                pass

                    if not price_data:
                        await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
                        continue

                    current_price = float(price_data.get('price', 0.0))
                    current_mcap = float(price_data.get('market_cap', 0.0) or 0.0)

                    token.last_checked = datetime.now(timezone.utc)
                    if current_price > token.max_price:
                        token.max_price = current_price

                    # write to DB
                    await self.db.upsert_token(token)

                    # detect dump (from max)
                    if token.max_price > 0:
                        loss = ((current_price - token.max_price) / token.max_price) * 100.0
                        if loss <= self.cfg.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"Dump detected for {token.symbol} {mint}: {loss:.1f}% -> removing")
                            await self._remove(mint, "dumped")
                            return

                    # evaluate alerts
                    alerts = self.alert_engine.evaluate(token, current_price, elapsed_min)
                    for alert in alerts:
                        # dedupe via redis first
                        key = f"alert:{mint}"
                        was = await self.redis.get(key)
                        if was:
                            logging.debug(f"Alert dup suppressed for {mint}")
                            continue
                        # store TTL 60s to avoid duplicates
                        await self.redis.set(key, "1", ex=60)
                        # record in DB
                        await self.db.record_alert(alert)
                        # send notification
                        await Notification.send_alert(token, alert)
                        # mark in memory
                        self.alerted.add(mint)
                        # remove from monitoring after alert
                        await self._remove(mint, "alert_sent")
                        return

                    await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                logging.info(f"Task cancelled for {mint}")
            except Exception as e:
                logging.error(f"Error monitoring {mint}: {e}")
                await self._remove(mint, "error")

    async def _price_from_dexscreener(self, mint: str) -> Optional[Dict]:
        try:
            async with aiohttp.ClientSession() as s:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                async with s.get(url, timeout=6) as resp:
                    if resp.status == 200:
                        js = await resp.json()
                        if js.get('pairs'):
                            p = js['pairs'][0]
                            price = float(p.get('priceUsd', p.get('price', 0)))
                            mcap = float(p.get('marketCap', 0) or 0)
                            return {'price': price, 'market_cap': mcap, 'source': 'dexscreener'}
        except Exception as e:
            logging.debug(f"Dexscreener error {e}")
        return None

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
        t = self.tasks.get(mint)
        if t:
            t.cancel()
            del self.tasks[mint]
        logging.info(f"Removed {mint}: {reason}")

# -----------------------
# PumpPortal websocket listener (adds tokens to manager)
# -----------------------
class PumpPortalListener:
    def __init__(self, cfg: Config, manager: TokenManager):
        self.cfg = cfg
        self.manager = manager
        self.running = False
        self.ws = None
        self.reconnect = 5
        self.max_reconnect = 60

    async def connect(self):
        self.running = True
        uri = self.cfg.PUMPPORTAL_WSS
        while self.running:
            try:
                logging.info(f"Connecting to PumpPortal WSS: {uri}")
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                    self.ws = websocket
                    self.reconnect = 5
                    # best-effort subscribe
                    try:
                        await websocket.send(json.dumps({"method":"subscribeNewToken"}))
                    except Exception:
                        pass
                    async for raw in websocket:
                        try:
                            data = json.loads(raw)
                            await self._handle(data)
                        except json.JSONDecodeError:
                            logging.debug("Invalid JSON from WSS")
                        except Exception as e:
                            logging.error(f"WSS message handling failed: {e}")
            except Exception as e:
                logging.error(f"WSS connection error: {e}")
                await asyncio.sleep(self.reconnect)
                self.reconnect = min(self.reconnect * 2, self.max_reconnect)
            finally:
                self.ws = None

    async def _handle(self, payload: Dict):
        if 'data' in payload and isinstance(payload['data'], dict):
            p = payload['data']
        else:
            p = payload
        mint = p.get('mint') or p.get('token') or None
        symbol = p.get('symbol') or p.get('tokenSymbol') or 'UNKNOWN'
        name = p.get('name') or p.get('tokenName') or symbol
        bonding_curve = p.get('bondingCurve') or p.get('bonding_curve') or None
        initial_price = 0.0
        initial_mcap = 0.0
        if isinstance(p.get('pairs'), list) and p.get('pairs'):
            try:
                initial_price = float(p['pairs'][0].get('priceUsd', p['pairs'][0].get('price', 0)))
                initial_mcap = float(p['pairs'][0].get('marketCap', 0) or 0)
            except Exception:
                initial_price = 0.0
        if not mint:
            logging.debug("WSS message without mint")
            return
        await self.manager.add_token(mint=str(mint), symbol=symbol, name=name, bonding_curve=bonding_curve, initial_price=initial_price, initial_market_cap=initial_mcap)

    async def stop(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

# -----------------------
# FastAPI app / webhook handling (we parse incoming updates)
# -----------------------
def create_app(bot_instance):
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"status": "ok"}

    @app.post(bot_instance.config.TELEGRAM_WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        # Telegram will send updates here. We handle simple text commands: /start, /iniciar
        try:
            data = await request.json()
        except Exception:
            return Response(status_code=400)
        # simple parsing
        message = data.get("message") or data.get("edited_message") or {}
        if not message:
            return {"ok": True}
        chat = message.get("chat", {})
        text = message.get("text", "")
        from_user = message.get("from", {})
        # only accept from configured chat id if set
        allowed = True
        if bot_instance.config.TELEGRAM_CHAT_ID:
            allowed = str(chat.get("id")) == str(bot_instance.config.TELEGRAM_CHAT_ID)
        if not allowed:
            logging.debug("Telegram update from unauthorized chat; ignored")
            return {"ok": True}
        # handle commands
        if text and text.startswith("/"):
            if text.split()[0] == "/start":
                # send menu with buttons: Iniciar (start monitoring), Status, Tokens
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ Iniciar Monitor", callback_data="cmd:start_monitor")],
                    [InlineKeyboardButton("📊 Status", callback_data="cmd:status"), InlineKeyboardButton("🔍 Tokens", callback_data="cmd:tokens")]
                ])
                await bot_instance.bot.send_message(chat_id=chat.get("id"), text="🤖 *Pump.fun Bot*\nUse the buttons below.", parse_mode="Markdown", reply_markup=kb)
                return {"ok": True}
            if text.split()[0] == "/iniciar":
                # start the WSS monitoring
                if not bot_instance.portal_task:
                    bot_instance.portal_task = asyncio.create_task(bot_instance.portal_listener.connect())
                    await bot_instance.bot.send_message(chat_id=chat.get("id"), text="▶️ Monitoring started (PumpPortal).")
                else:
                    await bot_instance.bot.send_message(chat_id=chat.get("id"), text="⚠️ Already running.")
                return {"ok": True}

        # also handle callback_query style updates (Telegram sends them under 'callback_query')
        if "callback_query" in data:
            cq = data['callback_query']
            cd = cq.get("data", "")
            cid = cq['message']['chat']['id']
            if cd == "cmd:start_monitor":
                if not bot_instance.portal_task:
                    bot_instance.portal_task = asyncio.create_task(bot_instance.portal_listener.connect())
                    await bot_instance.bot.send_message(chat_id=cid, text="▶️ Monitoring started (PumpPortal).")
                else:
                    await bot_instance.bot.send_message(chat_id=cid, text="⚠️ Already running.")
                return {"ok": True}
            if cd == "cmd:status":
                text = f"Monitored tokens: {len(bot_instance.token_manager.monitored)}\nActive tasks: {len(bot_instance.token_manager.tasks)}\nWSS connected: {bot_instance.portal_listener.ws is not None}"
                await bot_instance.bot.send_message(chat_id=cid, text=text)
                return {"ok": True}
            if cd == "cmd:tokens":
                items = list(bot_instance.token_manager.monitored.keys())[:10]
                msg = "Tokens:\n" + "\n".join(items) if items else "No tokens"
                await bot_instance.bot.send_message(chat_id=cid, text=msg)
                return {"ok": True}

        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok", "monitored": len(bot_instance.token_manager.monitored)}

    @app.get("/metrics")
    async def metrics():
        return {"monitored_tokens": len(bot_instance.token_manager.monitored), "active_tasks": len(bot_instance.token_manager.tasks)}

    return app

# -----------------------
# Main orchestrator
# -----------------------
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
        # redis
        self.redis = None
        # manager and listener created after redis & db connections
        self.token_manager = None
        self.portal_listener = None
        self.portal_task = None
        self.fastapi_app = None
        self.http_server = None

    async def start(self):
        # connect DB
        await self.db.connect()

        # connect redis
        try:
            self.redis = aioredis.from_url(self.config.REDIS_URL, decode_responses=True)
            # simple test
            await self.redis.ping()
            logging.info("Redis connected")
        except Exception as e:
            logging.error(f"Redis connect failed: {e}")
            raise

        # init token manager and portal
        self.token_manager = TokenManager(self.config, self.db, self.rpc, self.redis)
        self.portal_listener = PumpPortalListener(self.config, self.token_manager)
        self.portal_task = None

        # set webhook for telegram to our domain (if token and domain provided)
        if self.bot and self.config.TELEGRAM_BOT_TOKEN:
            # In Railway, set TELEGRAM_WEBHOOK_URL env var to your domain + path
            webhook_url = os.getenv('DOMAIN_URL', None)
            if webhook_url:
                full = webhook_url.rstrip("/") + self.config.TELEGRAM_WEBHOOK_PATH
                try:
                    self.bot.set_webhook(url=full)
                    logging.info(f"✅ Telegram webhook set to {full}")
                except Exception as e:
                    logging.error(f"Failed to set webhook: {e}")

        # FastAPI app
        self.fastapi_app = create_app(self)
        # Start uvicorn server programmatically in background
        # But when running as single process, invoking uvicorn.run is fine at the end.
        logging.info("Service started (ready to accept webhook / start command)")

    async def stop(self):
        # cancel tasks
        if self.portal_task:
            try:
                self.portal_listener.running = False
                self.portal_task.cancel()
            except Exception:
                pass
        if self.db:
            await self.db.disconnect()
        if self.redis:
            try:
                await self.redis.close()
            except Exception:
                pass
        logging.info("Service stopped")

# -----------------------
# Entrypoint
# -----------------------
async def _main():
    svc = PumpFunService()
    await svc.start()
    # run uvicorn server (this will block)
    app = svc.fastapi_app
    port = svc.config.HEALTH_PORT
    # uvicorn programmatic run (blocking) - use config to host on 0.0.0.0
    uvicorn_config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(uvicorn_config)
    await server.serve()

def run_main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logging.info("Interrupted")

if __name__ == "__main__":
    run_main()
