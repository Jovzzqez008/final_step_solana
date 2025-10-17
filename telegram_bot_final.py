#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot_final.py
Bot de monitorización Pump.fun (SIN compras automáticas).
- Escucha PumpPortal WSS para nuevos mints
- Valida mint (Base58 / Solana PublicKey)
- Decodifica bonding curve on-chain con getAccountInfo (Borsh-like decoding via struct)
- Monitorea precio (DexScreener rápido -> fallback on-chain bonding curve)
- Alerta por Telegram vía Webhook (FastAPI endpoint)
- Persistencia opcional en PostgreSQL
- Health / metrics con FastAPI (puerto configurable)
- Logging solo a stdout (Railway-friendly)
Config: config.json + env vars override
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
from datetime import datetime, timezone

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, Response
import uvicorn

# Telegram Bot API (we will call setWebhook and send messages using Bot)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot
# Use solana PublicKey for validation
from solana.publickey import PublicKey

# -----------------------
# CONFIGURATION
# -----------------------
DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "database_url": "",
    "quicknode_rpc_url": "",
    "helius_rpc_url": "",
    "pumpportal_wss": "wss://pumpportal.fun/api/data",
    "alert_rules": [
        {
            "name": "momentum_120_15",
            "alert_percent": 120.0,
            "time_window_min": 15,
            "description": "120% en 15 minutos"
        }
    ],
    "monitoring": {
        "max_monitor_time_min": 35,
        "dump_threshold_percent": -50,
        "price_poll_interval_sec": 5,
        "max_concurrent_monitors": 40
    },
    "log_level": "INFO",
    "health_port": 8080,
    "domain_url": ""  # e.g. https://finalstepsolana-production.up.railway.app
}

class Config:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = os.path.join(os.path.dirname(__file__), config_path)
        self._load()
    def _load(self):
        data = DEFAULT_CONFIG.copy()
        # load file if exists
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    file_data = json.load(f)
                    data.update(file_data)
            except Exception as e:
                print("Warning: could not read config.json:", e)
        # env override
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', data.get('telegram_chat_id', ''))
        self.DATABASE_URL = os.getenv('DATABASE_URL', data.get('database_url', ''))
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', data.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', data.get('helius_rpc_url', ''))
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', data.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))
        self.ALERT_RULES = data.get('alert_rules', DEFAULT_CONFIG['alert_rules'])
        monitoring = data.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', monitoring.get('max_monitor_time_min', 35)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', monitoring.get('dump_threshold_percent', -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', monitoring.get('price_poll_interval_sec', 5)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', monitoring.get('max_concurrent_monitors', 40)))
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', data.get('log_level', 'INFO'))
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', data.get('health_port', 8080)))
        self.ENABLE_DB = os.getenv('ENABLE_DB', str(data.get('enable_db', 'true'))).lower() == 'true'
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', str(data.get('enable_telegram', 'true'))).lower() == 'true'
        self.DOMAIN_URL = os.getenv('DOMAIN_URL', data.get('domain_url', ''))
        self.MODE = os.getenv('MODE', data.get('mode', 'PROD'))

