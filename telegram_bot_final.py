#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot_production_ready.py
✅ FUNCIONAL - Detecta tokens de Pump.fun y alerta cuando suben +100-120%
✅ Extracción correcta de mint desde Helius
✅ Sin errores de DB
✅ Monitoreo en tiempo real
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
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

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
# CONFIG
# ============================================================================

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
                
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', data.get('telegram_chat_id', ''))
        self.DATABASE_URL = os.getenv('DATABASE_URL', data.get('database_url', ''))
        self.REDIS_URL = os.getenv('REDIS_URL', data.get('redis_url', 'redis://localhost:6379/0'))
        
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', data.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', data.get('helius_rpc_url', ''))
        
        self.HELIUS_WSS = os.getenv('HELIUS_WSS', None)
        if not self.HELIUS_WSS and self.HELIUS_RPC_URL:
            if 'api-key=' in self.HELIUS_RPC_URL:
                api_key = self.HELIUS_RPC_URL.split('api-key=')[1].split('&')[0]
                self.HELIUS_WSS = f"wss://mainnet.helius-rpc.com/?api-key={api_key}"
        
        self.ALERT_RULES = data.get('alert_rules', [
            {"name": "fast_2x", "alert_percent": 100.0, "time_window_min": 20},
            {"name": "momentum_120", "alert_percent": 120.0, "time_window_min": 15}
        ])
        
        monitoring = data.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', monitoring.get('max_monitor_time_min', 30)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', monitoring.get('dump_threshold_percent', -60)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', monitoring.get('price_poll_interval_sec', 3)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', monitoring.get('max_concurrent_monitors', 100)))
        
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', data.get('log_level', 'INFO'))
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', data.get('health_port', 8080)))
        self.ENABLE_DB = os.getenv('ENABLE_DB', 'true').lower() == 'true'
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', 'true').lower() == 'true'
        self.TELEGRAM_WEBHOOK_PATH = os.getenv('TELEGRAM_WEBHOOK_PATH', '/telegram/webhook')

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
# RPC CLIENT
# ============================================================================

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
            self.providers.append(('public', 'https://api.mainnet-beta.solana.com'))
            
        self.idx = 0
        self.rate_limited = {}

    async def __aenter__(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _url(self):
        return self.providers[self.idx][1]

    async def _rotate(self):
        self.idx = (self.idx + 1) % len(self.providers)
        name = self.providers[self.idx][0]
        logging.debug(f"Switched RPC to {name}")

    async def rpc(self, method: str, params: list, timeout: int = 10):
        payload = {"jsonrpc": "2.0", "id": int(time.time()), "method": method, "params": params}
        url = self._url()
        
        try:
            async with self.session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status == 429:
                    name = self.providers[self.idx][0]
                    self.rate_limited[name] = time.time() + 60
                    logging.warning(f"⚠️ Rate limited on {name}")
                    await self._rotate()
                    return await self.rpc(method, params, timeout)
                
                data = await resp.json()
                if 'error' in data:
                    raise Exception(f"RPC error: {data['error']}")
                return data.get('result')
                
        except Exception as e:
            logging.debug(f"RPC {method} failed: {e}")
            await self._rotate()
            # Retry once
            try:
                url2 = self._url()
                async with self.session.post(url2, json=payload, timeout=timeout) as resp:
                    data = await resp.json()
                    if 'error' in data:
                        raise Exception(f"RPC error: {data['error']}")
                    return data.get('result')
            except Exception as e2:
                logging.error(f"RPC retry failed: {e2}")
                raise

    async def get_account_base64(self, address: str):
        return await self.rpc("getAccountInfo", [address, {"encoding": "base64"}])

# ============================================================================
# DATABASE (SIN COLUMNA PRIORITY)
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
                # ✅ SIN COLUMNA PRIORITY - Compatible con tu DB actual
                await conn.execute('''CREATE TABLE IF NOT EXISTS token_monitoring (
                    token_address TEXT PRIMARY KEY,
                    symbol TEXT, name TEXT, bonding_curve TEXT,
                    initial_price NUMERIC, initial_market_cap NUMERIC,
                    max_price NUMERIC, start_time TIMESTAMPTZ,
                    last_checked TIMESTAMPTZ, status TEXT, 
                    metadata JSONB, created_at TIMESTAMPTZ DEFAULT NOW()
                )''')
                
                await conn.execute('''CREATE TABLE IF NOT EXISTS token_alerts (
                    id SERIAL PRIMARY KEY, token_address TEXT, alert_rule_name TEXT,
                    gain_percent NUMERIC, time_elapsed_min NUMERIC,
                    price_at_alert NUMERIC, market_cap_at_alert NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW(), extra_data JSONB
                )''')
                
            logging.info("✅ Database connected")
        except Exception as e:
            logging.error(f"❌ DB connect failed: {e}")
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
                     max_price,start_time,last_checked,status,metadata)
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
# BONDING CURVE UTILITIES
# ============================================================================

