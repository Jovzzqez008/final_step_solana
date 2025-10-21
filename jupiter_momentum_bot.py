#!/usr/bin/env python3
"""
🚀 JUPITER ELITE MOMENTUM TRADING BOT
Estrategia Multi-Layer con Jito Bundles, Birdeye Analytics y PostgreSQL
Optimizado para Railway deployment

Autor: Trading Elite Team
Version: 2.0 (2025)
"""

import os
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from decimal import Decimal
from collections import deque

import aiohttp
import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.transaction import Transaction
from spl.token.instructions import get_associated_token_address
import base58

# PostgreSQL
import asyncpg
from asyncpg.pool import Pool

# Telegram
try:
    from telegram import Bot
    from telegram.error import TelegramError
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot no instalado")

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    # ═══ WALLET & RPC ═══
    PRIVATE_KEY: str = os.getenv('WALLET_PRIVATE_KEY', '')
    RPC_ENDPOINT: str = os.getenv('RPC_ENDPOINT', 'https://api.mainnet-beta.solana.com')
    
    # RPC Endpoints adicionales (fallback)
    RPC_BACKUP_1: str = os.getenv('RPC_BACKUP_1', '')
    RPC_BACKUP_2: str = os.getenv('RPC_BACKUP_2', '')
    
    # ═══ DATABASE ═══
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    
    # ═══ TELEGRAM ═══
    TELEGRAM_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')
    
    # ═══ TRADING ═══
    DRY_RUN: bool = os.getenv('DRY_RUN', 'true').lower() == 'true'
    TRADE_AMOUNT_SOL: float = float(os.getenv('TRADE_AMOUNT_SOL', '0.01'))
    SLIPPAGE_BPS: int = int(os.getenv('SLIPPAGE_BPS', '300'))
    
    # ═══ JITO BUNDLES ═══
    USE_JITO: bool = os.getenv('USE_JITO', 'true').lower() == 'true'
    JITO_TIP_LAMPORTS: int = int(os.getenv('JITO_TIP_LAMPORTS', '10000'))
    JITO_MAX_TIP_LAMPORTS: int = int(os.getenv('JITO_MAX_TIP_LAMPORTS', '100000'))
    JITO_TIP_ACCOUNT: str = os.getenv('JITO_TIP_ACCOUNT', 
        'Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY')
    
    # ═══ BIRDEYE API ═══
    BIRDEYE_API_KEY: str = os.getenv('BIRDEYE_API_KEY', '')
    USE_BIRDEYE: bool = os.getenv('USE_BIRDEYE', 'false').lower() == 'true'
    
    # ═══ FILTROS DE CALIDAD ═══
    MIN_ORGANIC_SCORE: float = float(os.getenv('MIN_ORGANIC_SCORE', '70'))
    MIN_LIQUIDITY_USD: float = float(os.getenv('MIN_LIQUIDITY_USD', '15000'))
    MIN_HOLDER_COUNT: int = int(os.getenv('MIN_HOLDER_COUNT', '150'))
    MIN_MARKET_CAP_USD: float = float(os.getenv('MIN_MARKET_CAP_USD', '50000'))
    MAX_MARKET_CAP_USD: float = float(os.getenv('MAX_MARKET_CAP_USD', '5000000'))
    
    # Filtros de seguridad (Birdeye)
    MIN_SECURITY_SCORE: int = int(os.getenv('MIN_SECURITY_SCORE', '80'))
    REQUIRE_MINT_DISABLED: bool = os.getenv('REQUIRE_MINT_DISABLED', 'true').lower() == 'true'
    REQUIRE_FREEZE_DISABLED: bool = os.getenv('REQUIRE_FREEZE_DISABLED', 'true').lower() == 'true'
    MAX_TOP_HOLDERS_PERCENT: float = float(os.getenv('MAX_TOP_HOLDERS_PERCENT', '30'))
    
    # ═══ SEÑALES DE MOMENTUM ═══
    MIN_PRICE_CHANGE_5M: float = float(os.getenv('MIN_PRICE_CHANGE_5M', '5'))
    MIN_VOLUME_5M_USD: float = float(os.getenv('MIN_VOLUME_5M_USD', '8000'))
    MIN_NET_BUYERS_5M: int = int(os.getenv('MIN_NET_BUYERS_5M', '75'))
    MIN_PRICE_CHANGE_1H: float = float(os.getenv('MIN_PRICE_CHANGE_1H', '8'))
    MIN_NET_BUYERS_1H: int = int(os.getenv('MIN_NET_BUYERS_1H', '150'))
    
    # Confirmación adicional
    MIN_ORGANIC_BUYERS_5M: int = int(os.getenv('MIN_ORGANIC_BUYERS_5M', '30'))
    MIN_TRADE_COUNT_1H: int = int(os.getenv('MIN_TRADE_COUNT_1H', '500'))
    
    # ═══ RISK MANAGEMENT ═══
    STOP_LOSS_PERCENT: float = float(os.getenv('STOP_LOSS_PERCENT', '-12'))
    TAKE_PROFIT_1: float = float(os.getenv('TAKE_PROFIT_1', '30'))
    TAKE_PROFIT_2: float = float(os.getenv('TAKE_PROFIT_2', '60'))
    TAKE_PROFIT_3: float = float(os.getenv('TAKE_PROFIT_3', '100'))
    TRAILING_ACTIVATION: float = float(os.getenv('TRAILING_ACTIVATION', '35'))
    TRAILING_PERCENT: float = float(os.getenv('TRAILING_PERCENT', '-8'))
    
    # Emergency stops
    EMERGENCY_STOP_LOSS: float = float(os.getenv('EMERGENCY_STOP_LOSS', '-25'))
    MAX_LOSS_PER_DAY_PERCENT: float = float(os.getenv('MAX_LOSS_PER_DAY_PERCENT', '10'))
    MAX_LOSS_PER_WEEK_PERCENT: float = float(os.getenv('MAX_LOSS_PER_WEEK_PERCENT', '20'))
    
    # ═══ POSICIONES ═══
    MAX_POSITIONS: int = int(os.getenv('MAX_POSITIONS', '3'))
    MAX_HOLD_TIME_MIN: int = int(os.getenv('MAX_HOLD_TIME_MIN', '120'))
    MAX_DAILY_TRADES: int = int(os.getenv('MAX_DAILY_TRADES', '12'))
    
    # Posición sizing
    POSITION_SIZE_MODE: str = os.getenv('POSITION_SIZE_MODE', 'fixed')
    KELLY_FRACTION: float = float(os.getenv('KELLY_FRACTION', '0.25'))
    
    # ═══ TIMING ═══
    SCAN_INTERVAL_SEC: int = int(os.getenv('SCAN_INTERVAL_SEC', '20'))
    POSITION_CHECK_SEC: int = int(os.getenv('POSITION_CHECK_SEC', '8'))
    HEALTH_CHECK_SEC: int = int(os.getenv('HEALTH_CHECK_SEC', '60'))
    
    # ═══ COMPUTE BUDGET ═══
    COMPUTE_UNIT_LIMIT: int = int(os.getenv('COMPUTE_UNIT_LIMIT', '200000'))
    COMPUTE_UNIT_PRICE_MICRO: int = int(os.getenv('COMPUTE_UNIT_PRICE_MICRO', '5000'))
    PRIORITY_FEE_AUTO: bool = os.getenv('PRIORITY_FEE_AUTO', 'true').lower() == 'true'
    
    # ═══ ADVANCED FEATURES ═══
    ENABLE_WHALE_TRACKING: bool = os.getenv('ENABLE_WHALE_TRACKING', 'false').lower() == 'true'
    ENABLE_COPY_TRADING: bool = os.getenv('ENABLE_COPY_TRADING', 'false').lower() == 'true'
    BLACKLIST_TOKENS: List[str] = field(default_factory=lambda: 
        os.getenv('BLACKLIST_TOKENS', '').split(',') if os.getenv('BLACKLIST_TOKENS') else [])