# -----------------------
# Logging (Railway-friendly)
# -----------------------
def setup_logging(log_level: str):
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# -----------------------
# Dataclasses
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
# RPC Client (QuickNode/Helius) + DexScreener fallback
# -----------------------
class RPCClient:
    def __init__(self, config: Config):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
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
            self.session = None

    def _current_url(self):
        return self.providers[self.provider_idx][1]

    async def _rotate_provider(self):
        self.provider_idx = (self.provider_idx + 1) % len(self.providers)
        logging.info(f"Rotated RPC provider to {self.providers[self.provider_idx][0]}")

    async def make_rpc_call(self, method: str, params: list, timeout: int = 10) -> Any:
        if not self.session:
            await self.__aenter__()
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
            # rotate once
            await self._rotate_provider()
            try:
                url2 = self._current_url()
                async with self.session.post(url2, json=payload, timeout=timeout) as resp2:
                    data2 = await resp2.json()
                    if 'error' in data2:
                        raise Exception(f"RPC error: {data2['error']}")
                    return data2.get('result')
            except Exception as e2:
                logging.error(f"RPC retry failed: {e2}")
                raise

    async def get_account_info_base64(self, address: str) -> Optional[Dict]:
        try:
            res = await self.make_rpc_call("getAccountInfo", [address, {"encoding": "base64"}])
            return res
        except Exception as e:
            logging.debug(f"getAccountInfo failed for {address}: {e}")
            return None

    async def get_token_price_from_dexscreener(self, mint: str) -> Optional[Dict]:
        # DexScreener returns priceUsd; this is fast but may miss new tokens
        if not self.session:
            await self.__aenter__()
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            async with self.session.get(url, timeout=6) as resp:
                if resp.status == 200:
                    js = await resp.json()
                    if js.get('pairs'):
                        pair = js['pairs'][0]
                        price = float(pair.get('priceUsd', 0) or 0)
                        market_cap = float(pair.get('marketCap', 0) or 0)
                        volume = float(pair.get('volume', {}).get('h24', 0) or 0)
                        return {'price': price, 'market_cap': market_cap, 'volume_24h': volume, 'source': 'dexscreener'}
        except Exception as e:
            logging.debug(f"DexScreener fetch failed for {mint}: {e}")
        return None

    async def fetch_price_onchain_bonding_curve(self, bonding_curve_address: str) -> Optional[Dict]:
        """
        Decodes the bonding curve account stored as Anchor struct:
        Anchor discriminator (8 bytes) + fields:
        u64 x5 + bool (complete)
        We decode little-endian U64s to derive approximate price = realSolReserves / realTokenReserves
        """
        try:
            account = await self.get_account_info_base64(bonding_curve_address)
            if not account or not account.get('value') or not account['value'].get('data'):
                return None
            b64data = account['value']['data'][0]
            raw = base64.b64decode(b64data)
            # check size
            if len(raw) < 8 + (8 * 5 + 1):
                logging.debug("Bonding curve account too small")
                return None
            offset = 8  # skip anchor discriminator
            try:
                vToken, vSol, rToken, rSol, supply = struct.unpack_from("<QQQQQ", raw, offset)
                offset += 8*5
                complete = struct.unpack_from("<?", raw, offset)[0]
            except Exception as e:
                logging.debug(f"Struct unpack error: {e}")
                return None
            price = 0.0
            market_cap = 0.0
            if rToken and rSol:
                try:
                    price = float(rSol) / float(rToken)
                except Exception:
                    price = 0.0
            try:
                market_cap = float(supply) * price
            except:
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
        except Exception as e:
            logging.debug(f"Error fetching bonding curve {bonding_curve_address}: {e}")
            return None

# -----------------------
# Database (Postgres) handler
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

# -----------------------
# Alert Engine (momentum rules)
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
            # We apply "accumulated from initial price" logic: current price vs initial_price at creation.
            if gain_percent >= float(rule['alert_percent']) and elapsed_min <= float(rule['time_window_min']):
                triggered.append(AlertData(
                    token_address=token.mint,
                    rule_name=rule['name'],
                    gain_percent=gain_percent,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=0.0,
                    extra_data={'symbol': token.symbol, 'name': token.name, 'initial_price': token.initial_price, 'max_price': token.max_price}
                ))
        return triggered

