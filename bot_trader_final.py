#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 SOLANA ELITE TRADING BOT - PRODUCTION READY
================================================
Bot de trading automático optimizado para Solana con:
- Jupiter V6 Swap API integration
- Jito Bundles para ejecución atómica
- Análisis de momentum multi-timeframe
- Risk management avanzado con trailing stops
- PostgreSQL para historial y analytics
- Telegram notifications
- Railway-optimized health checks

Autor: Trading Elite Team
Version: 3.0 (2025)
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
    TRADE_AMOUNT_SOL: float = float(os.getenv('TRADE_AMOUNT_SOL', '0.01'))
    SLIPPAGE_BPS: int = int(os.getenv('SLIPPAGE_BPS', '300'))
    
    # ═══ JITO BUNDLES ═══
    USE_JITO: bool = os.getenv('USE_JITO', 'true').lower() == 'true'
    JITO_TIP_LAMPORTS: int = int(os.getenv('JITO_TIP_LAMPORTS', '10000'))
    JITO_TIP_ACCOUNTS: List[str] = field(default_factory=lambda: [
        'HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe',
        'Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY',
        'ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49',
        'DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh',
        'ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt',
        '3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT',
        '96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5',
        'DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL'
    ])
    
    JITO_BLOCK_ENGINE_URLS: List[str] = field(default_factory=lambda: [
        'https://mainnet.block-engine.jito.wtf/api/v1/bundles',
        'https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/bundles',
        'https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles',
        'https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles',
        'https://tokyo.mainnet.block-engine.jito.wtf/api/v1/bundles'
    ])
    
    # ═══ JUPITER API ═══
    JUPITER_QUOTE_API: str = 'https://quote-api.jup.ag/v6/quote'
    JUPITER_SWAP_API: str = 'https://quote-api.jup.ag/v6/swap'
    JUPITER_PRICE_API: str = 'https://api.jup.ag/price/v2'
    JUPITER_TOKENS_API: str = 'https://lite-api.jup.ag/tokens/v2'
    
    # Categorías disponibles: toporganicscore, toptraded, toptrending
    JUPITER_SCAN_CATEGORY: str = os.getenv('JUPITER_SCAN_CATEGORY', 'toporganicscore')
    JUPITER_SCAN_INTERVAL: str = os.getenv('JUPITER_SCAN_INTERVAL', '5m')  # 5m, 1h, 6h, 24h
    
    # ═══ FILTROS DE SEÑALES ═══
    MIN_LIQUIDITY_USD: float = float(os.getenv('MIN_LIQUIDITY_USD', '10000'))
    MIN_VOLUME_24H_USD: float = float(os.getenv('MIN_VOLUME_24H_USD', '50000'))
    MIN_PRICE_CHANGE_5M: float = float(os.getenv('MIN_PRICE_CHANGE_5M', '3'))
    MIN_PRICE_CHANGE_1H: float = float(os.getenv('MIN_PRICE_CHANGE_1H', '5'))
    MAX_PRICE_CHANGE_1H: float = float(os.getenv('MAX_PRICE_CHANGE_1H', '100'))
    
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
    SCAN_INTERVAL_SEC: int = int(os.getenv('SCAN_INTERVAL_SEC', '30'))
    POSITION_CHECK_SEC: int = int(os.getenv('POSITION_CHECK_SEC', '10'))
    HEALTH_CHECK_SEC: int = int(os.getenv('HEALTH_CHECK_SEC', '60'))
    
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
            'api_errors': 0
        }
        
        self.last_trade_time = 0
        self.daily_reset_time = time.time()
        self.last_health_check = time.time()
        
        self.running = True

