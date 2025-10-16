#!/usr/bin/env python3
"""
telegram_bot_final.py
Bot de monitorización Pump.fun — Detecta mints, monitorea bonding curve on-chain,
aplica regla de momentum (+120% en 15min), notifica por Telegram, guarda histórico en Postgres,
exposición health/metrics vía FastAPI para Railway.
No hace compras automáticas.
"""

import os
import sys
import json
import base64
import time
import asyncio
import logging
import signal
import struct
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

import aiohttp
import asyncpg
import websockets
from fastapi import FastAPI, Request, BackgroundTasks
from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# --------------------------
# CONFIG (env first, fallback to config.json)
# --------------------------

class Config:
    def __init__(self, path: str = None):
        self.path = path or os.path.join(os.path.dirname(__file__), 'config.json')
        self.load()

    def load(self):
        cfg = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        # ENV override
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', cfg.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', cfg.get('telegram_chat_id', ''))
        self.DATABASE_URL = os.getenv('DATABASE_URL', cfg.get('database_url', ''))
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', cfg.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', cfg.get('helius_rpc_url', ''))
        self.PUMPPORTAL_WSS = os.getenv('PUMPPORTAL_WSS', cfg.get('pumpportal_wss', 'wss://pumpportal.fun/api/data'))
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', cfg.get('log_level', 'INFO'))
        # alert rules
        self.ALERT_RULES = cfg.get('alert_rules', [
            {"name": "momentum_120_15", "alert_percent": 120.0, "time_window_min": 15, "description": "120% en 15 minutos"}
        ])
        monitoring = cfg.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', monitoring.get('max_monitor_time_min', 30)))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', monitoring.get('dump_threshold_percent', -50)))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', monitoring.get('price_poll_interval_sec', 5)))
        self.MAX_CONCURRENT_MONITORS = int(os.getenv('MAX_CONCURRENT_MONITORS', monitoring.get('max_concurrent_monitors', 40)))
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', cfg.get('health_port', 8080)))

# --------------------------
# Logging (Railway-friendly: stdout only)
# --------------------------

def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# --------------------------
# DATA MODELS
# --------------------------

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

# --------------------------
# Metrics
# --------------------------

MET_TOKENS_MONITORED = Gauge("pump_bot_tokens_monitored", "Number of tokens currently monitored")
MET_ALERTS = Counter("pump_bot_alerts_total", "Total alerts sent")

# --------------------------
# IDL excerpt (pump.fun) — you provided earlier; embed address & BondingCurve layout
# --------------------------
PUMP_IDL = {
  "version":"0.1.0",
  "name":"pump",
  "metadata":{"address":"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"},
  # ... instructions/events/accounts truncated for brevity in runtime use
}

PUMP_PROGRAM_ID = PUMP_IDL["metadata"]["address"]

# BondingCurve layout per IDL:
# virtualTokenReserves: u64
# virtualSolReserves: u64
# realTokenReserves: u64
# realSolReserves: u64
# tokenTotalSupply: u64
# complete: bool
# Anchor accounts have 8-byte discriminator at start; fields follow little-endian.
BONDING_CURVE_STRUCT_FMT = "<QQQQQ?"  # 5 x u64 + bool (note: '?' may map to 1 byte)

# --------------------------
# RPC Client (QuickNode/Helius fallback), plus getAccountInfo decode
# --------------------------

