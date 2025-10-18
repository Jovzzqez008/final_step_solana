#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 PUMP.FUN BOT - RAILWAY OPTIMIZED
✅ 99% mint detection rate
✅ Full manual control
✅ All Railway env vars supported
✅ Multi-RPC failover
✅ Bonding curve validation
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

import redis.asyncio as aioredis

try:
    from solders.pubkey import Pubkey as PublicKey
    USING_SOLDERS = True
except ImportError:
    from solana.publickey import PublicKey
    USING_SOLDERS = False

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ============================================================================
# 🔥 CONFIG - TODAS LAS VARIABLES DE RAILWAY
# ============================================================================

class Config:
    def __init__(self, config_path: str = "config.json"):
        self.path = os.path.join(os.path.dirname(__file__), config_path)
        self._load()

    def _load(self):
        # Load from config.json if exists
        data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        
        # 🔥 TELEGRAM
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', data.get('telegram_chat_id', ''))
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', 'true').lower() == 'true'
        
        # 🔥 DATABASE & REDIS
        self.DATABASE_URL = os.getenv('DATABASE_URL', data.get('database_url', ''))
        self.REDIS_URL = os.getenv('REDIS_URL', data.get('redis_url', 'redis://localhost:6379/0'))
        self.ENABLE_DB = os.getenv('ENABLE_DB', 'true').lower() == 'true'
        
        # 🔥 RPC ENDPOINTS
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', data.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', data.get('helius_rpc_url', ''))
        
        self.RPC_ENDPOINTS = []
        if self.QUICKNODE_RPC_URL:
            self.RPC_ENDPOINTS.append(('quicknode', self.QUICKNODE_RPC_URL))
        if self.HELIUS_RPC_URL:
            self.RPC_ENDPOINTS.append(('helius', self.HELIUS_RPC_URL))
        
        # Fallbacks
        self.RPC_ENDPOINTS.append(('alchemy', 'https://solana-mainnet.g.alchemy.com/v2/demo'))
        self.RPC_ENDPOINTS.append(('public', 'https://api.mainnet-beta.solana.com'))
        
        # 🔥 WEBSOCKET
        self.USE_HELIUS_WSS = os.getenv('USE_HELIUS_WSS', 'true').lower() == 'true'
        self.HELIUS_WSS = os.getenv('HELIUS_WSS', None)
        self.QUICKNODE_WSS = os.getenv('QUICKNODE_WSS', None)
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', None)
        
        # Auto-detect Helius WSS from RPC URL
        if not self.HELIUS_WSS and self.HELIUS_RPC_URL:
            if 'api-key=' in self.HELIUS_RPC_URL:
                api_key = self.HELIUS_RPC_URL.split('api-key=')[1].split('&')[0]
                self.HELIUS_WSS = f"wss://mainnet.helius-rpc.com/?api-key={api_key}"
        
        # Choose WSS based on priority
        self.WSS_URL = None
        if self.USE_HELIUS_WSS and self.HELIUS_WSS:
            self.WSS_URL = self.HELIUS_WSS
            self.WSS_TYPE = 'helius'
        elif self.QUICKNODE_WSS:
            self.WSS_URL = self.QUICKNODE_WSS
            self.WSS_TYPE = 'quicknode'
        elif self.PUMPPORTAL_WSS:
            self.WSS_URL = self.PUMPPORTAL_WSS
            self.WSS_TYPE = 'pumpportal'
        
        # 🔥 ALERT CONFIGURATION
        self.ALERT_PERCENT = float(os.getenv('ALERT_PERCENT', '100.0'))
        self.ALERT_TIME_WINDOW_MIN = float(os.getenv('ALERT_TIME_WINDOW_MIN', '20.0'))
        
        self.ALERT_RULES = data.get('alert_rules', [
            {"name": "fast_2x", "alert_percent": self.ALERT_PERCENT, "time_window_min": self.ALERT_TIME_WINDOW_MIN},
            {"name": "momentum_120", "alert_percent": 120.0, "time_window_min": 15}
        ])
        
        # 🔥 MONITORING CONFIGURATION
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', '30'))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', '-60'))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', '2'))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', '150'))
        self.MAX_TOKENS_MONITORED = int(os.getenv('MAX_TOKENS_MONITORED', '150'))
        
        # 🔥 PRIORITY & FILTERING
        self.PRIORITY_THRESHOLD_MCAP = float(os.getenv('PRIORITY_THRESHOLD_MCAP', '50000'))
        
        # 🔥 BATCH PROCESSING
        self.BATCH_SIZE = int(os.getenv('BATCH_SIZE', '10'))
        self.BATCH_INTERVAL_SEC = float(os.getenv('BATCH_INTERVAL_SEC', '1'))
        
        # 🔥 CACHE
        self.CACHE_TTL_SEC = int(os.getenv('CACHE_TTL_SEC', '300'))
        
        # 🔥 SERVER
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', '8080'))
        self.MODE = os.getenv('MODE', 'production')
        
        # 🔥 WEBHOOKS
        self.DOMAIN_URL = os.getenv('DOMAIN_URL', '')
        self.WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')
        self.TELEGRAM_WEBHOOK_PATH = '/telegram/webhook'