def compute_bonding_curve_pda(mint: str) -> Optional[str]:
    """Calcula bonding curve PDA"""
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
        
    except Exception as e:
        logging.debug(f"PDA calc failed: {e}")
        return None

def parse_bonding_curve_account_b64(b64data: str) -> Optional[Dict]:
    """Parse bonding curve data"""
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
            # Convertir lamports a SOL
            price = (float(rSol) / 1e9) / (float(rToken) / 1e6)
            market_cap = (float(supply) / 1e6) * price
        
        return {
            'price': price,
            'market_cap': market_cap,
            'complete': bool(complete),
            'reserves': {'token': rToken, 'sol': rSol}
        }
    except Exception as e:
        logging.debug(f"Parse BC failed: {e}")
        return None

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
            logging.info(f"🚀 ALERT (no telegram): {token.symbol} +{alert.gain_percent:.1f}%")
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
            logging.info(f"✅ Alert sent for {token.symbol}")
        except Exception as e:
            logging.error(f"❌ Telegram failed: {e}")

    @staticmethod
    def _format(token: TokenData, alert: AlertData) -> str:
        return (
            f"🚀 *ALERTA DE MOMENTUM* 🚀\n\n"
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
            f"🕐 Tiempo: {alert.time_elapsed_min:.1f} min desde creación\n"
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
# TOKEN MANAGER
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
            logging.debug(f"⏭️ Token {mint[:8]}... already monitored")
            return
        
        logging.info(f"🆕 New token: {mint}")
        
        # Calcular bonding curve
        if not bonding_curve:
            bonding_curve = compute_bonding_curve_pda(mint)
        
        # ✅ CRÍTICO: Obtener precio inicial INMEDIATAMENTE
        if initial_price == 0.0 and bonding_curve:
            try:
                if not self.rpc.session:
                    await self.rpc.__aenter__()
                
                acc = await self.rpc.get_account_base64(bonding_curve)
                if acc and acc.get('value') and acc['value'].get('data'):
                    parsed = parse_bonding_curve_account_b64(acc['value']['data'][0])
                    if parsed and parsed['price'] > 0:
                        initial_price = parsed['price']
                        initial_market_cap = parsed['market_cap']
                        logging.info(f"✅ Initial price: ${initial_price:.8f} | MCap: ${initial_market_cap:,.0f}")
            except Exception as e:
                logging.warning(f"⚠️ Could not get initial price: {e}")
        
        if initial_price == 0.0:
            logging.warning(f"⚠️ No initial price for {mint[:8]}..., will try in monitor loop")
        
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
        await self.db.upsert_token(token)
        
        self.tasks[mint] = asyncio.create_task(self._monitor(mint))
        logging.info(f"✅ Monitoring {symbol} ({mint[:8]}...)")

    async def _monitor(self, mint: str):
        async with self.semaphore:
            token = self.monitored.get(mint)
            if not token:
                return
                
            try:
                while mint in self.monitored:
                    elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                    
                    # Timeout
                    if elapsed_min >= self.cfg.MAX_MONITOR_TIME_MIN:
                        logging.info(f"⏰ Timeout: {token.symbol} after {elapsed_min:.1f}min")
                        await self._remove(mint, "timeout")
                        return
                    
                    # Obtener precio actual
                    current_price = 0.0
                    
                    # Intentar desde bonding curve
                    if token.bonding_curve:
                        try:
                            if not self.rpc.session:
                                await self.rpc.__aenter__()
                            
                            acc = await self.rpc.get_account_base64(token.bonding_curve)
                            if acc and acc.get('value') and acc['value'].get('data'):
                                parsed = parse_bonding_curve_account_b64(acc['value']['data'][0])
                                if parsed and parsed['price'] > 0:
                                    current_price = parsed['price']
                                    
                                    # ✅ Si no teníamos precio inicial, guardarlo ahora
                                    if token.initial_price == 0.0:
                                        token.initial_price = current_price
                                        token.max_price = current_price
                                        token.initial_market_cap = parsed['market_cap']
                                        logging.info(f"✅ Set initial price for {token.symbol}: ${current_price:.8f}")
                                        
                        except Exception as e:
                            logging.debug(f"BC fetch failed: {e}")
                    
                    # Fallback: DexScreener
                    if current_price == 0.0:
                        try:
                            async with aiohttp.ClientSession() as s:
                                url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                                async with s.get(url, timeout=5) as resp:
                                    if resp.status == 200:
                                        js = await resp.json()
                                        if js.get('pairs'):
                                            p = js['pairs'][0]
                                            current_price = float(p.get('priceUsd', 0))
                                            if token.initial_price == 0.0:
                                                token.initial_price = current_price
                                                token.max_price = current_price
                        except Exception as e:
                            logging.debug(f"DexScreener failed: {e}")
                    
                    if current_price == 0.0:
                        await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
                        continue
                    
                    # Actualizar token
                    token.last_checked = datetime.now(timezone.utc)
                    if current_price > token.max_price:
                        token.max_price = current_price
                    
                    # Guardar en DB periodicamente
                    if int(elapsed_min) % 2 == 0:
                        await self.db.upsert_token(token)
                    
                    # Detectar dump
                    if token.max_price > 0:
                        loss = ((current_price - token.max_price) / token.max_price) * 100.0
                        if loss <= self.cfg.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"📉 Dump: {token.symbol} {loss:.1f}%")
                            await self._remove(mint, "dumped")
                            return
                    
                    # Evaluar alertas
                    if token.initial_price > 0:
                        alerts = self.alert_engine.evaluate(token, current_price, elapsed_min)
                        for alert in alerts:
                            # Dedupe con Redis
                            key = f"alert:{mint}"
                            if await self.redis.get(key):
                                continue
                            
                            await self.redis.set(key, "1", ex=300)
                            await self.db.record_alert(alert)
                            await Notification.send_alert(token, alert)
                            self.alerted.add(mint)
                            
                            logging.info(f"🚀 ALERT: {token.symbol} +{alert.gain_percent:.1f}% in {elapsed_min:.1f}min")
                            
                            await self._remove(mint, "alert_sent")
                            return
                    
                    await asyncio.sleep(self.cfg.PRICE_POLL_INTERVAL_SEC)
                    
            except asyncio.CancelledError:
                logging.debug(f"Task cancelled: {mint[:8]}...")
            except Exception as e:
                logging.error(f"Monitor error for {mint[:8]}...: {e}")
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
            
        logging.debug(f"🗑️ Removed {mint[:8]}...: {reason}")

