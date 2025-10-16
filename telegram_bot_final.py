#!/usr/bin/env python3
"""
🤖 Pump.fun Monitor & Alert Bot (no auto-buy)
- Detecta nuevos mints (Pump.fun), monitorea su bonding curve on-chain,
  evalúa momentum (por defecto 120% en 15 minutos) y envía alertas a Telegram.
- Diseñado para desplegar en Railway: logs a stdout, health endpoint, no file writes.
- Requiere configurar env vars (ver abajo).
"""

import os
import sys
import json
import asyncio
import logging
import signal
import base64
import struct
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from decimal import Decimal

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI
from prometheus_client import Counter, make_asgi_app
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Constants
PUMP_PROGRAM_ID = os.getenv('PUMP_PROGRAM_ID', '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P')
DEFAULT_ALERT_PERCENT = float(os.getenv('ALERT_PERCENT', '120.0'))
DEFAULT_ALERT_TIME_WINDOW_MIN = float(os.getenv('ALERT_TIME_WINDOW_MIN', '15'))
DEFAULT_MAX_MONITOR_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', '30'))
DEFAULT_DUMP_THRESHOLD = float(os.getenv('DUMP_THRESHOLD_PERCENT', '-50'))
DEFAULT_PRICE_POLL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', '5'))
DEFAULT_MAX_CONCURRENT = int(os.getenv('MAX_CONCURRENT_MONITORS', '40'))

# Prometheus metrics
MET_ALERTS_SENT = Counter('pumpfun_alerts_sent_total', 'Total pump.fun alerts sent')
MET_TOKENS_MONITORED = Counter('pumpfun_tokens_monitored_total', 'Total tokens started monitoring')

# ---------------------------
# CONFIG (loads config.json if present, env vars override)
# ---------------------------
class Config:
    def __init__(self, path: str = None):
        self.path = path or os.path.join(os.path.dirname(__file__), 'config.json')
        self._load()

    def _load(self):
        cfg = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        # env overrides
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', cfg.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', cfg.get('telegram_chat_id', ''))
        self.DATABASE_URL = os.getenv('DATABASE_URL', cfg.get('database_url', ''))
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', cfg.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', cfg.get('helius_rpc_url', ''))
        self.QUICKNODE_WSS = os.getenv('QUICKNODE_WSS', cfg.get('quicknode_wss', ''))
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', cfg.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', cfg.get('log_level', 'INFO'))
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', cfg.get('health_port', 8080)))

        # alert rules
        default_rules = cfg.get('alert_rules', [
            {"name": "momentum_120_15", "alert_percent": DEFAULT_ALERT_PERCENT, "time_window_min": DEFAULT_ALERT_TIME_WINDOW_MIN, "description": f"{DEFAULT_ALERT_PERCENT}% en {DEFAULT_ALERT_TIME_WINDOW_MIN} minutos"}
        ])
        self.ALERT_RULES = default_rules

        monitoring = cfg.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', monitoring.get('max_monitor_time_min', DEFAULT_MAX_MONITOR_MIN)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', monitoring.get('dump_threshold_percent', DEFAULT_DUMP_THRESHOLD)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', monitoring.get('price_poll_interval_sec', DEFAULT_PRICE_POLL_SEC)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', monitoring.get('max_concurrent_monitors', DEFAULT_MAX_CONCURRENT)))
        self.ENABLE_DB = os.getenv('ENABLE_DB', str(cfg.get('enable_db', 'true'))).lower() == 'true'
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', str(cfg.get('enable_telegram', 'true'))).lower() == 'true'

# ---------------------------
# Logging setup (Railway friendly)
# ---------------------------
def setup_logging(level: str = 'INFO'):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# ---------------------------
# Data models
# ---------------------------
@dataclass
class TokenData:
    mint: str
    symbol: str
    name: str
    bonding_curve: Optional[str]
    initial_price: float
    initial_market_cap: float
    max_price: float
    start_time: datetime
    status: str = "monitoring"
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

