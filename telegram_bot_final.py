#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot_optimized.py
Bot optimizado para monitorear miles de tokens de Pump.fun
Mejoras:
- Batch processing para reducir llamadas RPC
- Caché inteligente en Redis
- Detección más rápida con validación previa
- Manejo robusto de rate limits
- Priorización de tokens (los más prometedores primero)
"""

import os
import sys
import json
import base64
import asyncio
import logging
import struct
import time
import base58
from typing import Dict, List, Optional, Any, Set
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import aiohttp
import asyncpg
import websockets
import uvicorn
from fastapi import FastAPI, Request, Response

import redis.asyncio as aioredis

from solana.rpc.async_api import AsyncClient as SolanaAsyncClient

try:
    from solders.pubkey import Pubkey as PublicKey
    USING_SOLDERS = True
except ImportError:
    from solana.publickey import PublicKey
    USING_SOLDERS = False

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Bot

# ============================================================================
# CONFIGURACIÓN MEJORADA
# ============================================================================

PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

class Config:
    def __init__(self, config_path: str = "config.json"):
        self.path = os.path.join(os.path.dirname(__file__), config_path)
        self._load()

    def _load(self):
        data = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r') as f:
                    data = json.load(f)
            except Exception:
                data = {}
                
        self.TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', data.get('telegram_bot_token', ''))
        self.TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', data.get('telegram_chat_id', ''))
        self.DATABASE_URL = os.getenv('DATABASE_URL', data.get('database_url', ''))
        self.REDIS_URL = os.getenv('REDIS_URL', data.get('redis_url', 'redis://localhost:6379/0'))
        
        # RPC URLs
        self.QUICKNODE_RPC_URL = os.getenv('QUICKNODE_RPC_URL', data.get('quicknode_rpc_url', ''))
        self.HELIUS_RPC_URL = os.getenv('HELIUS_RPC_URL', data.get('helius_rpc_url', ''))
        
        # WebSocket URL (Helius preferido)
        self.HELIUS_WSS = os.getenv('HELIUS_WSS', None)
        if not self.HELIUS_WSS and self.HELIUS_RPC_URL:
            if 'api-key=' in self.HELIUS_RPC_URL:
                api_key = self.HELIUS_RPC_URL.split('api-key=')[1].split('&')[0]
                self.HELIUS_WSS = f"wss://mainnet.helius-rpc.com/?api-key={api_key}"
        
        # ✅ NUEVAS CONFIGURACIONES OPTIMIZADAS
        self.BATCH_SIZE = int(os.getenv('BATCH_SIZE', 50))  # Procesar 50 tokens a la vez
        self.BATCH_INTERVAL_SEC = float(os.getenv('BATCH_INTERVAL_SEC', 2.0))  # Cada 2 segundos
        self.CACHE_TTL_SEC = int(os.getenv('CACHE_TTL_SEC', 300))  # Caché de 5 minutos
        self.MAX_TOKENS_MONITORED = int(os.getenv('MAX_TOKENS_MONITORED', 1000))  # Máximo 1000 tokens simultáneos
        self.PRIORITY_THRESHOLD_MCAP = float(os.getenv('PRIORITY_THRESHOLD_MCAP', 10000))  # Priorizar tokens >$10k mcap
        
        # Alert rules
        self.ALERT_RULES = data.get('alert_rules', [
            {"name": "quick_2x", "alert_percent": 100.0, "time_window_min": 5},
            {"name": "momentum_3x", "alert_percent": 200.0, "time_window_min": 15}
        ])
        
        monitoring = data.get('monitoring', {})
        self.MAX_MONITOR_TIME_MIN = float(os.getenv('MAX_MONITOR_TIME_MIN', 30))
        self.DUMP_THRESHOLD_PERCENT = float(os.getenv('DUMP_THRESHOLD_PERCENT', -60))
        self.PRICE_POLL_INTERVAL_SEC = float(os.getenv('PRICE_POLL_INTERVAL_SEC', 3))
        
        self.LOG_LEVEL = os.getenv('LOG_LEVEL', data.get('log_level', 'INFO'))
        self.HEALTH_PORT = int(os.getenv('HEALTH_PORT', data.get('health_port', 8080)))
        self.ENABLE_DB = os.getenv('ENABLE_DB', 'true').lower() == 'true'
        self.ENABLE_TELEGRAM = os.getenv('ENABLE_TELEGRAM', 'true').lower() == 'true'
        self.TELEGRAM_WEBHOOK_PATH = os.getenv('TELEGRAM_WEBHOOK_PATH', '/telegram/webhook')

def setup_logging(cfg: Config):
    logging.basicConfig(
        level=getattr(logging, cfg.LOG_LEVEL.upper(), logging.INFO),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class TokenData:
    mint: str
    symbol: str = "UNKNOWN"
    name: str = "UNKNOWN"
    initial_price: float = 0.0
    initial_market_cap: float = 0.0
    max_price: float = 0.0
    current_price: float = 0.0
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "monitoring"
    bonding_curve: Optional[str] = None
    last_checked: Optional[datetime] = None
    priority: int = 0  # 0=low, 1=medium, 2=high
    checks_count: int = 0
    metadata: Dict = field(default_factory=dict)

@dataclass
class AlertData:
    token_address: str
    rule_name: str
    gain_percent: float
    time_elapsed_min: float
    price_at_alert: float
    market_cap_at_alert: float
    extra_data: Dict = field(default_factory=dict)

# ============================================================================
# RPC CLIENT MEJORADO CON RATE LIMIT HANDLING
# ============================================================================

class OptimizedRPCClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session: Optional[aiohttp.ClientSession] = None
        self.providers = []
        
        if self.cfg.QUICKNODE_RPC_URL:
            self.providers.append(('quicknode', self.cfg.QUICKNODE_RPC_URL))
        if self.cfg.HELIUS_RPC_URL:
            self.providers.append(('helius', self.cfg.HELIUS_RPC_URL))
        if not self.providers:
            self.providers.append(('public', 'https://api.mainnet-beta.solana.com'))
            
        self.idx = 0
        self.rate_limit_until = {}  # Track rate limits per provider
        self.request_counts = defaultdict(int)  # Track requests per provider

    async def __aenter__(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    def _get_best_provider(self):
        """Selecciona el mejor provider disponible (no rate-limited)"""
        now = time.time()
        available = []
        
        for i, (name, url) in enumerate(self.providers):
            rate_limit_time = self.rate_limit_until.get(name, 0)
            if now >= rate_limit_time:
                available.append((i, name, url))
        
        if not available:
            # Todos están rate-limited, usar el que expire primero
            min_time = min(self.rate_limit_until.values())
            wait_time = max(0, min_time - now)
            logging.warning(f"⏳ All RPCs rate-limited, waiting {wait_time:.1f}s")
            return None
        
        # Rotar entre disponibles para distribuir carga
        self.idx = (self.idx + 1) % len(available)
        return available[self.idx % len(available)][2]

    async def batch_get_accounts(self, addresses: List[str]) -> Dict[str, Optional[Dict]]:
        """Obtener múltiples cuentas en un solo request"""
        if not addresses:
            return {}
        
        url = self._get_best_provider()
        if not url:
            await asyncio.sleep(2)
            url = self._get_best_provider() or self.providers[0][1]
        
        # Solana RPC acepta múltiples getAccountInfo en un batch
        requests = [
            {"jsonrpc": "2.0", "id": i, "method": "getAccountInfo", 
             "params": [addr, {"encoding": "base64"}]}
            for i, addr in enumerate(addresses)
        ]
        
        try:
            async with self.session.post(url, json=requests) as resp:
                if resp.status == 429:
                    provider_name = next(n for n, u in self.providers if u == url)
                    self.rate_limit_until[provider_name] = time.time() + 60
                    logging.warning(f"⚠️ Rate limited on {provider_name}, cooling down 60s")
                    return {}
                
                results = await resp.json()
                
                output = {}
                for i, addr in enumerate(addresses):
                    result = results[i] if isinstance(results, list) else results
                    if 'result' in result:
                        output[addr] = result['result']
                    else:
                        output[addr] = None
                
                return output
                
        except Exception as e:
            logging.error(f"❌ Batch RPC failed: {e}")
            return {}

    async def get_account_base64(self, address: str) -> Optional[Dict]:
        """Obtener una cuenta individual"""
        result = await self.batch_get_accounts([address])
        return result.get(address)

# ============================================================================
# REDIS CACHE MANAGER
# ============================================================================

class CacheManager:
    def __init__(self, redis_client, ttl: int = 300):
        self.redis = redis_client
        self.ttl = ttl
    
    async def get_bonding_curve_data(self, bonding_curve: str) -> Optional[Dict]:
        """Obtener datos de bonding curve desde caché"""
        try:
            key = f"bc:{bonding_curve}"
            data = await self.redis.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            logging.debug(f"Cache get failed: {e}")
        return None
    
    async def set_bonding_curve_data(self, bonding_curve: str, data: Dict):
        """Guardar datos de bonding curve en caché"""
        try:
            key = f"bc:{bonding_curve}"
            await self.redis.set(key, json.dumps(data), ex=self.ttl)
        except Exception as e:
            logging.debug(f"Cache set failed: {e}")
    
    async def is_token_processed(self, mint: str) -> bool:
        """Verificar si un token ya fue procesado recientemente"""
        try:
            key = f"processed:{mint}"
            return await self.redis.exists(key) > 0
        except Exception:
            return False
    
    async def mark_token_processed(self, mint: str, ttl: int = 3600):
        """Marcar token como procesado por 1 hora"""
        try:
            key = f"processed:{mint}"
            await self.redis.set(key, "1", ex=ttl)
        except Exception:
            pass

# ============================================================================
# DATABASE (sin cambios mayores)
# ============================================================================

class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pool: Optional[asyncpg.pool.Pool] = None

    async def connect(self):
        if not self.cfg.ENABLE_DB or not self.cfg.DATABASE_URL:
            logging.info("📊 DB disabled")
            return
        try:
            self.pool = await asyncpg.create_pool(self.cfg.DATABASE_URL, min_size=2, max_size=20)
            async with self.pool.acquire() as conn:
                await conn.execute('''CREATE TABLE IF NOT EXISTS token_monitoring (
                    token_address TEXT PRIMARY KEY,
                    symbol TEXT, name TEXT, bonding_curve TEXT,
                    initial_price NUMERIC, initial_market_cap NUMERIC,
                    max_price NUMERIC, start_time TIMESTAMPTZ,
                    last_checked TIMESTAMPTZ, status TEXT, 
                    priority INT DEFAULT 0,
                    metadata JSONB, created_at TIMESTAMPTZ DEFAULT NOW()
                )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS token_alerts (
                    id SERIAL PRIMARY KEY, token_address TEXT, alert_rule_name TEXT,
                    gain_percent NUMERIC, time_elapsed_min NUMERIC,
                    price_at_alert NUMERIC, market_cap_at_alert NUMERIC,
                    created_at TIMESTAMPTZ DEFAULT NOW(), extra_data JSONB
                )''')
                # Índice para mejorar queries
                await conn.execute('''CREATE INDEX IF NOT EXISTS idx_token_status 
                    ON token_monitoring(status, priority DESC, start_time DESC)''')
            logging.info("✅ Database connected")
        except Exception as e:
            logging.error(f"❌ DB connect failed: {e}")
            self.pool = None

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def upsert_token(self, token: TokenData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_monitoring 
                    (token_address,symbol,name,bonding_curve,initial_price,initial_market_cap,
                     max_price,start_time,last_checked,status,priority,metadata)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (token_address) DO UPDATE SET
                        symbol=EXCLUDED.symbol, name=EXCLUDED.name, 
                        bonding_curve=EXCLUDED.bonding_curve,
                        initial_price=EXCLUDED.initial_price, 
                        initial_market_cap=EXCLUDED.initial_market_cap,
                        max_price = GREATEST(token_monitoring.max_price, EXCLUDED.max_price),
                        last_checked=EXCLUDED.last_checked, status=EXCLUDED.status,
                        priority=EXCLUDED.priority, metadata=EXCLUDED.metadata
                ''', token.mint, token.symbol, token.name, token.bonding_curve,
                      token.initial_price, token.initial_market_cap, token.max_price,
                      token.start_time, token.last_checked, token.status, token.priority,
                      json.dumps(token.metadata))
        except Exception as e:
            logging.debug(f"DB upsert failed: {e}")

    async def record_alert(self, alert: AlertData):
        if not self.pool:
            return
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO token_alerts 
                    (token_address, alert_rule_name, gain_percent, time_elapsed_min, 
                     price_at_alert, market_cap_at_alert, extra_data)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                ''', alert.token_address, alert.rule_name, alert.gain_percent, 
                     alert.time_elapsed_min, alert.price_at_alert, 
                     alert.market_cap_at_alert, json.dumps(alert.extra_data))
        except Exception as e:
            logging.debug(f"DB record_alert failed: {e}")

# ============================================================================
# BONDING CURVE UTILITIES
# ============================================================================

def compute_bonding_curve_pda(mint: str) -> Optional[str]:
    """Calcula la bonding curve PDA desde el mint address"""
    try:
        mint_bytes = base58.b58decode(mint)
        program_bytes = base58.b58decode(PUMP_FUN_PROGRAM_ID)
        
        if len(mint_bytes) != 32 or len(program_bytes) != 32:
            return None
        
        if USING_SOLDERS:
            from solders.pubkey import Pubkey
            program_pubkey = Pubkey(program_bytes)
            seeds = [b"bonding_curve", mint_bytes]
            pda, bump = Pubkey.find_program_address(seeds, program_pubkey)
        else:
            program_pubkey = PublicKey(program_bytes)
            seeds = [b"bonding_curve", mint_bytes]
            pda, bump = PublicKey.find_program_address(seeds, program_pubkey)
        
        return str(pda)
        
    except Exception as e:
        logging.debug(f"PDA calc failed: {e}")
        return None

def parse_bonding_curve_account_b64(b64data: str) -> Optional[Dict]:
    """Parse bonding curve account data"""
    try:
        raw = base64.b64decode(b64data)
        if len(raw) < 49:  # 8 + 5*8 + 1
            return None
            
        offset = 8
        vToken, vSol, rToken, rSol, supply = struct.unpack_from("<QQQQQ", raw, offset)
        offset += 40
        complete = struct.unpack_from("<?", raw, offset)[0]
        
        price = 0.0
        market_cap = 0.0
        
        if rToken > 0 and rSol > 0:
            price = float(rSol) / float(rToken) / 1e9  # Convert lamports to SOL
            market_cap = (float(supply) / 1e6) * price  # Assuming 6 decimals
        
        return {
            'virtualTokenReserves': vToken,
            'virtualSolReserves': vSol,
            'realTokenReserves': rToken,
            'realSolReserves': rSol,
            'tokenTotalSupply': supply,
            'complete': bool(complete),
            'price': price,
            'market_cap': market_cap
        }
    except Exception as e:
        logging.debug(f"Parse BC failed: {e}")
        return None

# ============================================================================
# TOKEN MANAGER OPTIMIZADO CON BATCH PROCESSING
# ============================================================================

class OptimizedTokenManager:
    def __init__(self, cfg: Config, db: Database, rpc: OptimizedRPCClient, cache: CacheManager):
        self.cfg = cfg
        self.db = db
        self.rpc = rpc
        self.cache = cache
        self.monitored: Dict[str, TokenData] = {}
        self.pending_queue: asyncio.Queue = asyncio.Queue()
        self.alerted: Set[str] = set()
        self.batch_task = None
        self.stats = {"processed": 0, "alerts": 0, "errors": 0}

    async def start(self):
        """Iniciar el procesamiento en batch"""
        self.batch_task = asyncio.create_task(self._batch_processor())
        logging.info("✅ Batch processor started")

    async def add_token(self, mint: str, symbol: str = "UNKNOWN", name: str = "UNKNOWN",
                       bonding_curve: Optional[str] = None, initial_price: float = 0.0,
                       initial_market_cap: float = 0.0):
        """Agregar token a la cola de procesamiento"""
        
        # Verificar si ya está siendo monitoreado
        if mint in self.monitored:
            return
        
        # Verificar límite de tokens
        if len(self.monitored) >= self.cfg.MAX_TOKENS_MONITORED:
            logging.warning(f"⚠️ Max tokens reached ({self.cfg.MAX_TOKENS_MONITORED}), skipping {symbol}")
            return
        
        # Verificar si ya fue procesado recientemente
        if await self.cache.is_token_processed(mint):
            logging.debug(f"⏭️ Token {mint[:8]} already processed recently")
            return
        
        # Calcular bonding curve si no se proporciona
        if not bonding_curve:
            bonding_curve = compute_bonding_curve_pda(mint)
        
        # Determinar prioridad
        priority = 0
        if initial_market_cap > self.cfg.PRIORITY_THRESHOLD_MCAP:
            priority = 2
        elif initial_market_cap > 0:
            priority = 1
        
        token = TokenData(
            mint=mint,
            symbol=symbol,
            name=name,
            initial_price=initial_price,
            initial_market_cap=initial_market_cap,
            max_price=initial_price,
            bonding_curve=bonding_curve,
            priority=priority
        )
        
        self.monitored[mint] = token
        await self.pending_queue.put(mint)
        await self.cache.mark_token_processed(mint)
        
        logging.info(f"➕ Queued {symbol} ({mint[:8]}) - Priority: {priority}")

    async def _batch_processor(self):
        """Procesa tokens en batches para optimizar RPC calls"""
        while True:
            try:
                # Recolectar batch de tokens
                batch_mints = []
                
                # Esperar el primer token
                try:
                    first_mint = await asyncio.wait_for(
                        self.pending_queue.get(), 
                        timeout=self.cfg.BATCH_INTERVAL_SEC
                    )
                    batch_mints.append(first_mint)
                except asyncio.TimeoutError:
                    # Si no hay tokens nuevos, procesar los existentes
                    if self.monitored:
                        batch_mints = list(self.monitored.keys())[:self.cfg.BATCH_SIZE]
                    else:
                        await asyncio.sleep(1)
                        continue
                
                # Recolectar más tokens hasta llenar el batch
                while len(batch_mints) < self.cfg.BATCH_SIZE:
                    try:
                        mint = self.pending_queue.get_nowait()
                        batch_mints.append(mint)
                    except asyncio.QueueEmpty:
                        break
                
                if not batch_mints:
                    continue
                
                # Procesar el batch
                await self._process_batch(batch_mints)
                
                await asyncio.sleep(self.cfg.BATCH_INTERVAL_SEC)
                
            except Exception as e:
                logging.error(f"❌ Batch processor error: {e}")
                await asyncio.sleep(5)

    async def _process_batch(self, mints: List[str]):
        """Procesar un batch de tokens"""
        logging.debug(f"🔄 Processing batch of {len(mints)} tokens")
        
        # Preparar las bonding curves a consultar
        bonding_curves_to_fetch = {}
        for mint in mints:
            token = self.monitored.get(mint)
            if not token or not token.bonding_curve:
                continue
            bonding_curves_to_fetch[token.bonding_curve] = mint
        
        if not bonding_curves_to_fetch:
            return
        
        # Obtener datos en batch
        accounts = await self.rpc.batch_get_accounts(list(bonding_curves_to_fetch.keys()))
        
        # Procesar resultados
        now = datetime.now(timezone.utc)
        
        for bc_address, account_data in accounts.items():
            mint = bonding_curves_to_fetch.get(bc_address)
            if not mint:
                continue
            
            token = self.monitored.get(mint)
            if not token:
                continue
            
            # Intentar obtener precio
            price_data = None
            
            # Primero intentar desde caché
            cached = await self.cache.get_bonding_curve_data(bc_address)
            if cached and (now - datetime.fromisoformat(cached.get('timestamp', '2000-01-01'))).seconds < 30:
                price_data = cached
            
            # Si no hay caché, parsear desde account data
            if not price_data and account_data and account_data.get('value'):
                data = account_data['value'].get('data')
                if data and isinstance(data, list):
                    parsed = parse_bonding_curve_account_b64(data[0])
                    if parsed:
                        price_data = {
                            'price': parsed['price'],
                            'market_cap': parsed['market_cap'],
                            'complete': parsed['complete'],
                            'timestamp': now.isoformat()
                        }
                        await self.cache.set_bonding_curve_data(bc_address, price_data)
            
            if not price_data or price_data.get('price', 0) <= 0:
                continue
            
            # Actualizar token
            current_price = float(price_data['price'])
            token.current_price = current_price
            token.last_checked = now
            token.checks_count += 1
            
            if current_price > token.max_price:
                token.max_price = current_price
            
            # Verificar timeout
            elapsed_min = (now - token.start_time).total_seconds() / 60.0
            if elapsed_min >= self.cfg.MAX_MONITOR_TIME_MIN:
                await self._remove_token(mint, "timeout")
                continue
            
            # Verificar dump
            if token.max_price > 0:
                loss = ((current_price - token.max_price) / token.max_price) * 100.0
                if loss <= self.cfg.DUMP_THRESHOLD_PERCENT:
                    logging.info(f"📉 Dump detected: {token.symbol} {loss:.1f}%")
                    await self._remove_token(mint, "dumped")
                    continue
            
            # Evaluar alertas
            await self._check_alerts(token, current_price, elapsed_min)
            
            # Guardar en DB cada 10 checks
            if token.checks_count % 10 == 0:
                await self.db.upsert_token(token)
        
        self.stats["processed"] += len(mints)

    async def _check_alerts(self, token: TokenData, current_price: float, elapsed_min: float):
        """Verificar si se debe enviar una alerta"""
        if token.mint in self.alerted or token.initial_price <= 0:
            return
        
        gain = ((current_price - token.initial_price) / token.initial_price) * 100.0
        
        for rule in self.cfg.ALERT_RULES:
            if gain >= float(rule['alert_percent']) and elapsed_min <= float(rule['time_window_min']):
                alert = AlertData(
                    token_address=token.mint,
                    rule_name=rule['name'],
                    gain_percent=gain,
                    time_elapsed_min=elapsed_min,
                    price_at_alert=current_price,
                    market_cap_at_alert=token.initial_market_cap * (1 + gain/100),
                    extra_data={
                        'symbol': token.symbol,
                        'name': token.name,
                        'initial_price': token.initial_price,
                        'max_price': token.max_price
                    }
                )
                
                await self.db.record_alert(alert)
                await Notification.send_alert(token, alert)
                self.alerted.add(token.mint)
                self.stats["alerts"] += 1
                
                logging.info(f"🚀 ALERT: {token.symbol} +{gain:.1f}% in {elapsed_min:.1f}min")
                
                # Remover después de alerta
                await self._remove_token(token.mint, "alert_sent")
                break

    async def _remove_token(self, mint: str, reason: str):
        """Remover token del monitoreo"""
        token = self.monitored.get(mint)
        if token:
            token.status = reason
            token.last_checked = datetime.now(timezone.utc)
            await self.db.upsert_token(token)
            del self.monitored[mint]
            logging.debug(f"🗑️ Removed {token.symbol}: {reason}")

    def get_stats(self) -> Dict:
        """Obtener estadísticas del manager"""
        return {
            "monitored": len(self.monitored),
            "processed": self.stats["processed"],
            "alerts": self.stats["alerts"],
            "errors": self.stats["errors"],
            "queue_size": self.pending_queue.qsize()
        }

# ============================================================================
# NOTIFICATION (Telegram)
# ============================================================================

class Notification:
    bot: Optional[Bot] = None
    cfg: Optional[Config] = None

    @classmethod
    def init(cls, cfg: Config):
        cls.cfg = cfg
        if cfg.ENABLE_TELEGRAM and cfg.TELEGRAM_BOT_TOKEN:
            cls.bot = Bot(token=cfg.TELEGRAM_BOT_TOKEN)

    @classmethod
    async def send_alert(cls, token: TokenData, alert: AlertData):
        if not cls.bot or not cls.cfg or not cls.cfg.TELEGRAM_CHAT_ID:
            logging.info(f"ALERT (no telegram): {token.symbol} +{alert.gain_percent:.1f}%")
            return
        try:
            message = cls._format(token, alert)
            keyboard = cls._keyboard(token.mint)
            
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: cls.bot.send_message(
                    chat_id=cls.cfg.TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode='Markdown',
                    reply_markup=keyboard
                )
            )
            logging.info(f"✅ Alert sent for {token.mint}")
        except Exception as e:
            logging.error(f"❌ Telegram send failed: {e}")

    @staticmethod
    def _format(token: TokenData, alert: AlertData) -> str:
        return (
            f"🚀 *ALERTA DE MOMENTUM* 🚀\n\n"
            f"*Token:* {token.name} ({token.symbol})\n"
            f"*Mint:* `{token.mint}`\n"
            f"*Ganancia:* +{alert.gain_percent:.1f}% en {alert.time_elapsed_min:.1f} min\n"
            f"*Precio inicial:* ${token.initial_price:.8f}\n"
            f"*Precio actual:* ${alert.price_at_alert:.8f}\n"
            f"*Market Cap:* ${alert.market_cap_at_alert:,.0f}\n\n"
            f"📈 *Enlaces rápidos*\n"
            f"• Pump.fun: https://pump.fun/{token.mint}\n"
            f"• DexScreener: https://dexscreener.com/solana/{token.mint}\n"
            f"• RugCheck: https://rugcheck.xyz/tokens/{token.mint}\n"
            f"• Birdeye: https://birdeye.so/token/{token.mint}?chain=solana\n\n"
            f"🕐 Tiempo: {alert.time_elapsed_min:.1f} minutos\n"
        )

    @staticmethod
    def _keyboard(mint: str) -> InlineKeyboardMarkup:
        kb = [
            [
                InlineKeyboardButton("Pump.fun", url=f"https://pump.fun/{mint}"),
                InlineKeyboardButton("DexScreener", url=f"https://dexscreener.com/solana/{mint}")
            ],
            [
                InlineKeyboardButton("RugCheck", url=f"https://rugcheck.xyz/tokens/{mint}"),
                InlineKeyboardButton("Birdeye", url=f"https://birdeye.so/token/{mint}?chain=solana")
            ]
        ]
        return InlineKeyboardMarkup(kb)

# ============================================================================
# HELIUS WEBSOCKET LISTENER MEJORADO
# ============================================================================

class HeliusListener:
    def __init__(self, cfg: Config, manager: OptimizedTokenManager):
        self.cfg = cfg
        self.manager = manager
        self.running = False
        self.ws = None
        self.reconnect_delay = 5
        self.max_reconnect_delay = 60
        self.last_message_time = time.time()
        self.subscription_id = None

    async def connect(self):
        if not self.cfg.HELIUS_WSS:
            logging.error("❌ HELIUS_WSS not configured")
            return
        
        self.running = True
        uri = self.cfg.HELIUS_WSS
        
        while self.running:
            try:
                logging.info(f"🔌 Connecting to Helius WSS...")
                
                async with websockets.connect(
                    uri,
                    ping_interval=15,
                    ping_timeout=10,
                    close_timeout=10
                ) as websocket:
                    self.ws = websocket
                    self.reconnect_delay = 5
                    self.last_message_time = time.time()
                    
                    # Subscribe to program logs
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [PUMP_FUN_PROGRAM_ID]},
                            {"commitment": "confirmed"}
                        ]
                    }
                    
                    await websocket.send(json.dumps(subscribe_msg))
                    logging.info("✅ Subscribed to pump.fun logs")
                    
                    # Heartbeat task
                    heartbeat_task = asyncio.create_task(self._heartbeat())
                    
                    try:
                        async for raw in websocket:
                            self.last_message_time = time.time()
                            try:
                                data = json.loads(raw)
                                
                                # Handle subscription response
                                if 'result' in data and 'id' in data:
                                    self.subscription_id = data['result']
                                    logging.info(f"✅ Subscription ID: {self.subscription_id}")
                                    continue
                                
                                await self._handle(data)
                                
                            except json.JSONDecodeError:
                                logging.debug("Invalid JSON from Helius")
                            except Exception as e:
                                logging.error(f"Message handling error: {e}")
                    finally:
                        heartbeat_task.cancel()
                        
            except websockets.exceptions.ConnectionClosed as e:
                logging.warning(f"⚠️ Helius WSS closed: {e}")
            except Exception as e:
                logging.error(f"❌ Helius WSS error: {e}")
            finally:
                self.ws = None
                if self.running:
                    logging.info(f"🔄 Reconnecting in {self.reconnect_delay}s...")
                    await asyncio.sleep(self.reconnect_delay)
                    self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def _heartbeat(self):
        """Monitor connection health"""
        while True:
            await asyncio.sleep(30)
            if time.time() - self.last_message_time > 60:
                logging.warning("⚠️ No messages for 60s, connection may be dead")
                if self.ws:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                break

    async def _handle(self, payload: Dict):
        """Handle incoming WebSocket messages"""
        try:
            if payload.get('method') != 'logsNotification':
                return
            
            result = payload.get('params', {}).get('result', {}).get('value', {})
            logs = result.get('logs', [])
            signature = result.get('signature', '')
            
            # Look for create instruction
            is_create = any('Instruction: Create' in log for log in logs)
            if not is_create:
                return
            
            logging.debug(f"🆕 New token in tx: {signature[:16]}...")
            
            # Extract mint from logs (faster than fetching full tx)
            mint = await self._extract_mint_from_logs(logs, signature)
            
            if mint:
                logging.info(f"🎯 Token detected: {mint[:16]}...")
                await self.manager.add_token(mint=mint)
                
        except Exception as e:
            logging.error(f"Handler error: {e}")

    async def _extract_mint_from_logs(self, logs: List[str], signature: str) -> Optional[str]:
        """Extract mint from logs or fetch transaction"""
        # Try to extract from logs first (faster)
        for log in logs:
            if 'Program data:' in log:
                # Try to decode mint from program data
                try:
                    parts = log.split('Program data:')
                    if len(parts) > 1:
                        # This is a heuristic - may need adjustment
                        data = parts[1].strip()
                        # Mint is typically logged in base58
                        if len(data) >= 32 and len(data) <= 48:
                            return data
                except Exception:
                    pass
        
        # Fallback: fetch full transaction
        try:
            if not self.manager.rpc.session:
                self.manager.rpc.session = aiohttp.ClientSession()
            
            tx_data = await self.manager.rpc.rpc.rpc("getTransaction", [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
            ])
            
            if not tx_data:
                return None
            
            # Extract from postTokenBalances
            meta = tx_data.get('meta', {})
            post_balances = meta.get('postTokenBalances', [])
            
            for balance in post_balances:
                mint = balance.get('mint')
                if mint:
                    return mint
            
            # Extract from account keys
            message = tx_data.get('transaction', {}).get('message', {})
            account_keys = message.get('accountKeys', [])
            
            for key in account_keys:
                if isinstance(key, dict):
                    pubkey = key.get('pubkey', '')
                elif isinstance(key, str):
                    pubkey = key
                else:
                    continue
                
                # Simple heuristic: new mint is usually one of the first accounts
                if len(pubkey) == 44:  # Base58 length for Solana addresses
                    return pubkey
            
        except Exception as e:
            logging.debug(f"TX fetch failed: {e}")
        
        return None

    async def stop(self):
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

# ============================================================================
# FASTAPI APP
# ============================================================================

def create_app(bot_instance):
    app = FastAPI()

    @app.get("/")
    async def root():
        return {"status": "ok", "service": "pump.fun monitor"}

    @app.get("/health")
    async def health():
        stats = bot_instance.token_manager.get_stats()
        wss_connected = bot_instance.helius_listener.ws is not None if bot_instance.helius_listener else False
        
        return {
            "status": "healthy",
            "websocket_connected": wss_connected,
            "stats": stats
        }

    @app.get("/metrics")
    async def metrics():
        return bot_instance.token_manager.get_stats()

    @app.post(bot_instance.config.TELEGRAM_WEBHOOK_PATH)
    async def telegram_webhook(request: Request):
        try:
            data = await request.json()
        except Exception:
            return Response(status_code=400)
        
        message = data.get("message") or data.get("edited_message") or {}
        if not message:
            # Handle callback queries
            if "callback_query" in data:
                await handle_callback_query(data['callback_query'], bot_instance)
            return {"ok": True}
        
        chat = message.get("chat", {})
        text = message.get("text", "")
        
        # Security check
        if bot_instance.config.TELEGRAM_CHAT_ID:
            if str(chat.get("id")) != str(bot_instance.config.TELEGRAM_CHAT_ID):
                return {"ok": True}
        
        # Handle commands
        if text.startswith("/"):
            await handle_command(text, chat, bot_instance)
        
        return {"ok": True}

    return app

async def handle_command(text: str, chat: Dict, bot_instance):
    """Handle Telegram commands"""
    command = text.split()[0]
    chat_id = chat.get("id")
    
    if command == "/start":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Iniciar", callback_data="cmd:start_monitor")],
            [
                InlineKeyboardButton("📊 Status", callback_data="cmd:status"),
                InlineKeyboardButton("🔍 Tokens", callback_data="cmd:tokens")
            ]
        ])
        await bot_instance.bot.send_message(
            chat_id=chat_id,
            text="🤖 *Pump.fun Monitor Bot*\n\nOptimized for thousands of tokens.\nUse buttons below to control the bot.",
            parse_mode="Markdown",
            reply_markup=kb
        )
    
    elif command == "/status":
        stats = bot_instance.token_manager.get_stats()
        wss_status = "✅" if bot_instance.helius_listener.ws else "❌"
        
        status_text = (
            f"📊 *Bot Status*\n\n"
            f"• Monitored tokens: {stats['monitored']}\n"
            f"• Queue size: {stats['queue_size']}\n"
            f"• Processed: {stats['processed']}\n"
            f"• Alerts sent: {stats['alerts']}\n"
            f"• WebSocket: {wss_status}\n"
            f"• Redis: {'✅' if bot_instance.redis else '❌'}\n"
            f"• Database: {'✅' if bot_instance.db.pool else '❌'}"
        )
        
        await bot_instance.bot.send_message(
            chat_id=chat_id,
            text=status_text,
            parse_mode="Markdown"
        )
    
    elif command == "/tokens":
        tokens = list(bot_instance.token_manager.monitored.items())[:15]
        if tokens:
            msg = "🔍 *Monitored Tokens* (Top 15):\n\n"
            for mint, token in tokens:
                priority_icon = "🔴" if token.priority == 2 else "🟡" if token.priority == 1 else "⚪"
                gain = 0
                if token.initial_price > 0:
                    gain = ((token.current_price - token.initial_price) / token.initial_price) * 100
                msg += f"{priority_icon} {token.symbol} | {gain:+.1f}% | {mint[:8]}...\n"
        else:
            msg = "📭 No tokens monitored currently."
        
        await bot_instance.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode="Markdown"
        )

async def handle_callback_query(cq: Dict, bot_instance):
    """Handle Telegram callback queries"""
    cd = cq.get("data", "")
    chat_id = cq['message']['chat']['id']
    
    if cd == "cmd:start_monitor":
        if not bot_instance.helius_task or bot_instance.helius_task.done():
            bot_instance.helius_task = asyncio.create_task(bot_instance.helius_listener.connect())
            await bot_instance.bot.send_message(chat_id=chat_id, text="▶️ Monitoring started!")
        else:
            await bot_instance.bot.send_message(chat_id=chat_id, text="⚠️ Already running")
    
    elif cd == "cmd:status":
        await handle_command("/status", {"id": chat_id}, bot_instance)
    
    elif cd == "cmd:tokens":
        await handle_command("/tokens", {"id": chat_id}, bot_instance)

# ============================================================================
# MAIN SERVICE
# ============================================================================

class PumpFunService:
    def __init__(self):
        self.config = Config()
        setup_logging(self.config)
        
        self.bot = None
        if self.config.ENABLE_TELEGRAM and self.config.TELEGRAM_BOT_TOKEN:
            self.bot = Bot(token=self.config.TELEGRAM_BOT_TOKEN)
        
        Notification.init(self.config)
        
        self.db = Database(self.config)
        self.rpc = OptimizedRPCClient(self.config)
        self.redis = None
        self.cache = None
        self.token_manager = None
        self.helius_listener = None
        self.helius_task = None
        self.fastapi_app = None

    async def start(self):
        logging.info("🚀 Starting Pump.fun Monitor Service...")
        
        # Connect database
        await self.db.connect()
        
        # Connect Redis
        try:
            self.redis = aioredis.from_url(self.config.REDIS_URL, decode_responses=True)
            await self.redis.ping()
            logging.info("✅ Redis connected")
        except Exception as e:
            logging.error(f"❌ Redis failed: {e}")
            raise
        
        # Initialize cache
        self.cache = CacheManager(self.redis, self.config.CACHE_TTL_SEC)
        
        # Initialize RPC client
        await self.rpc.__aenter__()
        
        # Initialize token manager
        self.token_manager = OptimizedTokenManager(self.config, self.db, self.rpc, self.cache)
        await self.token_manager.start()
        
        # Initialize Helius listener
        self.helius_listener = HeliusListener(self.config, self.token_manager)
        
        # Set webhook
        if self.bot:
            webhook_url = os.getenv('DOMAIN_URL')
            if webhook_url:
                full_url = webhook_url.rstrip("/") + self.config.TELEGRAM_WEBHOOK_PATH
                try:
                    await self.bot.set_webhook(url=full_url)
                    logging.info(f"✅ Webhook set: {full_url}")
                except Exception as e:
                    logging.error(f"❌ Webhook failed: {e}")
        
        # Create FastAPI app
        self.fastapi_app = create_app(self)
        
        logging.info("✅ Service ready")

    async def stop(self):
        logging.info("🛑 Stopping service...")
        
        if self.helius_listener:
            await self.helius_listener.stop()
        
        if self.helius_task:
            self.helius_task.cancel()
        
        if self.token_manager and self.token_manager.batch_task:
            self.token_manager.batch_task.cancel()
        
        if self.db:
            await self.db.disconnect()
        
        if self.redis:
            await self.redis.close()
        
        if self.rpc:
            await self.rpc.__aexit__(None, None, None)
        
        logging.info("✅ Service stopped")

# ============================================================================
# ENTRYPOINT
# ============================================================================

async def _main():
    svc = PumpFunService()
    await svc.start()
    
    # Start uvicorn server
    config = uvicorn.Config(
        svc.fastapi_app,
        host="0.0.0.0",
        port=svc.config.HEALTH_PORT,
        log_level="info"
    )
    server = uvicorn.Server(config)
    
    # Start WebSocket in background
    if svc.helius_listener:
        svc.helius_task = asyncio.create_task(svc.helius_listener.connect())
    
    # Run server
    await server.serve()

def run_main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logging.info("⚠️ Interrupted by user")
    except Exception as e:
        logging.error(f"❌ Fatal error: {e}")
        raise

if __name__ == "__main__":
    run_main()