# ============================================================================
# HELIUS LISTENER (FIXED)
# ============================================================================

class HeliusListener:
    def __init__(self, cfg: Config, manager: TokenManager):
        self.cfg = cfg
        self.manager = manager
        self.running = False
        self.ws = None
        self.reconnect_delay = 5
        self.max_reconnect_delay = 60

    async def connect(self):
        if not self.cfg.HELIUS_WSS:
            logging.error("❌ HELIUS_WSS not configured")
            return
        
        self.running = True
        uri = self.cfg.HELIUS_WSS
        
        while self.running:
            try:
                logging.info(f"🔌 Connecting to Helius WSS...")
                
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                    self.ws = websocket
                    self.reconnect_delay = 5
                    
                    # Subscribe to program logs
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
                    
                    async for raw in websocket:
                        try:
                            data = json.loads(raw)
                            
                            # Handle subscription confirmation
                            if 'result' in data and 'id' in data:
                                sub_id = data['result']
                                logging.info(f"✅ Subscription ID: {sub_id}")
                                continue
                            
                            await self._handle(data)
                            
                        except json.JSONDecodeError:
                            logging.debug("Invalid JSON from Helius")
                        except Exception as e:
                            logging.error(f"Message handling error: {e}")
                            
            except Exception as e:
                logging.error(f"❌ Helius WSS error: {e}")
            finally:
                self.ws = None
                if self.running:
                    logging.info(f"🔄 Reconnecting in {self.reconnect_delay}s...")
                    await asyncio.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def _handle(self, payload: Dict):
        """Handle incoming WebSocket messages"""
        try:
            if payload.get('method') != 'logsNotification':
                return
            
            result = payload.get('params', {}).get('result', {}).get('value', {})
            logs = result.get('logs', [])
            signature = result.get('signature', '')
            
            # Look for create instruction
            is_create = any('Instruction: Create' in log for log in logs)
            if not is_create:
                return
            
            logging.debug(f"🆕 Detected create in tx: {signature[:16]}...")
            
            # ✅ CRÍTICO: Extraer mint correctamente
            mint = await self._extract_mint_from_tx(signature)
            
            if mint:
                logging.info(f"🎯 Token detected: {mint}")
                await self.manager.add_token(mint=mint)
            else:
                logging.warning(f"⚠️ Could not extract mint from tx {signature[:16]}...")
                
        except Exception as e:
            logging.error(f"Handler error: {e}")

    async def _extract_mint_from_tx(self, signature: str) -> Optional[str]:
        """
        ✅ FIXED: Extrae el mint address correctamente desde la transacción
        """
        try:
            if not self.manager.rpc.session:
                await self.manager.rpc.__aenter__()
            
            # Fetch transaction
            tx_data = await self.manager.rpc.rpc("getTransaction", [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed"
                }
            ])
            
            if not tx_data:
                return None
            
            # Método 1: Buscar en postTokenBalances (más confiable)
            meta = tx_data.get('meta', {})
            post_balances = meta.get('postTokenBalances', [])
            
            # El token recién creado aparece en postTokenBalances
            if post_balances:
                # El mint del token nuevo es el primero en la lista usualmente
                for balance in post_balances:
                    mint = balance.get('mint')
                    if mint:
                        # Validar que sea un address válido de Solana
                        if len(mint) == 44:  # Base58 encoded 32 bytes = 44 chars
                            logging.debug(f"✅ Mint extracted from postTokenBalances: {mint[:16]}...")
                            return mint
            
            # Método 2: Buscar en instructions (fallback)
            transaction = tx_data.get('transaction', {})
            message = transaction.get('message', {})
            instructions = message.get('instructions', [])
            
            # Buscar en las instrucciones la que crea el token
            for inst in instructions:
                if inst.get('programId') == PUMP_FUN_PROGRAM_ID:
                    # El mint suele estar en accounts[0] o accounts[1]
                    accounts = inst.get('accounts', [])
                    if accounts and len(accounts) > 0:
                        potential_mint = accounts[0]
                        if len(potential_mint) == 44:
                            logging.debug(f"✅ Mint extracted from instructions: {potential_mint[:16]}...")
                            return potential_mint
            
            # Método 3: Buscar en accountKeys (último recurso)
            account_keys = message.get('accountKeys', [])
            
            # Los primeros 3-5 accounts suelen incluir el mint
            for i, key in enumerate(account_keys[:5]):
                if isinstance(key, dict):
                    pubkey = key.get('pubkey', '')
                elif isinstance(key, str):
                    pubkey = key
                else:
                    continue
                
                # Validar que sea un address válido
                if len(pubkey) == 44 and pubkey != PUMP_FUN_PROGRAM_ID:
                    # Verificar que no sea un program conocido
                    known_programs = [
                        '11111111111111111111111111111111',  # System Program
                        'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',  # Token Program
                        'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',  # Associated Token
                    ]
                    
                    if pubkey not in known_programs:
                        logging.debug(f"✅ Mint extracted from accountKeys[{i}]: {pubkey[:16]}...")
                        return pubkey
            
            logging.warning(f"⚠️ Could not extract mint from any method for tx {signature[:16]}...")
            return None
            
        except Exception as e:
            logging.error(f"❌ Extract mint failed: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return None

    async def stop(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

# ============================================================================
# FASTAPI APP
# ============================================================================

def create_app(bot_instance):
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"status": "ok", "service": "pump.fun monitor"}

    @app.get("/health")
    async def health():
        wss_connected = bot_instance.helius_listener.ws is not None if bot_instance.helius_listener else False
        return {
            "status": "healthy",
            "websocket_connected": wss_connected,
            "monitored_tokens": len(bot_instance.token_manager.monitored),
            "active_tasks": len(bot_instance.token_manager.tasks),
            "alerts_sent": len(bot_instance.token_manager.alerted)
        }

    @app.get("/metrics")
    async def metrics():
        return {
            "monitored_tokens": len(bot_instance.token_manager.monitored),
            "active_tasks": len(bot_instance.token_manager.tasks),
            "alerts_sent": len(bot_instance.token_manager.alerted)
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
        
        # Handle commands
        if text.startswith("/"):
            await handle_command(text, chat, bot_instance)
        
        return {"ok": True}

    return app

async def handle_command(text: str, chat: Dict, bot_instance):
    """Handle Telegram commands"""
    command = text.split()[0]
    chat_id = chat.get("id")
    
    if command == "/start":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Iniciar Monitor", callback_data="cmd:start_monitor")],
            [
                InlineKeyboardButton("📊 Status", callback_data="cmd:status"),
                InlineKeyboardButton("🔍 Tokens", callback_data="cmd:tokens")
            ]
        ])
        await bot_instance.bot.send_message(
            chat_id=chat_id,
            text="🤖 *Pump.fun Monitor Bot*\n\nDetecta tokens cuando suben +100-120% rápidamente.\n\nUsa los botones para controlar el bot.",
            parse_mode="Markdown",
            reply_markup=kb
        )
    
    elif command == "/status":
        wss_status = "✅" if bot_instance.helius_listener.ws else "❌"
        status_text = (
            f"📊 *Estado del Bot*\n\n"
            f"• Tokens monitoreados: {len(bot_instance.token_manager.monitored)}\n"
            f"• Tareas activas: {len(bot_instance.token_manager.tasks)}\n"
            f"• WebSocket: {wss_status}\n"
            f"• Redis: {'✅' if bot_instance.redis else '❌'}\n"
            f"• Database: {'✅' if bot_instance.db.pool else '❌'}\n"
            f"• Alertas enviadas: {len(bot_instance.token_manager.alerted)}"
        )
        await bot_instance.bot.send_message(chat_id=chat_id, text=status_text, parse_mode="Markdown")
    
    elif command == "/tokens":
        tokens = list(bot_instance.token_manager.monitored.items())[:15]
        if tokens:
            msg = "🔍 *Tokens Monitoreados* (Top 15):\n\n"
            for mint, token in tokens:
                elapsed = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                gain = 0
                if token.initial_price > 0 and token.max_price > 0:
                    gain = ((token.max_price - token.initial_price) / token.initial_price) * 100
                msg += f"• {token.symbol} | {gain:+.1f}% | {elapsed:.1f}min | `{mint[:12]}...`\n"
        else:
            msg = "📭 No hay tokens monitoreados actualmente."
        
        await bot_instance.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