config = Config()

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# MODELOS
# ═══════════════════════════════════════════════════════════════

@dataclass
class TokenData:
    mint: str
    symbol: str
    name: str
    price_usd: float
    liquidity: float
    mcap: float
    holder_count: int
    organic_score: float
    
    # Stats 5m
    price_change_5m: float
    volume_5m: float
    net_buyers_5m: int
    organic_buyers_5m: int = 0
    
    # Stats 1h
    price_change_1h: float
    volume_1h: float
    net_buyers_1h: int
    trade_count_1h: int = 0
    
    # Stats 6h
    price_change_6h: float
    
    # Security (Birdeye)
    security_score: int = 0
    mint_disabled: bool = False
    freeze_disabled: bool = False
    top_holders_pct: float = 100
    
    # Metadata
    first_seen: float = 0
    scan_count: int = 0
    
    def __post_init__(self):
        if self.first_seen == 0:
            self.first_seen = time.time()

@dataclass
class Position:
    mint: str
    symbol: str
    entry_price: float
    entry_time: float
    amount_sol: float
    highest_price: float
    lowest_price: float
    trailing_active: bool = False
    tp1_taken: bool = False
    tp2_taken: bool = False
    tp3_taken: bool = False
    entry_tx: str = ''
    
    # Analytics
    total_pnl: float = 0
    realized_pnl: float = 0
    peak_pnl: float = 0
    
    def current_pnl(self, current_price: float) -> float:
        return ((current_price - self.entry_price) / self.entry_price) * 100
    
    def hold_time_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60
    
    def update_price(self, price: float):
        if price > self.highest_price:
            self.highest_price = price
        if price < self.lowest_price:
            self.lowest_price = price
        
        current_pnl = self.current_pnl(price)
        if current_pnl > self.peak_pnl:
            self.peak_pnl = current_pnl

# ═══════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.wallet: Optional[Keypair] = None
        self.solana_client: Optional[AsyncClient] = None
        self.telegram_bot: Optional[Bot] = None
        self.db_pool: Optional[Pool] = None
        
        self.positions: Dict[str, Position] = {}
        self.watchlist: Dict[str, TokenData] = {}
        self.recent_scans: deque = deque(maxlen=100)
        
        # Performance tracking
        self.stats = {
            'scans': 0,
            'signals': 0,
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'total_pnl': 0.0,
            'today_trades': 0,
            'today_pnl': 0.0,
            'week_pnl': 0.0,
            'best_trade': 0.0,
            'worst_trade': 0.0,
            'avg_hold_time': 0.0,
            'jito_bundles_sent': 0,
            'jito_bundles_success': 0
        }
        
        # Rate limiting
        self.last_trade_time = 0
        self.daily_reset_time = time.time()
        self.weekly_reset_time = time.time()
        
        # Health monitoring
        self.rpc_errors = 0
        self.api_errors = 0
        self.last_health_check = time.time()
        
        self.running = True