class RPCClient:
    def __init__(self, config: Config):
        self.config = config
        self.providers = [p for p in [config.QUICKNODE_RPC_URL, config.HELIUS_RPC_URL] if p]
        if not self.providers:
            # fallback public
            self.providers = ["https://api.mainnet-beta.solana.com"]
        self.idx = 0
        self.session = aiohttp.ClientSession()

    def _current(self):
        return self.providers[self.idx % len(self.providers)]

    async def call(self, method: str, params: list, timeout: int = 10) -> Any:
        url = self._current()
        payload = {"jsonrpc":"2.0","id":1,"method":method,"params":params}
        try:
            async with self.session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "error" in data:
                        raise Exception(data["error"])
                    return data.get("result")
                else:
                    text = await resp.text()
                    raise Exception(f"RPC status {resp.status}: {text[:200]}")
        except Exception as e:
            logging.warning(f"RPC call {method} failed on {url}: {e}")
            # rotate provider and retry once
            self.idx += 1
            if self.idx < len(self.providers) * 3:
                await asyncio.sleep(0.3)
                return await self.call(method, params, timeout)
            raise

    async def get_account_info_base64(self, addr: str) -> Optional[bytes]:
        # getAccountInfo expects pubkey and encoding param in some RPC providers (for solana core).
        # Using generic form that most providers accept:
        try:
            res = await self.call("getAccountInfo", [addr, {"encoding":"base64"}])
            if res and res.get("value") and res["value"].get("data"):
                b64 = res["value"]["data"][0]
                raw = base64.b64decode(b64)
                return raw
            return None
        except Exception as e:
            logging.debug(f"getAccountInfo failed for {addr}: {e}")
            return None

    async def close(self):
        await self.session.close()

# --------------------------
# Bonding curve parser (simple, robust)
# --------------------------

def parse_bonding_curve_account(raw: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse the bonding curve account bytes according to the IDL layout.
    Skip the 8-byte Anchor discriminator if present.
    """
    try:
        if not raw or len(raw) < 8:
            return None
        # Heuristic: if length >= 8 + size of struct
        struct_size = struct.calcsize(BONDING_CURVE_STRUCT_FMT)
        # Try with discriminator skip
        start = 8
        if len(raw) >= start + struct_size:
            fields = struct.unpack_from(BONDING_CURVE_STRUCT_FMT, raw, start)
            return {
                "virtualTokenReserves": int(fields[0]),
                "virtualSolReserves": int(fields[1]),
                "realTokenReserves": int(fields[2]),
                "realSolReserves": int(fields[3]),
                "tokenTotalSupply": int(fields[4]),
                "complete": bool(fields[5])
            }
        # fallback: try from 0
        if len(raw) >= struct_size:
            fields = struct.unpack_from(BONDING_CURVE_STRUCT_FMT, raw, 0)
            return {
                "virtualTokenReserves": int(fields[0]),
                "virtualSolReserves": int(fields[1]),
                "realTokenReserves": int(fields[2]),
                "realSolReserves": int(fields[3]),
                "tokenTotalSupply": int(fields[4]),
                "complete": bool(fields[5])
            }
    except Exception as e:
        logging.debug(f"Error parsing bonding curve bytes: {e}")
    return None

# Price extraction helper: estimate price from bonding curve reserves (approx)
def compute_price_from_curve(curve: Dict[str, Any]) -> Optional[float]:
    try:
        # Simplified: price = realSolReserves / realTokenReserves (guard against 0)
        rsol = curve.get("realSolReserves", 0)
        rtok = curve.get("realTokenReserves", 0)
        if rtok and rsol:
            return float(rsol) / float(rtok)
    except Exception as e:
        logging.debug(f"Error computing price from curve: {e}")
    return None

# --------------------------
# Database helper
# --------------------------

class Database:
    def __init__(self, config: Config):
        self.config = config
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not self.config.DATABASE_URL or not self.config.DATABASE_URL.strip():
            logging.info("Database disabled (DATABASE_URL not set)")
            return
        try:
            self.pool = await asyncpg.create_pool(self.config.DATABASE_URL, min_size=1, max_size=10)
            async with self.pool.acquire() as conn:
                # create tables if not exists
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS token_monitoring (
                    token_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    name TEXT,
                    initial_price NUMERIC,
                    initial_market_cap NUMERIC,
                    max_price NUMERIC,
                    start_time TIMESTAMPTZ,
                    last_checked TIMESTAMPTZ,
                    status TEXT,
                    metadata JSONB,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )""")
                await conn.execute("""
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
                )""")
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS token_price_history (
                    id SERIAL PRIMARY KEY,
                    token_address TEXT,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    price NUMERIC,
                    market_cap NUMERIC,
                    volume_24h NUMERIC
                )""")
            logging.info("Database connected and schema ensured")
        except Exception as e:
            logging.error(f"Database connection failed: {e}")
            self.pool = None

    async def close(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def add_or_update_token(self, token: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                INSERT INTO token_monitoring (token_address, symbol, name, initial_price, initial_market_cap, max_price, start_time, last_checked, status, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8,$9)
                ON CONFLICT (token_address) DO UPDATE SET
                  last_checked = NOW(),
                  max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price),
                  status = EXCLUDED.status
                """,
                token.mint, token.symbol, token.name,
                token.initial_price, token.initial_market_cap, token.max_price, token.start_time, token.status, json.dumps(token.metadata))
        except Exception as e:
            logging.debug(f"DB add/update token error: {e}")

    async def record_price(self, token_address: str, price: float, market_cap: float, volume: float):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                INSERT INTO token_price_history (token_address, price, market_cap, volume_24h) VALUES ($1,$2,$3,$4)
                """, token_address, price, market_cap, volume)
        except Exception as e:
            logging.debug(f"DB price history error: {e}")

    async def record_alert(self, alert: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("""
                INSERT INTO token_alerts (token_address, alert_rule_name, gain_percent, time_elapsed_min, price_at_alert, market_cap_at_alert, extra_data)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                alert.token_address, alert.rule_name, alert.gain_percent, alert.time_elapsed_min, alert.price_at_alert, alert.market_cap_at_alert, json.dumps(alert.extra_data))
        except Exception as e:
            logging.debug(f"DB record alert error: {e}")

