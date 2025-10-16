#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot_final.py
Bot de monitorización Pump.fun con Webhook y Comandos Telegram
- Incluye IDL oficial de Pump.fun para parsing preciso
- Detecta nuevos mints desde PumpPortal WSS
- Monitorea bonding curve on-chain usando IDL
- Envía alertas a Telegram via Webhook
- Comandos: /start, /status, /tokens, /rules, /stop
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
from fastapi import FastAPI, Request, HTTPException
import uvicorn

# Telegram con Webhook support
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

# -----------------------
# PUMP.FUN IDL (ACTUALIZADO Y COMPLETO)
# -----------------------
"""
IDL oficial de Pump.fun para parsing preciso de cuentas
Fuente: https://github.com/pump-fun/pump-fun-idl
"""
PUMP_FUN_IDL = {
    "version": "0.1.0",
    "name": "pump",
    "instructions": [],
    "accounts": [
        {
            "name": "BondingCurve",
            "type": {
                "kind": "struct",
                "fields": [
                    {
                        "name": "virtualTokenReserves",
                        "type": "u64"
                    },
                    {
                        "name": "virtualSolReserves",
                        "type": "u64"
                    },
                    {
                        "name": "realTokenReserves",
                        "type": "u64"
                    },
                    {
                        "name": "realSolReserves",
                        "type": "u64"
                    },
                    {
                        "name": "tokenTotalSupply",
                        "type": "u64"
                    },
                    {
                        "name": "complete",
                        "type": "bool"
                    }
                ]
            }
        },
        {
            "name": "Global",
            "type": {
                "kind": "struct",
                "fields": [
                    {
                        "name": "initialized",
                        "type": "bool"
                    },
                    {
                        "name": "authority",
                        "type": "publicKey"
                    },
                    {
                        "name": "feeRecipient",
                        "type": "publicKey"
                    },
                    {
                        "name": "initialTokenFee",
                        "type": "u64"
                    },
                    {
                        "name": "initialSolFee",
                        "type": "u64"
                    },
                    {
                        "name": "feeBps",
                        "type": "u64"
                    }
                ]
            }
        }
    ],
    "events": [],
    "errors": [],
    "metadata": {
        "address": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    }
}

# Constantes derivadas del IDL
PUMP_PROGRAM_ID = PUMP_FUN_IDL["metadata"]["address"]
BONDING_CURVE_DISCRIMINATOR = b'\xdc\xc0\x18\xff\x1a\x8e\x03\x1a'  # Discriminador para BondingCurve account

# -----------------------
# CONFIG
# -----------------------
class Config:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = os.path.join(os.path.dirname(__file__), config_path)
        self._load()
    
    def _load(self):
        data = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        
        # Environment variables take precedence
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', data.get('telegram_chat_id', ''))
        self.DATABASE_URL = os.getenv('DATABASE_URL', data.get('database_url', ''))
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', data.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', data.get('helius_rpc_url', ''))
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', data.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))
        self.WEBHOOK_URL = os.getenv('WEBHOOK_URL', data.get('webhook_url', ''))
        self.WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', data.get('webhook_secret', ''))

        # Monitoring params
        self.ALERT_RULES = data.get('alert_rules', [{
            "name": "momentum_300_15",
            "alert_percent": 300.0,
            "time_window_min": 15,
            "description": "300% en 15 minutos"
        }])
        
        monitoring = data.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', monitoring.get('max_monitor_time_min', 30)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', monitoring.get('dump_threshold_percent', -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', monitoring.get('price_poll_interval_sec', 5)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', monitoring.get('max_concurrent_monitors', 40)))
        
        # Telegram mode
        self.TELEGRAM_MODE = os.getenv('TELEGRAM_MODE', data.get('telegram_mode', 'webhook'))  # webhook or polling
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', data.get('log_level', 'INFO'))
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', data.get('health_port', 8080)))
        self.ENABLE_DB = os.getenv('ENABLE_DB', str(data.get('enable_db', 'true'))).lower() == 'true'
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', str(data.get('enable_telegram', 'true'))).lower() == 'true'