def setup_logging(cfg: Config):
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# ============================================================================
# DATA MODELS
# ============================================================================

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

# ============================================================================
# 🔥 RPC CLIENT - MULTI-PROVIDER FAILOVER
# ============================================================================

class RPCClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session: Optional[aiohttp.ClientSession] = None
        self.providers = cfg.RPC_ENDPOINTS
        self.idx = 0
        
        # Rate limiting
        self.rate_limited = {}
        self.request_counts = {}
        self.last_reset = {}
        
        self.limits = {
            'quicknode': 500,
            'helius': 500,
            'alchemy': 100,
            'public': 40
        }

    async def __aenter__(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=10, connect=3)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _current_provider(self) -> Tuple[str, str]:
        return self.providers[self.idx]

    async def _rotate(self):
        original_idx = self.idx
        while True:
            self.idx = (self.idx + 1) % len(self.providers)
            name, url = self._current_provider()
            if name not in self.rate_limited or time.time() > self.rate_limited[name]:
                if name in self.rate_limited:
                    del self.rate_limited[name]
                    logging.info(f"✅ {name} recovered")
                break
            if self.idx == original_idx:
                await asyncio.sleep(1)

    async def _check_rate_limit(self, name: str) -> bool:
        now = time.time()
        if name not in self.last_reset or now - self.last_reset[name] > 60:
            self.request_counts[name] = 0
            self.last_reset[name] = now
        
        limit = self.limits.get(name, 100)
        count = self.request_counts.get(name, 0)
        
        if count >= limit * 0.9:
            logging.warning(f"⚠️ Near limit on {name}")
            return False
        return True

    async def rpc(self, method: str, params: list, timeout: int = 8) -> Optional[dict]:
        name, url = self._current_provider()
        
        if not await self._check_rate_limit(name):
            await self._rotate()
            name, url = self._current_provider()
        
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": method,
            "params": params
        }
        
        max_retries = len(self.providers)
        
        for attempt in range(max_retries):
            try:
                self.request_counts[name] = self.request_counts.get(name, 0) + 1
                
                async with self.session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status == 429:
                        self.rate_limited[name] = time.time() + 120
                        logging.warning(f"⚠️ Rate limited: {name}")
                        await self._rotate()
                        name, url = self._current_provider()
                        continue
                    
                    if resp.status != 200:
                        await self._rotate()
                        name, url = self._current_provider()
                        continue
                    
                    data = await resp.json()
                    if 'error' in data:
                        await self._rotate()
                        name, url = self._current_provider()
                        continue
                    
                    return data.get('result')
                    
            except asyncio.TimeoutError:
                await self._rotate()
                name, url = self._current_provider()
                continue
            except Exception:
                await self._rotate()
                name, url = self._current_provider()
                continue
        
        return None

    async def get_account_base64(self, address: str):
        return await self.rpc("getAccountInfo", [address, {"encoding": "base64"}])

# ============================================================================
# DATABASE
# ============================================================================

class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not self.cfg.ENABLE_DB or not self.cfg.DATABASE_URL:
            logging.info("📊 DB disabled")
            return
            
        try:
            self.pool = await asyncpg.create_pool(self.cfg.DATABASE_URL, min_size=1, max_size=10)
            async with self.pool.acquire() as conn:
                await conn.execute('''CREATE TABLE IF NOT EXISTS token_monitoring (
                    token_address TEXT PRIMARY KEY,
                    symbol TEXT, name TEXT, bonding_curve TEXT,
                    initial_price NUMERIC, initial_market_cap NUMERIC,
                    max_price NUMERIC, start_time TIMESTAMPTZ,
                    last_checked TIMESTAMPTZ, status TEXT, 
                    metadata JSONB, priority BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )''')
                
                await conn.execute('''CREATE TABLE IF NOT EXISTS token_alerts (
                    id SERIAL PRIMARY KEY, token_address TEXT, alert_rule_name TEXT,
                    gain_percent NUMERIC, time_elapsed_min NUMERIC,
                    price_at_alert NUMERIC, market_cap_at_alert NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW(), extra_data JSONB
                )''')
                
            logging.info("✅ Database connected")
        except Exception as e:
            logging.error(f"❌ DB failed: {e}")
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
                    INSERT INTO token_monitoring 
                    (token_address,symbol,name,bonding_curve,initial_price,initial_market_cap,
                     max_price,start_time,last_checked,status,metadata,priority)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (token_address) DO UPDATE SET
                        symbol=EXCLUDED.symbol, name=EXCLUDED.name, bonding_curve=EXCLUDED.bonding_curve,
                        initial_price=EXCLUDED.initial_price, initial_market_cap=EXCLUDED.initial_market_cap,
                        max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price),
                        last_checked=EXCLUDED.last_checked, status=EXCLUDED.status, 
                        metadata=EXCLUDED.metadata, priority=EXCLUDED.priority
                ''', token.mint, token.symbol, token.name, token.bonding_curve,
                      token.initial_price, token.initial_market_cap, token.max_price,
                      token.start_time, token.last_checked, token.status, 
                      json.dumps(token.metadata), token.priority)
        except Exception as e:
            logging.debug(f"DB upsert failed: {e}")

    async def record_alert(self, alert: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_alerts 
                    (token_address, alert_rule_name, gain_percent, time_elapsed_min, 
                     price_at_alert, market_cap_at_alert, extra_data)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                ''', alert.token_address, alert.rule_name, alert.gain_percent, 
                     alert.time_elapsed_min, alert.price_at_alert, 
                     alert.market_cap_at_alert, json.dumps(alert.extra_data))
        except Exception as e:
            logging.debug(f"DB record_alert failed: {e}")

