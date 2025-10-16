#!/usr/bin/env python3
"""
Pump.fun Monitor & Telegram Alert Bot
- Monitorea mints originados desde Pump.fun (vía PumpPortal WSS)
- Lee bonding curve on-chain para precio real
- Envía alertas por Telegram (webhook mode)
- Guarda histórico en PostgreSQL
- FastAPI /health y /telegram endpoints
"""

import os
import sys
import json
import asyncio
import logging
import signal
import time
import base64
import struct
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn

# Telegram simple send (we'll use Bot HTTP API)
import requests

# =========================
# CONFIG
# =========================

class Config:
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        self.load()
    def load(self):
        cfg = {}
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                try:
                    cfg = json.load(f)
                except Exception:
                    cfg = {}
        # env vars override
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', cfg.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', cfg.get('telegram_chat_id', ''))
        self.DATABASE_URL = os.getenv('DATABASE_URL', cfg.get('database_url', ''))
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', cfg.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', cfg.get('helius_rpc_url', ''))
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', cfg.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))
        self.ALERT_RULES = cfg.get('alert_rules', [
            {"name": "momentum_120_15", "alert_percent": 120.0, "time_window_min": 15, "description": "120% en 15 minutos"}
        ])
        mon = cfg.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', mon.get('max_monitor_time_min', 30)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', mon.get('dump_threshold_percent', -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', mon.get('price_poll_interval_sec', 5)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', mon.get('max_concurrent_monitors', 40)))
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', cfg.get('log_level', 'INFO')).upper()
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', cfg.get('health_port', 8080)))
        # pump.fun program id from IDL
        self.PUMP_PROGRAM_ID = cfg.get('pump_program_id', '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P')

# =========================
# LOGGING (console-only)
# =========================

