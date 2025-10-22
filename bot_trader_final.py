#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 SOLANA ELITE TRADING BOT V5.0 - SHYFT INTEGRATION
====================================================
✅ Shyft API como principal (más confiable que Jupiter)
✅ Batch requests para posiciones (eficiente)
✅ Fallback inteligente: Shyft → DexScreener → Jupiter
✅ Machine Learning integrado
✅ Health Server para Railway
✅ PostgreSQL para histórico
✅ Telegram notifications
✅ Modo DRY_RUN completo

Version: 5.0 (2025) - Shyft Integration
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
import numpy as np
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Processed, Confirmed

# ML Libraries
try:
    from sklearn.ensemble import RandomForestClassifier
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False
    print("⚠️ scikit-learn no instalado - ML deshabilitado")

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

# ═══════════════════════════════════════════════════════════════
# CONFIGURACIÓN CON SHYFT API
# ═══════════════════════════════════════════════════════════════

@dataclass
class Config:
    """Configuración centralizada con Shyft API"""
    
    # ═══ WALLET & RPC ═══
    PRIVATE_KEY: str = os.getenv('WALLET_PRIVATE_KEY', '')
    RPC_ENDPOINT: str = os.getenv('RPC_ENDPOINT', 'https://api.mainnet-beta.solana.com')
    
    # ═══ DATABASE ═══
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    ENABLE_DB: bool = os.getenv('ENABLE_DB', 'true').lower() == 'true'
    
    # ═══ TELEGRAM ═══
    TELEGRAM_TOKEN: str = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID: str = os.getenv('TELEGRAM_CHAT_ID', '')
    ENABLE_TELEGRAM: bool = os.getenv('ENABLE_TELEGRAM', 'true').lower() == 'true'
    
    # ═══ TRADING ═══
    DRY_RUN: bool = os.getenv('DRY_RUN', 'true').lower() == 'true'
    SIMULATION_MODE: bool = os.getenv('SIMULATION_MODE', 'false').lower() == 'true'
    TRADE_AMOUNT_SOL: float = float(os.getenv('TRADE_AMOUNT_SOL', '0.01'))
    SLIPPAGE_BPS: int = int(os.getenv('SLIPPAGE_BPS', '300'))
    
    # ═══ 🟢 SHYFT API (PRINCIPAL) ═══
    SHYFT_API_KEY: str = os.getenv('SHYFT_API_KEY', '')
    SHYFT_BASE_URL: str = 'https://api.shyft.to/sol/v1'
    SHYFT_NETWORK: str = 'mainnet-beta'
    SHYFT_TOKEN_PRICE: str = f'{SHYFT_BASE_URL}/token/get_price'
    SHYFT_TOKEN_INFO: str = f'{SHYFT_BASE_URL}/token/get_info'
    SHYFT_MULTIPLE_PRICES: str = f'{SHYFT_BASE_URL}/token/get_multiple_prices'
    
    # ═══ FALLBACK APIs ═══
    DEXSCREENER_API: str = 'https://api.dexscreener.com/latest/dex'
    JUPITER_PRICE_API_V3: str = 'https://lite-api.jup.ag/price/v3'
    JUPITER_TOKENS_API: str = 'https://lite-api.jup.ag/tokens/v2'
    
    JUPITER_SCAN_CATEGORY: str = os.getenv('JUPITER_SCAN_CATEGORY', 'toporganicscore')
    JUPITER_SCAN_INTERVAL: str = os.getenv('JUPITER_SCAN_INTERVAL', '5m')
    
    # ═══ FILTROS DE SEÑALES ═══
    MIN_LIQUIDITY_USD: float = float(os.getenv('MIN_LIQUIDITY_USD', '50000'))
    MIN_VOLUME_24H_USD: float = float(os.getenv('MIN_VOLUME_24H_USD', '100000'))
    MIN_PRICE_CHANGE_5M: float = float(os.getenv('MIN_PRICE_CHANGE_5M', '5'))
    MIN_PRICE_CHANGE_1H: float = float(os.getenv('MIN_PRICE_CHANGE_1H', '8'))
    MAX_PRICE_CHANGE_1H: float = float(os.getenv('MAX_PRICE_CHANGE_1H', '80'))
    MIN_ORGANIC_SCORE: float = float(os.getenv('MIN_ORGANIC_SCORE', '50'))
    
    # ═══ ML SETTINGS ═══
    USE_ML_PREDICTIONS: bool = os.getenv('USE_ML_PREDICTIONS', 'true').lower() == 'true'
    ML_MIN_CONFIDENCE: float = float(os.getenv('ML_MIN_CONFIDENCE', '70'))
    
    # ═══ RISK MANAGEMENT ═══
    STOP_LOSS_PERCENT: float = float(os.getenv('STOP_LOSS_PERCENT', '-8'))
    TAKE_PROFIT_1: float = float(os.getenv('TAKE_PROFIT_1', '15'))
    TAKE_PROFIT_2: float = float(os.getenv('TAKE_PROFIT_2', '30'))
    MAX_POSITIONS: int = int(os.getenv('MAX_POSITIONS', '3'))
    MAX_DAILY_TRADES: int = int(os.getenv('MAX_DAILY_TRADES', '10'))
    MAX_LOSS_PER_DAY_PERCENT: float = float(os.getenv('MAX_LOSS_PER_DAY_PERCENT', '5'))
    
    # ═══ TIMING ═══
    SCAN_INTERVAL_SEC: int = int(os.getenv('SCAN_INTERVAL_SEC', '60'))
    
    # ═══ RETRY & TIMEOUT ═══
    MAX_RETRIES: int = int(os.getenv('MAX_RETRIES', '3'))
    API_TIMEOUT_SEC: int = int(os.getenv('API_TIMEOUT_SEC', '15'))
    RETRY_DELAY_SEC: int = int(os.getenv('RETRY_DELAY_SEC', '2'))
    SHYFT_RATE_LIMIT_DELAY: float = 0.6  # 100 req/min
    
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', 'INFO')