# -----------------------
# Notification (Telegram via Bot, webhook-based)
# -----------------------
class Notification:
    bot: Optional[Bot] = None
    config: Optional[Config] = None

    @classmethod
    def init(cls, config: Config):
        cls.config = config
        if config.ENABLE_TELEGRAM and config.TELEGRAM_BOT_TOKEN:
            cls.bot = Bot(token=config.TELEGRAM_BOT_TOKEN)

    @classmethod
    async def set_webhook(cls):
        """Set webhook to DOMAIN_URL/telegram/webhook if available"""
        if not cls.bot or not cls.config or not cls.config.DOMAIN_URL:
            return False
        try:
            webhook_url = cls.config.DOMAIN_URL.rstrip("/") + "/telegram/webhook"
            # Bot.set_webhook is sync in some versions; use HTTP endpoint for reliability using aiohttp.
            async with aiohttp.ClientSession() as s:
                resp = await s.post(f"https://api.telegram.org/bot{cls.config.TELEGRAM_BOT_TOKEN}/setWebhook",
                                     json={"url": webhook_url, "allowed_updates": ["message", "edited_message", "callback_query"]},
                                     timeout=10)
                js = await resp.json()
                if js.get("ok"):
                    logging.info(f"✅ Telegram webhook set to {webhook_url}")
                    return True
                else:
                    logging.warning(f"Could not set webhook: {js}")
                    return False
        except Exception as e:
            logging.error(f"Error setting webhook: {e}")
            return False

    @classmethod
    async def send_token_alert(cls, token: TokenData, alert: AlertData):
        if not cls.bot or not cls.config or not cls.config.TELEGRAM_CHAT_ID:
            logging.info(f"Alert (no telegram): {token.symbol} {alert.gain_percent:.1f}%")
            return
        try:
            message = cls._format_message(token, alert)
            keyboard = cls._make_keyboard(token.mint)
            await cls.bot.send_message(chat_id=cls.config.TELEGRAM_CHAT_ID, text=message, parse_mode='Markdown', reply_markup=keyboard, disable_web_page_preview=True)
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
    def _make_keyboard(mint: str) -> InlineKeyboardMarkup:
        kb = [
            [InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"),
             InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")],
            [InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
             InlineKeyboardButton("Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")]
        ]
        return InlineKeyboardMarkup(kb)

# -----------------------
# TokenManager
# -----------------------
class TokenManager:
    def __init__(self, config: Config, db: Database, rpc: RPCClient, alert_engine: AlertEngine):
        self.config = config
        self.db = db
        self.rpc = rpc
        self.alert_engine = alert_engine
        self.monitored: Dict[str, TokenData] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.alerted: set = set()
        self.semaphore = asyncio.Semaphore(self.config.MAX_CONCURRENT_MONITORS)

    async def add_token(self, mint_raw: str, symbol: str = "UNKNOWN", name: str = "UNKNOWN", bonding_curve: Optional[str] = None, initial_price: float = 0.0, initial_market_cap: float = 0.0):
        """
        Normalize mint:
         - If mint endswith 'pump' suffix and length > len(suffix) -> strip the suffix for blockchain address
         - Validate via solana.PublicKey to ensure it's a real base58 pubkey
        """
        mint = mint_raw
        suffix = "pump"
        if mint.endswith(suffix) and len(mint) > len(suffix):
            # Only strip exact suffix if present; many pump addresses embed 'pump' in vanity, so be careful
            mint = mint[:-len(suffix)]
            logging.debug(f"Stripped suffix 'pump' -> {mint}")

        # Validate Solana PubKey (Base58)
        try:
            _ = PublicKey(mint)
        except Exception:
            logging.warning(f"Ignored invalid mint (not a valid Solana pubkey): {mint_raw}")
            return

        if mint in self.monitored:
            logging.debug(f"{mint} already monitored")
            return

        token = TokenData(
            mint=mint,
            symbol=symbol,
            name=name,
            initial_price=float(initial_price or 0.0),
            initial_market_cap=float(initial_market_cap or 0.0),
            max_price=float(initial_price or 0.0),
            start_time=datetime.now(timezone.utc),
            bonding_curve=bonding_curve
        )
        self.monitored[mint] = token
        token.last_checked = datetime.now(timezone.utc)
        await self.db.add_or_update_token(token)
        # create monitoring task
        t = asyncio.create_task(self._monitor_token(mint))
        self.tasks[mint] = t
        logging.info(f"Started monitoring {symbol} ({mint[:8]}...)")

    async def _monitor_token(self, mint: str):
        async with self.semaphore:
            token = self.monitored.get(mint)
            if not token:
                return
            try:
                start_time = token.start_time
                # ensure rpc session
                if not self.rpc.session:
                    await self.rpc.__aenter__()
                while mint in self.monitored:
                    elapsed_min = (datetime.now(timezone.utc) - start_time).total_seconds() / 60.0
                    # timeout (user requested 35 minutes)
                    if elapsed_min >= self.config.MAX_MONITOR_TIME_MIN:
                        logging.info(f"Token {token.symbol} {mint} timed out after {elapsed_min:.1f} min")
                        await self._remove_token(mint, "timeout")
                        return

                    # fetch price: DexScreener -> onchain bonding curve
                    price_data = None
                    try:
                        price_data = await self.rpc.get_token_price_from_dexscreener(mint)
                    except Exception:
                        price_data = None

                    if not price_data and token.bonding_curve:
                        try:
                            bc = await self.rpc.fetch_price_onchain_bonding_curve(token.bonding_curve)
                            if bc:
                                price_data = {'price': bc['price'], 'market_cap': bc['market_cap'], 'source': 'bonding_curve'}
                        except Exception:
                            price_data = None

                    if not price_data:
                        # Try heuristic: check possible bonding curve address is token.bonding_curve; if none, just wait
                        await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                        continue

                    current_price = float(price_data.get('price', 0.0) or 0.0)
                    current_market_cap = float(price_data.get('market_cap', 0.0) or 0.0)
                    token.last_checked = datetime.now(timezone.utc)

                    if current_price > token.max_price:
                        token.max_price = current_price

                    # persist quick update
                    await self.db.add_or_update_token(token)

                    # detect dump from max
                    if token.max_price > 0:
                        loss_from_max = ((current_price - token.max_price) / token.max_price) * 100
                        if loss_from_max <= self.config.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"Dump detected for {token.symbol} {mint}: {loss_from_max:.1f}% -> removing")
                            await self._remove_token(mint, "dumped")
                            return

                    # evaluate alerts (accumulated vs initial)
                    alerts = self.alert_engine.check_rules(token, current_price, elapsed_min)
                    for alert in alerts:
                        if mint in self.alerted:
                            continue
                        alert.market_cap_at_alert = current_market_cap
                        await self.db.record_alert(alert)
                        self.alerted.add(mint)
                        # send telegram notification (async)
                        await Notification.send_token_alert(token, alert)
                        # Option A: remove from monitoring after alert
                        await self._remove_token(mint, "alert_sent")
                        return

                    await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                logging.info(f"Monitor task cancelled for {mint}")
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
        logging.info(f"Removed token {mint}: {reason}")

