#!/usr/bin/env python3
"""
Pump.fun Alert Bot v2.0
BlockSubscribe + Anchor IDL Decoding + Metrics + Telegram Commands
"""

import os
import json
import asyncio
import logging
import signal
import sys
import base64
import time
import random
import aiohttp
import asyncpg
import websockets
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass
from decimal import Decimal
from aiohttp import web
from collections import Counter

# Telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ---------------------------
# CONFIGURACIÓN
# ---------------------------

# Program ID de Pump.fun
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

class Config:
    def __init__(self):
        self.config_path = os.path.join(os.path.dirname(__file__), "config.json")
        self.data = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception as e:
                logging.warning(f"Error loading config.json: {e}")

        # RPC endpoints
        self.QUICKNODE_RPC_URL = os.getenv("QUICKNODE_RPC_URL", self.data.get("quicknode_rpc_url", ""))
        self.HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", self.data.get("helius_rpc_url", ""))
        
        # WebSocket endpoints
        self.QUICKNODE_WSS = os.getenv("QUICKNODE_WSS", self._http_to_ws(self.QUICKNODE_RPC_URL))
        self.HELIUS_WSS = os.getenv("HELIUS_WSS", self._http_to_ws(self.HELIUS_RPC_URL))
        self.PUMPPORTAL_WSS = os.getenv("PUMPPORTAL_WSS", "wss://pumpportal.fun/api/data")
        
        # Telegram
        self.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", self.data.get("telegram_bot_token", ""))
        self.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", self.data.get("telegram_chat_id", ""))
        
        # Database
        self.DATABASE_URL = os.getenv("DATABASE_URL", self.data.get("database_url", ""))
        
        # Alert Rules
        self.ALERT_RULES = self.data.get("alert_rules", [
            {"name": "momentum_300_15", "alert_percent": 300.0, "time_window_min": 15, "description": "300% en 15 minutos"}
        ])
        
        # Monitoring
        monitoring_config = self.data.get("monitoring", {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv("MAX_MONITOR_TIME_MIN", monitoring_config.get("max_monitor_time_min", 30)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv("DUMP_THRESHOLD_PERCENT", monitoring_config.get("dump_threshold_percent", -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv("PRICE_POLL_INTERVAL_SEC", monitoring_config.get("price_poll_interval_sec", 5)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv("MAX_CONCURRENT_MONITORS", monitoring_config.get("max_concurrent_monitors", 40)))
        
        # System
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", self.data.get("log_level", "INFO"))
        self.HEALTH_PORT = int(os.getenv("HEALTH_PORT", self.data.get("health_port", 8080)))
        self.ENABLE_DB = os.getenv("ENABLE_DB", "true").lower() == "true"
        self.ENABLE_TELEGRAM = os.getenv("ENABLE_TELEGRAM", "true").lower() == "true"

    def _http_to_ws(self, http_url: str) -> str:
        if http_url.startswith("https://"):
            return http_url.replace("https://", "wss://")
        elif http_url.startswith("http://"):
            return http_url.replace("http://", "ws://")
        return http_url

# ---------------------------
# IDL DE PUMP.FUN (Embedded)
# ---------------------------

# Estructura simplificada del Bonding Curve Account basada en el IDL oficial
BONDING_CURVE_ACCOUNT_LAYOUT = {
    "initialized": {"offset": 0, "type": "bool"},
    "token_mint": {"offset": 1, "type": "pubkey"},
    "token_supply": {"offset": 33, "type": "u64"},
    "virtual_token_reserves": {"offset": 41, "type": "u64"},
    "virtual_sol_reserves": {"offset": 49, "type": "u64"},
    "real_token_reserves": {"offset": 57, "type": "u64"},
    "real_sol_reserves": {"offset": 65, "type": "u64"},
    "token_total_supply": {"offset": 73, "type": "u64"},
    "complete": {"offset": 81, "type": "bool"},
    "token_account": {"offset": 82, "type": "pubkey"},
    "name": {"offset": 114, "type": "string"},
    "symbol": {"offset": None, "type": "string"},  # Se calcula dinámicamente
    "uri": {"offset": None, "type": "string"},     # Se calcula dinámicamente
    "bonding_curve": {"offset": None, "type": "pubkey"},
    "associated_bonding_curve": {"offset": None, "type": "pubkey"},
    "creator": {"offset": None, "type": "pubkey"},
    "created_at": {"offset": None, "type": "i64"}
}

# ---------------------------
# DECODIFICACIÓN BONDING CURVE
# ---------------------------

class BondingCurveDecoder:
    @staticmethod
    def decode_bool(data: bytes, offset: int) -> Tuple[bool, int]:
        return bool(data[offset]), offset + 1

    @staticmethod
    def decode_u64(data: bytes, offset: int) -> Tuple[int, int]:
        value = int.from_bytes(data[offset:offset+8], 'little')
        return value, offset + 8

    @staticmethod
    def decode_pubkey(data: bytes, offset: int) -> Tuple[str, int]:
        pubkey_bytes = data[offset:offset+32]
        pubkey = base64.b64encode(pubkey_bytes).decode('utf-8')
        return pubkey, offset + 32

    @staticmethod
    def decode_string(data: bytes, offset: int) -> Tuple[str, int]:
        length = int.from_bytes(data[offset:offset+4], 'little')
        offset += 4
        string_data = data[offset:offset+length].decode('utf-8', errors='ignore')
        return string_data, offset + length

    @staticmethod
    def parse_bonding_curve_account(data_b64: str) -> Optional[Dict[str, Any]]:
        try:
            data = base64.b64decode(data_b64)
            result = {}
            offset = 0

            # Campos fijos según el layout
            result['initialized'], offset = BondingCurveDecoder.decode_bool(data, offset)
            result['token_mint'], offset = BondingCurveDecoder.decode_pubkey(data, offset)
            result['token_supply'], offset = BondingCurveDecoder.decode_u64(data, offset)
            result['virtual_token_reserves'], offset = BondingCurveDecoder.decode_u64(data, offset)
            result['virtual_sol_reserves'], offset = BondingCurveDecoder.decode_u64(data, offset)
            result['real_token_reserves'], offset = BondingCurveDecoder.decode_u64(data, offset)
            result['real_sol_reserves'], offset = BondingCurveDecoder.decode_u64(data, offset)
            result['token_total_supply'], offset = BondingCurveDecoder.decode_u64(data, offset)
            result['complete'], offset = BondingCurveDecoder.decode_bool(data, offset)
            result['token_account'], offset = BondingCurveDecoder.decode_pubkey(data, offset)

            # Calcular precio
            if result['virtual_token_reserves'] > 0:
                price_sol = result['virtual_sol_reserves'] / result['virtual_token_reserves']
                # Convertir a USD aproximado (1 SOL = $150 como placeholder)
                price_usd = price_sol * 150
                market_cap = price_usd * (result['token_total_supply'] / 1_000_000)  # 6 decimales
                
                result['price_sol'] = price_sol
                result['price_usd'] = price_usd
                result['market_cap'] = market_cap
                result['progress_to_69k'] = min(100.0, (market_cap / 69000) * 100)

            return result
        except Exception as e:
            logging.error(f"Error decoding bonding curve: {e}")
            return None

# ---------------------------
# MODELOS DE DATOS
# ---------------------------

@dataclass
class TokenData:
    mint: str
    bonding_curve: Optional[str] = None
    associated_bonding_curve: Optional[str] = None
    symbol: str = "UNKNOWN"
    name: str = "UNKNOWN"
    initial_price: float = 0.0
    max_price: float = 0.0
    start_time: datetime = None
    status: str = "monitoring"
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.start_time is None:
            self.start_time = datetime.now(timezone.utc)
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
    extra_data: Dict[str, Any] = None

    def __post_init__(self):
        if self.extra_data is None:
            self.extra_data = {}

# ---------------------------
# CLIENTE RPC
# ---------------------------

class RPCClient:
    def __init__(self, config: Config):
        self.config = config
        self.providers = []
        if config.QUICKNODE_RPC_URL:
            self.providers.append(("quicknode", config.QUICKNODE_RPC_URL))
        if config.HELIUS_RPC_URL:
            self.providers.append(("helius", config.HELIUS_RPC_URL))
        self.providers.append(("public", "https://api.mainnet-beta.solana.com"))
        self.current_provider = 0
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    def _get_current_url(self):
        return self.providers[self.current_provider][1]

    def _switch_provider(self):
        self.current_provider = (self.current_provider + 1) % len(self.providers)
        logging.info(f"Switched to RPC provider: {self.providers[self.current_provider][0]}")

    async def make_rpc_call(self, method: str, params: list, timeout: int = 10):
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        for attempt in range(len(self.providers)):
            url = self._get_current_url()
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params
            }
            
            try:
                async with self.session.post(url, json=payload, timeout=timeout) as response:
                    if response.status == 200:
                        result = await response.json()
                        if "error" in result:
                            raise Exception(f"RPC error: {result['error']}")
                        return result.get("result")
                    else:
                        raise Exception(f"HTTP {response.status}")
            except Exception as e:
                logging.warning(f"RPC call failed ({self.providers[self.current_provider][0]}): {e}")
                self._switch_provider()
                if attempt == len(self.providers) - 1:
                    raise e
                await asyncio.sleep(0.5 * (attempt + 1))

    async def get_account_info(self, account: str):
        return await self.make_rpc_call("getAccountInfo", [account, {"encoding": "base64"}])

    async def get_token_supply(self, mint: str):
        return await self.make_rpc_call("getTokenSupply", [mint])

# ---------------------------
# BASE DE DATOS
# ---------------------------

class Database:
    def __init__(self, config: Config):
        self.config = config
        self.pool = None

    async def connect(self):
        if not self.config.ENABLE_DB or not self.config.DATABASE_URL:
            logging.info("Database disabled")
            return
            
        try:
            self.pool = await asyncpg.create_pool(self.config.DATABASE_URL, min_size=1, max_size=10)
            async with self.pool.acquire() as conn:
                # Tabla de tokens monitoreados
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_monitoring (
                        token_address TEXT PRIMARY KEY,
                        bonding_curve TEXT,
                        associated_bonding_curve TEXT,
                        symbol TEXT,
                        name TEXT,
                        initial_price NUMERIC,
                        max_price NUMERIC,
                        start_time TIMESTAMPTZ,
                        last_checked TIMESTAMPTZ,
                        status TEXT,
                        metadata JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                ''')
                # Tabla de alertas
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_alerts (
                        id SERIAL PRIMARY KEY,
                        token_address TEXT NOT NULL,
                        alert_rule_name TEXT,
                        gain_percent NUMERIC,
                        time_elapsed_min NUMERIC,
                        price_at_alert NUMERIC,
                        market_cap_at_alert NUMERIC,
                        extra_data JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                ''')
                # Histórico de precios
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_price_history (
                        id SERIAL PRIMARY KEY,
                        token_address TEXT NOT NULL,
                        timestamp TIMESTAMPTZ DEFAULT NOW(),
                        price NUMERIC,
                        market_cap NUMERIC,
                        volume_24h NUMERIC
                    )
                ''')
                # Métricas del sistema
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS system_metrics (
                        id SERIAL PRIMARY KEY,
                        metric_name TEXT NOT NULL,
                        metric_value NUMERIC,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                ''')
            logging.info("Database connected and schema verified")
        except Exception as e:
            logging.error(f"Database connection failed: {e}")
            self.pool = None

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            logging.info("Database disconnected")

    async def upsert_token(self, token: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_monitoring 
                    (token_address, bonding_curve, associated_bonding_curve, symbol, name, 
                     initial_price, max_price, start_time, status, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (token_address) DO UPDATE SET
                    last_checked = NOW(),
                    status = EXCLUDED.status,
                    max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price)
                ''', token.mint, token.bonding_curve, token.associated_bonding_curve,
                    token.symbol, token.name, Decimal(str(token.initial_price)),
                    Decimal(str(token.max_price)), token.start_time, token.status,
                    json.dumps(token.metadata))
        except Exception as e:
            logging.error(f"Error upserting token: {e}")

    async def record_alert(self, alert: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_alerts 
                    (token_address, alert_rule_name, gain_percent, time_elapsed_min, 
                     price_at_alert, market_cap_at_alert, extra_data)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                ''', alert.token_address, alert.rule_name, Decimal(str(alert.gain_percent)),
                    Decimal(str(alert.time_elapsed_min)), Decimal(str(alert.price_at_alert)),
                    Decimal(str(alert.market_cap_at_alert)), json.dumps(alert.extra_data))
        except Exception as e:
            logging.error(f"Error recording alert: {e}")

    async def record_price_history(self, token_address: str, price: float, market_cap: float):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_price_history (token_address, price, market_cap)
                    VALUES ($1, $2, $3)
                ''', token_address, Decimal(str(price)), Decimal(str(market_cap)))
        except Exception as e:
            logging.error(f"Error recording price history: {e}")

# ---------------------------
# MOTOR DE ALERTAS
# ---------------------------

class AlertEngine:
    def __init__(self, config: Config):
        self.config = config
        self.rules = config.ALERT_RULES

    def check_rules(self, token: TokenData, current_price: float) -> List[AlertData]:
        if token.initial_price <= 0 or current_price <= 0:
            return []

        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100.0
        elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
        
        triggered_alerts = []
        for rule in self.rules:
            if (gain_percent >= rule["alert_percent"] and 
                elapsed_min <= rule["time_window_min"]):
                alert = AlertData(
                    token_address=token.mint,
                    rule_name=rule["name"],
                    gain_percent=gain_percent,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=0.0,  # Se calculará después
                    extra_data={
                        "symbol": token.symbol,
                        "name": token.name,
                        "initial_price": token.initial_price,
                        "max_price": token.max_price
                    }
                )
                triggered_alerts.append(alert)
                
        return triggered_alerts

# ---------------------------
# GESTOR DE TOKENS
# ---------------------------

class TokenManager:
    def __init__(self, config: Config, db: Database, alert_engine: AlertEngine):
        self.config = config
        self.db = db
        self.alert_engine = alert_engine
        self.monitored_tokens: Dict[str, TokenData] = {}
        self.monitor_tasks: Dict[str, asyncio.Task] = {}
        self.alerted_tokens: set = set()
        self.semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_MONITORS)
        self.metrics = {
            "tokens_monitored": 0,
            "alerts_triggered": 0,
            "rpc_calls": 0,
            "rpc_errors": 0,
            "bonding_curve_decodes": 0,
            "bonding_curve_errors": 0
        }

    async def add_token(self, mint: str, bonding_curve: str = None, 
                       associated_bonding_curve: str = None, 
                       symbol: str = "UNKNOWN", name: str = "UNKNOWN"):
        if mint in self.monitored_tokens:
            return

        token = TokenData(
            mint=mint,
            bonding_curve=bonding_curve,
            associated_bonding_curve=associated_bonding_curve,
            symbol=symbol,
            name=name
        )
        
        self.monitored_tokens[mint] = token
        await self.db.upsert_token(token)
        
        # Iniciar monitoreo
        task = asyncio.create_task(self._monitor_token(mint))
        self.monitor_tasks[mint] = task
        self.metrics["tokens_monitored"] += 1
        
        logging.info(f"Started monitoring token: {symbol} ({mint})")

    async def _monitor_token(self, mint: str):
        async with self.semaphore:
            token = self.monitored_tokens.get(mint)
            if not token:
                return

            try:
                async with RPCClient(self.config) as rpc_client:
                    while mint in self.monitored_tokens:
                        # Verificar timeout
                        elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                        if elapsed_min >= self.config.MAX_MONITOR_TIME_MIN:
                            logging.info(f"Token {mint} reached timeout ({elapsed_min:.1f} min)")
                            await self._remove_token(mint, "timeout")
                            break

                        # Obtener precio actual
                        price_data = await self._get_token_price(rpc_client, token)
                        if not price_data:
                            await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                            continue

                        current_price = price_data.get("price", 0.0)
                        current_market_cap = price_data.get("market_cap", 0.0)

                        # Actualizar precio máximo
                        if current_price > token.max_price:
                            token.max_price = current_price

                        # Registrar en histórico
                        await self.db.record_price_history(mint, current_price, current_market_cap)
                        await self.db.upsert_token(token)

                        # Detectar dump
                        if token.max_price > 0:
                            loss_from_max = ((current_price - token.max_price) / token.max_price) * 100.0
                            if loss_from_max <= self.config.DUMP_THRESHOLD_PERCENT:
                                logging.info(f"Token {mint} dumped {loss_from_max:.1f}% from peak")
                                await self._remove_token(mint, "dumped")
                                break

                        # Verificar alertas
                        alerts = self.alert_engine.check_rules(token, current_price)
                        for alert in alerts:
                            if mint not in self.alerted_tokens:
                                self.alerted_tokens.add(mint)
                                self.metrics["alerts_triggered"] += 1
                                
                                # Actualizar market cap en la alerta
                                alert.market_cap_at_alert = current_market_cap
                                alert.extra_data["market_cap"] = current_market_cap
                                
                                await self.db.record_alert(alert)
                                await self._send_telegram_alert(token, alert)
                                await self._remove_token(mint, "alert_triggered")
                                return

                        await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)

            except asyncio.CancelledError:
                logging.info(f"Monitoring task for {mint} cancelled")
            except Exception as e:
                logging.error(f"Error monitoring token {mint}: {e}")
                await self._remove_token(mint, "error")

    async def _get_token_price(self, rpc_client: RPCClient, token: TokenData) -> Optional[Dict]:
        """Obtiene precio via bonding curve (on-chain)"""
        try:
            if token.bonding_curve:
                account_info = await rpc_client.get_account_info(token.bonding_curve)
                self.metrics["rpc_calls"] += 1
                
                if account_info and account_info.get("value"):
                    data = account_info["value"]["data"]
                    if isinstance(data, list) and len(data) > 0:
                        bonding_data = BondingCurveDecoder.parse_bonding_curve_account(data[0])
                        if bonding_data:
                            self.metrics["bonding_curve_decodes"] += 1
                            return {
                                "price": bonding_data.get("price_usd", 0.0),
                                "market_cap": bonding_data.get("market_cap", 0.0),
                                "source": "onchain"
                            }
            
            self.metrics["bonding_curve_errors"] += 1
            return None
            
        except Exception as e:
            self.metrics["rpc_errors"] += 1
            logging.debug(f"Price fetch error for {token.mint}: {e}")
            return None

    async def _send_telegram_alert(self, token: TokenData, alert: AlertData):
        if not self.config.ENABLE_TELEGRAM or not self.config.TELEGRAM_BOT_TOKEN:
            return

        try:
            from telegram import Bot
            bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
            
            message = self._format_alert_message(token, alert)
            keyboard = self._format_alert_keyboard(token.mint)
            
            await bot.send_message(
                chat_id=self.config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            logging.info(f"Telegram alert sent for {token.mint}")
        except Exception as e:
            logging.error(f"Failed to send Telegram alert: {e}")

    def _format_alert_message(self, token: TokenData, alert: AlertData) -> str:
        return f"""🚀 **ALERTA DE MOMENTUM DETECTADA** 🚀

**Token:** {token.name} ({token.symbol})
**Mint:** `{token.mint}`
**Ganancia:** +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f} min
**Regla:** {alert.rule_name}

📊 **Métricas:**
• Precio Actual: ${alert.price_at_alert:.6f}
• Market Cap: ${alert.market_cap_at_alert:,.0f}
• Precio Inicial: ${token.initial_price:.6f}
• Máximo Alcanzado: ${token.max_price:.6f}

🔗 **Enlaces Rápidos:**
• [Pump.fun](https://pump.fun/{token.mint})
• [DexScreener](https://dexscreener.com/solana/{token.mint})
• [RugCheck](https://rugcheck.xyz/tokens/{token.mint})
• [Birdeye](https://birdeye.so/token/{token.mint}?chain=solana)

⏰ **Tiempo desde creación:** {alert.time_elapsed_min:.1f} minutos"""

    def _format_alert_keyboard(self, mint: str) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("🎯 Pump.fun", url=f"https://pump.fun/{mint}"),
                InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/solana/{mint}")
            ],
            [
                InlineKeyboardButton("🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                InlineKeyboardButton("👁️ Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def _remove_token(self, mint: str, reason: str):
        if mint in self.monitored_tokens:
            token = self.monitored_tokens[mint]
            token.status = reason
            await self.db.upsert_token(token)
            del self.monitored_tokens[mint]
            
        if mint in self.monitor_tasks:
            task = self.monitor_tasks[mint]
            task.cancel()
            del self.monitor_tasks[mint]
            
        logging.info(f"Removed token {mint}: {reason}")

    async def stop_all_monitoring(self):
        for mint in list(self.monitored_tokens.keys()):
            await self._remove_token(mint, "shutdown")

    def get_metrics(self) -> Dict[str, Any]:
        return {
            **self.metrics,
            "currently_monitoring": len(self.monitored_tokens),
            "active_tasks": len(self.monitor_tasks),
            "alerts_sent": len(self.alerted_tokens)
        }

# ---------------------------
# LISTENER (BLOCK SUBSCRIBE)
# ---------------------------

class BlockSubscribeListener:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.ws = None
        self.running = False
        self.reconnect_delay = 3

    async def start(self):
        self.running = True
        wss_candidates = []
        
        if self.config.QUICKNODE_WSS:
            wss_candidates.append(("quicknode", self.config.QUICKNODE_WSS))
        if self.config.HELIUS_WSS:
            wss_candidates.append(("helius", self.config.HELIUS_WSS))
        if self.config.PUMPPORTAL_WSS:
            wss_candidates.append(("pumpportal", self.config.PUMPPORTAL_WSS))

        while self.running:
            for name, uri in wss_candidates:
                try:
                    logging.info(f"Connecting to {name} WebSocket: {uri}")
                    await self._connect_and_listen(uri, name)
                except Exception as e:
                    logging.error(f"WebSocket connection failed ({name}): {e}")
                    await asyncio.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    async def _connect_and_listen(self, uri: str, name: str):
        async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
            self.ws = ws
            self.reconnect_delay = 3
            
            if name == "pumpportal":
                # PumpPortal protocol
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
            else:
                # Solana blockSubscribe
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "blockSubscribe",
                    "params": [
                        {"mentions": [PUMP_FUN_PROGRAM_ID]},
                        {"encoding": "jsonParsed", "transactionDetails": "full"}
                    ]
                }))

            logging.info(f"WebSocket connected and subscribed to {name}")

            async for message in ws:
                if not self.running:
                    break
                await self._handle_message(message, name)

    async def _handle_message(self, message: str, source: str):
        try:
            data = json.loads(message)
            
            if source == "pumpportal":
                await self._handle_pumpportal_message(data)
            else:
                await self._handle_block_subscribe_message(data)
                
        except Exception as e:
            logging.error(f"Error handling WebSocket message: {e}")

    async def _handle_pumpportal_message(self, data: dict):
        """Handle PumpPortal new token messages"""
        token_data = data.get("data") or data
        mint = token_data.get("mint")
        symbol = token_data.get("symbol", "UNKNOWN")
        name = token_data.get("name", symbol)
        bonding_curve = token_data.get("bonding_curve")
        associated_bonding_curve = token_data.get("associatedBondingCurve")

        if mint:
            await self.token_manager.add_token(
                mint=mint,
                bonding_curve=bonding_curve,
                associated_bonding_curve=associated_bonding_curve,
                symbol=symbol,
                name=name
            )

    async def _handle_block_subscribe_message(self, data: dict):
        """Handle Solana blockSubscribe messages"""
        try:
            result = data.get("params", {}).get("result") or data.get("result")
            if not result or not isinstance(result, dict):
                return

            # Extract transactions from block
            transactions = result.get("transactions", [])
            for tx in transactions:
                await self._process_transaction(tx)
                
        except Exception as e:
            logging.error(f"Error processing block message: {e}")

    async def _process_transaction(self, tx: dict):
        """Process individual transaction for new token creation"""
        try:
            # Look for create instructions in transaction
            message = tx.get("transaction", {}).get("message", {})
            instructions = message.get("instructions", [])
            account_keys = message.get("accountKeys", [])
            
            for instr in instructions:
                if self._is_create_instruction(instr, account_keys):
                    mint = self._extract_mint_address(instr, account_keys)
                    if mint:
                        await self.token_manager.add_token(mint=mint)
                        break
        except Exception as e:
            logging.debug(f"Error processing transaction: {e}")

    def _is_create_instruction(self, instruction: dict, account_keys: list) -> bool:
        """Check if instruction is a token create instruction"""
        program_id_index = instruction.get("programIdIndex")
        if program_id_index is not None and program_id_index < len(account_keys):
            program_id = account_keys[program_id_index]
            if isinstance(program_id, dict):
                program_id = program_id.get("pubkey", "")
            return program_id == PUMP_FUN_PROGRAM_ID
        return False

    def _extract_mint_address(self, instruction: dict, account_keys: list) -> Optional[str]:
        """Extract mint address from create instruction"""
        try:
            accounts = instruction.get("accounts", [])
            if accounts and len(accounts) > 0:
                mint_index = accounts[0]
                if mint_index < len(account_keys):
                    mint_address = account_keys[mint_index]
                    if isinstance(mint_address, dict):
                        return mint_address.get("pubkey")
                    return mint_address
        except Exception as e:
            logging.debug(f"Error extracting mint address: {e}")
        return None

    async def stop(self):
        self.running = False
        if self.ws:
            await self.ws.close()

# ---------------------------
# SERVIDOR WEB (Metrics & Health)
# ---------------------------

class MetricsServer:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.app = web.Application()
        self.runner = None
        self.setup_routes()

    def setup_routes(self):
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/metrics", self.handle_metrics)
        self.app.router.add_get("/status", self.handle_status)

    async def handle_health(self, request):
        return web.json_response({
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": "2.0"
        })

    async def handle_metrics(self, request):
        metrics = self.token_manager.get_metrics()
        prometheus_metrics = []
        
        for key, value in metrics.items():
            prometheus_metrics.append(f"pumpfun_bot_{key} {value}")
            
        prometheus_metrics.append(f"pumpfun_bot_uptime_seconds {int(time.time())}")
        
        return web.Response(
            text="\n".join(prometheus_metrics),
            content_type="text/plain"
        )

    async def handle_status(self, request):
        status = {
            "monitored_tokens": len(self.token_manager.monitored_tokens),
            "active_tasks": len(self.token_manager.monitor_tasks),
            "alerts_sent": len(self.token_manager.alerted_tokens),
            "metrics": self.token_manager.get_metrics(),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        return web.json_response(status)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.config.HEALTH_PORT)
        await site.start()
        logging.info(f"Metrics server started on port {self.config.HEALTH_PORT}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

# ---------------------------
# COMANDOS TELEGRAM
# ---------------------------

class TelegramBot:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.application = None

    async def start(self):
        if not self.config.ENABLE_TELEGRAM or not self.config.TELEGRAM_BOT_TOKEN:
            logging.info("Telegram bot disabled")
            return

        try:
            self.application = Application.builder().token(self.config.TELEGRAM_BOT_TOKEN).build()
            
            # Add command handlers
            self.application.add_handler(CommandHandler("start", self.handle_start))
            self.application.add_handler(CommandHandler("status", self.handle_status))
            self.application.add_handler(CommandHandler("tokens", self.handle_tokens))
            self.application.add_handler(CommandHandler("rules", self.handle_rules))
            self.application.add_handler(CommandHandler("metrics", self.handle_metrics))
            self.application.add_handler(CommandHandler("kill", self.handle_kill))
            self.application.add_handler(CommandHandler("ping", self.handle_ping))

            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling()
            
            logging.info("Telegram bot started")
        except Exception as e:
            logging.error(f"Failed to start Telegram bot: {e}")

    async def stop(self):
        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🤖 **Pump.fun Alert Bot v2.0**\n\n"
            "Comandos disponibles:\n"
            "/start - Mostrar este mensaje\n"
            "/status - Estado del bot\n" 
            "/tokens - Tokens en monitoreo\n"
            "/rules - Reglas de alerta\n"
            "/metrics - Métricas del sistema\n"
            "/kill <mint> - Eliminar token\n"
            "/ping - Verificar latencia\n",
            parse_mode="Markdown"
        )

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        metrics = self.token_manager.get_metrics()
        status_text = (
            "📊 **Estado del Bot**\n\n"
            f"• Tokens monitoreados: {metrics['currently_monitoring']}\n"
            f"• Alertas enviadas: {metrics['alerts_triggered']}\n"
            f"• Tareas activas: {metrics['active_tasks']}\n"
            f"• Llamadas RPC: {metrics['rpc_calls']}\n"
            f"• Decodificaciones: {metrics['bonding_curve_decodes']}\n"
            f"• Errores RPC: {metrics['rpc_errors']}\n"
        )
        await update.message.reply_text(status_text, parse_mode="Markdown")

    async def handle_tokens(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        tokens = list(self.token_manager.monitored_tokens.values())
        if not tokens:
            await update.message.reply_text("No hay tokens en monitoreo")
            return

        tokens_text = "🔍 **Tokens en Monitoreo:**\n\n"
        for i, token in enumerate(tokens[:10], 1):
            elapsed = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
            tokens_text += f"{i}. {token.symbol} - {elapsed:.1f}min - `{token.mint[:16]}...`\n"

        if len(tokens) > 10:
            tokens_text += f"\n... y {len(tokens) - 10} más"

        await update.message.reply_text(tokens_text, parse_mode="Markdown")

    async def handle_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        rules_text = "🎯 **Reglas de Alerta:**\n\n"
        for rule in self.config.ALERT_RULES:
            rules_text += f"• {rule['name']}: +{rule['alert_percent']}% en {rule['time_window_min']}min\n"
        
        await update.message.reply_text(rules_text, parse_mode="Markdown")

    async def handle_metrics(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        metrics = self.token_manager.get_metrics()
        metrics_text = "📈 **Métricas del Sistema:**\n\n"
        
        for key, value in metrics.items():
            metrics_text += f"• {key}: {value}\n"
            
        await update.message.reply_text(metrics_text, parse_mode="Markdown")

    async def handle_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Uso: /kill <mint_address>")
            return

        mint = context.args[0]
        if mint in self.token_manager.monitored_tokens:
            await self.token_manager._remove_token(mint, "manual_removal")
            await update.message.reply_text(f"✅ Token {mint[:16]}... eliminado")
        else:
            await update.message.reply_text("❌ Token no encontrado en monitoreo")

    async def handle_ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Bot online")

# ---------------------------
# BOT PRINCIPAL
# ---------------------------

class PumpFunBot:
    def __init__(self):
        self.config = Config()
        self.setup_logging()
        
        self.db = Database(self.config)
        self.alert_engine = AlertEngine(self.config)
        self.token_manager = TokenManager(self.config, self.db, self.alert_engine)
        self.listener = BlockSubscribeListener(self.config, self.token_manager)
        self.metrics_server = MetricsServer(self.config, self.token_manager)
        self.telegram_bot = TelegramBot(self.config, self.token_manager)
        
        self.tasks = []

    def setup_logging(self):
        logging.basicConfig(
            level=getattr(logging, self.config.LOG_LEVEL.upper(), logging.INFO),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[logging.StreamHandler(sys.stdout)]
        )

    async def start(self):
        logging.info("Starting PumpFunBot v2.0...")
        
        # Database
        await self.db.connect()
        
        # Metrics server
        await self.metrics_server.start()
        
        # Telegram bot
        await self.telegram_bot.start()
        
        # Start listener
        listener_task = asyncio.create_task(self.listener.start())
        self.tasks.append(listener_task)
        
        logging.info("PumpFunBot started successfully")

    async def stop(self):
        logging.info("Stopping PumpFunBot...")
        
        # Stop listener
        await self.listener.stop()
        
        # Stop token monitoring
        await self.token_manager.stop_all_monitoring()
        
        # Stop Telegram bot
        await self.telegram_bot.stop()
        
        # Stop metrics server
        await self.metrics_server.stop()
        
        # Stop database
        await self.db.disconnect()
        
        # Cancel all tasks
        for task in self.tasks:
            task.cancel()
        
        logging.info("PumpFunBot stopped")

# ---------------------------
# MANEJO DE SEÑALES
# ---------------------------

async def main():
    bot = PumpFunBot()
    
    # Setup signal handlers
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    
    def signal_handler():
        logging.info("Received shutdown signal")
        stop_event.set()
    
    for sig in [signal.SIGINT, signal.SIGTERM]:
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await bot.start()
        await stop_event.wait()
    finally:
        await bot.stop()

if __name__ == "__main__":
    # Environment checks
    config = Config()
    
    if not config.TELEGRAM_BOT_TOKEN:
        logging.warning("TELEGRAM_BOT_TOKEN not set - Telegram disabled")
    if not config.DATABASE_URL:
        logging.warning("DATABASE_URL not set - Database disabled")
    if not config.QUICKNODE_RPC_URL and not config.HELIUS_RPC_URL:
        logging.warning("No RPC URLs configured - Using public endpoints")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received")
    except Exception as e:
        logging.error(f"Fatal error: {e}")
        raise