config = Config()

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper()),
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot_trader.log')
    ]
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# MACHINE LEARNING
# ═══════════════════════════════════════════════════════════════

class MLPredictor:
    """Predictor ML ligero para señales de trading"""
    
    def __init__(self):
        self.model = None
        self.is_trained = False
        
        if ML_AVAILABLE:
            self._init_model()
    
    def _init_model(self):
        """Inicializar modelo con datos sintéticos"""
        try:
            X_train = np.array([
                [5, 10, 80, 120000, 250000],
                [1, 2, 40, 50000, 70000],
                [8, 15, 90, 300000, 900000],
                [0.5, 1, 30, 40000, 60000],
                [3, 5, 70, 100000, 180000],
                [10, 20, 95, 350000, 950000],
                [2, 4, 50, 80000, 120000],
                [6, 12, 85, 200000, 400000],
                [0.2, 0.5, 20, 20000, 30000],
                [9, 18, 92, 400000, 800000],
            ])
            y_train = np.array([1, 0, 1, 0, 1, 1, 0, 1, 0, 1])
            
            self.model = RandomForestClassifier(
                n_estimators=50,
                max_depth=5,
                random_state=42,
                n_jobs=-1
            )
            self.model.fit(X_train, y_train)
            self.is_trained = True
            
            logger.info("✅ ML Model inicializado")
            
        except Exception as e:
            logger.error(f"❌ Error inicializando ML: {e}")
            self.is_trained = False
    
    def predict_signal_strength(self, token_data: Dict) -> Tuple[float, str]:
        """Predecir probabilidad de éxito"""
        if not self.is_trained or not ML_AVAILABLE:
            return 50.0, "NO_ML"
        
        try:
            features = np.array([[
                token_data.get('price_change_5m', 0),
                token_data.get('price_change_1h', 0),
                token_data.get('organic_score', 50),
                token_data.get('liquidity', 50000),
                token_data.get('volume_24h', 100000)
            ]])
            
            prob = self.model.predict_proba(features)[0][1] * 100
            signal = "BUY" if prob >= config.ML_MIN_CONFIDENCE else "IGNORE"
            
            return round(prob, 2), signal
            
        except Exception as e:
            logger.debug(f"Error ML prediction: {e}")
            return 50.0, "ERROR"

ml_predictor = MLPredictor()

# ═══════════════════════════════════════════════════════════════
# MODELOS DE DATOS
# ═══════════════════════════════════════════════════════════════

@dataclass
class TokenData:
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
    first_seen: float = field(default_factory=time.time)