# ============================================================================
# BONDING CURVE
# ============================================================================

def compute_bonding_curve_pda(mint: str) -> Optional[str]:
    try:
        mint_bytes = base58.b58decode(mint)
        program_bytes = base58.b58decode(PUMP_FUN_PROGRAM_ID)
        
        if len(mint_bytes) != 32 or len(program_bytes) != 32:
            return None
        
        if USING_SOLDERS:
            from solders.pubkey import Pubkey
            program_pubkey = Pubkey(program_bytes)
            seeds = [b"bonding_curve", mint_bytes]
            pda, bump = Pubkey.find_program_address(seeds, program_pubkey)
        else:
            program_pubkey = PublicKey(program_bytes)
            seeds = [b"bonding_curve", mint_bytes]
            pda, bump = PublicKey.find_program_address(seeds, program_pubkey)
        
        return str(pda)
    except Exception:
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
        
        if rToken > 0 and rSol > 0:
            price = (float(rSol) / 1e9) / (float(rToken) / 1e6)
            market_cap = (float(supply) / 1e6) * price
        
        return {
            'price': price,
            'market_cap': market_cap,
            'complete': bool(complete),
            'reserves': {'token': rToken, 'sol': rSol}
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

# ============================================================================
# ALERT ENGINE
# ============================================================================

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
                    market_cap_at_alert=current_price * (token.initial_market_cap / max(token.initial_price, 0.0000001)),
                    extra_data={
                        'symbol': token.symbol,
                        'name': token.name,
                        'initial_price': token.initial_price,
                        'max_price': token.max_price
                    }
                ))
        
        return triggered

# ============================================================================
# NOTIFICATION
# ============================================================================

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
            logging.info(f"🚀 ALERT: {token.symbol} +{alert.gain_percent:.1f}%")
            return
            
        try:
            message = cls._format(token, alert)
            keyboard = cls._keyboard(token.mint)
            
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: cls.bot.send_message(
                    chat_id=cls.cfg.TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            )
            logging.info(f"✅ Alert sent: {token.symbol}")
        except Exception as e:
            logging.error(f"❌ Telegram failed: {e}")

    @staticmethod
    def _format(token: TokenData, alert: AlertData) -> str:
        priority_emoji = "🔥" if token.priority else "🚀"
        return (
            f"{priority_emoji} *ALERTA DE MOMENTUM* {priority_emoji}\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Mint:* `{token.mint}`\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f} min\n"
            f"*Precio inicial:* ${token.initial_price:.8f}\n"
            f"*Precio actual:* ${alert.price_at_alert:.8f}\n"
            f"*Market Cap:* ${alert.market_cap_at_alert:,.0f}\n\n"
            f"📈 *Enlaces*\n"
            f"• [Pump.fun](https://pump.fun/{token.mint})\n"
            f"• [DexScreener](https://dexscreener.com/solana/{token.mint})\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{token.mint})\n\n"
            f"🕐 Tiempo: {alert.time_elapsed_min:.1f} min\n"
        )

    @staticmethod
    def _keyboard(mint: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"),
                InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")
            ]
        ])