# ---------------------------
# RPC Client (HTTP + WebSocket fallback)
# ---------------------------
class RPCClient:
    def __init__(self, config: Config):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        # provider selection: http for getAccountInfo, websocket for logsSubscribe (if needed)
        self.provider_index = 0
        self.providers = []
        if self.config.QUICKNODE_RPC_URL:
            self.providers.append(self.config.QUICKNODE_RPC_URL)
        if self.config.HELIUS_RPC_URL:
            self.providers.append(self.config.HELIUS_RPC_URL)
        # allow dexscreener fetching via HTTPS
        self.dexscreener_base = "https://api.dexscreener.com/latest/dex/tokens/"

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _http_post(self, url: str, payload: dict, timeout: int = 10) -> dict:
        async with self.session.post(url, json=payload, timeout=timeout) as resp:
            return await resp.json()

    async def make_rpc_call(self, method: str, params: list, timeout: int = 10) -> Optional[dict]:
        # Try providers in order
        last_exc = None
        for url in self.providers or []:
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            try:
                data = await self._http_post(url, payload, timeout=timeout)
                return data.get('result')
            except Exception as e:
                logging.warning(f"RPC call {method} failed for {url}: {e}")
                last_exc = e
        # fallback: try heluis if none provided
        raise last_exc or Exception("No RPC providers configured")

    async def get_account_info(self, account: str) -> Optional[dict]:
        """Call getAccountInfo for an account and return parsed base64 data hex"""
        try:
            res = await self.make_rpc_call("getAccountInfo", [account, {"encoding": "base64"}])
            return res
        except Exception as e:
            logging.debug(f"getAccountInfo failed for {account}: {e}")
            return None

    async def get_token_price_from_dexscreener(self, mint: str) -> Optional[Dict]:
        """DexScreener quick price fetch (fallback)"""
        try:
            async with self.session.get(self.dexscreener_base + mint, timeout=6) as r:
                if r.status == 200:
                    j = await r.json()
                    pairs = j.get('pairs') or []
                    if pairs:
                        pair = pairs[0]
                        price = float(pair.get('priceUsd', 0) or pair.get('price', 0) or 0)
                        market_cap = float(pair.get('marketCap', 0) or 0)
                        volume = float((pair.get('volume') or {}).get('h24', 0) or 0)
                        return {'price': price, 'market_cap': market_cap, 'volume_24h': volume, 'source': 'dexscreener'}
        except Exception as e:
            logging.debug(f"Dexscreener error for {mint}: {e}")
        return None

    async def get_price_and_bonding_curve_state(self, bonding_curve_addr: str) -> Optional[Dict]:
        """
        Read on-chain bonding curve account and parse it according to pump.fun IDL:
        BondingCurve struct fields (after 8 byte discriminator):
            virtualTokenReserves: u64
            virtualSolReserves: u64
            realTokenReserves: u64
            realSolReserves: u64
            tokenTotalSupply: u64
            complete: bool (1 byte)
        Returns derived price estimate (approx) and raw reserves.
        """
        if not bonding_curve_addr:
            return None
        info = await self.get_account_info(bonding_curve_addr)
        if not info:
            return None
        # info['value']['data'][0] is base64
        try:
            data_b64 = info.get('value', {}).get('data', [None])[0]
            if not data_b64:
                return None
            raw = base64.b64decode(data_b64)
            # Anchor: 8-byte discriminator prefix
            offset = 8
            # Read 5 u64
            u64s = []
            for i in range(5):
                if offset + 8 > len(raw):
                    raise ValueError("insufficient data while parsing u64")
                val = struct.unpack_from("<Q", raw, offset)[0]
                u64s.append(val)
                offset += 8
            virtualTokenReserves, virtualSolReserves, realTokenReserves, realSolReserves, tokenTotalSupply = u64s
            # Read boolean (1 byte)
            complete = False
            if offset < len(raw):
                complete = bool(struct.unpack_from("<?", raw, offset)[0])
            # Estimate price: simplistic approach: price ~ (virtualSolReserves / virtualTokenReserves) if vt>0
            price = None
            if virtualTokenReserves and virtualTokenReserves > 0:
                price = virtualSolReserves / virtualTokenReserves
            return {
                'virtualTokenReserves': virtualTokenReserves,
                'virtualSolReserves': virtualSolReserves,
                'realTokenReserves': realTokenReserves,
                'realSolReserves': realSolReserves,
                'tokenTotalSupply': tokenTotalSupply,
                'complete': complete,
                'price': float(price) if price is not None else None
            }
        except Exception as e:
            logging.debug(f"Error decoding bonding curve {bonding_curve_addr}: {e}")
            return None