@dataclass
class Position:
    mint: str
    symbol: str
    entry_price: float
    entry_time: float
    amount_sol: float
    highest_price: float
    lowest_price: float
    entry_tx: str = ''
    ml_confidence: float = 0
    
    def current_pnl(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0
        return ((current_price - self.entry_price) / self.entry_price) * 100
    
    def hold_time_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60

# ═══════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.wallet: Optional[Keypair] = None
        self.solana_client: Optional[AsyncClient] = None
        self.telegram_bot: Optional[Bot] = None
        self.db_pool: Optional[Any] = None
        
        self.positions: Dict[str, Position] = {}
        self.watchlist: Dict[str, TokenData] = {}
        
        self.stats = {
            'scans': 0,
            'signals': 0,
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'total_pnl': 0.0,
            'today_trades': 0,
            'today_pnl': 0.0,
            'ml_predictions': 0,
            'ml_correct': 0,
            'api_errors': 0,
            
            # Shyft stats
            'shyft_success': 0,
            'shyft_failures': 0,
            'shyft_rate_limited': 0,
            
            # Fallback stats
            'dexscreener_fallback': 0,
            'jupiter_v3_fallback': 0,
            'jupiter_failures': 0
        }
        
        self.last_trade_time = 0
        self.running = True
        self.connector: Optional[aiohttp.TCPConnector] = None
        self.last_shyft_call = 0

state = BotState()

# ═══════════════════════════════════════════════════════════════
# HTTP CLIENT
# ═══════════════════════════════════════════════════════════════

async def get_http_session() -> aiohttp.ClientSession:
    """Sesión HTTP optimizada"""
    if not state.connector:
        state.connector = aiohttp.TCPConnector(
            limit=50,
            ttl_dns_cache=600,
            force_close=False,
            enable_cleanup_closed=True
        )
    
    timeout = aiohttp.ClientTimeout(total=config.API_TIMEOUT_SEC, connect=5)
    
    return aiohttp.ClientSession(
        connector=state.connector,
        timeout=timeout,
        headers={'User-Agent': 'Mozilla/5.0'}
    )

async def api_call_with_retry(url: str, method: str = 'GET', **kwargs) -> Optional[dict]:
    """API call con retry"""
    
    for attempt in range(config.MAX_RETRIES):
        session = None
        try:
            session = await get_http_session()
            
            if method == 'GET':
                async with session.get(url, **kwargs) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        if attempt < config.MAX_RETRIES - 1:
                            await asyncio.sleep(config.RETRY_DELAY_SEC * (attempt + 1))
                            continue
                        
            elif method == 'POST':
                async with session.post(url, **kwargs) as resp:
                    if resp.status == 200:
                        return await resp.json()
            
        except aiohttp.ClientConnectorError:
            state.stats['api_errors'] += 1
            if attempt < config.MAX_RETRIES - 1:
                await asyncio.sleep(config.RETRY_DELAY_SEC)
                continue
                
        except asyncio.TimeoutError:
            if attempt < config.MAX_RETRIES - 1:
                await asyncio.sleep(config.RETRY_DELAY_SEC)
                continue
                
        except Exception as e:
            logger.debug(f"API error: {str(e)[:50]}")
            if attempt < config.MAX_RETRIES - 1:
                continue
        
        finally:
            if session and not session.closed:
                await session.close()
    
    return None

# ═══════════════════════════════════════════════════════════════
# 🟢 SHYFT API CLIENT
# ═══════════════════════════════════════════════════════════════

async def shyft_rate_limit():
    """Rate limiting para Shyft (100 req/min)"""
    current_time = time.time()
    time_since_last_call = current_time - state.last_shyft_call
    
    if time_since_last_call < config.SHYFT_RATE_LIMIT_DELAY:
        await asyncio.sleep(config.SHYFT_RATE_LIMIT_DELAY - time_since_last_call)
    
    state.last_shyft_call = time.time()

async def get_token_price_shyft(mint: str) -> Optional[float]:
    """Obtener precio usando Shyft API"""
    if not config.SHYFT_API_KEY:
        return None
    
    try:
        await shyft_rate_limit()
        
        url = config.SHYFT_TOKEN_PRICE
        params = {
            'network': config.SHYFT_NETWORK,
            'token_address': mint
        }
        headers = {
            'x-api-key': config.SHYFT_API_KEY
        }
        
        result = await api_call_with_retry(url, params=params, headers=headers)
        
        if result and result.get('success'):
            price = float(result.get('result', {}).get('price', 0))
            if price > 0:
                state.stats['shyft_success'] += 1
                logger.debug(f"✅ Shyft: {mint[:8]} = ${price:.8f}")
                return price
        
        if result and not result.get('success'):
            error_msg = result.get('message', '')
            if 'rate limit' in error_msg.lower():
                state.stats['shyft_rate_limited'] += 1
                await asyncio.sleep(2)
        
        return None
        
    except Exception as e:
        logger.debug(f"Shyft error: {str(e)[:100]}")
        state.stats['shyft_failures'] += 1
        return None

async def get_multiple_prices_shyft(mints: List[str]) -> Dict[str, float]:
    """Obtener múltiples precios en una sola llamada"""
    if not config.SHYFT_API_KEY or not mints:
        return {}
    
    try:
        await shyft_rate_limit()
        
        url = config.SHYFT_MULTIPLE_PRICES
        headers = {
            'x-api-key': config.SHYFT_API_KEY,
            'Content-Type': 'application/json'
        }
        json_data = {
            'network': config.SHYFT_NETWORK,
            'token_addresses': mints
        }
        
        result = await api_call_with_retry(
            url, 
            method='POST',
            headers=headers,
            json=json_data
        )
        
        if result and result.get('success'):
            prices = {}
            for item in result.get('result', []):
                mint = item.get('address')
                price = float(item.get('price', 0))
                if mint and price > 0:
                    prices[mint] = price
            
            logger.info(f"✅ Shyft batch: {len(prices)}/{len(mints)} precios")
            return prices
        
        return {}
        
    except Exception as e:
        logger.debug(f"Shyft batch error: {str(e)[:100]}")
        return {}

# ═══════════════════════════════════════════════════════════════
# GET PRICE CON FALLBACK
# ═══════════════════════════════════════════════════════════════

async def get_token_price(mint: str) -> Optional[float]:
    """🟢 Shyft → DexScreener → Jupiter V3"""
    
    if config.SIMULATION_MODE:
        return 0.0001 + (hash(mint) % 100) * 0.000001
    
    # 1️⃣ SHYFT
    price = await get_token_price_shyft(mint)
    if price:
        return price
    
    # 2️⃣ DexScreener
    try:
        url = f"{config.DEXSCREENER_API}/tokens/{mint}"
        result = await api_call_with_retry(url)
        
        if result and 'pairs' in result and len(result['pairs']) > 0:
            pairs = sorted(
                result['pairs'], 
                key=lambda x: float(x.get('liquidity', {}).get('usd', 0) or 0), 
                reverse=True
            )
            if pairs:
                price = float(pairs[0].get('priceUsd', 0))
                if price > 0:
                    state.stats['dexscreener_fallback'] += 1
                    logger.info(f"✅ DexScreener: {mint[:8]} = ${price:.8f}")
                    return price
    except Exception:
        pass
    
    # 3️⃣ Jupiter V3
    try:
        url = f"{config.JUPITER_PRICE_API_V3}?ids={mint}"
        result = await api_call_with_retry(url)
        
        if result and mint in result:
            price = float(result[mint].get('usdPrice', 0))
            if price > 0:
                state.stats['jupiter_v3_fallback'] += 1
                logger.info(f"✅ Jupiter V3: {mint[:8]} = ${price:.8f}")
                return price
    except Exception:
        pass
    
    logger.warning(f"⚠️ No se pudo obtener precio para {mint[:8]}")
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
        state.db_pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
        
        async with state.db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trade_history (
                    id SERIAL PRIMARY KEY,
                    mint VARCHAR(44),
                    symbol VARCHAR(20),
                    entry_price NUMERIC(20, 10),
                    exit_price NUMERIC(20, 10),
                    price_change_5m NUMERIC(10, 2),
                    price_change_1h NUMERIC(10, 2),
                    organic_score NUMERIC(10, 2),
                    liquidity_usd NUMERIC(15, 2),
                    volume_24h_usd NUMERIC(15, 2),
                    ml_confidence NUMERIC(5, 2),
                    result_profit_percent NUMERIC(10, 4),
                    hold_time_min NUMERIC(10, 2),
                    entry_time TIMESTAMP,
                    exit_time TIMESTAMP,
                    exit_reason VARCHAR(50),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            ''')
            
        logger.info("✅ Database inicializada")
        
    except Exception as e:
        logger.error(f"❌ Error database: {e}")
        state.db_pool = None

async def save_trade_for_ml(token: TokenData, position: Position, exit_price: float, exit_reason: str):
    """Guardar trade para ML"""
    if not state.db_pool:
        return
    
    try:
        pnl = position.current_pnl(exit_price)
        
        async with state.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO trade_history (
                    mint, symbol, entry_price, exit_price,
                    price_change_5m, price_change_1h, organic_score,
                    liquidity_usd, volume_24h_usd, ml_confidence,
                    result_profit_percent, hold_time_min,
                    entry_time, exit_time, exit_reason
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ''',
                token.mint, token.symbol, position.entry_price, exit_price,
                token.price_change_5m, token.price_change_1h, token.organic_score,
                token.liquidity, token.volume_24h, position.ml_confidence,
                pnl, position.hold_time_minutes(),
                datetime.fromtimestamp(position.entry_time), datetime.now(),
                exit_reason
            )
            
        logger.info(f"💾 Trade guardado: {token.symbol} ({pnl:+.2f}%)")
        
    except Exception as e:
        logger.debug(f"Error save_trade: {e}")

# ═══════════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════════

async def send_telegram(message: str):
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
# ESCANEO DE TOKENS
# ═══════════════════════════════════════════════════════════════

async def scan_for_signals() -> List[TokenData]:
    """Escanear tokens con Jupiter"""
    
    if config.SIMULATION_MODE:
        logger.info("🧪 [SIMULATION] Generando tokens simulados")
        fake_tokens = []
        for i in range(5):
            fake_token = TokenData(
                mint=f"SIM{i}{'1' * 35}",
                symbol=f"SIM{i}",
                name=f"Simulated Token {i}",
                price_usd=0.0001 + (i * 0.0001),
                liquidity=100000 + (i * 50000),
                volume_24h=200000 + (i * 100000),
                price_change_5m=5 + (i * 2),
                price_change_1h=8 + (i * 3),
                organic_score=60 + (i * 5),
                is_verified=i % 2 == 0
            )
            fake_tokens.append(fake_token)
        
        return fake_tokens
    
    try:
        category = config.JUPITER_SCAN_CATEGORY
        interval = config.JUPITER_SCAN_INTERVAL
        url = f"{config.JUPITER_TOKENS_API}/{category}/{interval}"
        
        result = await api_call_with_retry(url, params={'limit': 100})
        
        if not result or not isinstance(result, list):
            logger.warning("⚠️ Jupiter API no respondió")
            state.stats['jupiter_failures'] += 1
            return []
        
        state.stats['jupiter_failures'] = 0
        candidates = []
        
        for token_data in result:
            try:
                mint = token_data.get('id')
                if not mint or mint in state.positions:
                    continue
                
                token = TokenData(
                    mint=mint,
                    symbol=token_data.get('symbol', 'UNKNOWN'),
                    name=token_data.get('name', 'Unknown'),
                    price_usd=float(token_data.get('usdPrice', 0) or 0),
                    liquidity=float(token_data.get('liquidity', 0) or 0),
                    volume_24h=float(token_data.get('stats24h', {}).get('buyVolume', 0) or 0) +
                               float(token_data.get('stats24h', {}).get('sellVolume', 0) or 0),
                    price_change_5m=float(token_data.get('stats5m', {}).get('priceChange', 0) or 0),
                    price_change_1h=float(token_data.get('stats1h', {}).get('priceChange', 0) or 0),
                    market_cap=float(token_data.get('mcap', 0) or 0),
                    organic_score=float(token_data.get('organicScore', 0) or 0),
                    is_verified=token_data.get('isVerified', False)
                )
                
                if token.price_usd > 0:
                    candidates.append(token)
                
            except Exception:
                continue
        
        if candidates:
            logger.info(f"✅ Scanned {len(candidates)} tokens")
        
        return candidates
        
    except Exception as e:
        logger.error(f"❌ Error scan_for_signals: {e}")
        state.stats['jupiter_failures'] += 1
        return []

def has_buy_signal(token: TokenData) -> Tuple[bool, float, float]:
    """Evaluar señal con ML"""
    
    if token.liquidity < config.MIN_LIQUIDITY_USD:
        return False, 0, 0
    
    if token.volume_24h < config.MIN_VOLUME_24H_USD:
        return False, 0, 0
    
    if token.price_usd <= 0:
        return False, 0, 0
    
    if token.organic_score < config.MIN_ORGANIC_SCORE:
        return False, 0, 0
    
    # Score tradicional
    score = 0
    
    if token.price_change_5m >= config.MIN_PRICE_CHANGE_5M:
        score += min(30, token.price_change_5m * 3)
    else:
        return False, 0, 0
    
    if token.price_change_1h >= config.MIN_PRICE_CHANGE_1H:
        score += min(30, token.price_change_1h * 2)
    else:
        return False, 0, 0
    
    if token.price_change_1h > config.MAX_PRICE_CHANGE_1H:
        return False, 0, 0
    
    if token.liquidity > config.MIN_LIQUIDITY_USD * 2:
        score += 10
    
    if token.organic_score > 70:
        score += 15
    
    if token.is_verified:
        score += 10
    
    # ML Prediction
    ml_confidence = 50.0
    
    if config.USE_ML_PREDICTIONS and ml_predictor.is_trained:
        token_dict = {
            'price_change_5m': token.price_change_5m,
            'price_change_1h': token.price_change_1h,
            'organic_score': token.organic_score,
            'liquidity': token.liquidity,
            'volume_24h': token.volume_24h
        }
        ml_confidence, _ = ml_predictor.predict_signal_strength(token_dict)
        state.stats['ml_predictions'] += 1
    
    # Decisión final
    min_score = 60
    signal_ok = score >= min_score
    
    if config.USE_ML_PREDICTIONS:
        signal_ok = signal_ok and ml_confidence >= config.ML_MIN_CONFIDENCE
    
    if signal_ok:
        logger.info(f"🎯 {token.symbol}: BUY | Score: {score:.0f} | ML: {ml_confidence:.1f}%")
    
    return signal_ok, score, ml_confidence

async def buy_token(token: TokenData, ml_confidence: float):
    """Ejecutar compra (DRY_RUN)"""
    
    if state.stats['today_trades'] >= config.MAX_DAILY_TRADES:
        return
    
    if len(state.positions) >= config.MAX_POSITIONS:
        return
    
    if config.DRY_RUN:
        logger.info(f"🧪 [DRY RUN] Simulando compra de {token.symbol}")
        
        await asyncio.sleep(0.3)
        
        position = Position(
            mint=token.mint,
            symbol=token.symbol,
            entry_price=token.price_usd,
            entry_time=time.time(),
            amount_sol=config.TRADE_AMOUNT_SOL,
            highest_price=token.price_usd,
            lowest_price=token.price_usd,
            entry_tx=f"dry-run-{int(time.time())}",
            ml_confidence=ml_confidence
        )
        
        state.positions[token.mint] = position
        state.stats['trades'] += 1
        state.stats['today_trades'] += 1
        state.last_trade_time = time.time()
        
        msg = (
            f"🧪 <b>[DRY RUN] Compra Simulada</b>\n\n"
            f"Token: {token.symbol}\n"
            f"Precio: ${token.price_usd:.8f}\n"
            f"Monto: {config.TRADE_AMOUNT_SOL} SOL\n"
            f"ML Confidence: {ml_confidence:.1f}%\n"
            f"Liquidity: ${token.liquidity:,.0f} | Vol: ${token.volume_24h:,.0f}\n\n"
            f"<i>Operación simulada</i>"
        )
        
        await send_telegram(msg)
        logger.info(f"✅ [DRY RUN] Posición abierta: {token.symbol}")
        
        return
    
    logger.warning(f"⚠️ MODO REAL no implementado")

# ═══════════════════════════════════════════════════════════════
# GESTIÓN DE POSICIONES (CON BATCH)
# ═══════════════════════════════════════════════════════════════

async def check_positions():
    """Monitorear posiciones con batch request"""
    
    if not state.positions:
        return
    
    # 🚀 OPTIMIZACIÓN: Batch request con Shyft
    mints = list(state.positions.keys())
    
    if config.SHYFT_API_KEY:
        prices = await get_multiple_prices_shyft(mints)
    else:
        prices = {}
    
    # Procesar cada posición
    for mint, position in list(state.positions.items()):
        try:
            # Usar precio del batch o fallback
            current_price = prices.get(mint)
            
            if not current_price:
                current_price = await get_token_price(mint)
            
            if not current_price:
                logger.debug(f"⚠️ No price para {position.symbol}")
                continue
            
            # Actualizar precios
            if current_price > position.highest_price:
                position.highest_price = current_price
            if current_price < position.lowest_price:
                position.lowest_price = current_price
            
            # Calcular P&L
            pnl = position.current_pnl(current_price)
            hold_time = position.hold_time_minutes()
            
            # Decisiones de salida
            exit_reason = None
            
            if pnl <= config.STOP_LOSS_PERCENT:
                exit_reason = "STOP_LOSS"
            elif pnl >= config.TAKE_PROFIT_1:
                exit_reason = "TAKE_PROFIT_1"
            elif pnl >= config.TAKE_PROFIT_2:
                exit_reason = "TAKE_PROFIT_2"
            
            if exit_reason:
                await exit_position(mint, position, current_price, exit_reason)
            else:
                logger.debug(f"📊 {position.symbol}: P&L {pnl:+.2f}% | {hold_time:.1f}min")
        
        except Exception as e:
            logger.error(f"❌ Error check_positions: {e}")

async def exit_position(mint: str, position: Position, exit_price: float, reason: str):
    """Cerrar posición"""
    
    try:
        pnl = position.current_pnl(exit_price)
        
        # Actualizar stats
        if pnl > 0:
            state.stats['wins'] += 1
        else:
            state.stats['losses'] += 1
        
        state.stats['total_pnl'] += pnl
        state.stats['today_pnl'] += pnl
        
        # ML accuracy
        if position.ml_confidence > 0 and pnl > 0:
            state.stats['ml_correct'] += 1
        
        # Guardar en DB
        token_data = state.watchlist.get(mint)
        if token_data:
            await save_trade_for_ml(token_data, position, exit_price, reason)
        
        # Notificar
        emoji = "✅" if pnl > 0 else "❌"
        msg = (
            f"{emoji} <b>Posición Cerrada</b>\n\n"
            f"Token: {position.symbol}\n"
            f"Entrada: ${position.entry_price:.8f}\n"
            f"Salida: ${exit_price:.8f}\n"
            f"P&L: {pnl:+.2f}%\n"
            f"Tiempo: {position.hold_time_minutes():.1f}min\n"
            f"Razón: {reason}\n"
            f"ML Confidence: {position.ml_confidence:.1f}%"
        )
        
        await send_telegram(msg)
        logger.info(f"{emoji} Cerrado {position.symbol}: {pnl:+.2f}% ({reason})")
        
        del state.positions[mint]
        
    except Exception as e:
        logger.error(f"❌ Error exit_position: {e}")

# ═══════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════

async def main_trading_loop():
    """Loop principal"""
    
    try:
        from health_server import update_bot_status
    except ImportError:
        update_bot_status = None
    
    logger.info("🚀 Bot iniciando...")
    
    # Validar Shyft API key
    if config.SHYFT_API_KEY:
        logger.info("✅ Shyft API key configurada")
    else:
        logger.warning("⚠️ SHYFT_API_KEY no configurada - usando solo fallbacks")
    
    # Inicializar
    await init_database()
    
    if config.ENABLE_TELEGRAM and TELEGRAM_AVAILABLE:
        try:
            state.telegram_bot = Bot(token=config.TELEGRAM_TOKEN)
            mode = "DRY_RUN" if config.DRY_RUN else ("SIMULATION" if config.SIMULATION_MODE else "REAL")
            await send_telegram(f"🚀 <b>Bot v5.0 Iniciado</b>\n\nModo: {mode}\nAPI: Shyft")
        except Exception as e:
            logger.error(f"❌ Error Telegram: {e}")
    
    logger.info(f"🧠 ML: {'ENABLED' if ml_predictor.is_trained else 'DISABLED'}")
    logger.info(f"🧪 DRY_RUN: {config.DRY_RUN}")
    logger.info(f"🎮 SIMULATION: {config.SIMULATION_MODE}")
    
    # Update health
    if update_bot_status:
        update_bot_status(
            running=True, scans=0, positions=0, signals=0,
            trades=0, wins=0, losses=0, total_pnl=0.0,
            ml_enabled=ml_predictor.is_trained,
            mode="DRY_RUN" if config.DRY_RUN else "REAL"
        )
    
    # Loop principal
    while state.running:
        try:
            state.stats['scans'] += 1
            
            if update_bot_status:
                update_bot_status(
                    running=True,
                    scans=state.stats['scans'],
                    positions=len(state.positions),
                    signals=state.stats['signals'],
                    trades=state.stats['trades'],
                    wins=state.stats['wins'],
                    losses=state.stats['losses'],
                    total_pnl=state.stats['total_pnl'],
                    ml_enabled=ml_predictor.is_trained,
                    mode="DRY_RUN" if config.DRY_RUN else "REAL"
                )
            
            # 1. Escanear
            tokens = await scan_for_signals()
            
            if tokens:
                logger.info(f"📊 Analizando {len(tokens)} tokens...")
                
                # 2. Evaluar señales
                for token in tokens:
                    if len(state.positions) >= config.MAX_POSITIONS:
                        break
                    
                    has_signal, score, ml_conf = has_buy_signal(token)
                    
                    if has_signal:
                        state.stats['signals'] += 1
                        state.watchlist[token.mint] = token
                        await buy_token(token, ml_conf)
                        await asyncio.sleep(1)
            
            # 3. Monitorear posiciones
            await check_positions()
            
            # 4. Stats periódicas
            if state.stats['scans'] % 10 == 0:
                win_rate = (state.stats['wins'] / max(1, state.stats['wins'] + state.stats['losses'])) * 100
                ml_accuracy = (state.stats['ml_correct'] / max(1, state.stats['ml_predictions'])) * 100 if state.stats['ml_predictions'] > 0 else 0
                
                logger.info(
                    f"📊 Stats: Scans: {state.stats['scans']} | "
                    f"Señales: {state.stats['signals']} | "
                    f"Trades: {state.stats['trades']} | "
                    f"W/L: {state.stats['wins']}/{state.stats['losses']} ({win_rate:.1f}%) | "
                    f"P&L: {state.stats['total_pnl']:+.2f}% | "
                    f"ML Acc: {ml_accuracy:.1f}%"
                )
                
                logger.info(
                    f"🟢 APIs: Shyft OK: {state.stats['shyft_success']} | "
                    f"Shyft Fails: {state.stats['shyft_failures']} | "
                    f"Rate Limited: {state.stats['shyft_rate_limited']} | "
                    f"DexScreener: {state.stats['dexscreener_fallback']} | "
                    f"Jupiter V3: {state.stats['jupiter_v3_fallback']} | "
                    f"Jupiter Scan Fails: {state.stats['jupiter_failures']}"
                )
            
            # 5. Esperar
            await asyncio.sleep(config.SCAN_INTERVAL_SEC)
            
        except KeyboardInterrupt:
            logger.info("⏸️ Deteniendo bot...")
            state.running = False
            break
            
        except Exception as e:
            logger.error(f"❌ Error en main loop: {e}", exc_info=True)
            await asyncio.sleep(10)
    
    # Cleanup
    if state.db_pool:
        await state.db_pool.close()
    
    if state.connector:
        await state.connector.close()
    
    logger.info("👋 Bot detenido")

# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

async def run_bot_with_health_server():
    """Ejecutar bot + health server"""
    
    try:
        from health_server import start_health_server, update_bot_status
        
        update_bot_status(
            running=True, scans=0, positions=0, signals=0,
            trades=0, wins=0, losses=0, total_pnl=0.0,
            ml_enabled=ml_predictor.is_trained,
            mode="DRY_RUN" if config.DRY_RUN else "REAL"
        )
        
        logger.info("🏥 Health server habilitado en puerto 8080")
        
        await asyncio.gather(
            main_trading_loop(),
            start_health_server(port=8080)
        )
        
    except ImportError:
        logger.warning("⚠️ health_server.py no encontrado - solo bot")
        await main_trading_loop()

if __name__ == "__main__":
    try:
        asyncio.run(run_bot_with_health_server())
    except KeyboardInterrupt:
        logger.info("👋 Hasta luego")
        sys.exit(0)
