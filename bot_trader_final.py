#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 SOLANA ELITE TRADING BOT - PRODUCTION READY V2
================================================
Bot de trading automático optimizado para Solana con:
- Jupiter V6 Swap API integration (FIXED DNS)
- Jupiter Tokens API V2 (OPTIMIZED)
- Better error handling and retry logic
- Jito Bundles para ejecución atómica
- PostgreSQL para historial y analytics
- Telegram notifications

Version: 3.1 (2025) - Fixed connectivity issues
"""

import os
import sys
import json
import time
import asyncio
import logging
import base64
import base58
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from decimal import Decimal
from collections import deque

import aiohttp
import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders import message as solders_message
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Processed, Confirmed

# PostgreSQL
try:
    import asyncpg
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
    print("⚠️ asyncpg no instalado - PostgreSQL deshabilitado")

# Telegram
try:
    from telegram import Bot
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot no instalado")

# FastAPI para health checks
try:
    from fastapi import FastAPI
    import uvicorn
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    print("⚠️ FastAPI no instalado - health checks deshabilitados")

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    """Configuración centralizada del bot"""
    
    # ═══ WALLET & RPC ═══
    PRIVATE_KEY: str = os.getenv('WALLET_PRIVATE_KEY', '')
    RPC_ENDPOINT: str = os.getenv('RPC_ENDPOINT', 'https://api.mainnet-beta.solana.com')
    RPC_WS_ENDPOINT: str = os.getenv('RPC_WS_ENDPOINT', 'wss://api.mainnet-beta.solana.com')
    
    # ═══ DATABASE ═══
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    ENABLE_DB: bool = os.getenv('ENABLE_DB', 'true').lower() == 'true'
    
    # ═══ TELEGRAM ═══
    TELEGRAM_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')
    ENABLE_TELEGRAM: bool = os.getenv('ENABLE_TELEGRAM', 'true').lower() == 'true'
    
    # ═══ TRADING ═══
    DRY_RUN: bool = os.getenv('DRY_RUN', 'true').lower() == 'true'
    SIMULATION_MODE: bool = os.getenv('SIMULATION_MODE', 'false').lower() == 'true'  # Simular TODO
    TRADE_AMOUNT_SOL: float = float(os.getenv('TRADE_AMOUNT_SOL', '0.01'))
    SLIPPAGE_BPS: int = int(os.getenv('SLIPPAGE_BPS', '300'))
    
    # ═══ JITO BUNDLES ═══
    USE_JITO: bool = os.getenv('USE_JITO', 'false').lower() == 'true'  # Deshabilitado por defecto
    JITO_TIP_LAMPORTS: int = int(os.getenv('JITO_TIP_LAMPORTS', '10000'))
    
    # ═══ JUPITER API (FIXED URLS) ═══
    # Usando lite-api para mejor disponibilidad
    JUPITER_QUOTE_API: str = 'https://quote-api.jup.ag/v6/quote'
    JUPITER_SWAP_API: str = 'https://quote-api.jup.ag/v6/swap'
    JUPITER_PRICE_API: str = 'https://api.jup.ag/price/v2'  # API pública de precios
    JUPITER_TOKENS_API: str = 'https://lite-api.jup.ag/tokens/v2'  # API V2 optimizada
    
    # API Key para rate limits (opcional)
    JUPITER_API_KEY: str = os.getenv('JUPITER_API_KEY', '')
    
    # Categorías disponibles: toporganicscore, toptraded, toptrending
    JUPITER_SCAN_CATEGORY: str = os.getenv('JUPITER_SCAN_CATEGORY', 'toporganicscore')
    JUPITER_SCAN_INTERVAL: str = os.getenv('JUPITER_SCAN_INTERVAL', '5m')  # 5m, 1h, 6h, 24h
    
    # ═══ FILTROS DE SEÑALES (más conservadores) ═══
    MIN_LIQUIDITY_USD: float = float(os.getenv('MIN_LIQUIDITY_USD', '50000'))  # Aumentado
    MIN_VOLUME_24H_USD: float = float(os.getenv('MIN_VOLUME_24H_USD', '100000'))  # Aumentado
    MIN_PRICE_CHANGE_5M: float = float(os.getenv('MIN_PRICE_CHANGE_5M', '5'))  # Aumentado
    MIN_PRICE_CHANGE_1H: float = float(os.getenv('MIN_PRICE_CHANGE_1H', '8'))  # Aumentado
    MAX_PRICE_CHANGE_1H: float = float(os.getenv('MAX_PRICE_CHANGE_1H', '80'))
    MIN_ORGANIC_SCORE: float = float(os.getenv('MIN_ORGANIC_SCORE', '50'))  # Nuevo filtro
    
    # ═══ RISK MANAGEMENT ═══
    STOP_LOSS_PERCENT: float = float(os.getenv('STOP_LOSS_PERCENT', '-8'))
    TAKE_PROFIT_1: float = float(os.getenv('TAKE_PROFIT_1', '15'))
    TAKE_PROFIT_2: float = float(os.getenv('TAKE_PROFIT_2', '30'))
    TAKE_PROFIT_3: float = float(os.getenv('TAKE_PROFIT_3', '50'))
    TRAILING_ACTIVATION: float = float(os.getenv('TRAILING_ACTIVATION', '20'))
    TRAILING_PERCENT: float = float(os.getenv('TRAILING_PERCENT', '-5'))
    EMERGENCY_STOP_LOSS: float = float(os.getenv('EMERGENCY_STOP_LOSS', '-15'))
    MAX_LOSS_PER_DAY_PERCENT: float = float(os.getenv('MAX_LOSS_PER_DAY_PERCENT', '5'))
    
    # ═══ POSICIONES ═══
    MAX_POSITIONS: int = int(os.getenv('MAX_POSITIONS', '3'))
    MAX_HOLD_TIME_MIN: int = int(os.getenv('MAX_HOLD_TIME_MIN', '60'))
    MAX_DAILY_TRADES: int = int(os.getenv('MAX_DAILY_TRADES', '10'))
    
    # ═══ TIMING ═══
    SCAN_INTERVAL_SEC: int = int(os.getenv('SCAN_INTERVAL_SEC', '60'))  # Aumentado
    POSITION_CHECK_SEC: int = int(os.getenv('POSITION_CHECK_SEC', '15'))  # Aumentado
    HEALTH_CHECK_SEC: int = int(os.getenv('HEALTH_CHECK_SEC', '120'))
    
    # ═══ RETRY & TIMEOUT ═══
    MAX_RETRIES: int = int(os.getenv('MAX_RETRIES', '3'))
    API_TIMEOUT_SEC: int = int(os.getenv('API_TIMEOUT_SEC', '30'))
    RETRY_DELAY_SEC: int = int(os.getenv('RETRY_DELAY_SEC', '5'))
    
    # ═══ HEALTH & DEPLOYMENT ═══
    HEALTH_PORT: int = int(os.getenv('PORT', os.getenv('HEALTH_PORT', '8080')))
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')
    MODE: str = os.getenv('MODE', 'PROD')

config = Config()

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper()),
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# MODELOS
# ═══════════════════════════════════════════════════════════════

@dataclass
class TokenData:
    """Información de token para análisis"""
    mint: str
    symbol: str
    name: str
    price_usd: float
    liquidity: float
    volume_24h: float
    price_change_5m: float = 0
    price_change_1h: float = 0
    price_change_24h: float = 0
    market_cap: float = 0
    organic_score: float = 0
    is_verified: bool = False
    holder_count: int = 0
    first_seen: float = 0
    
    def __post_init__(self):
        if self.first_seen == 0:
            self.first_seen = time.time()

@dataclass
class Position:
    """Posición abierta de trading"""
    mint: str
    symbol: str
    entry_price: float
    entry_time: float
    amount_sol: float
    highest_price: float
    lowest_price: float
    entry_tx: str = ''
    trailing_active: bool = False
    tp1_taken: bool = False
    tp2_taken: bool = False
    tp3_taken: bool = False
    peak_pnl: float = 0
    
    def current_pnl(self, current_price: float) -> float:
        """Calcula P&L actual en porcentaje"""
        if self.entry_price <= 0:
            return 0
        return ((current_price - self.entry_price) / self.entry_price) * 100
    
    def hold_time_minutes(self) -> float:
        """Tiempo de tenencia en minutos"""
        return (time.time() - self.entry_time) / 60
    
    def update_price(self, price: float):
        """Actualiza precios máximo y mínimo"""
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
    """Estado global del bot"""
    
    def __init__(self):
        self.wallet: Optional[Keypair] = None
        self.solana_client: Optional[AsyncClient] = None
        self.telegram_bot: Optional[Bot] = None
        self.db_pool: Optional[Any] = None
        
        self.positions: Dict[str, Position] = {}
        self.watchlist: Dict[str, TokenData] = {}
        
        # Stats
        self.stats = {
            'scans': 0,
            'signals': 0,
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'total_pnl': 0.0,
            'today_trades': 0,
            'today_pnl': 0.0,
            'best_trade': 0.0,
            'worst_trade': 0.0,
            'jito_bundles_sent': 0,
            'jito_bundles_success': 0,
            'rpc_errors': 0,
            'api_errors': 0,
            'connectivity_issues': 0,
            'last_successful_api_call': 0
        }
        
        self.last_trade_time = 0
        self.daily_reset_time = time.time()
        self.last_health_check = time.time()
        
        self.running = True
        
        # Connection pool para mejor manejo de conexiones
        self.connector: Optional[aiohttp.TCPConnector] = None

state = BotState()

# ═══════════════════════════════════════════════════════════════
# HTTP CLIENT CON MEJOR MANEJO DE ERRORES
# ═══════════════════════════════════════════════════════════════

async def get_http_session() -> aiohttp.ClientSession:
    """Obtener sesión HTTP con configuración optimizada"""
    if not state.connector:
        state.connector = aiohttp.TCPConnector(
            limit=100,
            ttl_dns_cache=300,
            force_close=False,
            enable_cleanup_closed=True
        )
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        'Accept': 'application/json'
    }
    
    if config.JUPITER_API_KEY:
        headers['Authorization'] = f'Bearer {config.JUPITER_API_KEY}'
    
    timeout = aiohttp.ClientTimeout(
        total=config.API_TIMEOUT_SEC,
        connect=10,
        sock_read=20
    )
    
    return aiohttp.ClientSession(
        connector=state.connector,
        headers=headers,
        timeout=timeout
    )

async def api_call_with_retry(url: str, method: str = 'GET', **kwargs) -> Optional[dict]:
    """Realizar llamada API con retry automático y mejor error handling"""
    
    for attempt in range(config.MAX_RETRIES):
        try:
            session = await get_http_session()
            
            if method == 'GET':
                async with session.get(url, **kwargs) as resp:
                    if resp.status == 200:
                        state.stats['last_successful_api_call'] = time.time()
                        return await resp.json()
                    elif resp.status == 429:
                        logger.warning(f"⚠️ Rate limit hit, retry {attempt + 1}/{config.MAX_RETRIES}")
                        await asyncio.sleep(config.RETRY_DELAY_SEC * (attempt + 1))
                        continue
                    else:
                        error_text = await resp.text()
                        logger.error(f"API error {resp.status}: {error_text[:200]}")
                        
            elif method == 'POST':
                async with session.post(url, **kwargs) as resp:
                    if resp.status == 200:
                        state.stats['last_successful_api_call'] = time.time()
                        return await resp.json()
                    elif resp.status == 429:
                        logger.warning(f"⚠️ Rate limit hit, retry {attempt + 1}/{config.MAX_RETRIES}")
                        await asyncio.sleep(config.RETRY_DELAY_SEC * (attempt + 1))
                        continue
                    else:
                        error_text = await resp.text()
                        logger.error(f"API error {resp.status}: {error_text[:200]}")
            
            await session.close()
            
        except aiohttp.ClientConnectorError as e:
            logger.error(f"❌ Connection error (attempt {attempt + 1}/{config.MAX_RETRIES}): {e}")
            state.stats['connectivity_issues'] += 1
            
            if attempt < config.MAX_RETRIES - 1:
                await asyncio.sleep(config.RETRY_DELAY_SEC * (attempt + 1))
                continue
                
        except asyncio.TimeoutError:
            logger.error(f"⏱️ Timeout (attempt {attempt + 1}/{config.MAX_RETRIES})")
            
            if attempt < config.MAX_RETRIES - 1:
                await asyncio.sleep(config.RETRY_DELAY_SEC)
                continue
                
        except Exception as e:
            logger.error(f"❌ Unexpected error: {e}", exc_info=True)
            state.stats['api_errors'] += 1
            
            if attempt < config.MAX_RETRIES - 1:
                await asyncio.sleep(config.RETRY_DELAY_SEC)
                continue
    
    logger.error(f"❌ All retries failed for {url}")
    return None

# ═══════════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════════

async def init_database():
    """Inicializar PostgreSQL"""
    if not POSTGRES_AVAILABLE or not config.ENABLE_DB or not config.DATABASE_URL:
        logger.warning("⚠️ Database deshabilitada")
        return
    
    try:
        state.db_pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=60
        )
        
        async with state.db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    mint VARCHAR(44) NOT NULL,
                    symbol VARCHAR(20),
                    entry_price NUMERIC(20, 10),
                    exit_price NUMERIC(20, 10),
                    amount_sol NUMERIC(10, 4),
                    pnl_percent NUMERIC(10, 4),
                    hold_time_min NUMERIC(10, 2),
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    exit_reason VARCHAR(50),
                    entry_tx VARCHAR(88),
                    exit_tx VARCHAR(88),
                    used_jito BOOLEAN DEFAULT false,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
        logger.info("✅ Database inicializada")
        
    except Exception as e:
        logger.error(f"❌ Error database: {e}")
        state.db_pool = None

async def log_trade_db(position: Position, exit_price: float, exit_reason: str, exit_tx: str = ''):
    """Guardar trade en database"""
    if not state.db_pool:
        return
    
    try:
        pnl = position.current_pnl(exit_price)
        
        async with state.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO trades (
                    mint, symbol, entry_price, exit_price, amount_sol,
                    pnl_percent, hold_time_min, entry_time, exit_time,
                    exit_reason, entry_tx, exit_tx, used_jito
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ''',
                position.mint, position.symbol, position.entry_price, exit_price,
                position.amount_sol, pnl, position.hold_time_minutes(),
                datetime.fromtimestamp(position.entry_time), datetime.now(),
                exit_reason, position.entry_tx, exit_tx, config.USE_JITO
            )
    except Exception as e:
        logger.debug(f"Error log_trade_db: {e}")

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════

async def send_telegram(message: str):
    """Enviar notificación a Telegram"""
    if not TELEGRAM_AVAILABLE or not state.telegram_bot or not config.TELEGRAM_CHAT_ID:
        return
    
    try:
        await state.telegram_bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=message,
            parse_mode='HTML'
        )
    except Exception as e:
        logger.debug(f"Error Telegram: {e}")

# ═══════════════════════════════════════════════════════════════
# JUPITER API (OPTIMIZADO CON MEJOR ERROR HANDLING)
# ═══════════════════════════════════════════════════════════════

async def get_jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int) -> Optional[dict]:
    """Obtener quote de Jupiter V6 con retry automático"""
    try:
        params = {
            'inputMint': str(input_mint),
            'outputMint': str(output_mint),
            'amount': str(int(amount_lamports)),
            'slippageBps': str(int(config.SLIPPAGE_BPS))
        }
        
        logger.debug(f"Requesting Jupiter quote: {amount_lamports} lamports")
        
        result = await api_call_with_retry(config.JUPITER_QUOTE_API, params=params)
        
        if result:
            logger.debug(f"Quote OK: {result.get('inAmount')} -> {result.get('outAmount')}")
            return result
        
        return None
        
    except Exception as e:
        logger.error(f"Error get_jupiter_quote: {e}", exc_info=True)
        return None

async def get_jupiter_swap_tx(quote: dict) -> Optional[str]:
    """Obtener transacción de swap de Jupiter"""
    try:
        payload = {
            'quoteResponse': quote,
            'userPublicKey': str(state.wallet.pubkey()),
            'wrapAndUnwrapSol': True,
            'dynamicComputeUnitLimit': True,
            'prioritizationFeeLamports': 'auto'
        }
        
        result = await api_call_with_retry(
            config.JUPITER_SWAP_API,
            method='POST',
            json=payload
        )
        
        if result:
            return result.get('swapTransaction')
        
        return None
        
    except Exception as e:
        logger.error(f"Error get_jupiter_swap_tx: {e}")
        return None

async def get_token_price(mint: str) -> Optional[float]:
    """Obtener precio actual de token vía Jupiter Price API V2"""
    
    # ═══ SIMULATION MODE ═══
    if config.SIMULATION_MODE:
        # Simular variación de precio para tokens simulados
        if mint.startswith('SIM'):
            base_price = 0.0001
            variation = (hash(mint) % 20 - 10) / 100  # -10% a +10%
            return base_price * (1 + variation)
        return None
    
    # ═══ MODO REAL ═══
    try:
        url = f"{config.JUPITER_PRICE_API}?ids={mint}"
        
        result = await api_call_with_retry(url)
        
        if result and 'data' in result and mint in result['data']:
            price_data = result['data'][mint]
            if 'price' in price_data:
                return float(price_data['price'])
        
        return None
        
    except Exception as e:
        logger.debug(f"Error get_token_price: {e}")
        return None

async def get_multiple_token_prices(mints: List[str]) -> Dict[str, float]:
    """Obtener precios de múltiples tokens"""
    try:
        mints_batch = mints[:100]
        ids_param = ','.join(mints_batch)
        url = f"{config.JUPITER_PRICE_API}?ids={ids_param}"
        
        result = await api_call_with_retry(url)
        
        if result and 'data' in result:
            prices = {}
            for mint, price_data in result['data'].items():
                if 'price' in price_data:
                    prices[mint] = float(price_data['price'])
            return prices
        
        return {}
        
    except Exception as e:
        logger.debug(f"Error get_multiple_token_prices: {e}")
        return {}

# ═══════════════════════════════════════════════════════════════
# SWAP EXECUTION
# ═══════════════════════════════════════════════════════════════

async def execute_swap(input_mint: str, output_mint: str, amount_lamports: int) -> Optional[str]:
    """Ejecutar swap con Jupiter (con retry mejorado)"""
    
    # ═══ DRY RUN: SIMULACIÓN COMPLETA (SIN LLAMADAS A JUPITER) ═══
    if config.DRY_RUN:
        logger.info("🧪 [DRY RUN] Simulando swap sin llamadas reales a Jupiter")
        
        # Simular delay de red
        await asyncio.sleep(0.5)
        
        # Simular éxito con ID fake
        dry_run_id = f"dry-run-{int(time.time())}-{input_mint[:8]}"
        
        logger.info(f"✅ [DRY RUN] Swap simulado: {amount_lamports} lamports")
        
        await send_telegram(
            f"🧪 <b>[DRY RUN] Trade Simulado</b>\n\n"
            f"Input: {amount_lamports} lamports ({amount_lamports/1e9:.4f} SOL)\n"
            f"Input Mint: {input_mint[:8]}...\n"
            f"Output Mint: {output_mint[:8]}...\n"
            f"✅ Swap simulado exitosamente\n\n"
            f"<i>Nota: No se realizaron llamadas reales a Jupiter</i>"
        )
        
        return dry_run_id
    
    # ═══ MODO REAL: EJECUTAR SWAP REAL ═══
    logger.warning("⚠️ MODO REAL ACTIVADO - Ejecutando swap real")
    
    for attempt in range(config.MAX_RETRIES):
        try:
            if attempt > 0:
                logger.info(f"🔄 Retry {attempt + 1}/{config.MAX_RETRIES}")
                await asyncio.sleep(config.RETRY_DELAY_SEC)
            
            # 1. Obtener quote
            quote = await get_jupiter_quote(input_mint, output_mint, amount_lamports)
            if not quote:
                if attempt < config.MAX_RETRIES - 1:
                    continue
                logger.error("❌ No se pudo obtener quote después de reintentos")
                return None
            
            out_amount = quote.get('outAmount', 'unknown')
            logger.info(f"📊 Quote: {amount_lamports} lamports -> {out_amount} tokens")
            
            # 2. Obtener transacción
            swap_tx = await get_jupiter_swap_tx(quote)
            if not swap_tx:
                if attempt < config.MAX_RETRIES - 1:
                    continue
                logger.error("❌ No se pudo obtener swap transaction")
                return None
            
            # 3. Ejecutar transacción real
            logger.info("⚡ Ejecutando swap real...")
            try:
                tx_bytes = base64.b64decode(swap_tx)
                tx = VersionedTransaction.from_bytes(tx_bytes)
                
                signature = state.wallet.sign_message(bytes(tx.message))
                signed_tx = VersionedTransaction.populate(tx.message, [signature])
                
                result = await state.solana_client.send_raw_transaction(
                    bytes(signed_tx),
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Processed)
                )
                
                sig = result.value
                logger.info(f"✅ Swap real ejecutado: {sig}")
                return str(sig)
                
            except Exception as e:
                logger.error(f"❌ Error ejecutando swap: {e}")
                state.stats['rpc_errors'] += 1
                if attempt < config.MAX_RETRIES - 1:
                    continue
                return None
                
        except Exception as e:
            logger.error(f"❌ Error execute_swap (attempt {attempt + 1}): {e}")
            if attempt < config.MAX_RETRIES - 1:
                continue
            return None
    
    return None

# ═══════════════════════════════════════════════════════════════
# TRADING LOGIC (OPTIMIZADO CON API V2)
# ═══════════════════════════════════════════════════════════════

async def scan_for_signals() -> List[TokenData]:
    """Escanear tokens usando Jupiter Tokens API V2"""
    
    # ═══ SIMULATION MODE: Generar datos fake para testing ═══
    if config.SIMULATION_MODE:
        logger.info("🧪 [SIMULATION MODE] Generando tokens simulados")
        
        fake_tokens = []
        for i in range(5):
            fake_token = TokenData(
                mint=f"SIM{i}111111111111111111111111111111111111",
                symbol=f"SIM{i}",
                name=f"Simulated Token {i}",
                price_usd=0.0001 + (i * 0.0001),
                liquidity=100000 + (i * 50000),
                volume_24h=200000 + (i * 100000),
                price_change_5m=5 + (i * 2),
                price_change_1h=8 + (i * 3),
                price_change_24h=15 + (i * 5),
                market_cap=500000 + (i * 250000),
                organic_score=60 + (i * 5),
                is_verified=i % 2 == 0,
                holder_count=1000 + (i * 500)
            )
            fake_tokens.append(fake_token)
        
        logger.info(f"✅ [SIMULATION] Generados {len(fake_tokens)} tokens simulados")
        return fake_tokens
    
    # ═══ MODO REAL: Consultar Jupiter API ═══
    try:
        category = config.JUPITER_SCAN_CATEGORY
        interval = config.JUPITER_SCAN_INTERVAL
        url = f"{config.JUPITER_TOKENS_API}/{category}/{interval}"
        
        params = {'limit': 100}
        
        result = await api_call_with_retry(url, params=params)
        
        if not result or not isinstance(result, list):
            logger.warning("Jupiter API response format unexpected")
            return []
        
        candidates = []
        
        for token_data in result:
            try:
                mint = token_data.get('id')
                
                if not mint or mint in state.positions:
                    continue
                
                # Extraer datos del token
                symbol = token_data.get('symbol', 'UNKNOWN')
                name = token_data.get('name', 'Unknown')
                price_usd = float(token_data.get('usdPrice', 0) or 0)
                liquidity = float(token_data.get('liquidity', 0) or 0)
                market_cap = float(token_data.get('mcap', 0) or 0)
                organic_score = float(token_data.get('organicScore', 0) or 0)
                is_verified = token_data.get('isVerified', False)
                holder_count = int(token_data.get('holderCount', 0) or 0)
                
                # Stats por intervalo
                stats_5m = token_data.get('stats5m', {})
                price_change_5m = float(stats_5m.get('priceChange', 0) or 0)
                
                stats_1h = token_data.get('stats1h', {})
                price_change_1h = float(stats_1h.get('priceChange', 0) or 0)
                buy_volume_1h = float(stats_1h.get('buyVolume', 0) or 0)
                sell_volume_1h = float(stats_1h.get('sellVolume', 0) or 0)
                
                stats_24h = token_data.get('stats24h', {})
                price_change_24h = float(stats_24h.get('priceChange', 0) or 0)
                volume_24h = float(stats_24h.get('buyVolume', 0) or 0) + float(stats_24h.get('sellVolume', 0) or 0)
                
                # Validaciones básicas
                if price_usd <= 0:
                    continue
                
                # Crear objeto TokenData
                token = TokenData(
                    mint=mint,
                    symbol=symbol,
                    name=name,
                    price_usd=price_usd,
                    liquidity=liquidity,
                    volume_24h=volume_24h,
                    price_change_5m=price_change_5m,
                    price_change_1h=price_change_1h,
                    price_change_24h=price_change_24h,
                    market_cap=market_cap,
                    organic_score=organic_score,
                    is_verified=is_verified,
                    holder_count=holder_count
                )
                
                candidates.append(token)
                
            except Exception as e:
                logger.debug(f"Error parsing token: {e}")
                continue
        
        if candidates:
            logger.info(f"✅ Scanned {len(candidates)} tokens from Jupiter API V2")
        return candidates
        
    except Exception as e:
        logger.error(f"❌ Error scan_for_signals: {e}")
        state.stats['api_errors'] += 1
        return []

def has_buy_signal(token: TokenData) -> Tuple[bool, float]:
    """
    Evaluar si un token tiene señal de compra
    Returns: (tiene_señal, score)
    """
    score = 0
    
    # Filtros básicos CRÍTICOS
    if token.liquidity < config.MIN_LIQUIDITY_USD:
        return False, 0
    
    if token.volume_24h < config.MIN_VOLUME_24H_USD:
        return False, 0
    
    # Validar que tenga precio válido
    if token.price_usd <= 0:
        return False, 0
    
    # NUEVO: Filtro por organic score (calidad del token)
    if token.organic_score < config.MIN_ORGANIC_SCORE:
        logger.debug(f"{token.symbol}: Organic score too low ({token.organic_score:.1f})")
        return False, 0
    
    # Evitar tokens con liquidez muy baja vs volumen (posible pump dump)
    if token.volume_24h > 0 and token.liquidity > 0:
        volume_to_liq_ratio = token.volume_24h / token.liquidity
        if volume_to_liq_ratio > 50:
            logger.debug(f"{token.symbol}: Volume/Liq ratio too high ({volume_to_liq_ratio:.1f})")
            return False, 0
    
    # Señales de momentum (OBLIGATORIOS)
    if token.price_change_5m >= config.MIN_PRICE_CHANGE_5M:
        score += min(30, token.price_change_5m * 3)
    else:
        return False, 0
    
    if token.price_change_1h >= config.MIN_PRICE_CHANGE_1H:
        score += min(30, token.price_change_1h * 2)
    else:
        return False, 0
    
    # Evitar pumps excesivos
    if token.price_change_1h > config.MAX_PRICE_CHANGE_1H:
        logger.debug(f"{token.symbol}: Price change too high ({token.price_change_1h:.1f}%)")
        return False, 0
    
    # Bonus por buena liquidez
    if token.liquidity > config.MIN_LIQUIDITY_USD * 2:
        score += 10
    
    # Bonus por alto volumen
    if token.volume_24h > config.MIN_VOLUME_24H_USD * 2:
        score += 10
    
    # Bonus por tendencia 24h positiva
    if token.price_change_24h > 0:
        score += 10
    
    # NUEVO: Bonus por organic score alto
    if token.organic_score > 70:
        score += 15
    
    # NUEVO: Bonus por token verificado
    if token.is_verified:
        score += 10
    
    # Score mínimo para señal
    min_score = 60
    
    if score >= min_score:
        logger.info(f"🎯 {token.symbol}: BUY SIGNAL | Score: {score:.0f} | OS: {token.organic_score:.0f} | 5m: {token.price_change_5m:+.1f}% | 1h: {token.price_change_1h:+.1f}%")
    
    return score >= min_score, score

async def buy_token(token: TokenData):
    """Ejecutar compra de token"""
    
    # Validaciones pre-trade
    if state.stats['today_trades'] >= config.MAX_DAILY_TRADES:
        logger.warning(f"⏸️ Límite diario alcanzado")
        return
    
    if state.stats['today_pnl'] <= -config.MAX_LOSS_PER_DAY_PERCENT:
        logger.warning(f"🛑 Límite pérdida diaria alcanzado")
        await send_telegram("🛑 <b>TRADING PAUSADO</b>\n\nPérdida diaria excedida")
        return
    
    # Cooldown entre trades
    time_since_last = time.time() - state.last_trade_time
    if time_since_last < 15:
        logger.debug(f"⏳ Cooldown activo ({15 - time_since_last:.