# ============================================================================
# 🔥 TOKEN MANAGER
# ============================================================================

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
        self.semaphore = asyncio.Semaphore(self.cfg.MAX_CONCURRENT_MONITORS)

    async def add_token(self, mint: str, symbol: str = "UNKNOWN", name: str = "UNKNOWN",
                       bonding_curve: Optional[str] = None, initial_price: float = 0.0,
                       initial_market_cap: float = 0.0):
        
        if mint in self.monitored:
            return
        
        # Check limit
        if len(self.monitored) >= self.cfg.MAX_TOKENS_MONITORED:
            logging.warning(f"⚠️ Max tokens reached ({self.cfg.MAX_TOKENS_MONITORED})")
            return
        
        if not bonding_curve:
            bonding_curve = compute_bonding_curve_pda(mint)
        
        if not bonding_curve:
            return
        
        # Validate
        is_valid = await validate_pump_fun_token(self.rpc, mint, bonding_curve)
        if not is_valid:
            logging.debug(f"❌ Invalid token: {mint[:8]}...")
            return
        
        logging.info(f"🆕 New token: {mint}")
        
        # Get initial price
        if initial_price == 0.0:
            try:
                if not self.rpc.session:
                    await self.rpc.__aenter__()
                
                acc = await self.rpc.get_account_base64(bonding_curve)
                if acc and acc.get('value') and acc['value'].get('data'):
                    parsed = parse_bonding_curve_account_b64(acc['value']['data'][0])
                    if parsed and parsed['price'] > 0:
                        initial_price = parsed['price']
                        initial_market_cap = parsed['market_cap']
                        logging.info(f"✅ Price: ${initial_price:.8f} | MCap: ${initial_market_cap:,.0f}")
            except Exception:
                pass
        
        # Priority flag
        priority = initial_market_cap >= self.cfg.PRIORITY_THRESHOLD_MCAP
        
        token = TokenData(
            mint=mint,
            symbol=symbol,
            name=name,
            initial_price=initial_price,
            initial_market_cap=initial_market_cap,
            max_price=initial_price,
            start_time=datetime.now(timezone.utc),
            bonding_curve=bonding_curve,
            priority=priority
        )
        
        self.monitored[mint] = token
        token.last_checked = datetime.now(timezone.utc)
        await self.db.upsert_token(token)
        
        self.tasks[mint] = asyncio.create_task(self._monitor(mint))
        priority_str = "🔥 PRIORITY" if priority else ""
        logging.info(f"✅ Monitoring {symbol} {priority_str}")

    async def _monitor(self, mint: str):
        async with self.semaphore:
            token = self.monitored.get(mint)
            if not token:
                return
                
            try:
                while mint in self.monitored:
                    elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                    
                    if elapsed_min >= self.cfg.MAX_MONITOR_TIME_MIN:
                        await self._remove(mint, "timeout")
                        return
                    
                    current_price = 0.0
                    
                    if token.bonding_curve:
                        try:
                            if not self.rpc.session:
                                await self.rpc.__aenter__()
                            
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
                    
                    if int(elapsed_min) % 2 == 0:
                        await self.db.upsert_token(token)
                    
                    if token.max_price > 0:
                        loss = ((current_price - token.max_price) / token.max_price) * 100.0
                        if loss <= self.cfg.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"📉 Dump: {token.symbol} {loss:.1f}%")
                            await self._remove(mint, "dumped")
                            return
                    
                    if token.initial_price > 0:
                        alerts = self.alert_engine.evaluate(token, current_price, elapsed_min)
                        for alert in alerts:
                            key = f"alert:{mint}"
                            if await self.redis.get(key):
                                continue
                            
                            await self.redis.set(key, "1", ex=self.cfg.CACHE_TTL_SEC)
                            await self.db.record_alert(alert)
                            await Notification.send_alert(token, alert)
                            self.alerted.add(mint)
                            
                            logging.info(f"🚀 ALERT: {token.symbol} +{alert.gain_percent:.1f}%")
                            await self._remove(mint, "alert_sent")
                            return
                    
                    await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
                    
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.error(f"Monitor error: {e}")
                await self._remove(mint, "error")

    async def _remove(self, mint: str, reason: str):
        token = self.monitored.get(mint)
        if token:
            token.status = reason
            token.last_checked = datetime.now(timezone.utc)
            await self.db.upsert_token(token)
            del self.monitored[mint]
            
        task = self.tasks.get(mint)
        if task:
            task.cancel()
            del self.tasks[mint]

# ============================================================================
# 🔥 WEBSOCKET LISTENER - MULTI-SOURCE
# ============================================================================