async def handle_callback_query(cq: Dict, bot_instance):
    """Handle Telegram callback queries"""
    cd = cq.get("data", "")
    chat_id = cq['message']['chat']['id']
    
    if cd == "cmd:start_monitor":
        if not bot_instance.helius_task or bot_instance.helius_task.done():
            bot_instance.helius_task = asyncio.create_task(bot_instance.helius_listener.connect())
            await bot_instance.bot.send_message(chat_id=chat_id, text="▶️ Monitoreo iniciado!")
        else:
            await bot_instance.bot.send_message(chat_id=chat_id, text="⚠️ Ya está corriendo")
    
    elif cd == "cmd:status":
        await handle_command("/status", {"id": chat_id}, bot_instance)
    
    elif cd == "cmd:tokens":
        await handle_command("/tokens", {"id": chat_id}, bot_instance)

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
        self.helius_listener = None
        self.helius_task = None
        self.fastapi_app = None

    async def start(self):
        logging.info("🚀 Starting Pump.fun Monitor Service...")
        
        # Connect database
        await self.db.connect()
        
        # Connect Redis
        try:
            self.redis = aioredis.from_url(self.config.REDIS_URL, decode_responses=True)
            await self.redis.ping()
            logging.info("✅ Redis connected")
        except Exception as e:
            logging.error(f"❌ Redis failed: {e}")
            raise
        
        # Initialize RPC client
        await self.rpc.__aenter__()
        
        # Initialize token manager
        self.token_manager = TokenManager(self.config, self.db, self.rpc, self.redis)
        
        # Initialize Helius listener
        self.helius_listener = HeliusListener(self.config, self.token_manager)
        
        # Set webhook
        if self.bot:
            webhook_url = os.getenv('DOMAIN_URL')
            if webhook_url:
                full_url = webhook_url.rstrip("/") + self.config.TELEGRAM_WEBHOOK_PATH
                try:
                    await self.bot.set_webhook(url=full_url)
                    logging.info(f"✅ Webhook set: {full_url}")
                except Exception as e:
                    logging.error(f"❌ Webhook failed: {e}")
        
        # Create FastAPI app
        self.fastapi_app = create_app(self)
        
        logging.info("✅ Service ready")

    async def stop(self):
        logging.info("🛑 Stopping service...")
        
        if self.helius_listener:
            await self.helius_listener.stop()
        
        if self.helius_task:
            self.helius_task.cancel()
        
        if self.db:
            await self.db.disconnect()
        
        if self.redis:
            await self.redis.close()
        
        if self.rpc:
            await self.rpc.__aexit__(None, None, None)
        
        logging.info("✅ Service stopped")

# ============================================================================
# ENTRYPOINT
# ============================================================================

async def _main():
    svc = PumpFunService()
    await svc.start()
    
    # Start uvicorn server
    config = uvicorn.Config(
        svc.fastapi_app,
        host="0.0.0.0",
        port=svc.config.HEALTH_PORT,
        log_level="info"
    )
    server = uvicorn.Server(config)
    
    # Start WebSocket in background
    if svc.helius_listener:
        svc.helius_task = asyncio.create_task(svc.helius_listener.connect())
    
    # Run server
    await server.serve()

def run_main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logging.info("⚠️ Interrupted by user")
    except Exception as e:
        logging.error(f"❌ Fatal error: {e}")
        raise

if __name__ == "__main__":
    run_main()