state = BotState()

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

async def init_database():
    """Inicializar PostgreSQL con schema"""
    if not config.DATABASE_URL:
        logger.warning("⚠️ DATABASE_URL no configurada - historial deshabilitado")
        return
    
    try:
        state.db_pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=60
        )
        
        async with state.db_pool.acquire() as conn:
            # Tabla de trades
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    mint VARCHAR(44) NOT NULL,
                    symbol VARCHAR(20),
                    entry_price NUMERIC(20, 10),
                    exit_price NUMERIC(20, 10),
                    amount_sol NUMERIC(10, 4),
                    pnl_percent NUMERIC(10, 4),
                    pnl_sol NUMERIC(10, 4),
                    hold_time_min NUMERIC(10, 2),
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    exit_reason VARCHAR(50),
                    entry_tx VARCHAR(88),
                    exit_tx VARCHAR(88),
                    used_jito BOOLEAN DEFAULT false,
                    jito_tip_lamports INTEGER,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Tabla de scans
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS scans (
                    id SERIAL PRIMARY KEY,
                    mint VARCHAR(44) NOT NULL,
                    symbol VARCHAR(20),
                    price_usd NUMERIC(20, 10),
                    liquidity NUMERIC(20, 2),
                    mcap NUMERIC(20, 2),
                    organic_score NUMERIC(5, 2),
                    price_change_5m NUMERIC(10, 4),
                    volume_5m NUMERIC(20, 2),
                    net_buyers_5m INTEGER,
                    signal_strength NUMERIC(5, 2),
                    passed_filters BOOLEAN,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Tabla de performance diaria
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_stats (
                    id SERIAL PRIMARY KEY,
                    date DATE UNIQUE,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_pnl_percent NUMERIC(10, 4),
                    total_pnl_sol NUMERIC(10, 4),
                    avg_hold_time NUMERIC(10, 2),
                    best_trade NUMERIC(10, 4),
                    worst_trade NUMERIC(10, 4),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            # Índices
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at)')
            
        logger.info("✅ Database inicializada")
        
    except Exception as e:
        logger.error(f"❌ Error inicializando database: {e}")
        state.db_pool = None

async def log_trade_to_db(position: Position, exit_price: float, exit_reason: str, exit_tx: str = ''):
    """Guardar trade en database"""
    if not state.db_pool:
        return
    
    try:
        pnl_percent = position.current_pnl(exit_price)
        pnl_sol = position.amount_sol * (pnl_percent / 100)
        
        async with state.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO trades (
                    mint, symbol, entry_price, exit_price, amount_sol,
                    pnl_percent, pnl_sol, hold_time_min,
                    entry_time, exit_time, exit_reason,
                    entry_tx, exit_tx, used_jito
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ''',
                position.mint, position.symbol, position.entry_price, exit_price,
                position.amount_sol, pnl_percent, pnl_sol, position.hold_time_minutes(),
                datetime.fromtimestamp(position.entry_time), datetime.now(),
                exit_reason, position.entry_tx, exit_tx, config.USE_JITO
            )
            
    except Exception as e:
        logger.error(f"Error logging trade: {e}")

async def log_scan_to_db(token: TokenData, signal_strength: float, passed: bool):
    """Guardar scan en database"""
    if not state.db_pool:
        return
    
    try:
        async with state.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO scans (
                    mint, symbol, price_usd, liquidity, mcap,
                    organic_score, price_change_5m, volume_5m,
                    net_buyers_5m, signal_strength, passed_filters
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ''',
                token.mint, token.symbol, token.price_usd, token.liquidity,
                token.mcap, token.organic_score, token.price_change_5m,
                token.volume_5m, token.net_buyers_5m, signal_strength, passed
            )
    except Exception as e:
        logger.debug(f"Error logging scan: {e}")

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════

async def send_telegram(message: str, parse_mode='HTML'):
    """Enviar notificación a Telegram con retry"""
    if not TELEGRAM_AVAILABLE or not state.telegram_bot:
        return
    
    for attempt in range(3):
        try:
            await state.telegram_bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode=parse_mode
            )
            return
        except Exception as e:
            if attempt == 2:
                logger.debug(f"Error Telegram: {e}")
            await asyncio.sleep(1)

# ═══════════════════════════════════════════════════════════════
# JUPITER API
# ═══════════════════════════════════════════════════════════════

async def fetch_top_tokens(category: str = 'toporganicscore', interval: str = '5m') -> List[dict]:
    """Obtener tokens top de Jupiter API v2"""
    url = f'https://lite-api.jup.ag/tokens/v2/{category}/{interval}?limit=50'
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Jupiter API error: {resp.status}")
                state.api_errors += 1
                return []
    except Exception as e:
        logger.error(f"Error fetching tokens: {e}")
        state.api_errors += 1
        return []