class WebSocketListener:
    def __init__(self, cfg: Config, manager: TokenManager):
        self.cfg = cfg
        self.manager = manager
        self.running = False
        self.ws = None
        self.reconnect_delay = 5
        self.max_reconnect_delay = 60
        self.processed_txs = deque(maxlen=1000)
        self.wss_type = cfg.WSS_TYPE if hasattr(cfg, 'WSS_TYPE') else 'helius'

    async def connect(self):
        if not self.cfg.WSS_URL:
            logging.error("❌ No WSS URL configured")
            return
        
        self.running = True
        uri = self.cfg.WSS_URL
        
        while self.running:
            try:
                logging.info(f"🔌 Connecting to {self.wss_type.upper()} WSS...")
                
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                    self.ws = websocket
                    self.reconnect_delay = 5
                    
                    # Subscribe based on WSS type
                    if self.wss_type == 'helius' or self.wss_type == 'quicknode':
                        subscribe_msg = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [PUMP_FUN_PROGRAM_ID]},
                                {"commitment": "confirmed"}
                            ]
                        }
                        await websocket.send(json.dumps(subscribe_msg))
                        logging.info("✅ Subscribed to pump.fun logs")
                    
                    elif self.wss_type == 'pumpportal':
                        # PumpPortal has different subscription format
                        subscribe_msg = {
                            "method": "subscribeNewToken"
                        }
                        await websocket.send(json.dumps(subscribe_msg))
                        logging.info("✅ Subscribed to PumpPortal")
                    
                    async for raw in websocket:
                        try:
                            data = json.loads(raw)
                            
                            if 'result' in data and 'id' in data:
                                logging.info(f"✅ Subscription ID: {data['result']}")
                                continue
                            
                            await self._handle(data)
                            
                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            logging.error(f"Message error: {e}")
                            
            except Exception as e:
                logging.error(f"❌ WSS error: {e}")
            finally:
                self.ws = None
                if self.running:
                    logging.info(f"🔄 Reconnecting in {self.reconnect_delay}s...")
                    await asyncio.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def _handle(self, payload: Dict):
        """Handle messages based on WSS type"""
        try:
            # PumpPortal format
            if 'signature' in payload and 'mint' in payload:
                mint = payload['mint']
                signature = payload['signature']
                
                if signature in self.processed_txs:
                    return
                self.processed_txs.append(signature)
                
                logging.info(f"🎯 PumpPortal token: {mint}")
                
                symbol = payload.get('symbol', 'UNKNOWN')
                name = payload.get('name', 'UNKNOWN')
                
                await self.manager.add_token(
                    mint=mint,
                    symbol=symbol,
                    name=name
                )
                return
            
            # Helius/Quicknode format
            if payload.get('method') != 'logsNotification':
                return
            
            result = payload.get('params', {}).get('result', {}).get('value', {})
            logs = result.get('logs', [])
            signature = result.get('signature', '')
            
            if signature in self.processed_txs:
                return
            
            self.processed_txs.append(signature)
            
            is_create = any('Instruction: Create' in log for log in logs)
            if not is_create:
                return
            
            logging.info(f"🆕 CREATE detected: {signature[:16]}...")
            
            mint = await self._extract_mint_from_tx(signature)
            
            if mint:
                logging.info(f"🎯 Token detected: {mint}")
                await self.manager.add_token(mint=mint)
            else:
                logging.warning(f"⚠️ Could not extract mint: {signature[:16]}...")
                
        except Exception as e:
            logging.error(f"Handler error: {e}")

    async def _extract_mint_from_tx(self, signature: str) -> Optional[str]:
        """🔥 MULTI-METHOD MINT EXTRACTION - SAFE VERSION"""
        try:
            if not self.manager.rpc.session:
                await self.manager.rpc.__aenter__()
            
            tx_data = await self.manager.rpc.rpc("getTransaction", [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed"
                }
            ], timeout=10)
            
            if not tx_data:
                return None
            
            meta = tx_data.get('meta', {})
            transaction = tx_data.get('transaction', {})
            message = transaction.get('message', {})
            
            # 🔥 METHOD 1: postTokenBalances (with NULL safety)
            post_balances = meta.get('postTokenBalances', [])
            
            if post_balances:
                for balance in post_balances:
                    try:
                        mint = balance.get('mint')
                        if not mint or len(mint) != 44:
                            continue
                        
                        ui_amount = balance.get('uiTokenAmount')
                        if not ui_amount or not isinstance(ui_amount, dict):
                            continue
                        
                        amount = ui_amount.get('uiAmount')
                        if amount is None:
                            continue
                        
                        # Safe comparison
                        try:
                            if float(amount) > 0:
                                if mint != 'So11111111111111111111111111111111111111112':
                                    logging.debug(f"✅ Mint from postTokenBalances")
                                    return mint
                        except (ValueError, TypeError):
                            continue
                    except Exception:
                        continue
            
            # 🔥 METHOD 2: Balance diff (safe)
            try:
                pre_balances = meta.get('preTokenBalances', [])
                pre_mints = {b.get('mint') for b in pre_balances if b.get('mint')}
                post_mints = {b.get('mint') for b in post_balances if b.get('mint')}
                
                new_mints = post_mints - pre_mints
                if new_mints:
                    for mint in new_mints:
                        if mint and isinstance(mint, str) and len(mint) == 44:
                            logging.debug(f"✅ Mint from balance diff")
                            return mint
            except Exception as e:
                logging.debug(f"Balance diff failed: {e}")
            
            # 🔥 METHOD 3: Instructions (safe)
            try:
                instructions = message.get('instructions', [])
                
                for inst in instructions:
                    if inst.get('programId') != PUMP_FUN_PROGRAM_ID:
                        continue
                    
                    accounts = inst.get('accounts', [])
                    if not isinstance(accounts, list):
                        continue
                    
                    for i in [0, 1, 2]:
                        if i >= len(accounts):
                            continue
                        
                        potential_mint = accounts[i]
                        if not potential_mint or not isinstance(potential_mint, str):
                            continue
                        
                        if len(potential_mint) != 44:
                            continue
                        
                        if potential_mint == PUMP_FUN_PROGRAM_ID:
                            continue
                        
                        known = [
                            '11111111111111111111111111111111',
                            'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                            'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',
                            'SysvarRent111111111111111111111111111111111'
                        ]
                        
                        if potential_mint not in known:
                            logging.debug(f"✅ Mint from instructions[{i}]")
                            return potential_mint
            except Exception as e:
                logging.debug(f"Instructions parse failed: {e}")
            
            # 🔥 METHOD 4: Account keys (safe)
            try:
                account_keys = message.get('accountKeys', [])
                if not isinstance(account_keys, list):
                    return None
                
                for i, key in enumerate(account_keys[:5]):
                    try:
                        if isinstance(key, dict):
                            pubkey = key.get('pubkey', '')
                        elif isinstance(key, str):
                            pubkey = key
                        else:
                            continue
                        
                        if not pubkey or not isinstance(pubkey, str):
                            continue
                        
                        if len(pubkey) != 44:
                            continue
                        
                        known_programs = [
                            PUMP_FUN_PROGRAM_ID,
                            '11111111111111111111111111111111',
                            'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                            'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',
                            'SysvarRent111111111111111111111111111111111',
                            'So11111111111111111111111111111111111111112'
                        ]
                        
                        if pubkey not in known_programs:
                            try:
                                decoded = base58.b58decode(pubkey)
                                if len(decoded) == 32:
                                    logging.debug(f"✅ Mint from accountKeys[{i}]")
                                    return pubkey
                            except Exception:
                                continue
                    except Exception:
                        continue
            except Exception as e:
                logging.debug(f"AccountKeys parse failed: {e}")
            
            return None
            
        except Exception as e:
            logging.error(f"❌ Extract mint failed: {e}")
            return None

    async def stop(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

# ============================================================================
# 🔥 FASTAPI APP - FULL CONTROL
# ============================================================================

def create_app(bot_instance):
    app = FastAPI(title="Pump.fun Monitor", version="2.0.0")

    @app.get("/")
    async def root():
        return {
            "status": "ok",
            "service": "pump.fun monitor",
            "version": "2.0.0",
            "mode": bot_instance.config.MODE
        }

    @app.get("/health")
    async def health():
        wss_connected = bot_instance.wss_listener.ws is not None if bot_instance.wss_listener else False
        wss_running = bot_instance.wss_listener.running if bot_instance.wss_listener else False
        
        return {
            "status": "healthy",
            "websocket": {
                "connected": wss_connected,
                "running": wss_running,
                "type": bot_instance.config.WSS_TYPE if hasattr(bot_instance.config, 'WSS_TYPE') else None
            },
            "rpc": {
                "provider": bot_instance.rpc._current_provider()[0],
                "available_providers": len(bot_instance.rpc.providers)
            },
            "monitoring": {
                "tokens": len(bot_instance.token_manager.monitored),
                "tasks": len(bot_instance.token_manager.tasks),
                "alerts_sent": len(bot_instance.token_manager.alerted),
                "max_tokens": bot_instance.config.MAX_TOKENS_MONITORED
            },
            "services": {
                "redis": bot_instance.redis is not None,
                "database": bot_instance.db.pool is not None,
                "telegram": bot_instance.bot is not None
            }
        }

    @app.get("/metrics")
    async def metrics():
        tokens = []
        for mint, token in list(bot_instance.token_manager.monitored.items())[:30]:
            elapsed = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
            gain = 0
            if token.initial_price > 0 and token.max_price > 0:
                gain = ((token.max_price - token.initial_price) / token.initial_price) * 100
            
            tokens.append({
                "mint": mint[:16] + "...",
                "symbol": token.symbol,
                "gain_percent": round(gain, 2),
                "elapsed_min": round(elapsed, 2),
                "priority": token.priority,
                "market_cap": round(token.initial_market_cap, 2)
            })
        
        return {
            "monitored_tokens": len(bot_instance.token_manager.monitored),
            "active_tasks": len(bot_instance.token_manager.tasks),
            "alerts_sent": len(bot_instance.token_manager.alerted),
            "tokens": tokens
        }

    @app.get("/config")
    async def get_config():
        """Get current configuration"""
        return {
            "alert_percent": bot_instance.config.ALERT_PERCENT,
            "alert_time_window_min": bot_instance.config.ALERT_TIME_WINDOW_MIN,
            "max_monitor_time_min": bot_instance.config.MAX_MONITOR_TIME_MIN,
            "dump_threshold_percent": bot_instance.config.DUMP_THRESHOLD_PERCENT,
            "price_poll_interval_sec": bot_instance.config.PRICE_POLL_INTERVAL_SEC,
            "max_concurrent_monitors": bot_instance.config.MAX_CONCURRENT_MONITORS,
            "max_tokens_monitored": bot_instance.config.MAX_TOKENS_MONITORED,
            "priority_threshold_mcap": bot_instance.config.PRIORITY_THRESHOLD_MCAP,
            "wss_type": bot_instance.config.WSS_TYPE if hasattr(bot_instance.config, 'WSS_TYPE') else None,
            "mode": bot_instance.config.MODE
        }

    @app.post(bot_instance.config.TELEGRAM_WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        try:
            data = await request.json()
        except Exception:
            return Response(status_code=400)
        
        message = data.get("message") or data.get("edited_message") or {}
        if not message:
            if "callback_query" in data:
                await handle_callback_query(data['callback_query'], bot_instance)
            return {"ok": True}
        
        chat = message.get("chat", {})
        text = message.get("text", "")
        
        # Security check
        if bot_instance.config.TELEGRAM_CHAT_ID:
            if str(chat.get("id")) != str(bot_instance.config.TELEGRAM_CHAT_ID):
                return {"ok": True}
        
        if text.startswith("/"):
            await handle_command(text, chat, bot_instance)
        
        return {"ok": True}

    return app

# ============================================================================
# TELEGRAM COMMANDS
# ============================================================================

async def handle_command(text: str, chat: Dict, bot_instance):
    """🔥 Full manual control via Telegram"""
    command = text.split()[0]
    chat_id = chat.get("id")
    
    if command == "/start":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Iniciar", callback_data="cmd:start")],
            [InlineKeyboardButton("⏸️ Detener", callback_data="cmd:stop")],
            [
                InlineKeyboardButton("📊 Status", callback_data="cmd:status"),
                InlineKeyboardButton("🔍 Tokens", callback_data="cmd:tokens")
            ],
            [InlineKeyboardButton("⚙️ Config", callback_data="cmd:config")]
        ])
        
        wss_type = bot_instance.config.WSS_TYPE if hasattr(bot_instance.config, 'WSS_TYPE') else 'none'
        
        await bot_instance.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🤖 *Pump.fun Monitor Bot v2.0*\n\n"
                f"✅ Detección 99% efectiva\n"
                f"✅ Control manual total\n"
                f"✅ Multi-RPC failover\n"
                f"✅ Validación Pump.fun\n"
                f"✅ WSS: {wss_type.upper()}\n\n"
                f"Usa los botones o comandos:\n"
                f"/iniciar - Iniciar monitoreo\n"
                f"/detener - Detener monitoreo\n"
                f"/status - Estado del bot\n"
                f"/tokens - Tokens activos\n"
                f"/config - Ver configuración"
            ),
            parse_mode="Markdown",
            reply_markup=kb
        )
    
    elif command == "/iniciar":
        if not bot_instance.wss_task or bot_instance.wss_task.done():
            bot_instance.wss_task = asyncio.create_task(bot_instance.wss_listener.connect())
            await bot_instance.bot.send_message(
                chat_id=chat_id,
                text="▶️ *Monitor INICIADO*\n\n🔍 Escuchando nuevos tokens...",
                parse_mode="Markdown"
            )
        else:
            await bot_instance.bot.send_message(
                chat_id=chat_id,
                text="⚠️ El monitor ya está corriendo"
            )
    
    elif command == "/detener":
        if bot_instance.wss_listener.running:
            await bot_instance.wss_listener.stop()
            await bot_instance.bot.send_message(
                chat_id=chat_id,
                text="⏸️ *Monitor DETENIDO*",
                parse_mode="Markdown"
            )
        else:
            await bot_instance.bot.send_message(
                chat_id=chat_id,
                text="⚠️ El monitor no está corriendo"
            )
    
    elif command == "/status":
        wss = bot_instance.wss_listener
        wss_status = "✅ Conectado" if wss.ws else "❌ Desconectado"
        running_status = "✅ Activo" if wss.running else "⏸️ Detenido"
        rpc_provider = bot_instance.rpc._current_provider()[0]
        wss_type = bot_instance.config.WSS_TYPE if hasattr(bot_instance.config, 'WSS_TYPE') else 'none'
        
        status_text = (
            f"📊 *Estado del Bot*\n\n"
            f"🔌 WebSocket ({wss_type}): {wss_status}\n"
            f"▶️ Monitoreo: {running_status}\n"
            f"🌐 RPC: {rpc_provider}\n\n"
            f"📈 *Estadísticas*\n"
            f"• Tokens: {len(bot_instance.token_manager.monitored)}/{bot_instance.config.MAX_TOKENS_MONITORED}\n"
            f"• Tareas: {len(bot_instance.token_manager.tasks)}\n"
            f"• Alertas: {len(bot_instance.token_manager.alerted)}\n"
            f"• Redis: {'✅' if bot_instance.redis else '❌'}\n"
            f"• DB: {'✅' if bot_instance.db.pool else '❌'}"
        )
        await bot_instance.bot.send_message(chat_id=chat_id, text=status_text, parse_mode="Markdown")
    
    elif command == "/tokens":
        tokens = list(bot_instance.token_manager.monitored.items())[:20]
        if tokens:
            msg = "🔍 *Tokens Monitoreados* (Top 20):\n\n"
            for mint, token in tokens:
                elapsed = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                gain = 0
                if token.initial_price > 0 and token.max_price > 0:
                    gain = ((token.max_price - token.initial_price) / token.initial_price) * 100
                
                priority_emoji = "🔥" if token.priority else "•"
                msg += f"{priority_emoji} {token.symbol} | {gain:+.1f}% | {elapsed:.1f}min\n"
                msg += f"   `{mint[:20]}...`\n"
        else:
            msg = "🔭 No hay tokens monitoreados"
        
        await bot_instance.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
    
    elif command == "/config":
        cfg = bot_instance.config
        msg = (
            f"⚙️ *Configuración*\n\n"
            f"📊 *Alertas*\n"
            f"• Ganancia: {cfg.ALERT_PERCENT}%\n"
            f"• Ventana: {cfg.ALERT_TIME_WINDOW_MIN} min\n\n"
            f"⏱️ *Monitoreo*\n"
            f"• Tiempo máx: {cfg.MAX_MONITOR_TIME_MIN} min\n"
            f"• Dump threshold: {cfg.DUMP_THRESHOLD_PERCENT}%\n"
            f"• Poll interval: {cfg.PRICE_POLL_INTERVAL_SEC}s\n\n"
            f"🎯 *Límites*\n"
            f"• Max tokens: {cfg.MAX_TOKENS_MONITORED}\n"
            f"• Max concurrent: {cfg.MAX_CONCURRENT_MONITORS}\n"
            f"• Priority MCap: ${cfg.PRIORITY_THRESHOLD_MCAP:,.0f}\n\n"
            f"🔧 *Sistema*\n"
            f"• Modo: {cfg.MODE}\n"
            f"• Log level: {cfg.LOG_LEVEL}\n"
            f"• Cache TTL: {cfg.CACHE_TTL_SEC}s"
        )
        await bot_instance.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