# ---------------------------
# Database handler (Postgres)
# ---------------------------
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
                        start_time TIMESTAMP WITH TIME ZONE,
                        last_checked TIMESTAMP WITH TIME ZONE,
                        status TEXT,
                        metadata JSONB,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
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
                    );
                ''')
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS token_price_history (
                        id SERIAL PRIMARY KEY,
                        token_address TEXT NOT NULL,
                        timestamp TIMESTAMPTZ DEFAULT NOW(),
                        price NUMERIC,
                        market_cap NUMERIC,
                        volume_24h NUMERIC
                    );
                ''')
            logging.info("Database connected and schema ensured")
        except Exception as e:
            logging.error(f"Database connection error: {e}")
            self.pool = None

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            logging.info("Database connection closed")

    async def add_or_update_token(self, token: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_monitoring (token_address, symbol, name, bonding_curve, initial_price, initial_market_cap, max_price, start_time, last_checked, status, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,NOW(),$9,$10)
                    ON CONFLICT (token_address) DO UPDATE SET
                        last_checked = NOW(),
                        status = EXCLUDED.status,
                        max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price)
                ''', token.mint, token.symbol, token.name, token.bonding_curve,
                     Decimal(str(token.initial_price)), Decimal(str(token.initial_market_cap)),
                     Decimal(str(token.max_price)), token.start_time, token.status,
                     json.dumps(token.metadata))
        except Exception as e:
            logging.error(f"DB add_or_update_token error: {e}")

    async def record_price_history(self, token_address: str, price: float, market_cap: float, volume: float):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_price_history (token_address, price, market_cap, volume_24h) VALUES ($1,$2,$3,$4)
                ''', token_address, Decimal(str(price)), Decimal(str(market_cap)), Decimal(str(volume)))
        except Exception as e:
            logging.debug(f"DB record_price_history error: {e}")

    async def record_alert(self, alert: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_alerts (token_address, alert_rule_name, gain_percent, time_elapsed_min, price_at_alert, market_cap_at_alert, extra_data)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                ''', alert.token_address, alert.rule_name, Decimal(str(alert.gain_percent)),
                     Decimal(str(alert.time_elapsed_min)), Decimal(str(alert.price_at_alert)),
                     Decimal(str(alert.market_cap_at_alert)), json.dumps(alert.extra_data))
        except Exception as e:
            logging.error(f"DB record_alert error: {e}")

# ---------------------------
# Alert Engine (momentum rules)
# ---------------------------
class AlertEngine:
    def __init__(self, config: Config):
        self.config = config
        self.alert_rules = config.ALERT_RULES

    async def evaluate_token(self, token: TokenData, current_price: float, current_market_cap: float) -> List[AlertData]:
        if current_price is None or token.initial_price <= 0:
            return []
        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100
        elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
        alerts = []
        for rule in self.alert_rules:
            req_gain = float(rule.get('alert_percent', DEFAULT_ALERT_PERCENT))
            time_window = float(rule.get('time_window_min', DEFAULT_ALERT_TIME_WINDOW_MIN))
            if gain_percent >= req_gain and elapsed_min <= time_window:
                alert = AlertData(
                    token_address=token.mint,
                    rule_name=rule.get('name', f'momentum_{req_gain}_{time_window}'),
                    gain_percent=gain_percent,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=current_market_cap,
                    extra_data={'symbol': token.symbol, 'name': token.name}
                )
                alerts.append(alert)
        return alerts

# ---------------------------
# Token Manager: monitoring tasks + concurrency control
# ---------------------------
class TokenManager:
    def __init__(self, config: Config, db: Database, rpc_client: RPCClient, alert_engine: AlertEngine, telegram_bot: Optional[Bot] = None):
        self.config = config
        self.db = db
        self.rpc_client = rpc_client
        self.alert_engine = alert_engine
        self.telegram_bot = telegram_bot

        self.monitored_tokens: Dict[str, TokenData] = {}
        self.monitor_tasks: Dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(self.config.MAX_CONCURRENT_MONITORS)
        self._alerted: set = set()  # dedup set for token alerts

    async def add_token(self, mint: str, symbol: str = 'UNKNOWN', name: str = 'UNKNOWN', bonding_curve: Optional[str] = None, initial_price: float = 0.0, initial_market_cap: float = 0.0):
        if mint in self.monitored_tokens:
            logging.debug(f"Token {mint} already monitored")
            return
        token = TokenData(
            mint=mint, symbol=symbol, name=name, bonding_curve=bonding_curve,
            initial_price=initial_price, initial_market_cap=initial_market_cap,
            max_price=initial_price, start_time=datetime.now(timezone.utc)
        )
        self.monitored_tokens[mint] = token
        await self.db.add_or_update_token(token)
        MET_TOKENS_MONITORED.inc()
        # spawn monitor
        task = asyncio.create_task(self._monitor_token(mint))
        self.monitor_tasks[mint] = task
        logging.info(f"Started monitoring {symbol} ({mint[:8]}...)")

    async def _monitor_token(self, mint: str):
        token = self.monitored_tokens.get(mint)
        if not token:
            return
        async with self.semaphore:
            try:
                # Try to use bonding curve on-chain pricing first if available
                start = datetime.now(timezone.utc)
                while mint in self.monitored_tokens:
                    elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                    # check overall timeout
                    if elapsed_min >= self.config.MAX_MONITOR_TIME_MIN:
                        logging.info(f"Token {token.symbol} ({mint}) timeout after {elapsed_min:.1f}min - removing")
                        await self._remove_token(mint, "timeout")
                        break

                    # Fetch price: on-chain bonding curve if available
                    price_data = None
                    if token.bonding_curve:
                        bc_state = await self.rpc_client.get_price_and_bonding_curve_state(token.bonding_curve)
                        if bc_state and bc_state.get('price') is not None:
                            price_data = {
                                'price': bc_state['price'],
                                'market_cap': (bc_state.get('tokenTotalSupply', 0) * bc_state.get('price', 0)) if bc_state.get('price') else 0,
                                'volume_24h': 0,
                                'source': 'onchain'
                            }
                            # if bonding curve complete, mark accordingly
                            if bc_state.get('complete'):
                                logging.info(f"Bonding curve for {mint} marked complete (migrated).")
                    # Fallback to DexScreener
                    if not price_data:
                        price_data = await self.rpc_client.get_token_price_from_dexscreener(token.mint)

                    if not price_data:
                        await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                        continue

                    current_price = float(price_data['price'] or 0)
                    current_market_cap = float(price_data.get('market_cap', 0) or 0)

                    # Update token stats
                    if current_price > token.max_price:
                        token.max_price = current_price
                        await self.db.add_or_update_token(token)

                    await self.db.record_price_history(mint, current_price, current_market_cap, price_data.get('volume_24h', 0))

                    # Check dump from max
                    if token.max_price > 0:
                        loss_from_max = ((current_price - token.max_price) / token.max_price) * 100.0
                        if loss_from_max <= self.config.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"Token {token.symbol} ({mint}) dumped {loss_from_max:.1f}% from max -> removing")
                            await self._remove_token(mint, "dumped")
                            break

                    # Evaluate alerts
                    alerts = await self.alert_engine.evaluate_token(token, current_price, current_market_cap)
                    for alert in alerts:
                        # Deduplicate
                        if mint in self._alerted:
                            logging.debug(f"Alert already sent for {mint}, skipping")
                            continue
                        logging.info(f"ALERT {token.symbol} ({mint}): +{alert.gain_percent:.1f}% in {alert.time_elapsed_min:.1f}min")
                        await self.db.record_alert(alert)
                        await self._notify_telegram(token, alert)
                        MET_ALERTS_SENT.inc()
                        self._alerted.add(mint)
                        # After alert, remove from monitoring or keep? We'll remove to avoid duplicates.
                        await self._remove_token(mint, "alert_sent")
                        break

                    await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)

            except asyncio.CancelledError:
                logging.info(f"Monitor task for {mint} cancelled")
            except Exception as e:
                logging.error(f"Error monitoring {mint}: {e}")
                await self._remove_token(mint, "error")

    async def _remove_token(self, mint: str, reason: str):
        token = self.monitored_tokens.get(mint)
        if token:
            token.status = reason
            try:
                await self.db.add_or_update_token(token)
            except Exception:
                pass
            del self.monitored_tokens[mint]
        task = self.monitor_tasks.pop(mint, None)
        if task:
            try:
                task.cancel()
            except Exception:
                pass
        logging.info(f"Removed token {mint}: {reason}")

    async def _notify_telegram(self, token: TokenData, alert: AlertData):
        """Sends Telegram message to configured chat (if enabled)"""
        if not self.config.ENABLE_TELEGRAM or not self.config.TELEGRAM_BOT_TOKEN:
            logging.info("Telegram disabled or token missing, skipping notification")
            return
        try:
            # Build message
            msg = self._format_message(token, alert)
            keyboard = self._format_keyboard(token.mint)
            bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
            # Send to configured chat id
            if self.config.TELEGRAM_CHAT_ID:
                await bot.send_message(chat_id=self.config.TELEGRAM_CHAT_ID, text=msg, parse_mode='Markdown', reply_markup=keyboard, disable_web_page_preview=True)
            logging.info(f"Telegram alert sent for {token.symbol} ({token.mint})")
        except Exception as e:
            logging.error(f"Telegram send error: {e}")

    def _format_message(self, token: TokenData, alert: AlertData) -> str:
        # Create clickable links (DexScreener, RugCheck, Pump.fun, Birdeye)
        dex_url = f"https://dexscreener.com/solana/{token.mint}"
        rug_url = f"https://rugcheck.xyz/tokens/{token.mint}"
        pump_url = f"https://pump.fun/{token.mint}"
        birdeye = f"https://birdeye.so/token/{token.mint}?chain=solana"
        return (
            f"🚀 *ALERTA DE PUMP DETECTADA* 🚀\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f}min\n"
            f"*Regla:* {alert.rule_name}\n\n"
            f"📊 *Métricas:*\n"
            f"• Precio estimado: {alert.price_at_alert:.8f} (denominado en SOL)\n"
            f"• Market Cap: ${alert.market_cap_at_alert:,.0f}\n\n"
            f"🔗 *Enlaces:* \n"
            f"• [Pump.fun]({pump_url})\n"
            f"• [DexScreener]({dex_url})\n"
            f"• [RugCheck]({rug_url})\n"
            f"• [Birdeye]({birdeye})\n\n"
            f"🕒 Tiempo desde creación: {alert.time_elapsed_min:.1f} minutos\n"
        )

    def _format_keyboard(self, mint: str) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"), InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"), InlineKeyboardButton("Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def stop_all(self):
        for m in list(self.monitored_tokens.keys()):
            await self._remove_token(m, "shutdown")

    def get_status(self):
        return {
            'monitored_tokens': len(self.monitored_tokens),
            'active_tasks': len(self.monitor_tasks),
            'tokens': list(self.monitored_tokens.keys())
        }

# ---------------------------
# WebSocket client: listen to PumpPortal or RPC logsSubscribe
# ---------------------------
class WebSocketListener:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.ws = None
        self.running = False
        self.reconnect_delay = 5
        self.max_reconnect = 60

    async def connect_pumpportal(self):
        uri = self.config.PUMPPORTAL_WSS
        logging.info(f"Connecting to PumpPortal WSS: {uri}")
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                self.ws = ws
                # subscribe protocol depends on pumpportal; many implementations expect a JSON request:
                try:
                    subscribe_msg = {"method": "subscribeNewToken"}
                    await ws.send(json.dumps(subscribe_msg))
                except Exception:
                    pass
                logging.info("Connected to pumpportal, listening for new tokens...")
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        await self._handle_portal_message(data)
                    except Exception as e:
                        logging.debug(f"pumpportal message decode error: {e}")
        except Exception as e:
            logging.error(f"PumpPortal WSS error: {e}")

    async def _handle_portal_message(self, data: dict):
        """
        Expected pumpportal message formats vary. Typical payload contains:
        - mint
        - symbol
        - name
        - bondingCurve or pairs[]
        We'll attempt to parse many shapes.
        """
        try:
            # Some portals wrap payload in "data" key
            payload = data.get('data') or data
            mint = payload.get('mint') or payload.get('token') or payload.get('tokenMint')
            symbol = payload.get('symbol') or payload.get('tokenSymbol') or payload.get('name') or 'UNKNOWN'
            name = payload.get('name') or symbol
            bonding_curve = payload.get('bondingCurve') or payload.get('bonding_curve') or payload.get('associatedBondingCurve')
            initial_price = 0.0
            initial_market_cap = 0.0
            # try parse pairs
            if isinstance(payload.get('pairs'), list) and payload.get('pairs'):
                try:
                    pair = payload['pairs'][0]
                    initial_price = float(pair.get('priceUsd') or pair.get('price') or 0)
                    initial_market_cap = float(pair.get('marketCap') or 0)
                except Exception:
                    pass
            if mint:
                await self.token_manager.add_token(mint=mint, symbol=symbol, name=name, bonding_curve=bonding_curve, initial_price=initial_price, initial_market_cap=initial_market_cap)
        except Exception as e:
            logging.error(f"_handle_portal_message error: {e}")

    async def connect_logs_subscribe(self):
        """
        Connect to RPC websocket (if present) and logsSubscribe to pump program directly.
        Note: Many RPC providers require a WSS endpoint supporting logsSubscribe.
        """
        wss = self.config.QUICKNODE_WSS or self.config.HELIUS_RPC_URL
        if not wss:
            logging.info("No WSS RPC provider configured for logsSubscribe")
            return
        logging.info(f"Connecting to RPC WSS for logsSubscribe: {wss}")
        try:
            async with websockets.connect(wss, ping_interval=20, ping_timeout=10) as ws:
                # subscribe logsSubscribe for program
                # JSON RPC format:
                request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [PUMP_PROGRAM_ID]},
                        {"commitment": "finalized", "encoding": "jsonParsed"}
                    ]
                }
                await ws.send(json.dumps(request))
                logging.info("Subscribed logsSubscribe to pump program")
                async for raw in ws:
                    try:
                        packet = json.loads(raw)
                        # packet likely has "params" -> "result" -> "value" -> logs or transaction
                        params = packet.get('params')
                        if not params:
                            continue
                        value = params.get('result', params.get('value'))
                        # logsSubscribe returns value.logs array.
                        logs = value.get('logs') if isinstance(value, dict) else None
                        if logs:
                            # Look for CreateEvent or anchor event printed as "Program log: <EVENT_JSON>" or similar
                            for l in logs:
                                if 'CreateEvent' in l or 'create' in l.lower():
                                    # Attempt to parse mint and bondingCurve from the log line
                                    # Many Anchor programs emit Program log: "EVENT: <json>" - we attempt to find JSON
                                    if '{' in l and '}' in l:
                                        try:
                                            start = l.index('{')
                                            j = json.loads(l[start:])
                                            mint = j.get('mint')
                                            bonding_curve = j.get('bondingCurve') or j.get('bonding_curve') or j.get('bondingCurveAddress')
                                            name = j.get('name') or j.get('symbol') or 'UNKNOWN'
                                            symbol = j.get('symbol') or name
                                            if mint:
                                                await self.token_manager.add_token(mint=mint, symbol=symbol, name=name, bonding_curve=bonding_curve)
                                        except Exception:
                                            pass
                        else:
                            # fallback: some providers return transaction with logs in params.result.value.logs
                            pass
                    except Exception as e:
                        logging.debug(f"logsSubscribe packet parse error: {e}")
        except Exception as e:
            logging.error(f"logsSubscribe connection error: {e}")

    async def run(self):
        self.running = True
        # Prefer pumpportal (third-party) if configured, but also attempt logsSubscribe
        # Run both in parallel (pumpportal preferred for simplicity & richer payloads)
        tasks = []
        if self.config.PUMPPORTAL_WSS:
            tasks.append(asyncio.create_task(self.connect_pumpportal()))
        # also try RPC logsSubscribe if wss present
        if self.config.QUICKNODE_WSS or self.config.HELIUS_RPC_URL:
            tasks.append(asyncio.create_task(self.connect_logs_subscribe()))
        if not tasks:
            logging.warning("No websocket sources configured (PUMPPORTAL_WSS or QUICKNODE_WSS/HELIUS_RPC_URL). Listener inactive.")
            return
        # Run until cancelled
        await asyncio.gather(*tasks, return_exceptions=True)

# ---------------------------
# Telegram command handlers
# ---------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "🤖 Pump.fun Monitor Bot\nCommands:\n/status - system status\n/tokens - tokens in monitoring\n/rules - alert rules\n/stop <mint> - stop monitoring a mint"
    await update.message.reply_text(text)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_app):
    status = bot_app.token_manager.get_status()
    txt = f"📊 Status\nMonitored tokens: {status['monitored_tokens']}\nActive tasks: {status['active_tasks']}\n"
    await update.message.reply_text(txt)

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_app):
    status = bot_app.token_manager.get_status()
    if not status['monitored_tokens']:
        await update.message.reply_text("No tokens currently monitored")
        return
    text = "Tokens monitored:\n"
    for m in status['tokens'][:25]:
        t = bot_app.token_manager.monitored_tokens.get(m)
        if not t:
            continue
        elapsed = (datetime.now(timezone.utc) - t.start_time).total_seconds() / 60.0
        text += f"- {t.symbol} ({m[:12]}...) {elapsed:.1f}min\n"
    await update.message.reply_text(text)

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_app):
    text = "Active alert rules:\n"
    for r in bot_app.config.ALERT_RULES:
        text += f"- {r.get('name')}: +{r.get('alert_percent')}% en {r.get('time_window_min')}min ({r.get('description','')})\n"
    await update.message.reply_text(text)

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_app):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /stop <mint>")
        return
    mint = args[0].strip()
    if mint in bot_app.token_manager.monitored_tokens:
        await bot_app.token_manager._remove_token(mint, "stopped_by_user")
        await update.message.reply_text(f"Stopped monitoring {mint}")
    else:
        await update.message.reply_text("Mint not found in monitored list")

# ---------------------------
# Main PumpFunBot application wrapper
# ---------------------------
class PumpFunBotApp:
    def __init__(self):
        self.config = Config()
        setup_logging(self.config.LOG_LEVEL)
        logging.info("Starting PumpFunBot...")
        self.db = Database(self.config)
        self.rpc_client = RPCClient(self.config)
        self.alert_engine = AlertEngine(self.config)
        self.token_manager: Optional[TokenManager] = None
        self.ws_listener: Optional[WebSocketListener] = None
        self.telegram_app: Optional[Application] = None
        self.health_app = FastAPI()
        # mount prometheus
        self.health_app.mount("/metrics", make_asgi_app())

        @self.health_app.get("/health")
        async def health():
            return {"status": "ok"}

    async def start(self):
        # DB connect
        await self.db.connect()
        # rpc client session
        await self.rpc_client.__aenter__()
        # token manager
        self.token_manager = TokenManager(self.config, self.db, self.rpc_client, self.alert_engine)
        # websocket listener
        self.ws_listener = WebSocketListener(self.config, self.token_manager)
        # telegram
        if self.config.ENABLE_TELEGRAM and self.config.TELEGRAM_BOT_TOKEN:
            # Setup bot application (polling)
            self.telegram_app = Application.builder().token(self.config.TELEGRAM_BOT_TOKEN).build()
            # Bind handlers with reference to this app via closures
            self.telegram_app.add_handler(CommandHandler("start", cmd_start))
            # lambda wrappers to pass this app instance
            self.telegram_app.add_handler(CommandHandler("status", lambda u, c: cmd_status(u, c, self)))
            self.telegram_app.add_handler(CommandHandler("tokens", lambda u, c: cmd_tokens(u, c, self)))
            self.telegram_app.add_handler(CommandHandler("rules", lambda u, c: cmd_rules(u, c, self)))
            self.telegram_app.add_handler(CommandHandler("stop", lambda u, c: cmd_stop(u, c, self)))
            # run polling in background
            logging.info("Starting Telegram polling...")
            asyncio.create_task(self.telegram_app.run_polling())
        else:
            logging.info("Telegram not enabled or token missing")

        # Start ws listeners
        asyncio.create_task(self.ws_listener.run())
        logging.info("PumpFunBot started")

    async def stop(self):
        logging.info("Stopping PumpFunBot...")
        try:
            if self.ws_listener:
                self.ws_listener.running = False
            if self.token_manager:
                await self.token_manager.stop_all()
            if self.telegram_app:
                await self.telegram_app.shutdown()
            if self.rpc_client:
                await self.rpc_client.__aexit__(None, None, None)
            await self.db.disconnect()
        except Exception as e:
            logging.error(f"Error during shutdown: {e}")
        logging.info("PumpFunBot stopped")

# ---------------------------
# Entrypoint
# ---------------------------
app_instance: Optional[PumpFunBotApp] = None
async def _main():
    global app_instance
    app_instance = PumpFunBotApp()
    await app_instance.start()

def run_main():
    loop = asyncio.get_event_loop()
    # handle signals
    try:
        loop.run_until_complete(_main())
        loop.run_forever()
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received")
    finally:
        if app_instance:
            loop.run_until_complete(app_instance.stop())

if __name__ == "__main__":
    run_main()