# --------------------------
# Alert Engine
# --------------------------

class AlertEngine:
    def __init__(self, config: Config):
        self.config = config
        self.alert_rules = config.ALERT_RULES

    async def evaluate(self, token: TokenData, current_price: float) -> List[AlertData]:
        if current_price <= 0 or token.initial_price <= 0:
            return []
        gain_percent = ((current_price - token.initial_price) / token.initial_price) * 100.0
        elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
        triggered = []
        for rule in self.alert_rules:
            if gain_percent >= float(rule["alert_percent"]) and elapsed_min <= float(rule["time_window_min"]):
                triggered.append(AlertData(
                    token_address=token.mint,
                    rule_name=rule["name"],
                    gain_percent=gain_percent,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=0.0,
                    extra_data={"symbol": token.symbol, "name": token.name, "initial_price": token.initial_price, "max_price": token.max_price}
                ))
        return triggered

# --------------------------
# Token Manager
# --------------------------

class TokenManager:
    def __init__(self, config: Config, db: Database, rpc: RPCClient, alert_engine: AlertEngine, telegram_notify):
        self.config = config
        self.db = db
        self.rpc = rpc
        self.alert_engine = alert_engine
        self.telegram_notify = telegram_notify

        self.monitored: Dict[str, TokenData] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(self.config.MAX_CONCURRENT_MONITORS)
        self.sent_alerts = set()  # dedupe

    async def add_token(self, mint: str, symbol: str, name: str, initial_price: float = 0.0, initial_market_cap: float = 0.0):
        if mint in self.monitored:
            logging.debug(f"Already monitoring {mint}")
            return
        token = TokenData(
            mint=mint, symbol=symbol, name=name,
            initial_price=initial_price, initial_market_cap=initial_market_cap,
            max_price=initial_price,
            start_time=datetime.now(timezone.utc)
        )
        self.monitored[mint] = token
        await self.db.add_or_update_token(token)
        task = asyncio.create_task(self._monitor(mint))
        self.tasks[mint] = task
        MET_TOKENS_MONITORED.set(len(self.monitored))
        logging.info(f"Started monitoring {symbol} ({mint[:8]}...)")

    async def _monitor(self, mint: str):
        token = self.monitored.get(mint)
        if not token:
            return
        try:
            async with self.semaphore:
                while mint in self.monitored:
                    # timeout enforcement
                    elapsed_min = (datetime.now(timezone.utc) - token.start_time).total_seconds() / 60.0
                    if elapsed_min >= self.config.MAX_MONITOR_TIME_MIN:
                        logging.info(f"Timeout: removing {mint} after {elapsed_min:.1f} min")
                        await self._remove(mint, "timeout")
                        break

                    # get bonding curve account info - attempt to find bondingCurve from metadata stored?
                    # For PumpPortal messages we expect to receive bondingCurve in payload; here we will try to resolve via RPC if needed.
                    # We'll call getAccountInfo on associated bonding curve if present in metadata
                    bonding_curve_addr = token.metadata.get("bondingCurve") if token.metadata else None
                    raw = None
                    if bonding_curve_addr:
                        raw = await self.rpc.get_account_info_base64(bonding_curve_addr)
                    # fallback: if no bonding curve, attempt to query DexScreener (not implemented here). We'll try on-chain only.
                    curve_state = parse_bonding_curve_account(raw) if raw else None
                    current_price = None
                    if curve_state:
                        current_price = compute_price_from_curve(curve_state)
                    # if could not fetch price on-chain, skip and wait
                    if current_price is None:
                        await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
                        continue

                    # update max price
                    if current_price > token.max_price:
                        token.max_price = current_price
                        await self.db.add_or_update_token(token)

                    # record history
                    await self.db.record_price(mint, current_price, 0.0, 0.0)

                    # check dump from max
                    if token.max_price > 0:
                        loss_from_max = ((current_price - token.max_price) / token.max_price) * 100.0
                        if loss_from_max <= self.config.DUMP_THRESHOLD_PERCENT:
                            logging.info(f"Dump detected for {token.symbol} {mint}: {loss_from_max:.1f}% -> removing")
                            await self._remove(mint, "dumped")
                            break

                    # evaluate alert rules
                    alerts = await self.alert_engine.evaluate(token, current_price)
                    for alert in alerts:
                        # dedupe: key by mint + rule
                        dedupe_key = f"{mint}:{alert.rule_name}"
                        if dedupe_key in self.sent_alerts:
                            continue
                        # store alert and notify
                        await self.db.record_alert(alert)
                        await self.telegram_notify(token, alert)
                        MET_ALERTS.inc()
                        self.sent_alerts.add(dedupe_key)
                        await self._remove(mint, "alert_sent")
                        break

                    await asyncio.sleep(self.config.PRICE_POLL_INTERVAL_SEC)
        except asyncio.CancelledError:
            logging.info(f"Monitor task cancelled for {mint}")
        except Exception as e:
            logging.error(f"Error in monitor for {mint}: {e}")
            await self._remove(mint, "error")

    async def _remove(self, mint: str, reason: str):
        token = self.monitored.pop(mint, None)
        if mint in self.tasks:
            t = self.tasks.pop(mint)
            t.cancel()
        MET_TOKENS_MONITORED.set(len(self.monitored))
        logging.info(f"Removed token {mint}: {reason}")
        if token:
            token.status = reason
            await self.db.add_or_update_token(token)

    async def stop_all(self):
        for mint in list(self.monitored.keys()):
            await self._remove(mint, "shutdown")