# -----------------------
# PumpPortal WSS client
# -----------------------
class PumpPortalClient:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.ws = None
        self.running = False
        self.is_connected = False
        self.reconnect_delay = 5
        self.max_reconnect = 60

    async def connect(self):
        uri = self.config.PUMPPORTAL_WSS
        self.running = True
        while self.running:
            try:
                logging.info(f"Connecting to PumpPortal WSS: {uri}")
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                    self.ws = websocket
                    self.is_connected = True
                    self.reconnect_delay = 5
                    # Try subscribe if endpoint requires it (some portals expect subscribe message)
                    try:
                        await websocket.send(json.dumps({"method":"subscribeNewToken"}))
                    except Exception:
                        pass
                    async for raw in websocket:
                        try:
                            data = json.loads(raw)
                            await self._handle_message(data)
                        except json.JSONDecodeError:
                            logging.debug("Invalid JSON from WSS")
                        except Exception as e:
                            logging.error(f"Error handling websocket message: {e}")
            except Exception as e:
                logging.error(f"WSS connection error: {e}")
                self.is_connected = False
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect)
            finally:
                self.is_connected = False

    async def _handle_message(self, data: Dict):
        """
        Normalize payloads:
         - payload may be top-level or under 'data'
         - support 'mint', 'symbol', 'name', 'bondingCurve', 'pairs'
        """
        try:
            payload = data
            if 'data' in data and isinstance(data['data'], dict):
                payload = data['data']
            mint = payload.get('mint') or payload.get('token') or None
            symbol = payload.get('symbol') or payload.get('tokenSymbol') or 'UNKNOWN'
            name = payload.get('name') or payload.get('tokenName') or symbol
            bonding_curve = payload.get('bondingCurve') or payload.get('bonding_curve') or None
            initial_price = 0.0
            initial_mcap = 0.0
            if isinstance(payload.get('pairs'), list) and payload.get('pairs'):
                p = payload['pairs'][0]
                try:
                    initial_price = float(p.get('priceUsd', p.get('price', 0)) or 0)
                except:
                    initial_price = 0.0
                try:
                    initial_mcap = float(p.get('marketCap', 0) or 0)
                except:
                    initial_mcap = 0.0
            if not mint:
                logging.debug("WSS message without mint")
                return
            # ensure mint is string
            mint = str(mint)
            await self.token_manager.add_token(mint_raw=mint, symbol=symbol, name=name, bonding_curve=bonding_curve, initial_price=initial_price, initial_market_cap=initial_mcap)
        except Exception as e:
            logging.error(f"Error in _handle_message: {e}")

    async def stop(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

# -----------------------
# FastAPI (health + webhook) and dispatch
# -----------------------
def create_fastapi_app(bot_ref):
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok", "monitored_tokens": len(bot_ref.token_manager.monitored), "is_ws_connected": bot_ref.portal_client.is_connected}

    @app.get("/metrics")
    async def metrics():
        return {"monitored_tokens": len(bot_ref.token_manager.monitored), "active_tasks": len(bot_ref.token_manager.tasks)}

    # Telegram webhook endpoint (POST)
    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request):
        """
        Telegram will POST update JSON here.
        We parse message text and dispatch minimal commands:
         - /start : send menu
         - /iniciar : start monitoring (start PumpPortal WSS)
         - /detener : stop monitoring (stop WSS + cancel tasks)
         - /status : returns current status
         - /tokens : list active tokens (top 10)
        """
        try:
            body = await request.json()
        except Exception:
            return Response(status_code=400)
        # Telegram Update structure: 'message' or 'edited_message' or 'callback_query'
        update = body
        chat_id = None
        text = None
        # message
        if update.get('message'):
            msg = update['message']
            chat_id = msg.get('chat', {}).get('id')
            text = msg.get('text')
        elif update.get('edited_message'):
            msg = update['edited_message']
            chat_id = msg.get('chat', {}).get('id')
            text = msg.get('text')
        elif update.get('callback_query'):
            cq = update['callback_query']
            chat_id = cq.get('from', {}).get('id')
            text = cq.get('data')
        else:
            # unknown update type
            return {"ok": True}
        if not chat_id:
            return {"ok": True}
        # Normalize text
        if not text:
            return {"ok": True}
        text = text.strip()
        # Dispatch commands
        if text.startswith("/start"):
            # Send menu with buttons
            kb = [
                [InlineKeyboardButton("▶️ Iniciar monitor", callback_data="/iniciar")],
                [InlineKeyboardButton("⏹ Detener monitor", callback_data="/detener")],
                [InlineKeyboardButton("📊 Estado", callback_data="/status"), InlineKeyboardButton("🔎 Tokens", callback_data="/tokens")]
            ]
            markup = InlineKeyboardMarkup(kb)
            try:
                await Notification.bot.send_message(chat_id=chat_id,
                                                    text="🤖 *Pump.fun Monitor*\nUsa los botones o comandos:\n/iniciar - Iniciar monitoreo\n/detener - Detener monitoreo\n/status - Estado\n/tokens - Tokens en monitoreo",
                                                    parse_mode='Markdown',
                                                    reply_markup=markup)
            except Exception as e:
                logging.error(f"Failed send /start reply: {e}")
            return {"ok": True}

        elif text.startswith("/iniciar") or text == "/iniciar" or text == "iniciar":
            # Start DB + portal client + monitoring
            if bot_ref.is_monitoring:
                try:
                    await Notification.bot.send_message(chat_id=chat_id, text="✅ Monitor ya está en ejecución.")
                except:
                    pass
                return {"ok": True}
            # start monitoring
            await bot_ref.start_monitoring()
            try:
                await Notification.bot.send_message(chat_id=chat_id, text="✅ Monitor iniciado. Escuchando nuevos mints de Pump.fun.")
            except Exception:
                pass
            return {"ok": True}

        elif text.startswith("/detener") or text == "/detener":
            if not bot_ref.is_monitoring:
                try:
                    await Notification.bot.send_message(chat_id=chat_id, text="ℹ️ Monitor ya está detenido.")
                except:
                    pass
                return {"ok": True}
            await bot_ref.stop_monitoring()
            try:
                await Notification.bot.send_message(chat_id=chat_id, text="🛑 Monitor detenido.")
            except:
                pass
            return {"ok": True}

        elif text.startswith("/status") or text == "/status":
            status = bot_ref.token_manager
            text_out = (f"📊 Estado\nMonitoreando: {len(status.monitored)} tokens\nTareas activas: {len(status.tasks)}\nWS conectado: {bot_ref.portal_client.is_connected}\n")
            try:
                await Notification.bot.send_message(chat_id=chat_id, text=text_out)
            except:
                pass
            return {"ok": True}

        elif text.startswith("/tokens") or text == "/tokens":
            tm = bot_ref.token_manager
            if not tm.monitored:
                try:
                    await Notification.bot.send_message(chat_id=chat_id, text="🔍 No hay tokens en monitoreo.")
                except:
                    pass
                return {"ok": True}
            tokens_text = "🔍 *TOKENS EN MONITOREO*\n\n"
            i = 0
            for mint, token in list(tm.monitored.items())[:20]:
                i += 1
                elapsed = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                tokens_text += f"{i}. *{token.symbol}* - {elapsed:.1f} min\n`{mint}`\n"
            try:
                await Notification.bot.send_message(chat_id=chat_id, text=tokens_text, parse_mode='Markdown')
            except:
                pass
            return {"ok": True}

        else:
            # Unknown command - ignore
            return {"ok": True}

    return app