state = BotState()

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
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_stats (
                    id SERIAL PRIMARY KEY,
                    date DATE UNIQUE,
                    total_trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_pnl_percent NUMERIC(10, 4),
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
# JUPITER API
# ═══════════════════════════════════════════════════════════════

async def get_jupiter_quote(input_mint: str, output_mint: str, amount_lamports: int) -> Optional[dict]:
    """Obtener quote de Jupiter V6"""
    try:
        params = {
            'inputMint': input_mint,
            'outputMint': output_mint,
            'amount': str(amount_lamports),  # Convertir a string
            'slippageBps': str(config.SLIPPAGE_BPS),  # Convertir a string
            'onlyDirectRoutes': 'false',  # String en minúscula
            'maxAccounts': '64'  # String
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(config.JUPITER_QUOTE_API, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                
                # Log error details
                error_text = await resp.text()
                logger.warning(f"Jupiter quote error {resp.status}: {error_text[:200]}")
                state.stats['api_errors'] += 1
                return None
    except Exception as e:
        logger.error(f"Error get_jupiter_quote: {e}")
        state.stats['api_errors'] += 1
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
        
        async with aiohttp.ClientSession() as session:
            async with session.post(config.JUPITER_SWAP_API, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('swapTransaction')
                logger.error(f"Jupiter swap error: {resp.status}")
                return None
    except Exception as e:
        logger.error(f"Error get_jupiter_swap_tx: {e}")
        return None

async def get_token_price(mint: str) -> Optional[float]:
    """Obtener precio actual de token vía Jupiter"""
    try:
        url = f"{config.JUPITER_PRICE_API}?ids={mint}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if 'data' in data and mint in data['data']:
                        return float(data['data'][mint].get('price', 0))
        return None
    except Exception as e:
        logger.debug(f"Error get_token_price: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# JITO BUNDLES
# ═══════════════════════════════════════════════════════════════

async def send_jito_bundle(swap_tx_b64: str) -> Optional[str]:
    """Enviar bundle a Jito Block Engine"""
    if not config.USE_JITO:
        return None
    
    try:
        # Deserializar transacción
        tx_bytes = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        
        # Firmar
        signature = state.wallet.sign_message(bytes(tx.message))
        signed_tx = VersionedTransaction.populate(tx.message, [signature])
        
        # Serializar para envío
        signed_tx_b64 = base64.b64encode(bytes(signed_tx)).decode('utf-8')
        
        # Preparar bundle
        bundle_payload = {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'sendBundle',
            'params': [[signed_tx_b64]]
        }
        
        state.stats['jito_bundles_sent'] += 1
        
        # Intentar múltiples regiones
        for jito_url in config.JITO_BLOCK_ENGINE_URLS[:3]:
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
                                logger.info(f"✅ Jito bundle: {bundle_id}")
                                state.stats['jito_bundles_success'] += 1
                                return bundle_id
            except Exception as e:
                logger.debug(f"Jito region falló: {e}")
                continue
        
        logger.warning("⚠️ Todas las regiones Jito fallaron")
        return None
        
    except Exception as e:
        logger.error(f"Error send_jito_bundle: {e}")
        return None

async def execute_swap(input_mint: str, output_mint: str, amount_lamports: int) -> Optional[str]:
    """Ejecutar swap con Jupiter + Jito"""
    
    try:
        # 1. Obtener quote
        logger.debug(f"Requesting quote: {input_mint[:8]}... -> {output_mint[:8]}... ({amount_lamports} lamports)")
        
        quote = await get_jupiter_quote(input_mint, output_mint, amount_lamports)
        if not quote:
            logger.error("❌ No se pudo obtener quote")
            return None
        
        logger.debug(f"Quote received: inAmount={quote.get('inAmount')}, outAmount={quote.get('outAmount')}")
        
        # 2. Obtener transacción
        swap_tx = await get_jupiter_swap_tx(quote)
        if not swap_tx:
            logger.error("❌ No se pudo obtener swap transaction")
            return None
        
        logger.debug("Swap transaction received")
        
        # 3. DRY RUN check
        if config.DRY_RUN:
            logger.info("🧪 [DRY RUN] Swap simulado exitosamente")
            return f"dry-run-{int(time.time())}"
        
        # 4. Enviar con Jito o método estándar
        if config.USE_JITO:
            logger.debug("Attempting Jito bundle...")
            bundle_id = await send_jito_bundle(swap_tx)
            if bundle_id:
                return bundle_id
            logger.warning("⚠️ Jito falló, usando método estándar")
        
        # 5. Método estándar (fallback)
        logger.debug("Using standard transaction method...")
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
            logger.info(f"✅ Swap estándar: {sig}")
            return str(sig)
            
        except Exception as e:
            logger.error(f"❌ Error swap estándar: {e}")
            state.stats['rpc_errors'] += 1
            return None
            
    except Exception as e:
        logger.error(f"❌ Error general en execute_swap: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# TRADING LOGIC
# ═══════════════════════════════════════════════════════════════

async def scan_for_signals() -> List[TokenData]:
    """Escanear tokens buscando señales de trading usando Jupiter Tokens API V2"""
    try:
        # Construir URL dinámicamente
        category = config.JUPITER_SCAN_CATEGORY
        interval = config.JUPITER_SCAN_INTERVAL
        url = f"{config.JUPITER_TOKENS_API}/{category}/{interval}"
        params = {'limit': 100}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"Jupiter API error: {resp.status}")
                    state.stats['api_errors'] += 1
                    return []
                
                tokens = await resp.json()
                
                if not isinstance(tokens, list):
                    logger.warning("Jupiter API response format unexpected")
                    return []
                
                candidates = []
                
                for token_data in tokens:
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
                        
                        # Stats 5m
                        stats_5m = token_data.get('stats5m', {})
                        price_change_5m = float(stats_5m.get('priceChange', 0) or 0)
                        buy_volume_5m = float(stats_5m.get('buyVolume', 0) or 0)
                        sell_volume_5m = float(stats_5m.get('sellVolume', 0) or 0)
                        volume_5m = buy_volume_5m + sell_volume_5m
                        
                        # Stats 1h
                        stats_1h = token_data.get('stats1h', {})
                        price_change_1h = float(stats_1h.get('priceChange', 0) or 0)
                        buy_volume_1h = float(stats_1h.get('buyVolume', 0) or 0)
                        sell_volume_1h = float(stats_1h.get('sellVolume', 0) or 0)
                        volume_1h = buy_volume_1h + sell_volume_1h
                        
                        # Stats 24h
                        stats_24h = token_data.get('stats24h', {})
                        price_change_24h = float(stats_24h.get('priceChange', 0) or 0)
                        buy_volume_24h = float(stats_24h.get('buyVolume', 0) or 0)
                        sell_volume_24h = float(stats_24h.get('sellVolume', 0) or 0)
                        volume_24h = buy_volume_24h + sell_volume_24h
                        
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
                            market_cap=market_cap
                        )
                        
                        candidates.append(token)
                        
                    except Exception as e:
                        logger.debug(f"Error parsing token: {e}")
                        continue
                
                if candidates:
                    logger.info(f"✅ Scanned {len(candidates)} tokens from Jupiter API")
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
    
    # Filtros básicos
    if token.liquidity < config.MIN_LIQUIDITY_USD:
        return False, 0
    
    if token.volume_24h < config.MIN_VOLUME_24H_USD:
        return False, 0
    
    # Señales de momentum
    if token.price_change_5m >= config.MIN_PRICE_CHANGE_5M:
        score += 30
    
    if token.price_change_1h >= config.MIN_PRICE_CHANGE_1H:
        score += 30
    
    # Evitar pumps excesivos
    if token.price_change_1h > config.MAX_PRICE_CHANGE_1H:
        return False, 0
    
    # Score mínimo para señal
    return score >= 40, score

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
        logger.debug(f"⏳ Cooldown activo ({15 - time_since_last:.0f}s restantes)")
        return
    
    logger.info(f"💰 Comprando {token.symbol} @ ${token.price_usd:.8f}")
    
    SOL_MINT = 'So11111111111111111111111111111111111111112'
    amount_lamports = int(config.TRADE_AMOUNT_SOL * 1e9)
    
    # Log detalles del trade
    logger.debug(f"Trade details: {token.mint} | Amount: {amount_lamports} lamports ({config.TRADE_AMOUNT_SOL} SOL)")
    
    # Ejecutar swap
    tx_sig = await execute_swap(SOL_MINT, token.mint, amount_lamports)
    
    if not tx_sig:
        logger.error(f"❌ Compra fallida: {token.symbol}")
        return
    
    # Crear posición
    position = Position(
        mint=token.mint,
        symbol=token.symbol,
        entry_price=token.price_usd,
        entry_time=time.time(),
        amount_sol=config.TRADE_AMOUNT_SOL,
        highest_price=token.price_usd,
        lowest_price=token.price_usd,
        entry_tx=tx_sig
    )
    
    state.positions[token.mint] = position
    state.stats['trades'] += 1
    state.stats['today_trades'] += 1
    state.last_trade_time = time.time()
    
    # Notificación
    message = (
        f"🟢 <b>COMPRA EJECUTADA</b>\n\n"
        f"<b>{token.name}</b> ({token.symbol})\n"
        f"💰 {config.TRADE_AMOUNT_SOL} SOL @ ${token.price_usd:.8f}\n"
        f"📊 Liq: ${token.liquidity:,.0f} | Vol24h: ${token.volume_24h:,.0f}\n"
        f"📈 5m: {token.price_change_5m:+.1f}% | 1h: {token.price_change_1h:+.1f}%\n"
        f"💵 MC: ${token.market_cap:,.0f}\n\n"
        f"{'🚀 Jito' if config.USE_JITO else '⚡ Standard'}\n"
        f"Tx: https://solscan.io/tx/{tx_sig}\n"
        f"Token: https://solscan.io/token/{token.mint}"
    )
    
    await send_telegram(message)
    logger.info(f"✅ Posición abierta: {token.symbol}")

async def sell_token(position: Position, current_price: float, reason: str, exit_tx: str = ''):
    """Ejecutar venta de token"""
    
    pnl = position.current_pnl(current_price)
    hold_time = position.hold_time_minutes()
    
    logger.info(f"💸 Vendiendo {position.symbol} - {reason} (P&L: {pnl:+.2f}%)")
    
    # Actualizar stats
    if pnl > 0:
        state.stats['wins'] += 1
    else:
        state.stats['losses'] += 1
    
    state.stats['total_pnl'] += pnl
    state.stats['today_pnl'] += pnl
    
    if pnl > state.stats['best_trade']:
        state.stats['best_trade'] = pnl
    if pnl < state.stats['worst_trade']:
        state.stats['worst_trade'] = pnl
    
    # Guardar en DB
    await log_trade_db(position, current_price, reason, exit_tx)
    
    # Eliminar posición
    del state.positions[position.mint]
    
    # Notificación
    emoji = '🟢' if pnl > 0 else '🔴'
    message = (
        f"{emoji} <b>VENTA EJECUTADA</b>\n\n"
        f"<b>{position.symbol}</b>\n"
        f"📊 P&L: <b>{pnl:+.2f}%</b>\n"
        f"💰 Entry: ${position.entry_price:.8f}\n"
        f"💰 Exit: ${current_price:.8f}\n"
        f"📈 Peak: ${position.highest_price:.8f} (+{position.peak_pnl:.1f}%)\n"
        f"⏱️ Hold: {hold_time:.1f} min\n"
        f"🎯 {reason}\n\n"
        f"Tx: https://solscan.io/tx/{exit_tx}"
    )
    
    await send_telegram(message)
    logger.info(f"✅ Venta completada: {position.symbol} ({pnl:+.2f}%)")

async def monitor_position(position: Position):
    """Monitorear y gestionar una posición abierta"""
    
    # Obtener precio actual
    current_price = await get_token_price(position.mint)
    
    if not current_price or current_price <= 0:
        logger.debug(f"⚠️ No se pudo obtener precio de {position.symbol}")
        return
    
    # Actualizar precio
    position.update_price(current_price)
    
    pnl = position.current_pnl(current_price)
    hold_time = position.hold_time_minutes()
    
    # ═══ EMERGENCY STOP LOSS ═══
    if pnl <= config.EMERGENCY_STOP_LOSS:
        logger.warning(f"🚨 EMERGENCY STOP: {position.symbol} ({pnl:.1f}%)")
        
        SOL_MINT = 'So11111111111111111111111111111111111111112'
        amount_lamports = int(position.amount_sol * 1e9)
        
        tx_sig = await execute_swap(position.mint, SOL_MINT, amount_lamports)
        await sell_token(position, current_price, f"⚠️ EMERGENCY STOP ({pnl:.1f}%)", tx_sig or '')
        return
    
    # ═══ STOP LOSS ═══
    if pnl <= config.STOP_LOSS_PERCENT:
        SOL_MINT = 'So11111111111111111111111111111111111111112'
        amount_lamports = int(position.amount_sol * 1e9)
        
        tx_sig = await execute_swap(position.mint, SOL_MINT, amount_lamports)
        await sell_token(position, current_price, f"Stop Loss ({pnl:.1f}%)", tx_sig or '')
        return
    
    # ═══ MAX HOLD TIME ═══
    if hold_time >= config.MAX_HOLD_TIME_MIN:
        SOL_MINT = 'So11111111111111111111111111111111111111112'
        amount_lamports = int(position.amount_sol * 1e9)
        
        tx_sig = await execute_swap(position.mint, SOL_MINT, amount_lamports)
        await sell_token(position, current_price, f"Max Hold ({hold_time:.0f}min, {pnl:+.1f}%)", tx_sig or '')
        return
    
    # ═══ TRAILING STOP ═══
    if not position.trailing_active and pnl >= config.TRAILING_ACTIVATION:
        position.trailing_active = True
        logger.info(f"🛡️ Trailing activado: {position.symbol} (+{pnl:.1f}%)")
        await send_telegram(
            f"🛡️ <b>Trailing Stop Activado</b>\n\n"
            f"{position.symbol}: +{pnl:.1f}%\n"
            f"Peak: ${position.highest_price:.8f}"
        )
    
    if position.trailing_active:
        trailing_stop_price = position.highest_price * (1 + config.TRAILING_PERCENT / 100)
        if current_price <= trailing_stop_price:
            SOL_MINT = 'So11111111111111111111111111111111111111112'
            amount_lamports = int(position.amount_sol * 1e9)
            
            tx_sig = await execute_swap(position.mint, SOL_MINT, amount_lamports)
            await sell_token(position, current_price, f"Trailing Stop ({pnl:.1f}%)", tx_sig or '')
            return
    
    # ═══ TAKE PROFITS ═══
    if pnl >= config.TAKE_PROFIT_3 and not position.tp3_taken:
        SOL_MINT = 'So11111111111111111111111111111111111111112'
        amount_lamports = int(position.amount_sol * 1e9)
        
        tx_sig = await execute_swap(position.mint, SOL_MINT, amount_lamports)
        await sell_token(position, current_price, f"TP3 ({pnl:.1f}%)", tx_sig or '')
        return
    
    if pnl >= config.TAKE_PROFIT_2 and not position.tp2_taken:
        logger.info(f"🎯 TP2 alcanzado: {position.symbol} (+{pnl:.1f}%)")
        position.tp2_taken = True
    
    if pnl >= config.TAKE_PROFIT_1 and not position.tp1_taken:
        logger.info(f"🎯 TP1 alcanzado: {position.symbol} (+{pnl:.1f}%)")
        position.tp1_taken = True

# ═══════════════════════════════════════════════════════════════
# LOOPS PRINCIPALES
# ═══════════════════════════════════════════════════════════════

async def scanner_loop():
    """Loop principal de escaneo"""
    logger.info("🔄 Scanner iniciado")
    
    while state.running:
        try:
            state.stats['scans'] += 1
            
            # Reset diario
            if time.time() - state.daily_reset_time > 86400:
                state.stats['today_trades'] = 0
                state.stats['today_pnl'] = 0
                state.daily_reset_time = time.time()
                await send_telegram("🔄 <b>Nuevo Día</b> - Stats reseteados")
            
            # Validar límites
            if len(state.positions) >= config.MAX_POSITIONS:
                logger.debug(f"⏸️ Límite de posiciones alcanzado ({config.MAX_POSITIONS})")
                await asyncio.sleep(config.SCAN_INTERVAL_SEC)
                continue
            
            if state.stats['today_pnl'] <= -config.MAX_LOSS_PER_DAY_PERCENT:
                await asyncio.sleep(config.SCAN_INTERVAL_SEC)
                continue
            
            # Escanear tokens
            tokens = await scan_for_signals()
            
            if not tokens:
                await asyncio.sleep(config.SCAN_INTERVAL_SEC)
                continue
            
            logger.info(f"📊 Analizando {len(tokens)} tokens...")
            
            # Buscar mejor señal
            best_token = None
            best_score = 0
            
            for token in tokens:
                if token.mint in state.positions:
                    continue
                
                has_signal, score = has_buy_signal(token)
                
                if has_signal and score > best_score:
                    best_token = token
                    best_score = score
            
            # Ejecutar trade si hay señal
            if best_token:
                state.stats['signals'] += 1
                logger.info(f"⚡ SEÑAL: {best_token.symbol} (Score: {best_score:.0f})")
                await buy_token(best_token)
            
            await asyncio.sleep(config.SCAN_INTERVAL_SEC)
            
        except Exception as e:
            logger.error(f"❌ Error scanner_loop: {e}", exc_info=True)
            state.stats['api_errors'] += 1
            await asyncio.sleep(10)

async def position_monitor_loop():
    """Loop de monitoreo de posiciones"""
    logger.info("🔄 Position monitor iniciado")
    
    while state.running:
        try:
            if state.positions:
                logger.debug(f"📊 Monitoreando {len(state.positions)} posiciones")
                
                for mint in list(state.positions.keys()):
                    if mint in state.positions:
                        await monitor_position(state.positions[mint])
                        await asyncio.sleep(2)
            
            await asyncio.sleep(config.POSITION_CHECK_SEC)
            
        except Exception as e:
            logger.error(f"❌ Error position_monitor_loop: {e}", exc_info=True)
            await asyncio.sleep(10)

async def stats_loop():
    """Loop de estadísticas y health checks"""
    logger.info("🔄 Stats loop iniciado")
    
    while state.running:
        await asyncio.sleep(config.HEALTH_CHECK_SEC)
        
        try:
            win_rate = (state.stats['wins'] / state.stats['trades'] * 100) if state.stats['trades'] > 0 else 0
            
            stats_msg = (
                f"📊 Scans: {state.stats['scans']} | "
                f"Señales: {state.stats['signals']} | "
                f"Trades: {state.stats['trades']} | "
                f"W/L: {state.stats['wins']}/{state.stats['losses']} ({win_rate:.1f}%) | "
                f"P&L: {state.stats['total_pnl']:+.2f}%"
            )
            
            logger.info(stats_msg)
            
            # Health check cada hora
            if time.time() - state.last_health_check > 3600:
                jito_rate = (state.stats['jito_bundles_success'] / state.stats['jito_bundles_sent'] * 100) if state.stats['jito_bundles_sent'] > 0 else 0
                
                await send_telegram(
                    f"💓 <b>Health Check</b>\n\n"
                    f"{stats_msg}\n"
                    f"Posiciones activas: {len(state.positions)}\n"
                    f"Jito success: {jito_rate:.1f}%\n"
                    f"RPC errors: {state.stats['rpc_errors']}\n"
                    f"API errors: {state.stats['api_errors']}"
                )
                
                state.last_health_check = time.time()
                
        except Exception as e:
            logger.error(f"❌ Error stats_loop: {e}")

# ═══════════════════════════════════════════════════════════════
# FASTAPI HEALTH SERVER
# ═══════════════════════════════════════════════════════════════

def create_health_app():
    """Crear app FastAPI para health checks (Railway)"""
    if not FASTAPI_AVAILABLE:
        return None
    
    app = FastAPI()
    
    @app.get("/")
    async def root():
        return {"status": "ok", "bot": "Solana Elite Trader"}
    
    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "running": state.running,
            "positions": len(state.positions),
            "trades_today": state.stats['today_trades'],
            "pnl_today": state.stats['today_pnl'],
            "total_trades": state.stats['trades'],
            "win_rate": (state.stats['wins'] / state.stats['trades'] * 100) if state.stats['trades'] > 0 else 0
        }
    
    @app.get("/stats")
    async def stats():
        return state.stats
    
    return app

async def run_health_server():
    """Ejecutar servidor de health checks"""
    if not FASTAPI_AVAILABLE:
        logger.warning("⚠️ FastAPI no disponible - health checks deshabilitados")
        return
    
    app = create_health_app()
    if not app:
        return
    
    config_uvicorn = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=config.HEALTH_PORT,
        log_level="warning"
    )
    
    server = uvicorn.Server(config_uvicorn)
    
    logger.info(f"✅ Health server en puerto {config.HEALTH_PORT}")
    
    await server.serve()

# ═══════════════════════════════════════════════════════════════
# INICIALIZACIÓN
# ═══════════════════════════════════════════════════════════════

async def initialize():
    """Inicializar bot"""
    logger.info("🚀 Iniciando Solana Elite Trading Bot...")
    logger.info(f"Mode: {config.MODE} | DRY_RUN: {config.DRY_RUN}")
    
    # Validar wallet
    if not config.PRIVATE_KEY:
        logger.error("❌ WALLET_PRIVATE_KEY no configurada")
        return False
    
    try:
        key_bytes = base58.b58decode(config.PRIVATE_KEY)
        state.wallet = Keypair.from_bytes(key_bytes)
        logger.info(f"✅ Wallet: {state.wallet.pubkey()}")
    except Exception as e:
        logger.error(f"❌ Error wallet: {e}")
        return False
    
    # Inicializar RPC client
    try:
        state.solana_client = AsyncClient(config.RPC_ENDPOINT)
        logger.info(f"✅ RPC: {config.RPC_ENDPOINT}")
    except Exception as e:
        logger.error(f"❌ Error RPC: {e}")
        return False
    
    # Inicializar database
    await init_database()
    
    # Inicializar Telegram
    if TELEGRAM_AVAILABLE and config.ENABLE_TELEGRAM and config.TELEGRAM_TOKEN:
        try:
            state.telegram_bot = Bot(token=config.TELEGRAM_TOKEN)
            await send_telegram(
                f"🚀 <b>Bot Iniciado</b>\n\n"
                f"Wallet: <code>{str(state.wallet.pubkey())[:8]}...</code>\n"
                f"DRY RUN: {'✅ SI' if config.DRY_RUN else '❌ NO'}\n"
                f"Trade Size: {config.TRADE_AMOUNT_SOL} SOL\n"
                f"Max Positions: {config.MAX_POSITIONS}\n"
                f"Jito: {'✅' if config.USE_JITO else '❌'}\n"
                f"Mode: {config.MODE}"
            )
            logger.info("✅ Telegram conectado")
        except Exception as e:
            logger.warning(f"⚠️ Telegram no disponible: {e}")
    
    logger.info("✅ Inicialización completa")
    return True

async def shutdown():
    """Cerrar recursos"""
    logger.info("🛑 Cerrando bot...")
    
    state.running = False
    
    # Cerrar posiciones abiertas
    if state.positions and not config.DRY_RUN:
        logger.info("⚠️ Cerrando posiciones abiertas...")
        for position in list(state.positions.values()):
            current_price = await get_token_price(position.mint)
            if current_price:
                SOL_MINT = 'So11111111111111111111111111111111111111112'
                amount_lamports = int(position.amount_sol * 1e9)
                tx_sig = await execute_swap(position.mint, SOL_MINT, amount_lamports)
                await sell_token(position, current_price, "Bot shutdown", tx_sig or '')
    
    # Cerrar conexiones
    if state.solana_client:
        await state.solana_client.close()
    
    if state.db_pool:
        await state.db_pool.close()
    
    await send_telegram("🛑 <b>Bot Detenido</b>")
    
    logger.info("✅ Shutdown completo")

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    """Función principal"""
    
    if not await initialize():
        logger.error("❌ Inicialización fallida")
        return
    
    try:
        # Crear tasks
        tasks = [
            asyncio.create_task(scanner_loop()),
            asyncio.create_task(position_monitor_loop()),
            asyncio.create_task(stats_loop())
        ]
        
        # Agregar health server si está disponible
        if FASTAPI_AVAILABLE:
            tasks.append(asyncio.create_task(run_health_server()))
        
        # Ejecutar todas las tasks
        await asyncio.gather(*tasks)
        
    except KeyboardInterrupt:
        logger.info("⚠️ Ctrl+C detectado")
    except Exception as e:
        logger.error(f"❌ Error fatal: {e}", exc_info=True)
        await send_telegram(f"🚨 <b>ERROR FATAL</b>\n\n{str(e)}")
    finally:
        await shutdown()

if __name__ == "__main__":
    try:
        # Validaciones iniciales
        if not config.PRIVATE_KEY:
            print("❌ ERROR: WALLET_PRIVATE_KEY no configurada")
            print("Configura la variable de entorno WALLET_PRIVATE_KEY")
            sys.exit(1)
        
        if not config.DRY_RUN:
            print("⚠️ ADVERTENCIA: DRY_RUN=false - Bot ejecutará trades reales")
            print("Presiona Ctrl+C en 5 segundos para cancelar...")
            time.sleep(5)
        
        # Iniciar bot
        asyncio.run(main())
        
    except KeyboardInterrupt:
        logger.info("👋 Bot terminado por usuario")
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}", exc_info=True)
        sys.exit(1)