# --------------------------
# Telegram notifier and bot commands
# --------------------------

class TelegramBridge:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.app: Optional[Application] = None

    async def start(self):
        if not self.config.TELEGRAM_BOT_TOKEN:
            logging.info("Telegram disabled (TELEGRAM_BOT_TOKEN not set)")
            return
        self.app = Application.builder().token(self.config.TELEGRAM_BOT_TOKEN).build()
        # register handlers
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("tokens", self.cmd_tokens))
        # run background polling
        asyncio.create_task(self.app.run_polling())

    async def stop(self):
        if self.app:
            await self.app.stop()

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome = ("🤖 Pump.fun Monitor\n"
                   "/status - estado\n"
                   "/tokens - tokens en monitoreo\n")
        await update.message.reply_text(welcome)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        status = self.token_manager.monitored
        text = (f"Monitored tokens: {len(status)}\n"
                f"Alert rules: {len(self.config.ALERT_RULES)}\n"
                f"Poll interval: {self.config.PRICE_POLL_INTERVAL_SEC}s\n"
                f"Max monitor time: {self.config.MAX_MONITOR_TIME_MIN} min\n")
        await update.message.reply_text(text)

    async def cmd_tokens(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        items = list(self.token_manager.monitored.values())[:20]
        if not items:
            await update.message.reply_text("No tokens monitored")
            return
        txt = "Tokens:\n"
        for t in items:
            elapsed = (datetime.now(timezone.utc) - t.start_time).total_seconds() / 60.0
            txt += f"- {t.symbol} {t.mint[:16]}... {elapsed:.1f}min\n"
        await update.message.reply_text(txt)

    async def notify(self, token: TokenData, alert: AlertData):
        """Send Telegram notification to configured chat with direct links"""
        chat_id = self.config.TELEGRAM_CHAT_ID
        if not chat_id:
            logging.info("TELEGRAM_CHAT_ID not set; skipping notify")
            return
        try:
            msg = self.format_message(token, alert)
            keyboard = [
                [InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{token.mint}"),
                 InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{token.mint}")],
                [InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{token.mint}"),
                 InlineKeyboardButton("Birdeye", url=f"https://birdeye.so/token/{token.mint}?chain=solana")]
            ]
            from telegram import Bot
            bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
            await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
            logging.info(f"Telegram alert sent for {token.symbol}")
        except Exception as e:
            logging.error(f"Error sending Telegram message: {e}")

    def format_message(self, token: TokenData, alert: AlertData) -> str:
        return (
            f"🚨 *ALERTA PUMP* 🚨\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Mint:* `{token.mint}`\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f}min\n"
            f"*Precio en alerta:* {alert.price_at_alert:.9f} (unidad chain)\n\n"
            f"Links:\n"
            f"• Pump.fun: https://pump.fun/{token.mint}\n"
            f"• DexScreener: https://dexscreener.com/solana/{token.mint}\n"
            f"• RugCheck: https://rugcheck.xyz/tokens/{token.mint}\n"
            f"• Birdeye: https://birdeye.so/token/{token.mint}?chain=solana\n"
        )

# --------------------------
# WebSocket listener: PumpPortal (third-party stream) or fallback to logsSubscribe via RPC
# The example assumes PumpPortal WSS sends messages containing mint, symbol, name, bondingCurve.
# --------------------------

class WebSocketListener:
    def __init__(self, config: Config, token_manager: TokenManager):
        self.config = config
        self.token_manager = token_manager
        self.ws = None
        self.should_run = True
        self.reconnect_delay = 2

    async def run(self):
        uri = self.config.PUMPPORTAL_WSS
        logging.info(f"Connecting to PumpPortal WSS: {uri}")
        while self.should_run:
            try:
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    self.ws = ws
                    # attempt subscribe if API required
                    try:
                        await ws.send(json.dumps({"method":"subscribeNewToken"}))
                    except Exception:
                        pass
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                        except Exception:
                            logging.debug("Non-json message from websocket")
                            continue
                        await self._handle_message(data)
            except Exception as e:
                logging.error(f"WSS connection error: {e}")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    async def _handle_message(self, data: dict):
        # Robust parsing: payload may come nested
        try:
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            mint = payload.get("mint") or payload.get("mintAddress") or payload.get("tokenMint")
            symbol = payload.get("symbol") or payload.get("tokenSymbol") or payload.get("name") or "UNKNOWN"
            name = payload.get("name") or symbol
            bonding_curve = payload.get("bondingCurve") or payload.get("bonding_curve") or payload.get("associatedBondingCurve")
            # initial price if present (e.g., dexscreener pairs)
            initial_price = 0.0
            initial_mcap = 0.0
            # store bondingCurve in metadata for later on-chain reads
            metadata = {}
            if bonding_curve:
                metadata["bondingCurve"] = bonding_curve
            if mint:
                await self.token_manager.add_token(mint, symbol, name, initial_price, initial_mcap)
                # attach metadata (safer via DB update)
                if mint in self.token_manager.monitored:
                    self.token_manager.monitored[mint].metadata.update(metadata)
                    await self.token_manager.db.add_or_update_token(self.token_manager.monitored[mint])
        except Exception as e:
            logging.debug(f"Error processing incoming ws message: {e}")

    async def stop(self):
        self.should_run = False
        if self.ws:
            await self.ws.close()

# --------------------------
# FastAPI app (health + metrics + optional webhook endpoint)
# --------------------------

app = FastAPI()
CONFIG = Config()
setup_logging(CONFIG.LOG_LEVEL)
LOG = logging.getLogger("pumpfun")
LOG.info("Starting PumpFunBot...")

# Create global instances (will be initialised in main startup)
DB = Database(CONFIG)
RPC = RPCClient(CONFIG)
ALERT_ENGINE = AlertEngine(CONFIG)
TOKEN_MANAGER: Optional[TokenManager] = None
TELEGRAM_BRIDGE: Optional[TelegramBridge] = None
WS_LISTENER: Optional[WebSocketListener] = None

@app.on_event("startup")
async def startup_event():
    global DB, RPC, ALERT_ENGINE, TOKEN_MANAGER, TELEGRAM_BRIDGE, WS_LISTENER
    await DB.connect()
    ALERT_ENGINE = AlertEngine(CONFIG)
    TOKEN_MANAGER = TokenManager(CONFIG, DB, RPC, ALERT_ENGINE, lambda t,a: TELEGRAM_BRIDGE.notify(t,a) if TELEGRAM_BRIDGE else None)
    TELEGRAM_BRIDGE = TelegramBridge(CONFIG, TOKEN_MANAGER)
    await TELEGRAM_BRIDGE.start()
    WS_LISTENER = WebSocketListener(CONFIG, TOKEN_MANAGER)
    # start websocket listener task
    asyncio.create_task(WS_LISTENER.run())
    LOG.info("PumpFunBot started")

@app.on_event("shutdown")
async def shutdown_event():
    if WS_LISTENER:
        await WS_LISTENER.stop()
    if TOKEN_MANAGER:
        await TOKEN_MANAGER.stop_all()
    if TELEGRAM_BRIDGE:
        await TELEGRAM_BRIDGE.stop()
    await RPC.close()
    await DB.close()
    LOG.info("PumpFunBot stopped")

@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)  # type: ignore

# Optional webhook endpoint (if you prefer webhook POST from Telegram)
@app.post("/telegram")
async def telegram_webhook(request: Request):
    """If you want to use Telegram webhooks instead of polling, configure webhook to /telegram."""
    body = await request.json()
    # If you want to process updates via python-telegram-bot Application, you'd need to pass updates to it.
    # For simplicity, we'll just acknowledge. (We run polling by default.)
    return {"status": "ok"}

# --------------------------
# ENTRYPOINT
# --------------------------

async def main_loop():
    # keep alive
    while True:
        await asyncio.sleep(3600)

def run():
    # run uvicorn via Procfile; but when imported by uvicorn, FastAPI lifecycle runs.
    pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("telegram_bot_final:app", host="0.0.0.0", port=int(os.getenv("PORT", CONFIG.HEALTH_PORT)), log_level="info")