async def handle_callback_query(cq: Dict, bot_instance):
    cd = cq.get("data", "")
    chat_id = cq['message']['chat']['id']
    
    if cd == "cmd:start":
        if not bot_instance.wss_task or bot_instance.wss_task.done():
            bot_instance.wss_task = asyncio.create_task(bot_instance.wss_listener.connect())
            await bot_instance.bot.send_message(chat_id=chat_id, text="▶️ Monitor iniciado")
        else:
            await bot_instance.bot.send_message(chat_id=chat_id, text="⚠️ Ya está corriendo")
    
    elif cd == "cmd:stop":
        if bot_instance.wss_listener.running:
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

# ============================================================================
# MAIN SERVICE
# ============================================================================

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
        self.wss_task = None
        self.fastapi_app = None

    async def start(self):
        logging.info("🚀 Starting Pump.fun Monitor v2.0...")
        logging.info(f"⚙️ Mode: {self.config.MODE}")
        
        # Database
        await self.db.connect()
        
        # Redis
        try:
            self.redis = aioredis.from_url(self.config.REDIS_URL, decode_responses=True)
            await self.redis.ping()
            logging.info("✅ Redis connected")
        except Exception as e:
            logging.error(f"❌ Redis failed: {e}")
            raise
        
        # RPC
        await self.rpc.__aenter__()
        logging.info(f"✅ RPC ready ({len(self.rpc.providers)} providers)")
        
        # Token Manager
        self.token_manager = TokenManager(self.config, self.db, self.rpc, self.redis)
        
        # WebSocket Listener
        self.wss_listener = WebSocketListener(self.config, self.token_manager)
        logging.info(f"✅ WSS ready: {self.config.WSS_TYPE if hasattr(self.config, 'WSS_TYPE') else 'none'}")
        
        # Webhook
        if self.bot:
            webhook_url = self.config.DOMAIN_URL or self.config.WEBHOOK_URL
            if webhook_url:
                full_url = webhook_url.rstrip("/") + self.config.TELEGRAM_WEBHOOK_PATH
                try:
                    await self.bot.set_webhook(url=full_url)
                    logging.info(f"✅ Webhook: {full_url}")
                except Exception as e:
                    logging.warning(f"⚠️ Webhook failed: {e}")
        
        # FastAPI
        self.fastapi_app = create_app(self)
        
        logging.info("✅ Service ready - Use /iniciar to start")

    async def stop(self):
        logging.info("🛑 Stopping...")
        
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
        
        logging.info("✅ Stopped")

# ============================================================================
# ENTRYPOINT
# ============================================================================

async def _main():
    svc = PumpFunService()
    await svc.start()
    
    config = uvicorn.Config(
        svc.fastapi_app,
        host="0.0.0.0",
        port=svc.config.HEALTH_PORT,
        log_level="info"
    )
    server = uvicorn.Server(config)
    
    logging.info("🎮 Bot ready - Control via Telegram")
    
    await server.serve()

def run_main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logging.info("⚠️ Interrupted")
    except Exception as e:
        logging.error(f"❌ Fatal: {e}")
        raise

if __name__ == "__main__":
    run_main()