# -----------------------
# PumpFunBot main orchestrator
# -----------------------
class PumpFunBot:
    def __init__(self):
        self.config = Config()
        setup_logging(self.config.LOG_LEVEL)
        self.db = Database(self.config)
        self.rpc = RPCClient(self.config)
        self.alert_engine = AlertEngine(self.config)
        self.token_manager = TokenManager(self.config, self.db, self.rpc, self.alert_engine)
        self.portal_client = PumpPortalClient(self.config, self.token_manager)
        Notification.init(self.config)
        self.fastapi_app = create_fastapi_app(self)
        self.http_server_task: Optional[asyncio.Task] = None
        self.ws_task: Optional[asyncio.Task] = None
        self.is_monitoring = False

    def setup_signal_handlers(self):
        loop = asyncio.get_event_loop()
        def _handler(sig, frame):
            logging.info(f"Signal {sig} received: shutting down")
            asyncio.create_task(self.shutdown())
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    async def start_monitoring(self):
        if self.is_monitoring:
            return
        # DB connect
        await self.db.connect()
        # set webhook (if domain provided)
        try:
            await Notification.set_webhook()
        except Exception:
            pass
        # start portal client
        self.ws_task = asyncio.create_task(self.portal_client.connect())
        self.is_monitoring = True
        logging.info("Started monitoring (PumpPortal WSS)...")

    async def stop_monitoring(self):
        if not self.is_monitoring:
            return
        try:
            await self.portal_client.stop()
        except Exception:
            pass
        if self.ws_task:
            self.ws_task.cancel()
            self.ws_task = None
        # cancel token monitoring tasks
        for t in list(self.token_manager.tasks.values()):
            t.cancel()
        self.token_manager.tasks.clear()
        self.token_manager.monitored.clear()
        self.is_monitoring = False
        # keep DB connected (optional). We do NOT disconnect DB here to keep history.

    async def shutdown(self):
        logging.info("Shutting down PumpFunBot...")
        await self.stop_monitoring()
        await self.db.disconnect()
        logging.info("Shutdown complete.")

    async def run_http(self):
        # Run uvicorn programmatically
        uv_cfg = uvicorn.Config(self.fastapi_app, host="0.0.0.0", port=self.config.HEALTH_PORT, log_level="info")
        server = uvicorn.Server(uv_cfg)
        await server.serve()

# -----------------------
# Entrypoint
# -----------------------
async def main():
    bot = PumpFunBot()
    bot.setup_signal_handlers()
    # Start HTTP server (contains webhook + health)
    http_task = asyncio.create_task(bot.run_http())
    # Do not auto-start monitoring — user triggers /iniciar via Telegram
    # Set webhook proactively if domain available and bot token set
    try:
        await Notification.set_webhook()
    except Exception:
        pass
    logging.info("Server started. Awaiting /iniciar (via Telegram webhook) to begin monitoring.")
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logging.info("Main cancelled; shutting down...")
    finally:
        await bot.shutdown()

if __name__ == "__main__":
    cfg = Config()
    setup_logging(cfg.LOG_LEVEL)
    # Warn missing config
    if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        logging.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Alerts will not be delivered.")
    if not cfg.QUICKNODE_RPC_URL and not cfg.HELIUS_RPC_URL:
        logging.warning("No QUICKNODE_RPC_URL or HELIUS_RPC_URL set - falling back to public RPC (may be rate-limited).")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