def setup_logging(level_str: str):
    logging.basicConfig(
        level=getattr(logging, level_str, logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# =========================
# DATA CLASSES
# =========================

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
    last_checked: Optional[datetime] = None

@dataclass
class AlertData:
    token_address: str
    rule_name: str
    gain_percent: float
    time_elapsed_min: float
    price_at_alert: float
    market_cap_at_alert: float

# =========================
# RPC / BONDING CURVE PARSING
# =========================

class OnChainPriceFetcher:
    """
    Fetch price and bonding curve state from on-chain bonding curve account
    using getAccountInfo and direct byte parsing (Anchor layout: 8 byte discriminator + struct)
    BondingCurve layout (from IDL):
        virtualTokenReserves: u64
        virtualSolReserves: u64
        realTokenReserves: u64
        realSolReserves: u64
        tokenTotalSupply: u64
        complete: bool (u8)
    We'll read little endian u64s from after the 8-byte discriminator.
    """
    def __init__(self, config: Config, session: aiohttp.ClientSession):
        self.config = config
        self.session = session
        self.current_provider = 'quicknode'
    def _get_rpc_url(self):
        if self.current_provider == 'quicknode' and self.config.QUICKNODE_RPC_URL:
            return self.config.QUICKNODE_RPC_URL
        if self.current_provider == 'helius' and self.config.HELIUS_RPC_URL:
            return self.config.HELIUS_RPC_URL
        # fallback
        return "https://api.mainnet-beta.solana.com"
    async def get_account_info(self, account: str) -> Optional[bytes]:
        url = self._get_rpc_url()
        payload = {"jsonrpc":"2.0","id":1,"method":"getAccountInfo","params":[account, {"encoding":"base64"}]}
        try:
            async with self.session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    value = data.get('result', {}).get('value')
                    if not value:
                        return None
                    encoded = value.get('data')[0]
                    return base64.b64decode(encoded)
                else:
                    logging.warning(f"getAccountInfo RPC status {resp.status}")
                    return None
        except Exception as e:
            logging.debug(f"get_account_info error with provider {self.current_provider}: {e}")
            # try switch provider
            if self.current_provider == 'quicknode' and self.config.HELIUS_RPC_URL:
                self.current_provider = 'helius'
            elif self.current_provider == 'helius' and self.config.QUICKNODE_RPC_URL:
                self.current_provider = 'quicknode'
            return None
    def parse_bonding_curve(self, raw: bytes) -> Optional[Dict[str, Any]]:
        """
        Expect raw to contain at least 8 + (5*8 + 1) = 8 + 41 = 49 bytes
        We'll unpack u64 little endian in order.
        """
        if not raw or len(raw) < 8 + (5*8 + 1):
            return None
        try:
            offset = 8  # anchor discriminator
            # little endian u64
            vToken = struct.unpack_from('<Q', raw, offset)[0]; offset += 8
            vSol = struct.unpack_from('<Q', raw, offset)[0]; offset += 8
            rToken = struct.unpack_from('<Q', raw, offset)[0]; offset += 8
            rSol = struct.unpack_from('<Q', raw, offset)[0]; offset += 8
            tokenTotal = struct.unpack_from('<Q', raw, offset)[0]; offset += 8
            complete_flag = struct.unpack_from('<B', raw, offset)[0]; offset += 1
            complete = bool(complete_flag)
            # compute price if possible (avoid division by zero)
            price = None
            market_cap = None
            if rToken > 0:
                price = (rSol / (10**9)) / (rToken / (10**6))  # note: bond tokens may be 6 decimals
                # tokenTotal supplied is raw supply (u64) -> convert to decimals (6)
                market_cap = price * (tokenTotal / (10**6))
            return {
                'virtualTokenReserves': vToken,
                'virtualSolReserves': vSol,
                'realTokenReserves': rToken,
                'realSolReserves': rSol,
                'tokenTotalSupply': tokenTotal,
                'complete': complete,
                'price_sol': price,
                'market_cap': market_cap
            }
        except Exception as e:
            logging.debug(f"parse_bonding_curve error: {e}")
            return None
    async def fetch_price_from_bonding_curve(self, bonding_curve_account: str) -> Optional[Dict[str, Any]]:
        raw = await self.get_account_info(bonding_curve_account)
        if not raw:
            return None
        return self.parse_bonding_curve(raw)

# =========================
# DexScreener fallback
# =========================

async def fetch_price_dexscreener(session: aiohttp.ClientSession, mint: str) -> Optional[Dict[str, Any]]:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with session.get(url, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get('pairs') or []
                if pairs:
                    pair = pairs[0]
                    price = float(pair.get('priceUsd') or pair.get('price') or 0)
                    market_cap = float(pair.get('marketCap') or 0)
                    return {'price_usd': price, 'market_cap': market_cap, 'source': 'dexscreener'}
    except Exception as e:
        logging.debug(f"DexScreener fetch error for {mint}: {e}")
    return None

# =========================
# DATABASE
# =========================

class Database:
    def __init__(self, config: Config):
        self.config = config
        self.pool = None
    async def connect(self):
        if not self.config.DATABASE_URL:
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
                        initial_price DOUBLE PRECISION,
                        initial_market_cap DOUBLE PRECISION,
                        max_price DOUBLE PRECISION,
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
                        gain_percent DOUBLE PRECISION,
                        time_elapsed_min DOUBLE PRECISION,
                        price_at_alert DOUBLE PRECISION,
                        market_cap_at_alert DOUBLE PRECISION,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                ''')
            logging.info("Database connected and schema ensured")
        except Exception as e:
            logging.error(f"Database connection failed: {e}")
            self.pool = None
    async def close(self):
        if self.pool:
            await self.pool.close()
            logging.info("Database connection closed")
    async def upsert_token(self, token: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_monitoring (token_address, symbol, name, initial_price,
                        initial_market_cap, max_price, start_time, last_checked, status, metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    ON CONFLICT (token_address) DO UPDATE SET
                        last_checked = EXCLUDED.last_checked,
                        status = EXCLUDED.status,
                        max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price)
                ''', token.mint, token.symbol, token.name, token.initial_price,
                     token.initial_market_cap, token.max_price, token.start_time,
                     token.last_checked, token.status, json.dumps({}))
        except Exception as e:
            logging.debug(f"upsert_token error: {e}")
    async def record_alert(self, alert: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_alerts (token_address, alert_rule_name, gain_percent,
                        time_elapsed_min, price_at_alert, market_cap_at_alert)
                    VALUES ($1,$2,$3,$4,$5,$6)
                ''', alert.token_address, alert.rule_name, alert.gain_percent,
                     alert.time_elapsed_min, alert.price_at_alert, alert.market_cap_at_alert)
        except Exception as e:
            logging.debug(f"record_alert error: {e}")

# =========================
# TELEGRAM (send only)
# =========================

class TelegramNotifier:
    def __init__(self, bot_token: str, default_chat: str):
        self.bot_token = bot_token
        self.chat_id = default_chat
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
    def send_message(self, chat_id: Optional[str], text: str, parse_mode='Markdown'):
        chat = chat_id or self.chat_id
        if not self.bot_token or not chat:
            logging.debug("Telegram not configured")
            return False
        try:
            resp = requests.post(f"{self.api_base}/sendMessage", json={
                "chat_id": chat,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True
            }, timeout=10)
            if resp.status_code != 200:
                logging.debug(f"Telegram send failed: {resp.status_code} {resp.text}")
                return False
            return True
        except Exception as e:
            logging.debug(f"Telegram send exception: {e}")
            return False

# =========================
# TOKEN MANAGER
# =========================

class TokenManager:
    def __init__(self, config: Config, db: Database, notifier: TelegramNotifier):
        self.config = config
        self.db = db
        self.notifier = notifier
        self.monitored: Dict[str, TokenData] = {}
        self.monitor_tasks: Dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(self.config.MAX_CONCURRENT_MONITORS)
        self.alerted: set = set()  # dedupe
        self.session = aiohttp.ClientSession()
        self.onchain = OnChainPriceFetcher(config, self.session)
    async def add_token(self, mint: str, symbol: str, name: str, initial_price: float=0.0, initial_market_cap: float=0.0, bonding_curve: Optional[str]=None):
        if mint in self.monitored:
            return
        td = TokenData(
            mint=mint,
            symbol=symbol or "UNKNOWN",
            name=name or symbol or "UNKNOWN",
            initial_price=initial_price or 0.0,
            initial_market_cap=initial_market_cap or 0.0,
            max_price=initial_price or 0.0,
            start_time=datetime.now(timezone.utc),
            last_checked=datetime.now(timezone.utc)
        )
        td.metadata = {'bonding_curve': bonding_curve} if bonding_curve else {}
        self.monitored[mint] = td
        await self.db.upsert_token(td)
        # launch monitor
        task = asyncio.create_task(self._monitor_token(mint))
        self.monitor_tasks[mint] = task
        logging.info(f"Started monitoring {td.symbol} ({mint[:8]}...)")
    async def _monitor_token(self, mint: str):
        # concurrency guard
        async with self.semaphore:
            token = self.monitored.get(mint)
            if not token:
                return
            start = token.start_time
            max_price = token.max_price or 0.0
            try:
                while mint in self.monitored:
                    # check TTL
                    elapsed_min = (datetime.now(timezone.utc) - start).total_seconds() / 60.0
                    if elapsed_min >= self.config.MAX_MONITOR_TIME_MIN:
                        logging.info(f"Token {token.symbol} {mint} expired after {elapsed_min:.1f}min")
                        await self._remove_token(mint, "timeout")
                        break
                    # fetch price: try on-chain bonding curve if available
                    price_sol = None
                    market_cap = None
                    bc = token.metadata.get('bonding_curve')
                    if bc:
                        info = await self.onchain.fetch_price_from_bonding_curve(bc)
                        if info and info.get('price_sol') is not None:
                            price_sol = info['price_sol']
                            market_cap = info.get('market_cap')
                    # fallback to DexScreener (price in USD) -> convert later if needed (we keep USD fallback)
                    if price_sol is None:
                        ds = await fetch_price_dexscreener(self.session, token.mint)
                        if ds:
                            # store USD price as placeholder
                            price_sol = None
                            market_cap = ds.get('market_cap')
                            # keep ds info in metadata for reference
                            token.metadata['dexscreener'] = ds
                    # if we have a price in SOL, use it; else if only marketcap in USD use that
                    current_price = price_sol if price_sol is not None else token.initial_price
                    # update max price
                    if current_price and current_price > max_price:
                        max_price = current_price
                        token.max_price = max_price
                    token.last_checked = datetime.now(timezone.utc)
                    await self.db.upsert_token(token)
                    # compute gain % from initial (if initial_price is 0, skip)
                    if token.initial_price and current_price:
                        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100.0
                    else:
                        gain_percent = 0.0
                    # check dump from max
                    if max_price > 0 and current_price:
                        loss_from_max = ((current_price - max_price) / max_price) * 100.0
                        if loss_from_max <= self.config.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"Token {token.symbol} ({mint}) dumped {loss_from_max:.1f}% -> removing")
                            await self._remove_token(mint, "dumped")
                            break
                    # check alert rules
                    for rule in self.config.ALERT_RULES:
                        required_gain = float(rule['alert_percent'])
                        time_window = float(rule['time_window_min'])
                        # compute elapsed since creation in minutes
                        if elapsed_min <= time_window and gain_percent >= required_gain:
                            # dedupe
                            if mint in self.alerted:
                                logging.debug(f"Alert for {mint} already sent")
                                break
                            # prepare alert
                            alert = AlertData(
                                token_address=mint,
                                rule_name=rule['name'],
                                gain_percent=gain_percent,
                                time_elapsed_min=elapsed_min,
                                price_at_alert=current_price or 0.0,
                                market_cap_at_alert=market_cap or 0.0
                            )
                            # store alert
                            await self.db.record_alert(alert)
                            # send telegram (links)
                            msg = self._format_telegram_message(token, alert)
                            self.notifier.send_message(self.notifier.chat_id, msg)
                            self.alerted.add(mint)
                            logging.info(f"ALERT sent for {mint} -> +{gain_percent:.1f}% in {elapsed_min:.1f}min")
                            # remove from monitoring after alert (per design)
                            await self._remove_token(mint, "alert_sent")
                            break
                    await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
            except asyncio.CancelledError:
                logging.info(f"Monitor task for {mint} cancelled")
            except Exception as e:
                logging.error(f"Error monitoring {mint}: {e}")
                await self._remove_token(mint, "error")
    async def _remove_token(self, mint: str, reason: str):
        if mint in self.monitored:
            token = self.monitored[mint]
            token.status = reason
            token.last_checked = datetime.now(timezone.utc)
            await self.db.upsert_token(token)
            del self.monitored[mint]
        if mint in self.monitor_tasks:
            task = self.monitor_tasks[mint]
            task.cancel()
            del self.monitor_tasks[mint]
        logging.info(f"Removed token {mint}: {reason}")
    def _format_telegram_message(self, token: TokenData, alert: AlertData) -> str:
        mint = token.mint
        lines = []
        lines.append(f"🚨 *ALERTA PUMP.FUN* 🚨")
        lines.append(f"*Token:* {token.name} ({token.symbol})")
        lines.append(f"*Mint:* `{mint}`")
        lines.append(f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f}min")
        lines.append(f"*Precio actual (SOL):* {alert.price_at_alert:.8f}" if alert.price_at_alert else "Precio SOL: N/A")
        lines.append(f"*Market cap:* {alert.market_cap_at_alert:,.0f}" if alert.market_cap_at_alert else "Market cap: N/A")
        lines.append("")
        lines.append("🔗 *Enlaces:*")
        lines.append(f"[Pump.fun](https://pump.fun/{mint})")
        lines.append(f"[DexScreener](https://dexscreener.com/solana/{mint})")
        lines.append(f"[RugCheck](https://rugcheck.xyz/tokens/{mint})")
        lines.append(f"[Birdeye](https://birdeye.so/token/{mint}?chain=solana)")
        return "\n".join(lines)

# =========================
# WEBSOCKET CLIENT: PumpPortal
# =========================

class PumpPortalListener:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.ws = None
        self.is_running = False
    async def connect(self):
        uri = self.config.PUMPPORTAL_WSS
        logging.info(f"Connecting to PumpPortal WSS: {uri}")
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                self.ws = ws
                self.is_running = True
                # subscribe if required by portal API
                try:
                    await ws.send(json.dumps({"method":"subscribeNewToken"}))
                except Exception:
                    pass
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        await self._handle_message(data)
                    except Exception as e:
                        logging.debug(f"ws message error: {e}")
        except Exception as e:
            logging.error(f"PumpPortal WSS error: {e}")
            await asyncio.sleep(5)
    async def _handle_message(self, data: Dict[str, Any]):
        """
        Expected shapes vary. We'll robustly parse:
        - data may include 'mint', 'symbol', 'name', 'pairs', 'bondingCurve'
        - or may be nested: data.get('data')
        """
        payload = data.get('data') if isinstance(data.get('data'), dict) else data
        mint = payload.get('mint') or payload.get('token') or payload.get('tokenMint')
        symbol = payload.get('symbol') or payload.get('tokenSymbol') or payload.get('meta', {}).get('symbol')
        name = payload.get('name') or payload.get('tokenName')
        bonding_curve = payload.get('bondingCurve') or payload.get('bonding_curve') or payload.get('associatedBondingCurve')
        initial_price = 0.0
        initial_market_cap = 0.0
        # try extract price from pairs
        pairs = payload.get('pairs') or []
        if isinstance(pairs, list) and pairs:
            p = pairs[0]
            try:
                initial_price = float(p.get('priceUsd') or p.get('price') or 0)
                initial_market_cap = float(p.get('marketCap') or 0)
            except Exception:
                pass
        if not mint:
            logging.debug("Incoming message without mint, skipping")
            return
        await self.token_manager.add_token(mint=mint, symbol=symbol, name=name,
                                           initial_price=initial_price, initial_market_cap=initial_market_cap,
                                           bonding_curve=bonding_curve)

# =========================
# FASTAPI app (health + telegram webhook for commands)
# =========================

app = FastAPI()
# We'll set these global variables later
GLOBAL_STATE = {}

@app.get("/health")
async def health():
    tm: TokenManager = GLOBAL_STATE.get('token_manager')
    db: Database = GLOBAL_STATE.get('db')
    status = {
        "uptime": int(time.time() - GLOBAL_STATE.get('start_time', time.time())),
        "monitored": len(tm.monitored) if tm else 0,
        "db_connected": bool(db and db.pool),
    }
    return JSONResponse(status_code=200, content=status)

@app.post("/telegram")
async def telegram_webhook(req: Request):
    """
    Telegram webhook endpoint. This receives updates from Telegram (set webhook via BotFather).
    We'll handle simple commands: /status, /tokens, /rules
    """
    body = await req.json()
    # simple parsing
    message = body.get('message') or body.get('edited_message') or {}
    text = message.get('text','').strip() if message else ''
    chat = message.get('chat', {})
    chat_id = chat.get('id')
    # quick handlers
    tm: TokenManager = GLOBAL_STATE.get('token_manager')
    cfg: Config = GLOBAL_STATE.get('config')
    notifier: TelegramNotifier = GLOBAL_STATE.get('notifier')
    if text.startswith('/status'):
        status = {
            "monitored_tokens": len(tm.monitored) if tm else 0,
            "rules": [r['name'] for r in cfg.ALERT_RULES],
            "price_poll_interval_sec": cfg.PRICE_POLL_INTERVAL_SEC
        }
        text_out = "Status:\n" + json.dumps(status, indent=2)
        notifier.send_message(chat_id, text_out, parse_mode=None)
    elif text.startswith('/tokens'):
        if not tm or not tm.monitored:
            notifier.send_message(chat_id, "No tokens monitored")
        else:
            lines = ["Tokens monitored:"]
            for i,(k,v) in enumerate(list(tm.monitored.items())[:20],1):
                elapsed = (datetime.now(timezone.utc) - v.start_time).total_seconds()/60.0
                lines.append(f"{i}. {v.symbol} {k[:12]}... - {elapsed:.1f}min")
            notifier.send_message(chat_id, "\n".join(lines))
    elif text.startswith('/rules'):
        lines = ["Active rules:"]
        for r in cfg.ALERT_RULES:
            lines.append(f"- {r['name']}: +{r['alert_percent']}% en {r['time_window_min']}min")
        notifier.send_message(chat_id, "\n".join(lines))
    else:
        # default echo / unrecognized
        notifier.send_message(chat_id, "Comando no reconocido. Usa /status /tokens /rules")
    return Response(status_code=200)

# =========================
# MAIN APP MANAGER
# =========================

class PumpFunApp:
    def __init__(self, config: Config):
        self.config = config
        setup_logging(self.config.LOG_LEVEL)
        self.db = Database(config)
        self.notifier = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
        self.tm = TokenManager(config, self.db, self.notifier)
        self.listener = PumpPortalListener(config, self.tm)
        GLOBAL_STATE['token_manager'] = self.tm
        GLOBAL_STATE['db'] = self.db
        GLOBAL_STATE['notifier'] = self.notifier
        GLOBAL_STATE['config'] = self.config
        GLOBAL_STATE['start_time'] = time.time()
    async def start(self):
        logging.info("Starting PumpFunBot...")
        await self.db.connect()
        # start websocket listener
        self._ws_task = asyncio.create_task(self.listener.connect())
        logging.info("PumpFunBot started")
    async def stop(self):
        logging.info("Stopping PumpFunBot...")
        # cancel ws
        try:
            self._ws_task.cancel()
        except Exception:
            pass
        await self.tm.session.close()
        await self.db.close()
        logging.info("PumpFunBot stopped")

# =========================
# RUN
# =========================

def run_main():
    cfg = Config()
    app_instance = PumpFunApp(cfg)

    loop = asyncio.get_event_loop()

    async def _main():
        await app_instance.start()

    # run uvicorn in background using asyncio.create_task
    # We'll run uvicorn programmatically so the whole process is single
    import multiprocessing

    # FastAPI/uvicorn runner
    config_uvicorn = {
        "app": app,
        "host": "0.0.0.0",
        "port": cfg.HEALTH_PORT,
        "log_level": "info",
        "loop": "asyncio",
    }

    # schedule start of app_instance then start uvicorn
    try:
        loop.create_task(_main())
        # start uvicorn (blocking)
        uvicorn.run(app, host=config_uvicorn['host'], port=config_uvicorn['port'], log_level="info")
    except (KeyboardInterrupt, SystemExit):
        logging.info("Shutting down...")
    finally:
        # attempt graceful shutdown
        try:
            loop.run_until_complete(app_instance.stop())
        except Exception:
            pass

if __name__ == "__main__":
    run_main()