def parse_token_data(token_json: dict) -> Optional[TokenData]:
    """Parsear respuesta de Jupiter API a TokenData"""
    try:
        stats_5m = token_json.get('stats5m', {})
        stats_1h = token_json.get('stats1h', {})
        stats_6h = token_json.get('stats6h', {})
        audit = token_json.get('audit', {})
        
        return TokenData(
            mint=token_json['id'],
            symbol=token_json.get('symbol', 'UNKNOWN'),
            name=token_json.get('name', 'Unknown'),
            price_usd=token_json.get('usdPrice', 0),
            liquidity=token_json.get('liquidity', 0),
            mcap=token_json.get('mcap', 0),
            holder_count=token_json.get('holderCount', 0),
            organic_score=token_json.get('organicScore', 0),
            
            price_change_5m=stats_5m.get('priceChange', 0),
            volume_5m=(stats_5m.get('buyVolume', 0) + stats_5m.get('sellVolume', 0)),
            net_buyers_5m=stats_5m.get('numNetBuyers', 0),
            organic_buyers_5m=stats_5m.get('numOrganicBuyers', 0),
            
            price_change_1h=stats_1h.get('priceChange', 0),
            volume_1h=(stats_1h.get('buyVolume', 0) + stats_1h.get('sellVolume', 0)),
            net_buyers_1h=stats_1h.get('numNetBuyers', 0),
            trade_count_1h=stats_1h.get('numBuys', 0) + stats_1h.get('numSells', 0),
            
            price_change_6h=stats_6h.get('priceChange', 0),
            
            # Security
            mint_disabled=audit.get('mintAuthorityDisabled', False),
            freeze_disabled=audit.get('freezeAuthorityDisabled', False),
            top_holders_pct=audit.get('topHoldersPercentage', 100)
        )
    except Exception as e:
        logger.error(f"Error parsing token: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# BIRDEYE API (OPCIONAL)
# ═══════════════════════════════════════════════════════════════

async def fetch_birdeye_security(mint: str) -> Optional[dict]:
    """Obtener security check de Birdeye"""
    if not config.USE_BIRDEYE or not config.BIRDEYE_API_KEY:
        return None
    
    url = f'https://public-api.birdeye.so/defi/token_security?address={mint}'
    headers = {'X-API-KEY': config.BIRDEYE_API_KEY}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('data')
                return None
    except Exception as e:
        logger.debug(f"Birdeye error: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# JUPITER SWAP CON JITO
# ═══════════════════════════════════════════════════════════════

async def get_dynamic_priority_fee() -> int:
    """Calcular priority fee dinámicamente"""
    if not config.PRIORITY_FEE_AUTO:
        return config.COMPUTE_UNIT_PRICE_MICRO
    
    try:
        response = await state.solana_client.get_recent_prioritization_fees()
        if response and 'result' in response:
            fees = [f['prioritizationFee'] for f in response['result']]
            if fees:
                fees.sort()
                p75_fee = fees[int(len(fees) * 0.75)]
                return max(p75_fee, config.COMPUTE_UNIT_PRICE_MICRO)
    except:
        pass
    
    return config.COMPUTE_UNIT_PRICE_MICRO

async def get_quote(input_mint: str, output_mint: str, amount_lamports: int) -> Optional[dict]:
    """Obtener quote de Jupiter"""
    url = 'https://quote-api.jup.ag/v6/quote'
    params = {
        'inputMint': input_mint,
        'outputMint': output_mint,
        'amount': amount_lamports,
        'slippageBps': config.SLIPPAGE_BPS,
        'onlyDirectRoutes': False,
        'maxAccounts': 64
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Quote API error: {resp.status}")
                return None
    except Exception as e:
        logger.error(f"Error getting quote: {e}")
        return None

async def build_jito_bundle_transaction(quote: dict, tip_lamports: int = None) -> Optional[Transaction]:
    """Construir transacción con Jito bundle"""
    if not config.USE_JITO:
        return None
    
    try:
        url = 'https://quote-api.jup.ag/v6/swap'
        
        if tip_lamports is None:
            tip_lamports = config.JITO_TIP_LAMPORTS
        
        payload = {
            'quoteResponse': quote,
            'userPublicKey': str(state.wallet.pubkey()),
            'wrapAndUnwrapSol': True,
            'dynamicComputeUnitLimit': True,
            'prioritizationFeeLamports': 'auto'
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.error(f"Swap API error: {resp.status}")
                    return None
                    
                swap_data = await resp.json()
                
                tx_bytes = base58.b58decode(swap_data['swapTransaction'])
                tx = Transaction.deserialize(tx_bytes)
                
                tip_ix = transfer(
                    TransferParams(
                        from_pubkey=state.wallet.pubkey(),
                        to_pubkey=Pubkey.from_string(config.JITO_TIP_ACCOUNT),
                        lamports=tip_lamports
                    )
                )
                tx.add(tip_ix)
                
                priority_fee = await get_dynamic_priority_fee()
                tx.add(set_compute_unit_limit(config.COMPUTE_UNIT_LIMIT))
                tx.add(set_compute_unit_price(priority_fee))
                
                return tx
                
    except Exception as e:
        logger.error(f"Error building Jito bundle: {e}")
        return None

async def send_jito_bundle(tx: Transaction) -> Optional[str]:
    """Enviar bundle a Jito"""
    try:
        tx.sign(state.wallet)
        
        tx_bytes = bytes(tx.serialize())
        tx_b58 = base58.b58encode(tx_bytes).decode('utf-8')
        
        jito_urls = [
            'https://mainnet.block-engine.jito.wtf/api/v1/bundles',
            'https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles',
            'https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles',
            'https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles',
            'https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles'
        ]
        
        bundle_payload = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'sendBundle',
            'params': [[tx_b58]]
        }
        
        state.stats['jito_bundles_sent'] += 1
        
        for jito_url in jito_urls[:3]:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        jito_url, 
                        json=bundle_payload,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            if 'result' in result:
                                bundle_id = result['result']
                                logger.info(f"✅ Jito bundle enviado: {bundle_id}")
                                state.stats['jito_bundles_success'] += 1
                                return bundle_id
            except Exception as e:
                logger.debug(f"Jito region {jito_url} falló: {e}")
                continue
        
        logger.warning("⚠️ Todas las regiones Jito fallaron")
        return None
        
    except Exception as e:
        logger.error(f"Error enviando Jito bundle: {e}")
        return None

async def execute_swap(quote: dict, use_jito: bool = True) -> Optional[str]:
    """Ejecutar swap (con o sin Jito)"""
    if config.DRY_RUN:
        logger.info("🧪 [DRY RUN] Swap simulado")
        return f"dry-run-{int(time.time())}"
    
    if use_jito and config.USE_JITO:
        tx = await build_jito_bundle_transaction(quote)
        if tx:
            signature = await send_jito_bundle(tx)
            if signature:
                return signature
        
        logger.warning("⚠️ Jito falló, usando método estándar")
    
    try:
        url = 'https://quote-api.jup.ag/v6/swap'
        
        payload = {
            'quoteResponse': quote,
            'userPublicKey': str(state.wallet.pubkey()),
            'wrapAndUnwrapSol': True,
            'dynamicComputeUnitLimit': True,
            'prioritizationFeeLamports': 'auto'
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    swap_data = await resp.json()
                    
                    tx_bytes = base58.b58decode(swap_data['swapTransaction'])
                    tx = Transaction.deserialize(tx_bytes)
                    
                    priority_fee = await get_dynamic_priority_fee()
                    tx.add(set_compute_unit_limit(config.COMPUTE_UNIT_LIMIT))
                    tx.add(set_compute_unit_price(priority_fee))
                    
                    tx.sign(state.wallet)
                    
                    result = await state.solana_client.send_transaction(
                        tx,
                        opts={'skip_preflight': False, 'preflight_commitment': Confirmed}
                    )
                    
                    signature = result['result']
                    logger.info(f"✅ Swap estándar ejecutado: {signature}")
                    return signature
                else:
                    logger.error(f"Swap API error: {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"Error ejecutando swap estándar: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# TRADING LOGIC
# ═══════════════════════════════════════════════════════════════

def passes_quality_filters(token: TokenData) -> bool:
    """Filtros de calidad de token"""
    if token.mint in config.BLACKLIST_TOKENS:
        return False
    
    if token.organic_score < config.MIN_ORGANIC_SCORE:
        return False
    
    if token.liquidity < config.MIN_LIQUIDITY_USD:
        return False
    
    if token.holder_count < config.MIN_HOLDER_COUNT:
        return False
    
    if token.mcap < config.MIN_MARKET_CAP_USD:
        return False
    
    if token.mcap > config.MAX_MARKET_CAP_USD:
        return False
    
    if config.REQUIRE_MINT_DISABLED and not token.mint_disabled:
        return False
    
    if config.REQUIRE_FREEZE_DISABLED and not token.freeze_disabled:
        return False
    
    if token.top_holders_pct > config.MAX_TOP_HOLDERS_PERCENT:
        return False
    
    return True

def calculate_signal_strength(token: TokenData) -> float:
    """Calcular fuerza de señal (0-100)"""
    score = 0
    
    if token.price_change_5m >= config.MIN_PRICE_CHANGE_5M:
        score += min(30, (token.price_change_5m / config.MIN_PRICE_CHANGE_5M) * 15)
    
    if token.volume_5m >= config.MIN_VOLUME_5M_USD:
        score += min(20, (token.volume_5m / config.MIN_VOLUME_5M_USD) * 10)
    
    if token.net_buyers_5m >= config.MIN_NET_BUYERS_5M:
        score += min(20, (token.net_buyers_5m / config.MIN_NET_BUYERS_5M) * 10)
    
    if token.price_change_1h >= config.MIN_PRICE_CHANGE_1H:
        score += min(10, (token.price_change_1h / config.MIN_PRICE_CHANGE_1H) * 5)
    if token.net_buyers_1h >= config.MIN_NET_BUYERS_1H:
        score += 10
    
    score += min(10, (token.organic_score / 100) * 10)
    
    return min(100, score)

def has_momentum_signal(token: TokenData) -> Tuple[bool, float]:
    """Detectar señal de momentum"""
    signal_5m = (
        token.price_change_5m >= config.MIN_PRICE_CHANGE_5M and
        token.volume_5m >= config.MIN_VOLUME_5M_USD and
        token.net_buyers_5m >= config.MIN_NET_BUYERS_5M
    )
    
    if not signal_5m:
        return False, 0
    
    confirmation_1h = (
        token.price_change_1h >= config.MIN_PRICE_CHANGE_1H and
        token.net_buyers_1h >= config.MIN_NET_BUYERS_1H
    )
    
    if not confirmation_1h:
        return False, 0
    
    trend_6h_ok = token.price_change_6h >= 0
    organic_ok = token.organic_buyers_5m >= config.MIN_ORGANIC_BUYERS_5M
    trade_count_ok = token.trade_count_1h >= config.MIN_TRADE_COUNT_1H
    
    if not (trend_6h_ok and organic_ok and trade_count_ok):
        return False, 0
    
    strength = calculate_signal_strength(token)
    
    return strength >= 60, strength

async def buy_token(token: TokenData):
    """Comprar token"""
    if state.stats['today_trades'] >= config.MAX_DAILY_TRADES:
        logger.warning(f"⏸️ Límite diario alcanzado ({config.MAX_DAILY_TRADES})")
        return
    
    if state.stats['today_pnl'] <= -config.MAX_LOSS_PER_DAY_PERCENT:
        logger.warning(f"🛑 Límite de pérdida diaria alcanzado ({state.stats['today_pnl']:.2f}%)")
        await send_telegram(
            f"🛑 <b>TRADING PAUSADO</b>\n\n"
            f"Pérdida diaria: {state.stats['today_pnl']:.2f}%"
        )
        return
    
    if state.stats['week_pnl'] <= -config.MAX_LOSS_PER_WEEK_PERCENT:
        logger.warning(f"🛑 Límite de pérdida semanal alcanzado")
        return
    
    time_since_last = time.time() - state.last_trade_time
    if time_since_last < 15:
        logger.debug(f"⏳ Cooldown ({15 - time_since_last:.0f}s)")
        return
    
    logger.info(f"💰 Comprando {token.symbol} @ ${token.price_usd:.8f}")
    
    SOL_MINT = 'So11111111111111111111111111111111111111112'
    amount_lamports = int(config.TRADE_AMOUNT_SOL * 1e9)
    
    quote = await get_quote(SOL_MINT, token.mint, amount_lamports)
    if not quote:
        logger.error(f"❌ No se pudo obtener quote para {token.symbol}")
        return
    
    signature = await execute_swap(quote, use_jito=True)
    if not signature:
        logger.error(f"❌ Swap fallido para {token.symbol}")
        return
    
    position = Position(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=token.price_usd,
        entry_time=time.time(),
        amount_sol=config.TRADE_AMOUNT_SOL,
        highest_price=token.price_usd,
        lowest_price=token.price_usd,
        entry_tx=signature
    )
    
    state.positions[token.mint] = position
    state.stats['trades'] += 1
    state.stats['today_trades'] += 1
    state.last_trade_time = time.time()
    
    signal_strength = calculate_signal_strength(token)
    
    message = (
        f"🟢 <b>POSICIÓN ABIERTA</b>\n\n"
        f"<b>{token.name}</b> ({token.symbol})\n"
        f"💰 {config.TRADE_AMOUNT_SOL} SOL @ ${token.price_usd:.8f}\n\n"
        f"📊 <b>Señales:</b>\n"
        f"├ 5m: +{token.price_change_5m:.1f}% | {token.net_buyers_5m} buyers\n"
        f"├ 1h: +{token.price_change_1h:.1f}% | {token.net_buyers_1h} buyers\n"
        f"├ 6h: +{token.price_change_6h:.1f}%\n"
        f"└ Signal: <b>{signal_strength:.0f}/100</b>\n\n"
        f"🎯 Organic: {token.organic_score:.0f} | Holders: {token.holder_count:,}\n"
        f"💵 Liq: ${token.liquidity:,.0f} | MC: ${token.mcap:,.0f}\n\n"
        f"🔒 Security:\n"
        f"├ Mint: {'✅' if token.mint_disabled else '❌'}\n"
        f"├ Freeze: {'✅' if token.freeze_disabled else '❌'}\n"
        f"└ Top10: {token.top_holders_pct:.1f}%\n\n"
        f"{'🚀 Jito Bundle' if config.USE_JITO else '⚡ Standard TX'}\n"
        f"Tx: https://solscan.io/tx/{signature}"
    )
    
    await send_telegram(message)
    logger.info(f"✅ Posición abierta: {token.symbol}")

async def sell_token(position: Position, current_price: float, reason: str, percentage: int = 100):
    """Vender token"""
    logger.info(f"💸 Vendiendo {percentage}% de {position.symbol} - {reason}")
    
    SOL_MINT = 'So11111111111111111111111111111111111111112'
    
    amount_sol = position.amount_sol * (percentage / 100)
    amount_lamports = int(amount_sol * 1e9)
    
    quote = await get_quote(position.mint, SOL_MINT, amount_lamports)
    if not quote:
        logger.error(f"❌ No se pudo obtener quote de venta para {position.symbol}")
        return
    
    signature = await execute_swap(quote, use_jito=True)
    if not signature:
        logger.error(f"❌ Venta fallida para {position.symbol}")
        return
    
    pnl = position.current_pnl(current_price)
    hold_time = position.hold_time_minutes()
    pnl_sol = amount_sol * (pnl / 100)
    
    if percentage == 100:
        if pnl > 0:
            state.stats['wins'] += 1
        else:
            state.stats['losses'] += 1
        
        state.stats['total_pnl'] += pnl
        state.stats['today_pnl'] += pnl
        state.stats['week_pnl'] += pnl
        
        if pnl > state.stats['best_trade']:
            state.stats['best_trade'] = pnl
        if pnl < state.stats['worst_trade']:
            state.stats['worst_trade'] = pnl
        
        await log_trade_to_db(position, current_price, reason, signature)
        del state.positions[position.mint]
        
    else:
        position.amount_sol *= (100 - percentage) / 100
        position.realized_pnl += pnl_sol
        state.stats['today_pnl'] += pnl
        state.stats['week_pnl'] += pnl
    
    emoji = '🟢' if pnl > 0 else '🔴'
    message = (
        f"{emoji} <b>POSICIÓN {'CERRADA' if percentage == 100 else 'PARCIAL'}</b> ({percentage}%)\n\n"
        f"<b>{position.symbol}</b>\n"
        f"📊 P&L: <b>{pnl:+.2f}%</b> ({pnl_sol:+.4f} SOL)\n"
        f"💰 Entry: ${position.entry_price:.8f}\n"
        f"💰 Exit: ${current_price:.8f}\n"
        f"📈 Peak: ${position.highest_price:.8f} (+{position.peak_pnl:.1f}%)\n"
        f"⏱️ Hold: {hold_time:.1f} min\n"
        f"🎯 {reason}\n\n"
        f"{'🚀 Jito Bundle' if config.USE_JITO else '⚡ Standard TX'}\n"
        f"Tx: https://solscan.io/tx/{signature}"
    )
    
    await send_telegram(message)
    logger.info(f"✅ Venta ejecutada: {position.symbol} ({pnl:+.2f}%)")

async def monitor_position(position: Position):
    """Monitorear posición"""
    tokens = await fetch_top_tokens('toporganicscore', '5m')
    current_token = None
    
    for t in tokens:
        if t['id'] == position.mint:
            current_token = parse_token_data(t)
            break
    
    if not current_token:
        logger.warning(f"⚠️ No se encontró {position.symbol}")
        return
    
    current_price = current_token.price_usd
    pnl = position.current_pnl(current_price)
    hold_time = position.hold_time_minutes()
    
    position.update_price(current_price)
    
    if not position.trailing_active and pnl >= config.TRAILING_ACTIVATION:
        position.trailing_active = True
        logger.info(f"🛡️ Trailing activado para {position.symbol} (+{pnl:.1f}%)")
        await send_telegram(
            f"🛡️ <b>Trailing Stop Activado</b>\n\n"
            f"{position.symbol}: +{pnl:.1f}%\n"
            f"Peak: ${position.highest_price:.8f}"
        )
    
    if pnl <= config.EMERGENCY_STOP_LOSS:
        await sell_token(position, current_price, f"⚠️ EMERGENCY STOP ({pnl:.1f}%)")
        return
    
    if pnl <= config.STOP_LOSS_PERCENT:
        await sell_token(position, current_price, f"Stop Loss ({pnl:.1f}%)")
        return
    
    if hold_time >= config.MAX_HOLD_TIME_MIN:
        await sell_token(position, current_price, f"Max Hold Time ({hold_time:.0f}min, {pnl:+.1f}%)")
        return
    
    if position.trailing_active:
        trailing_stop_price = position.highest_price * (1 + config.TRAILING_PERCENT / 100)
        if current_price <= trailing_stop_price:
            await sell_token(position, current_price, f"Trailing Stop ({pnl:.1f}%)")
            return
    
    if pnl >= config.TAKE_PROFIT_1 and not position.tp1_taken:
        position.tp1_taken = True
        await sell_token(position, current_price, f"TP1 ({pnl:.1f}%)", percentage=33)
        return
    
    if pnl >= config.TAKE_PROFIT_2 and not position.tp2_taken and position.tp1_taken:
        position.tp2_taken = True
        await sell_token(position, current_price, f"TP2 ({pnl:.1f}%)", percentage=50)
        return
    
    if pnl >= config.TAKE_PROFIT_3 and not position.tp3_taken and position.tp2_taken:
        position.tp3_taken = True
        await sell_token(position, current_price, f"TP3 ({pnl:.1f}%)", percentage=100)
        return

# ═══════════════════════════════════════════════════════════════
# LOOPS PRINCIPALES
# ═══════════════════════════════════════════════════════════════

async def scan_loop():
    """Loop principal de escaneo"""
    logger.info("🔄 Scanner iniciado")
    
    while state.running:
        try:
            state.stats['scans'] += 1
            
            if time.time() - state.daily_reset_time > 86400:
                state.stats['today_trades'] = 0
                state.stats['today_pnl'] = 0
                state.daily_reset_time = time.time()
                await send_telegram("🔄 <b>Nuevo Día</b> - Stats reseteados")
            
            if time.time() - state.weekly_reset_time > 604800:
                state.stats['week_pnl'] = 0
                state.weekly_reset_time = time.time()
            
            if len(state.positions) >= config.MAX_POSITIONS:
                logger.debug(f"⏸️ Límite de posiciones ({config.MAX_POSITIONS})")
                await asyncio.sleep(config.SCAN_INTERVAL_SEC)
                continue
            
            if state.stats['today_pnl'] <= -config.MAX_LOSS_PER_DAY_PERCENT:
                await asyncio.sleep(config.SCAN_INTERVAL_SEC)
                continue
            
            tokens = await fetch_top_tokens('toporganicscore', '5m')
            
            if not tokens:
                logger.warning("⚠️ No se obtuvieron tokens")
                await asyncio.sleep(config.SCAN_INTERVAL_SEC)
                continue
            
            logger.info(f"📊 Escaneando {len(tokens)} tokens...")
            
            best_signal = None
            best_strength = 0
            
            for token_json in tokens:
                token = parse_token_data(token_json)
                
                if not token:
                    continue
                
                if token.mint in state.positions:
                    continue
                
                if not passes_quality_filters(token):
                    continue
                
                has_signal, strength = has_momentum_signal(token)
                
                await log_scan_to_db(token, strength, has_signal)
                
                if has_signal and strength > best_strength:
                    best_signal = token
                    best_strength = strength
            
            if best_signal:
                state.stats['signals'] += 1
                
                logger.info(
                    f"⚡ MEJOR SEÑAL: {best_signal.symbol} | "
                    f"Strength: {best_strength:.0f} | "
                    f"5m: +{best_signal.price_change_5m:.1f}% | "
                    f"1h: +{best_signal.price_change_1h:.1f}%"
                )
                
                await buy_token(best_signal)
            
            await asyncio.sleep(config.SCAN_INTERVAL_SEC)
            
        except Exception as e:
            logger.error(f"❌ Error en scan loop: {e}", exc_info=True)
            state.api_errors += 1
            await asyncio.sleep(10)

async def position_monitor_loop():
    """Loop de monitoreo de posiciones"""
    logger.info("🔄 Monitor de posiciones iniciado")
    
    while state.running:
        try:
            if state.positions:
                logger.debug(f"📊 Monitoreando {len(state.positions)} posiciones...")
                
                for mint in list(state.positions.keys()):
                    if mint in state.positions:
                        await monitor_position(state.positions[mint])
                        await asyncio.sleep(2)
            
            await asyncio.sleep(config.POSITION_CHECK_SEC)
            
        except Exception as e:
            logger.error(f"❌ Error en monitor loop: {e}", exc_info=True)
            await asyncio.sleep(10)

async def stats_loop():
    """Loop de estadísticas"""
    logger.info("🔄 Stats loop iniciado")
    
    while state.running:
        await asyncio.sleep(300)
        
        try:
            win_rate = (state.stats['wins'] / state.stats['trades'] * 100) if state.stats['trades'] > 0 else 0
            jito_rate = (state.stats['jito_bundles_success'] / state.stats['jito_bundles_sent'] * 100) if state.stats['jito_bundles_sent'] > 0 else 0
            
            stats_msg = (
                f"📊 Scans: {state.stats['scans']} | "
                f"Señales: {state.stats['signals']} | "
                f"Trades: {state.stats['trades']} | "
                f"W/L: {state.stats['wins']}/{state.stats['losses']} ({win_rate:.1f}%) | "
                f"P&L: {state.stats['total_pnl']:+.2f}%"
            )
            
            logger.info(stats_msg)
            
            if time.time() - state.last_health_check > 3600:
                await send_telegram(
                    f"💓 <b>Health Check</b>\n\n"
                    f"{stats_msg}\n"
                    f"RPC Errors: {state.rpc_errors}\n"
                    f"API Errors: {state.api_errors}\n"
                    f"Jito Success: {jito_rate:.1f}%\n"
                    f"Posiciones: {len(state.positions)}"
                )
                state.last_health_check = time.time()
                
        except Exception as e:
            logger.error(f"❌ Error en stats loop: {e}")

# ═══════════════════════════════════════════════════════════════
# INICIALIZACIÓN Y MAIN
# ═══════════════════════════════════════════════════════════════

async def initialize():
    """Inicializar bot"""
    logger.info("🚀 Iniciando Jupiter Elite Bot...")
    
    # Validar configuración
    if not config.PRIVATE_KEY:
        logger.error("❌ WALLET_PRIVATE_KEY no configurada")
        return False
    
    # Inicializar wallet
    try:
        key_bytes = base58.b58decode(config.PRIVATE_KEY)
        state.wallet = Keypair.from_bytes(key_bytes)
        logger.info(f"✅ Wallet: {state.wallet.pubkey()}")
    except Exception as e:
        logger.error(f"❌ Error inicializando wallet: {e}")
        return False
    
    # Inicializar RPC client
    state.solana_client = AsyncClient(config.RPC_ENDPOINT)
    logger.info(f"✅ RPC: {config.RPC_ENDPOINT}")
    
    # Inicializar database
    await init_database()
    
    # Inicializar Telegram
    if TELEGRAM_AVAILABLE and config.TELEGRAM_TOKEN:
        try:
            state.telegram_bot = Bot(token=config.TELEGRAM_TOKEN)
            await send_telegram(
                f"🚀 <b>Bot Iniciado</b>\n\n"
                f"Wallet: <code>{str(state.wallet.pubkey())[:8]}...</code>\n"
                f"DRY RUN: {'✅ SI' if config.DRY_RUN else '❌ NO'}\n"
                f"Trade Amount: {config.TRADE_AMOUNT_SOL} SOL\n"
                f"Max Positions: {config.MAX_POSITIONS}\n"
                f"Jito: {'✅' if config.USE_JITO else '❌'}"
            )
            logger.info("✅ Telegram bot conectado")
        except Exception as e:
            logger.warning(f"⚠️ Telegram no disponible: {e}")
    
    logger.info("✅ Inicialización completa")
    return True

async def shutdown():
    """Cerrar recursos"""
    logger.info("🛑 Cerrando bot...")
    
    state.running = False
    
    if state.solana_client:
        await state.solana_client.close()
    
    if state.db_pool:
        await state.db_pool.close()
    
    await send_telegram("🛑 <b>Bot Detenido</b>")
    
    logger.info("✅ Shutdown completo")

async def main():
    """Función principal"""
    if not await initialize():
        logger.error("❌ Inicialización fallida")
        return
    
    try:
        # Crear tasks
        tasks = [
            asyncio.create_task(scan_loop()),
            asyncio.create_task(position_monitor_loop()),
            asyncio.create_task(stats_loop())
        ]
        
        # Ejecutar hasta Ctrl+C
        await asyncio.gather(*tasks)
        
    except KeyboardInterrupt:
        logger.info("⚠️ Ctrl+C detectado")
    except Exception as e:
        logger.error(f"❌ Error fatal: {e}", exc_info=True)
    finally:
        await shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot terminado")