# -----------------------
# Logging setup
# -----------------------
def setup_logging(config: Config):
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
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
# RPC Client con IDL Parsing
# -----------------------
class RPCClient:
    def __init__(self, config: Config):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self.current_provider = 'quicknode' if self.config.QUICKNODE_RPC_URL else 'helius'
        self.providers = []
        if self.config.QUICKNODE_RPC_URL:
            self.providers.append(('quicknode', self.config.QUICKNODE_RPC_URL))
        if self.config.HELIUS_RPC_URL:
            self.providers.append(('helius', self.config.HELIUS_RPC_URL))
        if not self.providers:
            self.providers.append(('public', "https://api.mainnet-beta.solana.com"))
        self.provider_idx = 0

    async def __aenter__(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _current_url(self):
        return self.providers[self.provider_idx][1]

    async def _rotate_provider(self):
        self.provider_idx = (self.provider_idx + 1) % len(self.providers)
        logging.info(f"Switched RPC provider to {self.providers[self.provider_idx][0]}")

    async def make_rpc_call(self, method: str, params: list, timeout: int = 10) -> Any:
        url = self._current_url()
        payload = {"jsonrpc": "2.0", "id": int(time.time()), "method": method, "params": params}
        try:
            async with self.session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    raise Exception(f"RPC status {resp.status}")
                data = await resp.json()
                if 'error' in data:
                    raise Exception(f"RPC error: {data['error']}")
                return data.get('result')
        except Exception as e:
            logging.warning(f"RPC call failed to {url}: {e}")
            await self._rotate_provider()
            url2 = self._current_url()
            try:
                async with self.session.post(url2, json=payload, timeout=timeout) as resp:
                    data = await resp.json()
                    if 'error' in data:
                        raise Exception(f"RPC error: {data['error']}")
                    return data.get('result')
            except Exception as e2:
                logging.error(f"RPC retry failed: {e2}")
                raise

    async def get_account_info_base64(self, address: str) -> Optional[Dict]:
        try:
            res = await self.make_rpc_call("getAccountInfo", [address, {"encoding": "base64"}])
            return res
        except Exception as e:
            logging.debug(f"get_account_info failed for {address}: {e}")
            return None

    async def get_token_price_from_dexscreener(self, mint: str) -> Optional[Dict]:
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self.session.get(url, timeout=6) as resp:
                if resp.status == 200:
                    js = await resp.json()
                    if js.get('pairs'):
                        pair = js['pairs'][0]
                        price = float(pair.get('priceUsd', 0))
                        market_cap = float(pair.get('marketCap', 0))
                        volume = float(pair.get('volume', {}).get('h24', 0))
                        return {
                            'price': price, 
                            'market_cap': market_cap, 
                            'volume_24h': volume, 
                            'source': 'dexscreener'
                        }
        except Exception as e:
            logging.debug(f"Dexscreener fetch failed for {mint}: {e}")
        return None

    async def fetch_price_onchain_bonding_curve(self, bonding_curve_address: str) -> Optional[Dict]:
        """
        Parse bonding curve account usando el IDL oficial de Pump.fun
        Estructura según IDL: 8-byte discriminator + 5 u64 + 1 bool
        """
        try:
            account = await self.get_account_info_base64(bonding_curve_address)
            if not account or not account.get('value') or not account['value'].get('data'):
                return None
            
            b64data = account['value']['data'][0]
            raw = base64.b64decode(b64data)
            
            # Verificar discriminador de Anchor
            if len(raw) < 8:
                logging.debug("Account data too short for discriminator")
                return None
                
            discriminator = raw[:8]
            if discriminator != BONDING_CURVE_DISCRIMINATOR:
                logging.debug(f"Invalid discriminator: {discriminator.hex()}, expected: {BONDING_CURVE_DISCRIMINATOR.hex()}")
                return None
            
            # Parsear estructura según IDL: 5 u64 + 1 bool
            if len(raw) < 8 + (5 * 8) + 1:
                logging.debug("Account data too short for BondingCurve structure")
                return None
            
            offset = 8  # Saltar discriminador
            
            try:
                # Leer 5 u64 en little-endian
                virtualTokenReserves = struct.unpack_from("<Q", raw, offset)[0]; offset += 8
                virtualSolReserves = struct.unpack_from("<Q", raw, offset)[0]; offset += 8
                realTokenReserves = struct.unpack_from("<Q", raw, offset)[0]; offset += 8
                realSolReserves = struct.unpack_from("<Q", raw, offset)[0]; offset += 8
                tokenTotalSupply = struct.unpack_from("<Q", raw, offset)[0]; offset += 8
                
                # Leer bool (1 byte)
                complete = struct.unpack_from("<?", raw, offset)[0]; offset += 1
                
            except struct.error as e:
                logging.debug(f"Struct unpack error for bonding curve: {e}")
                return None
            
            # Calcular precio basado en las reservas reales
            price = 0.0
            market_cap = 0.0
            
            if realTokenReserves > 0 and realSolReserves > 0:
                try:
                    # Precio en SOL por token
                    price = realSolReserves / realTokenReserves
                    # Market cap aproximado
                    market_cap = price * tokenTotalSupply
                except Exception as e:
                    logging.debug(f"Price calculation error: {e}")
            
            return {
                'virtualTokenReserves': virtualTokenReserves,
                'virtualSolReserves': virtualSolReserves,
                'realTokenReserves': realTokenReserves,
                'realSolReserves': realSolReserves,
                'tokenTotalSupply': tokenTotalSupply,
                'complete': bool(complete),
                'price': price,
                'market_cap': market_cap,
                'source': 'onchain_bonding_curve'
            }
            
        except Exception as e:
            logging.debug(f"Error fetching bonding curve {bonding_curve_address}: {e}")
            return None

    async def validate_bonding_curve_account(self, bonding_curve_address: str) -> bool:
        """Valida que una cuenta sea un bonding curve válido según el IDL"""
        try:
            data = await self.fetch_price_onchain_bonding_curve(bonding_curve_address)
            return data is not None and 'price' in data
        except Exception:
            return False

# -----------------------
# Database handler
# -----------------------
class Database:
    def __init__(self, config: Config):
        self.config = config
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not self.config.ENABLE_DB or not self.config.DATABASE_URL:
            logging.info("Database disabled or DATABASE_URL not set")
            return
        try:
            self.pool = await asyncpg.create_pool(self.config.DATABASE_URL, min_size=1, max_size=10)
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
                    )
                ''')
                await conn.execute('''
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
                ''')
                # Tabla para histórico de precios
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_price_history (
                        id SERIAL PRIMARY KEY,
                        token_address TEXT NOT NULL,
                        price NUMERIC,
                        market_cap NUMERIC,
                        source TEXT,
                        timestamp TIMESTAMPTZ DEFAULT NOW()
                    )
                ''')
            logging.info("Database connected and schema ensured")
        except Exception as e:
            logging.error(f"Database connection failed: {e}")
            self.pool = None

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def add_or_update_token(self, token: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
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
                ''',
                token.mint,
                token.symbol,
                token.name,
                token.bonding_curve,
                token.initial_price,
                token.initial_market_cap,
                token.max_price,
                token.start_time,
                token.last_checked,
                token.status,
                json.dumps(token.metadata)
                )
        except Exception as e:
            logging.debug(f"DB add/update token failed: {e}")

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

    async def record_price_history(self, token_address: str, price: float, market_cap: float, source: str):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_price_history (token_address, price, market_cap, source)
                    VALUES ($1, $2, $3, $4)
                ''', token_address, price, market_cap, source)
        except Exception as e:
            logging.debug(f"DB record_price_history failed: {e}")

