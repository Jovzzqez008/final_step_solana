#!/usr/bin/env python3
# coding: utf-8
"""
Pump.fun monitoring bot (no automatic buying) - Single-file deployment
Features:
 - Detect new mints via PumpPortal WSS
 - Decode bonding curve on-chain (IDL embedded + struct decode)
 - Redis dedupe + TTL
 - PostgreSQL optional storage
 - Telegram webhook handler (FastAPI)
 - Health/metrics endpoints
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
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

# Redis (async)
import redis.asyncio as aioredis

# Anchor + Solana
from anchorpy import Program, Idl
from solders.pubkey import Pubkey as PublicKey

# Telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot
# NOTE: we use low-level Bot for sending; incoming webhook we parse manually
# (python-telegram-bot Application with webhook dispatch is more complex to embed in single-file)

# -------------------------
# Embedded IDL (pump.fun)
# -------------------------
PUMP_FUN_IDL = {
  "version": "0.1.0",
  "name": "pump",
  "instructions": [
    # ... minimal/partial IDL; full IDL may be loaded from upstream if desired
  ],
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

# -------------------------
# Config loader
# -------------------------
class Config:
    def __init__(self, path: str = "config.json"):
        self._path = os.path.join(os.path.dirname(__file__), path)
        self._load()
    def _load(self):
        data = {}
        if os.path.exists(self._path):
            try:
                with open(self._path, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        env = os.environ
        self.TELEGRAM_BOT_TOKEN = env.get('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = env.get('TELEGRAM_CHAT_ID', data.get('telegram_chat_id', ''))
        self.DATABASE_URL = env.get('DATABASE_URL', data.get('database_url', ''))
        self.QUICKNODE_RPC_URL = env.get('QUICKNODE_RPC_URL', data.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = env.get('HELIUS_RPC_URL', data.get('helius_rpc_url', ''))
        self.PUMPPORTAL_WSS = env.get('PUMPPORTAL_WSS', data.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))
        self.REDIS_URL = env.get('REDIS_URL', '')
        self.DOMAIN_URL = env.get('DOMAIN_URL', '')
        self.MODE = env.get('MODE', data.get('mode', 'PROD'))
        monitoring = data.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(env.get('MAX_MONITOR_TIME_MIN', monitoring.get('max_monitor_time_min', 30)))
        self.DUMP_THRESHOLD_PERCENT = float(env.get('DUMP_THRESHOLD_PERCENT', monitoring.get('dump_threshold_percent', -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(env.get('PRICE_POLL_INTERVAL_SEC', monitoring.get('price_poll_interval_sec', 5)))
        self.MAX_CONCURRENT_MONITORS = int(env.get('MAX_CONCURRENT_MONITORS', monitoring.get('max_concurrent_monitors', 40)))
        self.ALERT_RULES = data.get('alert_rules', [
            {"name":"momentum_120_15","alert_percent":120.0,"time_window_min":15,"description":"120% en 15 minutos"}
        ])
        self.LOG_LEVEL = env.get('LOG_LEVEL', data.get('log_level', 'INFO'))
        self.HEALTH_PORT = int(env.get('HEALTH_PORT', data.get('health_port', 8080)))
        self.ENABLE_DB = env.get('ENABLE_DB', data.get('enable_db', 'true')).lower() == 'true'
        self.ENABLE_TELEGRAM = env.get('ENABLE_TELEGRAM', data.get('enable_telegram', 'true')).lower() == 'true'

# -------------------------
# Logging
# -------------------------
def setup_logging(cfg: Config):
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# -------------------------
# Data models
# -------------------------
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

# -------------------------
# RPC client (QuickNode/Helius fallback)
# -------------------------
class RPCClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session: Optional[aiohttp.ClientSession] = None
        self.providers = []
        if cfg.QUICKNODE_RPC_URL:
            self.providers.append(('quicknode', cfg.QUICKNODE_RPC_URL))
        if cfg.HELIUS_RPC_URL:
            self.providers.append(('helius', cfg.HELIUS_RPC_URL))
        if not self.providers:
            self.providers.append(('public', 'https://api.mainnet-beta.solana.com'))
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
        logging.info("Switched RPC provider to %s", self.providers[self.idx][0])

    async def call(self, method: str, params: list, timeout: int = 10) -> Any:
        url = self._url()
        payload = {"jsonrpc":"2.0","id":int(time.time()),"method":method,"params":params}
        try:
            async with self.session.post(url, json=payload, timeout=timeout) as resp:
                data = await resp.json()
                if resp.status != 200 or 'error' in data:
                    raise Exception(f"RPC {url} err {data.get('error')}")
                return data.get('result')
        except Exception as e:
            logging.warning("RPC call failed %s -> %s", url, e)
            await self._rotate()
            # single retry
            url2 = self._url()
            try:
                async with self.session.post(url2, json=payload, timeout=timeout) as resp:
                    data = await resp.json()
                    if resp.status != 200 or 'error' in data:
                        raise Exception(f"RPC retry err {data.get('error')}")
                    return data.get('result')
            except Exception as e2:
                logging.error("RPC retry failed: %s", e2)
                return None

    async def get_account_base64(self, address: str) -> Optional[Dict]:
        return await self.call("getAccountInfo", [address, {"encoding":"base64"}])

    async def decompose_transaction(self, sig: str) -> Optional[Dict]:
        return await self.call("getTransaction", [sig, {"encoding":"jsonParsed"}])

    async def close(self):
        if self.session:
            await self.session.close()

# -------------------------
# Bonding curve decoding (on-chain)
# -------------------------
def decode_bonding_curve_from_base64(b64data: str) -> Optional[Dict]:
    try:
        raw = base64.b64decode(b64data)
        # Anchor discriminator 8 bytes, followed by struct of 5 u64 + bool
        if len(raw) < 8 + (8*5 + 1):
            return None
        offset = 8
        virtualTokenReserves, virtualSolReserves, realTokenReserves, realSolReserves, tokenTotalSupply = struct.unpack_from("<QQQQQ", raw, offset)
        offset += 40
        complete = struct.unpack_from("<?", raw, offset)[0]
        price = 0.0
        if realTokenReserves and realSolReserves:
            price = float(realSolReserves) / float(realTokenReserves)
        market_cap = float(tokenTotalSupply) * price if tokenTotalSupply else 0.0
        return {
            "virtualTokenReserves": virtualTokenReserves,
            "virtualSolReserves": virtualSolReserves,
            "realTokenReserves": realTokenReserves,
            "realSolReserves": realSolReserves,
            "tokenTotalSupply": tokenTotalSupply,
            "complete": bool(complete),
            "price": price,
            "market_cap": market_cap
        }
    except Exception as e:
        logging.debug("decode bonding curve error: %s", e)
        return None

# -------------------------
# Database wrapper (optional)
# -------------------------
class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool: Optional[asyncpg.pool.Pool] = None
    async def connect(self):
        if not self.cfg.ENABLE_DB or not self.cfg.DATABASE_URL:
            logging.info("DB disabled or no DATABASE_URL")
            return
        try:
            self.pool = await asyncpg.create_pool(self.cfg.DATABASE_URL, min_size=1, max_size=10)
            async with self.pool.acquire() as conn:
                await conn.execute('''
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
                )''')
                await conn.execute('''
                CREATE TABLE IF NOT EXISTS token_alerts (
                    id SERIAL PRIMARY KEY,
                    token_address TEXT,
                    alert_rule_name TEXT,
                    gain_percent NUMERIC,
                    time_elapsed_min NUMERIC,
                    price_at_alert NUMERIC,
                    market_cap_at_alert NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    extra_data JSONB
                )''')
            logging.info("Database connected and tables OK")
        except Exception as e:
            logging.error("DB connect error: %s", e)
            self.pool = None
    async def close(self):
        if self.pool:
            await self.pool.close()
    async def upsert_token(self, token: TokenData):
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                INSERT INTO token_monitoring (token_address,symbol,name,bonding_curve,initial_price,initial_market_cap,max_price,start_time,last_checked,status,metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (token_address) DO UPDATE SET
                  symbol=EXCLUDED.symbol,
                  name=EXCLUDED.name,
                  bonding_curve=EXCLUDED.bonding_curve,
                  initial_price=EXCLUDED.initial_price,
                  initial_market_cap=EXCLUDED.initial_market_cap,
                  max_price=GREATEST(token_monitoring.max_price, EXCLUDED.max_price),
                  last_checked=EXCLUDED.last_checked,
                  status=EXCLUDED.status,
                  metadata=EXCLUDED.metadata
                ''', token.mint, token.symbol, token.name, token.bonding_curve, token.initial_price, token.initial_market_cap, token.max_price, token.start_time, token.last_checked, token.status, json.dumps(token.metadata))
        except Exception as e:
            logging.debug("DB upsert token error: %s", e)
    async def record_alert(self, alert: AlertData):
        if not self.pool: return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                INSERT INTO token_alerts (token_address,alert_rule_name,gain_percent,time_elapsed_min,price_at_alert,market_cap_at_alert,extra_data)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ''', alert.token_address, alert.rule_name, alert.gain_percent, alert.time_elapsed_min, alert.price_at_alert, alert.market_cap_at_alert, json.dumps(alert.extra_data))
        except Exception as e:
            logging.debug("DB record alert error: %s", e)

# -------------------------
# Alerts engine
# -------------------------
class AlertEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rules = cfg.ALERT_RULES
    def check(self, token: TokenData, current_price: float, elapsed_min: float) -> List[AlertData]:
        if token.initial_price <= 0:
            return []
        gain = ((current_price - token.initial_price) / token.initial_price) * 100.0
        triggered = []
        for rule in self.rules:
            if gain >= float(rule["alert_percent"]) and elapsed_min <= float(rule["time_window_min"]):
                triggered.append(AlertData(
                    token_address=token.mint,
                    rule_name=rule["name"],
                    gain_percent=gain,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=0.0,
                    extra_data={"symbol": token.symbol, "name": token.name, "initial_price": token.initial_price, "max_price": token.max_price}
                ))
        return triggered

# -------------------------
# Notification (Telegram webhook send)
# -------------------------
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
            logging.info("Alert (no telegram): %s +%.1f%%", token.mint, alert.gain_percent)
            return
        try:
            msg = cls.format_msg(token, alert)
            kb = cls.make_keyboard(token.mint)
            # Bot.send_message is sync; use aiohttp to call Telegram HTTP API using Bot.token if needed.
            # But python-telegram-bot's Bot has async send_message in newer releases. Use directly:
            await cls.bot.send_message(chat_id=cls.cfg.TELEGRAM_CHAT_ID, text=msg, parse_mode='Markdown', reply_markup=kb, disable_web_page_preview=True)
            logging.info("Telegram alert sent for %s", token.mint)
        except Exception as e:
            logging.error("Telegram send failed: %s", e)

    @staticmethod
    def format_msg(token: TokenData, alert: AlertData) -> str:
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
    def make_keyboard(mint: str) -> InlineKeyboardMarkup:
        kb = [
            [InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"),
             InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
             InlineKeyboardButton("Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")]
        ]
        return InlineKeyboardMarkup(kb)

# -------------------------
# Token manager (monitors tokens)
# -------------------------
class TokenManager:
    def __init__(self, cfg: Config, db: Database, rpc: RPCClient, redis_client):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.alert_engine = AlertEngine(cfg)
        self.monitored: Dict[str, TokenData] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.alerted = set()
        self.semaphore = asyncio.Semaphore(cfg.MAX_CONCURRENT_MONITORS)
        self.redis = redis_client

    async def add_token(self, mint: str, symbol="UNKNOWN", name="UNKNOWN", bonding_curve: Optional[str]=None, initial_price: float = 0.0, initial_mcap: float = 0.0):
        # Redis dedupe: if key exists, skip
        if self.redis:
            key = f"mint_dedupe:{mint}"
            existed = await self.redis.setnx(key, "1")
            if not existed:
                logging.debug("Mint %s already processed by redis dedupe", mint)
                return
            # set TTL
            await self.redis.expire(key, int(self.cfg.MAX_MONITOR_TIME_MIN*60 + 30))
        if mint in self.monitored:
            logging.debug("Mint already monitored in memory %s", mint)
            return
        token = TokenData(mint=mint, symbol=symbol, name=name, initial_price=initial_price, initial_market_cap=initial_mcap, max_price=initial_price, start_time=datetime.now(timezone.utc), bonding_curve=bonding_curve)
        self.monitored[mint] = token
        token.last_checked = datetime.now(timezone.utc)
        # DB upsert
        await self.db.upsert_token(token)
        task = asyncio.create_task(self._monitor_token(mint))
        self.tasks[mint] = task
        logging.info("Started monitoring %s (%s...)", symbol, mint[:8])

    async def _monitor_token(self, mint: str):
        async with self.semaphore:
            token = self.monitored.get(mint)
            if not token:
                return
            try:
                # ensure RPC session
                await self.rpc.__aenter__()
                start = token.start_time
                while mint in self.monitored:
                    elapsed_min = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
                    if elapsed_min >= self.cfg.MAX_MONITOR_TIME_MIN:
                        logging.info("Token %s timed out after %.1f min", token.symbol, elapsed_min)
                        await self._remove_token(mint, "timeout")
                        return
                    # Fetch price: try DexScreener
                    price_data = None
                    try:
                        # DexScreener
                        async with aiohttp.ClientSession() as s:
                            try:
                                async with s.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=6) as r:
                                    if r.status == 200:
                                        js = await r.json()
                                        if js.get('pairs'):
                                            p = js['pairs'][0]
                                            price_data = {'price': float(p.get('priceUsd', 0)), 'market_cap': float(p.get('marketCap', 0)), 'source':'dexscreener'}
                            except Exception:
                                price_data = None
                    except Exception:
                        price_data = None
                    # Fallback: on-chain bonding curve decode
                    if (not price_data) and token.bonding_curve:
                        acct = await self.rpc.get_account_base64(token.bonding_curve)
                        if acct and acct.get('value') and acct['value'].get('data'):
                            bc = decode_bonding_curve_from_base64(acct['value']['data'][0])
                            if bc:
                                price_data = {'price': bc['price'], 'market_cap': bc['market_cap'], 'source':'bonding_curve', 'complete': bc['complete']}
                    # Extra fallback: compute associated bonding curve PDA using bonding curve derivation
                    if (not price_data) and not token.bonding_curve:
                        # attempt to compute PDA heuristics if mint present
                        try:
                            program_pub = PublicKey.from_string(PUMP_FUN_IDL['metadata']['address'])
                        except Exception:
                            program_pub = None
                        try:
                            # compute PDA seeds per docs: [b"bonding", mint] or similar - this is heuristic, many repos implement exact seeds.
                            # We'll attempt a few common combinations; if none, skip.
                            if program_pub:
                                m_pub = PublicKey.from_string(mint)
                                # Seed attempt: ["bonding_curve", mint]
                                seeds = [b"bonding_curve", bytes(m_pub)]
                                # Use solders Pubkey.find_program_address? Using anchorpy would be more correct but we keep heuristic:
                                # We'll try getAccountInfo for several candidate PDAs not implemented here for brevity.
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
                    await self.db.upsert_token(token)
                    # detect dump
                    if token.max_price > 0:
                        loss_from_max = ((current_price - token.max_price) / token.max_price) * 100.0
                        if loss_from_max <= self.cfg.DUMP_THRESHOLD_PERCENT:
                            logging.info("Dump detected for %s %s: %.1f%%", token.symbol, mint, loss_from_max)
                            await self._remove_token(mint, "dumped")
                            return
                    # evaluate alert
                    alerts = self.alert_engine.check(token, current_price, elapsed_min)
                    for alert in alerts:
                        if mint in self.alerted:
                            continue
                        alert.market_cap_at_alert = current_mcap
                        await self.db.record_alert(alert)
                        self.alerted.add(mint)
                        # send Telegram
                        await Notification.send_alert(token, alert)
                        # remove token from monitoring after alert to avoid duplicates (configurable)
                        await self._remove_token(mint, "alert_sent")
                        return
                    await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                logging.info("Monitor cancelled %s", mint)
            except Exception as e:
                logging.error("Error monitor token %s: %s", mint, e)
                await self._remove_token(mint, "error")

    async def _remove_token(self, mint: str, reason: str):
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
        logging.info("Removed %s: %s", mint, reason)

# -------------------------
# PumpPortal client (WSS)
# -------------------------
class PumpPortalClient:
    def __init__(self, cfg: Config, token_manager: TokenManager):
        self.cfg = cfg
        self.tm = token_manager
        self.ws = None
        self.running = False
        self.reconnect = 5
        self.max_reconnect = 60

    async def connect(self):
        uri = self.cfg.PUMPPORTAL_WSS
        self.running = True
        while self.running:
            try:
                logging.info("Connecting to PumpPortal WSS: %s", uri)
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    self.ws = ws
                    # subscribe if needed
                    try:
                        await ws.send(json.dumps({"method":"subscribeNewToken"}))
                    except Exception:
                        pass
                    async for raw in ws:
                        try:
                            payload = json.loads(raw)
                            await self.handle(payload)
                        except Exception as e:
                            logging.debug("WSS payload error: %s", e)
            except Exception as e:
                logging.error("WSS error: %s", e)
                await asyncio.sleep(self.reconnect)
                self.reconnect = min(self.reconnect*2, self.max_reconnect)

    async def handle(self, data: Dict):
        try:
            payload = data.get('data') if isinstance(data.get('data'), dict) else data
            mint = payload.get('mint') or payload.get('token') or None
            symbol = payload.get('symbol') or payload.get('tokenSymbol') or 'UNKNOWN'
            name = payload.get('name') or payload.get('tokenName') or symbol
            bonding_curve = payload.get('bondingCurve') or payload.get('bonding_curve') or None
            initial_price = 0.0
            initial_mcap = 0.0
            if isinstance(payload.get('pairs'), list) and payload['pairs']:
                p = payload['pairs'][0]
                try:
                    initial_price = float(p.get('priceUsd', p.get('price', 0)))
                except:
                    initial_price = 0.0
                try:
                    initial_mcap = float(p.get('marketCap', 0))
                except:
                    initial_mcap = 0.0
            if not mint:
                logging.debug("WSS message without mint")
                return
            mint = str(mint)
            await self.tm.add_token(mint=mint, symbol=symbol, name=name, bonding_curve=bonding_curve, initial_price=initial_price, initial_mcap=initial_mcap)
        except Exception as e:
            logging.error("handle message error: %s", e)

    async def stop(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass

# -------------------------
# Main Bot Orchestrator
# -------------------------
class PumpFunBot:
    def __init__(self):
        self.cfg = Config()
        setup_logging(self.cfg)
        self.redis = None
        self.db = Database(self.cfg)
        self.rpc = RPCClient(self.cfg)
        self.token_manager = None
        self.portal_client = None
        self.fastapi = None
        self.http_task = None
        self.wss_task = None
        self.is_monitoring = False

    async def init_resources(self):
        # Redis
        if self.cfg.REDIS_URL:
            try:
                self.redis = aioredis.from_url(self.cfg.REDIS_URL, decode_responses=True)
                await self.redis.ping()
                logging.info("Connected to Redis")
            except Exception as e:
                logging.error("Redis connect failed: %s", e)
                self.redis = None
        # DB
        await self.db.connect()
        # Notification init
        Notification.init(self.cfg)
        # Token manager
        self.token_manager = TokenManager(self.cfg, self.db, self.rpc, self.redis)
        # Portal client
        self.portal_client = PumpPortalClient(self.cfg, self.token_manager)
        # FastAPI app
        self.fastapi = self.create_app()

    def create_app(self):
        app = FastAPI()
        @app.get("/health")
        async def health():
            return {"status":"ok","monitored": len(self.token_manager.monitored) if self.token_manager else 0, "wss": bool(self.portal_client and self.portal_client.ws)}
        @app.get("/metrics")
        async def metrics():
            return {"monitored_tokens": len(self.token_manager.monitored) if self.token_manager else 0, "active_tasks": len(self.token_manager.tasks) if self.token_manager else 0}
        @app.post("/telegram/webhook")
        async def telegram_webhook(request: Request, background: BackgroundTasks):
            body = await request.json()
            # Minimal command parser
            msg = body.get("message") or body.get("edited_message") or {}
            text = (msg.get("text") or "").strip()
            chat = msg.get("chat", {})
            chat_id = chat.get("id")
            # When user triggers /start show menu
            if text.startswith("/start"):
                keyboard = [
                    [{"text":"Iniciar monitor","callback_data":"iniciar"}],
                    [{"text":"Status","callback_data":"status"}]
                ]
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("Iniciar monitor", callback_data="iniciar")],
                                           [InlineKeyboardButton("Status", callback_data="status")]])
                # send menu
                if Notification.bot:
                    await Notification.bot.send_message(chat_id=chat_id, text="🤖 *Pump.fun Monitor*\nUsa /iniciar para comenzar el monitoreo", parse_mode="Markdown", reply_markup=kb)
                return JSONResponse({"ok": True})
            if text.startswith("/iniciar"):
                # start monitoring WSS if not started
                if not self.is_monitoring:
                    asyncio.create_task(self.start_monitoring())
                    self.is_monitoring = True
                    if Notification.bot:
                        await Notification.bot.send_message(chat_id=chat_id, text="▶️ Monitor iniciado", parse_mode="Markdown")
                else:
                    if Notification.bot:
                        await Notification.bot.send_message(chat_id=chat_id, text="ℹ️ El monitor ya está en ejecución", parse_mode="Markdown")
                return JSONResponse({"ok": True})
            if text.startswith("/status"):
                if Notification.bot:
                    await Notification.bot.send_message(chat_id=chat_id, text=f"Monitoreando {len(self.token_manager.monitored)} tokens", parse_mode="Markdown")
                return JSONResponse({"ok": True})
            # handle button callbacks (Telegram will send callback_query)
            if "callback_query" in body:
                cb = body["callback_query"]
                data = cb.get("data")
                from_id = cb.get("from", {}).get("id")
                if data == "iniciar":
                    if not self.is_monitoring:
                        asyncio.create_task(self.start_monitoring())
                        self.is_monitoring = True
                        if Notification.bot:
                            await Notification.bot.answer_callback_query(cb.get("id"), text="Monitor iniciado")
                    else:
                        if Notification.bot:
                            await Notification.bot.answer_callback_query(cb.get("id"), text="Monitor ya en ejecución")
                if data == "status":
                    if Notification.bot:
                        await Notification.bot.answer_callback_query(cb.get("id"), text=f"Monitoreando {len(self.token_manager.monitored)} tokens")
                return JSONResponse({"ok": True})
            return JSONResponse({"ok": True})
        return app

    async def start_monitoring(self):
        logging.info("Starting monitoring (PumpPortal WSS)...")
        # Ensure resources
        await self.init_resources()
        # Start portal client
        self.wss_task = asyncio.create_task(self.portal_client.connect())
        logging.info("Monitoring started")

    async def stop(self):
        logging.info("Stopping PumpFunBot...")
        try:
            await self.portal_client.stop()
        except:
            pass
        if self.wss_task:
            self.wss_task.cancel()
        await self.db.close()
        if self.redis:
            await self.redis.close()
        logging.info("Stopped")

# -------------------------
# Entrypoint
# -------------------------
def run():
    bot = PumpFunBot()
    # Setup signal handlers
    loop = asyncio.get_event_loop()
    def _sig(sig, frame):
        logging.info("Signal received, shutting down")
        asyncio.create_task(bot.stop())
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    # start uvicorn for FastAPI and setup webhook on Telegram (if domain provided)
    async def _serve():
        await bot.init_resources()
        app = bot.fastapi
        # Set webhook if domain provided and TELEGRAM_BOT_TOKEN exists
        if bot.cfg.DOMAIN_URL and bot.cfg.TELEGRAM_BOT_TOKEN:
            webhook_url = f"{bot.cfg.DOMAIN_URL.rstrip('/')}/telegram/webhook"
            try:
                Notification.init(bot.cfg)
                await Notification.bot.set_webhook(webhook_url)
                logging.info("✅ Telegram webhook set to %s", webhook_url)
            except Exception as e:
                logging.error("Failed to set Telegram webhook: %s", e)
        # Run uvicorn server in background
        config = uvicorn.Config(app, host="0.0.0.0", port=bot.cfg.HEALTH_PORT, log_level="info")
        server = uvicorn.Server(config)
        # Don't auto-start monitoring; wait for /iniciar
        await server.serve()
    try:
        loop.run_until_complete(_serve())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutdown requested")
    finally:
        try:
            loop.run_until_complete(bot.stop())
        except:
            pass

if __name__ == "__main__":
    run()