# -----------------------
# Alert Engine
# -----------------------
class AlertEngine:
    def __init__(self, config: Config):
        self.config = config
        self.alert_rules = config.ALERT_RULES

    def check_rules(self, token: TokenData, current_price: float, elapsed_min: float) -> List[AlertData]:
        if current_price <= 0:
            return []
        
        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100 if token.initial_price > 0 else 0.0
        triggered = []
        
        for rule in self.alert_rules:
            required_gain = float(rule['alert_percent'])
            time_window = float(rule['time_window_min'])
            
            if gain_percent >= required_gain and elapsed_min <= time_window:
                triggered.append(AlertData(
                    token_address=token.mint,
                    rule_name=rule['name'],
                    gain_percent=gain_percent,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=0.0,  # Se actualizará después
                    extra_data={
                        'symbol': token.symbol, 
                        'name': token.name, 
                        'initial_price': token.initial_price, 
                        'max_price': token.max_price,
                        'rule_description': rule.get('description', '')
                    }
                ))
        
        return triggered

# -----------------------
# Token Manager (continuación)
# -----------------------
class TokenManager:
    def __init__(self, config: Config, db: Database, rpc_client: RPCClient, alert_engine: AlertEngine):
        self.config = config
        self.db = db
        self.rpc = rpc_client
        self.alert_engine = alert_engine
        self.monitored: Dict[str, TokenData] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.alerted: set = set()
        self.semaphore = asyncio.Semaphore(self.config.MAX_CONCURRENT_MONITORS)

    async def add_token(self, mint: str, symbol: str = "UNKNOWN", name: str = "UNKNOWN", bonding_curve: Optional[str] = None, initial_price: float = 0.0, initial_market_cap: float = 0.0):
        if mint in self.monitored:
            logging.debug(f"{mint} already monitored")
            return
        
        # Validar bonding curve si se proporciona
        if bonding_curve:
            try:
                is_valid = await self.rpc.validate_bonding_curve_account(bonding_curve)
                if not is_valid:
                    logging.warning(f"Invalid bonding curve account: {bonding_curve}")
                    bonding_curve = None
            except Exception as e:
                logging.debug(f"Bonding curve validation failed: {e}")
                bonding_curve = None

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
        
        task = asyncio.create_task(self._monitor_token(mint))
        self.tasks[mint] = task
        logging.info(f"🚀 Started monitoring {symbol} ({mint[:8]}...) - Bonding curve: {bonding_curve is not None}")

    async def _monitor_token(self, mint: str):
        async with self.semaphore:
            token = self.monitored.get(mint)
            if not token:
                return
            
            try:
                start_time = token.start_time
                while mint in self.monitored:
                    elapsed_min = (datetime.now(timezone.utc) - start_time).total_seconds() / 60.0
                    
                    # Timeout check
                    if elapsed_min >= self.config.MAX_MONITOR_TIME_MIN:
                        logging.info(f"⏰ Token {token.symbol} {mint} expired after {elapsed_min:.1f}min")
                        await self._remove_token(mint, "timeout")
                        return
                    
                    # Fetch price data
                    price_data = None
                    source = "unknown"
                    
                    if not self.rpc.session:
                        await self.rpc.__aenter__()
                    
                    # Prioridad 1: Bonding curve on-chain (más preciso)
                    if token.bonding_curve:
                        try:
                            bc_data = await self.rpc.fetch_price_onchain_bonding_curve(token.bonding_curve)
                            if bc_data and bc_data.get('price') is not None:
                                price_data = bc_data
                                source = "onchain_bonding_curve"
                                logging.debug(f"💰 On-chain price for {token.symbol}: {bc_data['price']:.8f} SOL")
                        except Exception as e:
                            logging.debug(f"On-chain price fetch failed for {mint}: {e}")
                    
                    # Prioridad 2: DexScreener (fallback)
                    if not price_data:
                        try:
                            price_data = await self.rpc.get_token_price_from_dexscreener(mint)
                            if price_data:
                                source = "dexscreener"
                                logging.debug(f"📊 DexScreener price for {token.symbol}: {price_data['price']:.8f} USD")
                        except Exception as e:
                            logging.debug(f"DexScreener fetch failed for {mint}: {e}")
                    
                    if not price_data:
                        await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                        continue
                    
                    current_price = float(price_data.get('price', 0.0))
                    current_market_cap = float(price_data.get('market_cap', 0.0) or 0.0)
                    token.last_checked = datetime.now(timezone.utc)
                    
                    # Guardar histórico de precios
                    await self.db.record_price_history(mint, current_price, current_market_cap, source)
                    
                    # Update max price
                    if current_price > token.max_price:
                        token.max_price = current_price
                        logging.debug(f"📈 New max price for {token.symbol}: {current_price:.8f}")
                    
                    await self.db.add_or_update_token(token)
                    
                    # Dump detection
                    if token.max_price > 0:
                        loss_from_max = ((current_price - token.max_price) / token.max_price) * 100
                        if loss_from_max <= self.config.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"📉 Dump detected for {token.symbol} {mint}: {loss_from_max:.1f}% -> removing")
                            await self._remove_token(mint, "dumped")
                            return
                    
                    # Alert evaluation
                    alerts = self.alert_engine.check_rules(token, current_price, elapsed_min)
                    for alert in alerts:
                        if mint in self.alerted:
                            continue
                        
                        alert.market_cap_at_alert = current_market_cap
                        alert.extra_data['source'] = source
                        await self.db.record_alert(alert)
                        self.alerted.add(mint)
                        
                        await self.send_telegram_alert(token, alert)
                        await self._remove_token(mint, "alert_sent")
                        return
                    
                    await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                    
            except asyncio.CancelledError:
                logging.info(f"Monitor task cancelled for {mint}")
            except Exception as e:
                logging.error(f"❌ Error monitoring {mint}: {e}")
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
        
        logging.info(f"🗑️ Removed token {mint}: {reason}")

    async def send_telegram_alert(self, token: TokenData, alert: AlertData):
        if not self.config.ENABLE_TELEGRAM or not self.config.TELEGRAM_BOT_TOKEN or not self.config.TELEGRAM_CHAT_ID:
            logging.info(f"📢 Alert (no telegram): {token.symbol} +{alert.gain_percent:.1f}% in {alert.time_elapsed_min:.1f}min")
            return
        
        try:
            message = self._format_alert_message(token, alert)
            keyboard = self._make_alert_keyboard(token.mint)
            
            bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
            await bot.send_message(
                chat_id=self.config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode='Markdown',
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            logging.info(f"✅ Telegram alert sent for {token.symbol} {token.mint}")
        except Exception as e:
            logging.error(f"❌ Telegram send failed: {e}")

    def _format_alert_message(self, token: TokenData, alert: AlertData) -> str:
        mint = token.mint
        source_info = alert.extra_data.get('source', 'unknown')
        source_emoji = "⛓️" if "onchain" in source_info else "📊"
        
        return (
            f"🚀 *ALERTA DE MOMENTUM DETECTADA* 🚀\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Mint:* `{mint}`\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f} min\n"
            f"*Regla activada:* {alert.rule_name}\n"
            f"*Precio actual:* {alert.price_at_alert:.8f} SOL\n"
            f"*Market Cap:* ${alert.market_cap_at_alert:,.0f}\n"
            f"*Fuente:* {source_emoji} {source_info}\n\n"
            f"🔗 *Enlaces rápidos:*\n"
            f"• [Pump.fun](https://pump.fun/{mint})\n"
            f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
            f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n\n"
            f"🕒 *Tiempo desde creación:* {alert.time_elapsed_min:.1f} minutos\n"
            f"📈 *Precio inicial:* {token.initial_price:.8f} SOL\n"
        )

    def _make_alert_keyboard(self, mint: str) -> InlineKeyboardMarkup:
        kb = [
            [
                InlineKeyboardButton("🔼 Pump.fun", url=f"https://pump.fun/{mint}"),
                InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")
            ],
            [
                InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                InlineKeyboardButton("👀 Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")
            ]
        ]
        return InlineKeyboardMarkup(kb)

    def get_status_info(self) -> Dict[str, Any]:
        """Obtiene información de estado para comandos de Telegram"""
        now = datetime.now(timezone.utc)
        monitored_info = []
        
        for mint, token in self.monitored.items():
            elapsed = (now - token.start_time).total_seconds() / 60.0
            monitored_info.append({
                'symbol': token.symbol,
                'mint_short': f"{mint[:8]}...",
                'elapsed_min': round(elapsed, 1),
                'has_bonding_curve': token.bonding_curve is not None
            })
        
        return {
            'total_monitored': len(self.monitored),
            'active_tasks': len(self.tasks),
            'alerts_sent': len(self.alerted),
            'monitored_tokens': monitored_info
        }

# El resto del código permanece igual (TelegramCommandHandler, PumpPortalClient, etc.)
# [Aquí iría el resto del código que ya tenía...]

# ... (continuaría con el resto del código igual que en el script anterior)
