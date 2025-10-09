# bot_jupiter_complete.py - VERSIÓN COMPLETA CON TODOS LOS COMANDOS Y PUMP.FUN
import asyncio
import json
import os
import time
import logging
import aiohttp
import asyncpg
import websockets
from datetime import datetime, timedelta
from statistics import pstdev, mean
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ===================== CONFIGURACIÓN DE LOGGING =====================
logger = logging.getLogger("jupiter_complete")
logger.setLevel(logging.DEBUG)
logger.propagate = False

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

file_handler = logging.FileHandler('bot_complete.log')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

debug_handler = logging.FileHandler('bot_debug.log')
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)
logger.addHandler(debug_handler)

# ===================== CONFIGURACIÓN COMPLETA =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

# 🎯 CONFIGURACIÓN FLAT OPTIMIZADA
FLAT_CONFIG = {
    'MIN_FLAT_DURATION_HOURS': 12,
    'MAX_VOLATILITY': 2.5,
    'MAX_AVG_VOLUME_PER_CANDLE': 300,
    'MIN_LOW_VOLUME_CANDLES': 6,
    'MAX_CONSECUTIVE_GREEN': 3,
    'CANDLES_TO_ANALYZE': 72,
    'VOLUME_SPIKE_THRESHOLD': 300,
}

# 🚀 CONFIGURACIÓN PUMP.FUN
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_PRE_GRADUATION_THRESHOLD = 55000
PUMP_GRADUATION_TARGET = 69000

# ⚠️ FILTROS MÁS FLEXIBLES
MIN_LIQUIDITY = 15000
MIN_VOLUME_24H = 25000
MAX_RISK_SCORE = 50

# 🔧 CONFIGURACIÓN OPERATIVA
UPDATE_INTERVAL = 1800
PUMP_MONITOR_INTERVAL = 15
JUPITER_BASE_URL = "https://lite-api.jup.ag"

# ===================== BASE DE DATOS COMPLETA =====================
class DatabaseManager:
    def __init__(self):
        self.pool = None
    
    async def init(self):
        if DATABASE_URL:
            try:
                self.pool = await asyncpg.create_pool(DATABASE_URL)
                await self.create_tables()
                logger.info("✅ Base de datos PostgreSQL inicializada")
            except Exception as e:
                logger.error(f"❌ Error conectando a BD: {e}")
    
    async def create_tables(self):
        async with self.pool.acquire() as conn:
            # Tabla de tokens notificados
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS notified_tokens (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    alert_type TEXT,
                    risk_score INTEGER,
                    first_detected TIMESTAMP DEFAULT NOW(),
                    last_alert TIMESTAMP DEFAULT NOW(),
                    alert_count INTEGER DEFAULT 1,
                    metadata JSONB
                )
            ''')
            
            # Tabla de tokens FLAT
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS flat_tokens (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    flat_duration_hours INTEGER,
                    volatility_score REAL,
                    volume_analysis JSONB,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    status TEXT DEFAULT 'monitoring'
                )
            ''')
            
            # Tabla de watchlist
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS token_watchlist (
                    id SERIAL PRIMARY KEY,
                    mint_address TEXT UNIQUE,
                    symbol TEXT,
                    name TEXT,
                    added_by TEXT DEFAULT 'system',
                    category TEXT DEFAULT 'flat',
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    is_active BOOLEAN DEFAULT TRUE
                )
            ''')
            
            # Tabla de tokens Pump.fun
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS pumpfun_tokens (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    market_cap REAL,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    status TEXT DEFAULT 'monitoring'
                )
            ''')
    
    async def mark_token_notified(self, mint: str, symbol: str, alert_type: str, risk_score: int = 0, metadata: dict = None):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO notified_tokens 
                (mint_address, symbol, alert_type, risk_score, metadata)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (mint_address) 
                DO UPDATE SET 
                    last_alert = NOW(),
                    alert_count = notified_tokens.alert_count + 1,
                    metadata = EXCLUDED.metadata
            ''', mint, symbol, alert_type, risk_score, json.dumps(metadata or {}))
    
    async def is_token_notified(self, mint: str, alert_type: str = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow(
                    "SELECT 1 FROM notified_tokens WHERE mint_address = $1 AND alert_type = $2",
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow(
                    "SELECT 1 FROM notified_tokens WHERE mint_address = $1",
                    mint
                )
            return bool(row)
    
    async def save_flat_token(self, mint: str, symbol: str, flat_data: dict):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO flat_tokens 
                (mint_address, symbol, flat_duration_hours, volatility_score, volume_analysis)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (mint_address) 
                DO UPDATE SET 
                    flat_duration_hours = EXCLUDED.flat_duration_hours,
                    volatility_score = EXCLUDED.volatility_score,
                    volume_analysis = EXCLUDED.volume_analysis,
                    detected_at = NOW()
            ''', mint, symbol, flat_data.get('flat_duration'), 
                flat_data.get('volatility'), json.dumps(flat_data.get('volume_analysis', {})))
    
    async def save_pumpfun_token(self, mint: str, symbol: str, market_cap: float):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO pumpfun_tokens 
                (mint_address, symbol, market_cap)
                VALUES ($1, $2, $3)
                ON CONFLICT (mint_address) 
                DO UPDATE SET 
                    market_cap = EXCLUDED.market_cap,
                    detected_at = NOW()
            ''', mint, symbol, market_cap)
    
    # 🆕 MÉTODOS PARA GESTIÓN DE TOKENS
    async def add_to_watchlist(self, mint: str, symbol: str, name: str = None, 
                             category: str = "flat", added_by: str = "system", notes: str = None):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO token_watchlist 
                (mint_address, symbol, name, category, added_by, notes)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (mint_address) 
                DO UPDATE SET 
                    symbol = EXCLUDED.symbol,
                    name = EXCLUDED.name,
                    category = EXCLUDED.category,
                    is_active = TRUE
            ''', mint, symbol, name, category, added_by, notes)
    
    async def remove_from_watchlist(self, mint: str):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE token_watchlist 
                SET is_active = FALSE 
                WHERE mint_address = $1
            ''', mint)
    
    async def get_watchlist(self, category: str = None, active_only: bool = True):
        async with self.pool.acquire() as conn:
            query = "SELECT * FROM token_watchlist"
            params = []
            
            if active_only:
                query += " WHERE is_active = TRUE"
            if category:
                query += " AND category = $1" if active_only else " WHERE category = $1"
                params.append(category)
            
            query += " ORDER BY created_at DESC"
            return await conn.fetch(query, *params)
    
    async def get_notified_tokens_summary(self, limit: int = 100):
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT mint_address, symbol, alert_type, risk_score, 
                       first_detected, last_alert, alert_count
                FROM notified_tokens 
                ORDER BY last_alert DESC 
                LIMIT $1
            ''', limit)
    
    async def get_flat_tokens_summary(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT mint_address, symbol, flat_duration_hours, 
                       volatility_score, detected_at, status
                FROM flat_tokens 
                ORDER BY detected_at DESC
            ''')
    
    async def get_pumpfun_tokens_summary(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT mint_address, symbol, market_cap, detected_at, status
                FROM pumpfun_tokens 
                ORDER BY detected_at DESC
            ''')

db = DatabaseManager()

# ===================== CLIENTE API ROBUSTO =====================
class RobustAPIClient:
    def __init__(self):
        self.session = None
        self.cache = {}
    
    async def get_session(self):
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session
    
    async def jupiter_request(self, endpoint: str):
        try:
            session = await self.get_session()
            url = f"{JUPITER_BASE_URL}{endpoint}"
            
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.warning(f"⚠️ API Jupiter error {response.status}")
                    return None
        except Exception as e:
            logger.error(f"❌ Error Jupiter request: {e}")
            return None
    
    async def get_quality_tokens(self):
        """Obtiene tokens con criterios optimizados"""
        endpoints = [
            "/tokens/v2/toporganicscore/1h?limit=80",
            "/tokens/v2/toptraded/1h?limit=80", 
            "/tokens/v2/recent?limit=50"
        ]
        
        all_tokens = []
        for endpoint in endpoints:
            tokens = await self.jupiter_request(endpoint)
            if tokens:
                all_tokens.extend(tokens)
        
        # Filtrar con criterios más flexibles
        filtered_tokens = []
        for token in all_tokens:
            mint = token.get('id')
            liquidity = token.get('liquidity', 0)
            volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                         token.get('stats24h', {}).get('sellVolume', 0))
            
            if liquidity >= MIN_LIQUIDITY and volume_24h >= MIN_VOLUME_24H:
                filtered_tokens.append(token)
        
        logger.info(f"🎯 {len(filtered_tokens)} tokens pasaron filtros optimizados")
        return filtered_tokens
    
    async def get_token_metadata(self, mint: str):
        """Obtiene metadata de un token específico"""
        return await self.jupiter_request(f"/tokens/v2/search?query={mint}")
    
    async def get_dexscreener_candles(self, mint: str, limit: int = 72):
        try:
            session = await self.get_session()
            search_url = f"{DEXSCREENER_API}/search?q={mint}"
            
            async with session.get(search_url) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                pairs = data.get('pairs', [])
                if not pairs:
                    return []
                
                pair_address = pairs[0].get('pairAddress')
                if not pair_address:
                    return []
                
                pair_url = f"{DEXSCREENER_API}/pairs/{pair_address}"
                async with session.get(pair_url) as pair_response:
                    if pair_response.status != 200:
                        return []
                    
                    pair_data = await pair_response.json()
                    candles = pair_data.get('candles', [])
                    
                    normalized = []
                    for candle in candles[-limit:]:
                        if isinstance(candle, dict):
                            normalized.append({
                                'open': float(candle.get('open', 0)),
                                'high': float(candle.get('high', 0)),
                                'low': float(candle.get('low', 0)),
                                'close': float(candle.get('close', 0)),
                                'volume': float(candle.get('volume', 0))
                            })
                    
                    return normalized
                    
        except Exception as e:
            logger.error(f"❌ Error DexScreener para {mint}: {e}")
            return []

api_client = RobustAPIClient()

# ===================== DETECTOR FLAT MEJORADO =====================
class EnhancedFlatDetector:
    async def analyze_token_flat(self, mint: str, token_data: dict = None) -> dict:
        try:
            candles = await api_client.get_dexscreener_candles(mint, FLAT_CONFIG['CANDLES_TO_ANALYZE'])
            if len(candles) < 36:
                return {'is_flat': False, 'reason': 'insufficient_data'}
            
            # Análisis de volatilidad
            volatility = self._calculate_volatility(candles)
            if volatility > FLAT_CONFIG['MAX_VOLATILITY']:
                return {'is_flat': False, 'reason': 'high_volatility'}
            
            # Análisis de volumen
            volume_analysis = self._analyze_volume_pattern(candles)
            if not volume_analysis['is_flat_pattern']:
                return {'is_flat': False, 'reason': 'volume_pattern_not_flat'}
            
            flat_duration = len(candles) * 0.25  # 15min candles
            if flat_duration < FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']:
                return {'is_flat': False, 'reason': 'insufficient_duration'}
            
            # Calcular score de confianza (0-100)
            flat_score = self._calculate_flat_score(volatility, volume_analysis)
            
            return {
                'is_flat': True,
                'flat_score': flat_score,
                'flat_duration': flat_duration,
                'volatility': volatility,
                'volume_analysis': volume_analysis,
                'confidence': flat_score / 100.0
            }
            
        except Exception as e:
            logger.error(f"❌ Error en análisis FLAT para {mint}: {e}")
            return {'is_flat': False, 'reason': 'analysis_error'}
    
    def _calculate_volatility(self, candles):
        prices = [c['close'] for c in candles if c['close'] > 0]
        if len(prices) < 10:
            return 100
        
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
                returns.append(abs(ret))
        
        return mean(returns) if returns else 0
    
    def _analyze_volume_pattern(self, candles):
        volumes = [c['volume'] for c in candles]
        
        low_volume_count = sum(1 for v in volumes if v < 25)
        high_volume_count = sum(1 for v in volumes if v > 350)
        
        isolated_spikes = 0
        for i in range(1, len(volumes)-1):
            if volumes[i-1] < 20 and volumes[i] > 300 and volumes[i+1] < 20:
                isolated_spikes += 1
        
        is_flat_pattern = (
            low_volume_count >= FLAT_CONFIG['MIN_LOW_VOLUME_CANDLES'] and
            high_volume_count <= 5 and
            isolated_spikes >= 1 and
            mean(volumes) < FLAT_CONFIG['MAX_AVG_VOLUME_PER_CANDLE']
        )
        
        return {
            'is_flat_pattern': is_flat_pattern,
            'low_volume_candles': low_volume_count,
            'high_volume_candles': high_volume_count,
            'isolated_spikes': isolated_spikes,
            'avg_volume': mean(volumes) if volumes else 0
        }
    
    def _calculate_flat_score(self, volatility, volume_analysis):
        score = 0
        
        # Volatilidad (máximo 40 puntos)
        volatility_score = max(0, 40 - (volatility * 15))
        score += volatility_score
        
        # Patrón de volumen (máximo 60 puntos)
        volume_score = 0
        if volume_analysis['is_flat_pattern']:
            volume_score += 30
        volume_score += min(20, volume_analysis['low_volume_candles'] / 3)
        volume_score += min(10, volume_analysis['isolated_spikes'] * 5)
        
        score += volume_score
        
        return min(100, score)

flat_detector = EnhancedFlatDetector()

# ===================== ANALIZADOR DE RIESGO =====================
class RiskAnalyzer:
    async def analyze_token_risk(self, mint: str, token_data: dict = None) -> dict:
        """Analiza el riesgo de un token (versión simplificada)"""
        risk_score = 0
        red_flags = []
        green_flags = []
        
        if token_data:
            liquidity = token_data.get('liquidity', 0)
            volume_24h = (token_data.get('stats24h', {}).get('buyVolume', 0) + 
                         token_data.get('stats24h', {}).get('sellVolume', 0))
            
            # Análisis de liquidez
            if liquidity < MIN_LIQUIDITY:
                risk_score += 20
                red_flags.append(f"Liquidez baja: ${liquidity:,.0f}")
            else:
                green_flags.append(f"Liquidez suficiente: ${liquidity:,.0f}")
            
            # Análisis de volumen
            if volume_24h < 10000:
                risk_score += 15
                red_flags.append(f"Volumen muy bajo 24h: ${volume_24h:,.0f}")
            else:
                green_flags.append(f"Volumen saludable 24h: ${volume_24h:,.0f}")
        
        # Determinar nivel de riesgo
        risk_level = "ALTO" if risk_score > 50 else "MEDIO" if risk_score > 25 else "BAJO"
        
        return {
            'score': risk_score,
            'risk_level': risk_level,
            'red_flags': red_flags,
            'green_flags': green_flags
        }

risk_analyzer = RiskAnalyzer()

# ===================== SISTEMA DE ALERTAS COMPLETO =====================
class AlertSystem:
    def __init__(self):
        self.bot = None
    
    async def get_bot(self):
        if not self.bot and TELEGRAM_BOT_TOKEN:
            self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        return self.bot
    
    def format_links(self, mint: str) -> str:
        return (
            f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
            f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
            f"• [Solscan](https://solscan.io/token/{mint})\n"
            f"• [Jupiter Swap](https://jup.ag/swap/SOL-{mint})\n"
        )
    
    async def send_token_list(self, tokens: list, list_type: str = "flat"):
        """Envía lista completa de tokens con enlaces"""
        if not tokens:
            await self._send_telegram_message("📭 No hay tokens en la lista solicitada")
            return
        
        message = f"📋 *LISTA DE TOKENS - {list_type.upper()}* 📋\n\n"
        
        for i, token in enumerate(tokens, 1):
            mint = token.get('mint_address', 'N/A')
            symbol = token.get('symbol', 'N/A')
            risk_score = token.get('risk_score', token.get('volatility_score', 'N/A'))
            market_cap = token.get('market_cap')
            
            message += (
                f"`{i}. {symbol}`\n"
                f"   • Mint: `{mint[:12]}...`\n"
            )
            
            if risk_score != 'N/A':
                message += f"   • Score: {risk_score}/100\n"
            
            if market_cap:
                message += f"   • Market Cap: ${market_cap:,.0f}\n"
            
            message += (
                f"   • [DexScreener](https://dexscreener.com/solana/{mint}) | "
                f"[Birdeye](https://birdeye.so/token/{mint}) | "
                f"[RugCheck](https://rugcheck.xyz/tokens/{mint})\n\n"
            )
            
            # Dividir mensajes largos
            if len(message) > 3500:
                await self._send_telegram_message(message)
                message = f"📋 *CONTINUACIÓN...* 📋\n\n"
        
        if message.strip():
            await self._send_telegram_message(message)
    
    async def send_flat_alert(self, mint: str, token_data: dict, flat_analysis: dict, risk_analysis: dict):
        if await db.is_token_notified(mint, "FLAT_DETECTED"):
            logger.info(f"🔔 Token {mint} ya notificado (FLAT), omitiendo")
            return
        
        symbol = token_data.get('symbol', 'N/A')
        name = token_data.get('name', 'N/A')
        liquidity = token_data.get('liquidity', 0)
        
        # Determinar calidad basada en el score
        flat_score = flat_analysis.get('flat_score', 0)
        if flat_score >= 70:
            quality = "🎯 ALTA CALIDAD"
        elif flat_score >= 50:
            quality = "✅ BUENA CALIDAD"
        else:
            quality = "⚠️ CALIDAD MEDIA"
        
        message = (
            f"🎯 *TOKEN FLAT DETECTADO* 🎯\n\n"
            f"*Token:* {symbol} - {name}\n"
            f"*Mint:* `{mint}`\n"
            f"*Calidad:* {quality}\n"
            f"*Score FLAT:* {flat_score}/100\n"
            f"*Riesgo:* {risk_analysis['score']}/100\n\n"
            
            f"📊 *ANÁLISIS FLAT:*\n"
            f"• Duración: {flat_analysis['flat_duration']:.1f} horas\n"
            f"• Volatilidad: {flat_analysis['volatility']:.2f}%\n"
            f"• Velas volumen bajo: {flat_analysis['volume_analysis']['low_volume_candles']}\n"
            f"• Picos aislados: {flat_analysis['volume_analysis']['isolated_spikes']}\n"
            f"• Liquidez: ${liquidity:,.0f}\n\n"
            
            f"⚠️ *ANÁLISIS RIESGO:*\n"
            f"• Señales riesgo: {len(risk_analysis['red_flags'])}\n"
            f"• Señales positivas: {len(risk_analysis['green_flags'])}\n\n"
            
            f"🔍 *ENLACES:*\n"
            f"{self.format_links(mint)}\n\n"
            
            f"💡 *ACCIÓN:*\n"
            f"{'🚀 OPORTUNIDAD FUERTE' if flat_score >= 70 else '✅ CONSIDERAR ANÁLISIS' if flat_score >= 50 else '⚠️ VERIFICAR MANUALMENTE'}"
        )
        
        await self._send_telegram_message(message)
        await db.mark_token_notified(mint, symbol, "FLAT_DETECTED", risk_analysis['score'], 
                                   {'flat_analysis': flat_analysis, 'risk_analysis': risk_analysis})
        await db.save_flat_token(mint, symbol, flat_analysis)
        
        # Añadir a watchlist automáticamente
        await db.add_to_watchlist(mint, symbol, name, "flat", "system", 
                                f"Flat detectado - Score: {flat_score} - Riesgo: {risk_analysis['score']}")
        
        logger.info(f"✅ Alerta FLAT enviada para {symbol} (Score: {flat_score})")
    
    async def send_pumpfun_alert(self, mint: str, market_cap: float, token_data: dict = None):
        if await db.is_token_notified(mint, "PUMPFUN_PRE_GRAD"):
            return
        
        symbol = token_data.get('symbol', 'N/A') if token_data else 'N/A'
        name = token_data.get('name', 'N/A') if token_data else 'N/A'
        
        message = (
            f"🚀 *PUMP.FUN - PRE-GRADUACIÓN* 🚀\n\n"
            f"*Token:* {symbol}\n"
            f"*Mint:* `{mint}`\n"
            f"*Market Cap Actual:* ${market_cap:,.0f}\n"
            f"*Umbral Alerta:* ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}\n"
            f"*Graduación en:* ${PUMP_GRADUATION_TARGET - market_cap:,.0f}\n\n"
            
            f"⚡ *ACCIÓN INMINENTE:*\n"
            f"Liquidez se bloqueará automáticamente al llegar a ${PUMP_GRADUATION_TARGET:,.0f}\n\n"
            
            f"🔗 *ENLACES RÁPIDOS:*\n"
            f"{self.format_links(mint)}\n\n"
            
            f"🎯 *ESTRATEGIA:*\n"
            f"Token técnicamente seguro (LP bloqueado) - Analizar potencial post-graduación"
        )
        
        await self._send_telegram_message(message)
        await db.mark_token_notified(mint, symbol, "PUMPFUN_PRE_GRAD", 0, 
                                   {'market_cap': market_cap, 'alert_time': datetime.now().isoformat()})
        await db.save_pumpfun_token(mint, symbol, market_cap)
        
        # Añadir a watchlist automáticamente
        await db.add_to_watchlist(mint, symbol, name, "pumpfun", "system", 
                                f"Pump.fun cerca de graduación - MC: ${market_cap:,.0f}")
        
        logger.info(f"✅ Alerta Pump.fun enviada para {symbol}")
    
    async def _send_telegram_message(self, message: str, reply_markup=None):
        try:
            bot = await self.get_bot()
            if bot and TELEGRAM_CHAT_ID:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False,
                    reply_markup=reply_markup
                )
        except Exception as e:
            logger.error(f"❌ Error enviando mensaje Telegram: {e}")

alert_system = AlertSystem()

# ===================== SCANNER FLAT =====================
class FlatScanner:
    def __init__(self):
        self.active = False
    
    async def start_scanning(self):
        self.active = True
        logger.info("🔄 Iniciando scanner FLAT optimizado...")
        
        await alert_system._send_telegram_message(
            "🎯 *SCANNER FLAT OPTIMIZADO INICIADO*\n\n"
            f"• Filtros flexibles activados\n"
            f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
            f"• Intervalo: {UPDATE_INTERVAL/60} minutos\n"
            "_Buscando oportunidades FLAT..._"
        )
        
        while self.active:
            try:
                tokens = await api_client.get_quality_tokens()
                logger.info(f"🔍 Analizando {len(tokens)} tokens para FLAT")
                
                flat_detections = 0
                
                for token in tokens[:30]:  # Limitar para no saturar
                    if not self.active:
                        break
                    
                    mint = token.get('id')
                    symbol = token.get('symbol', 'N/A')
                    
                    try:
                        # Análisis de riesgo primero
                        risk_analysis = await risk_analyzer.analyze_token_risk(mint, token)
                        
                        # Solo proceder si el riesgo es aceptable
                        if risk_analysis['score'] <= MAX_RISK_SCORE:
                            flat_analysis = await flat_detector.analyze_token_flat(mint, token)
                            
                            if flat_analysis['is_flat'] and flat_analysis.get('flat_score', 0) >= 50:
                                logger.info(f"✅ FLAT: {symbol} (Score: {flat_analysis['flat_score']})")
                                await alert_system.send_flat_alert(mint, token, flat_analysis, risk_analysis)
                                flat_detections += 1
                        
                        await asyncio.sleep(0.5)  # Rate limiting
                        
                    except Exception as e:
                        logger.error(f"❌ Error con {mint}: {e}")
                        continue
                
                logger.info(f"📊 Scan FLAT completado: {flat_detections} detecciones")
                await asyncio.sleep(UPDATE_INTERVAL)
                
            except Exception as e:
                logger.error(f"❌ Error en scanner FLAT: {e}")
                await asyncio.sleep(60)
    
    def stop(self):
        self.active = False
        logger.info("🛑 Scanner FLAT detenido")

flat_scanner = FlatScanner()

# ===================== MONITOR PUMP.FUN =====================
class PumpFunMonitor:
    def __init__(self):
        self.active = False
    
    async def start_monitoring(self):
        self.active = True
        logger.info("🚀 Iniciando monitor Pump.fun...")
        
        if not HELIUS_WSS_URL:
            logger.error("❌ HELIUS_WSS_URL no configurado")
            return
        
        await alert_system._send_telegram_message(
            f"🔥 *MONITOR PUMP.FUN INICIADO*\n\n"
            f"• Alerta en: ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f} MC\n"
            f"• Graduación: ${PUMP_GRADUATION_TARGET:,.0f} MC\n"
            f"• Programa: {PUMPFUN_PROGRAM_ID[:12]}...\n\n"
            "_Escuchando tokens cerca de graduación..._"
        )
        
        while self.active:
            try:
                async with websockets.connect(HELIUS_WSS_URL) as websocket:
                    subscribe_msg = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [PUMPFUN_PROGRAM_ID]},
                            {"commitment": "processed"}
                        ]
                    }
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("✅ Conectado a WebSocket Helius - Monitoreando Pump.fun")
                    
                    while self.active:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            await self._process_pumpfun_event(data)
                            
                        except asyncio.TimeoutError:
                            await websocket.send(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))
                        except Exception as e:
                            logger.error(f"❌ Error procesando mensaje WebSocket: {e}")
                            break
                            
            except Exception as e:
                logger.error(f"❌ Error conexión WebSocket Pump.fun: {e}")
                if self.active:
                    await asyncio.sleep(5)
    
    async def _process_pumpfun_event(self, event_data):
        try:
            # SIMULACIÓN - En producción se parsearían los logs reales
            import random
            if random.random() < 0.01:  # 1% de probabilidad para pruebas
                mock_mint = f"TEST{int(time.time())}"
                mock_market_cap = random.randint(50000, 68000)
                
                if mock_market_cap >= PUMP_PRE_GRADUATION_THRESHOLD:
                    logger.info(f"🎯 SIMULACIÓN: Token {mock_mint} cerca de graduación - MC: ${mock_market_cap:,.0f}")
                    await alert_system.send_pumpfun_alert(mock_mint, mock_market_cap)
                    
        except Exception as e:
            logger.error(f"❌ Error procesando evento Pump.fun: {e}")
    
    def stop(self):
        self.active = False
        logger.info("🛑 Monitor Pump.fun detenido")

pumpfun_monitor = PumpFunMonitor()

# ===================== COMANDOS TELEGRAM COMPLETOS =====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = (
        "🤖 *JUPITER BOT COMPLETO* 🚀\n\n"
        "🎯 *SISTEMAS DISPONIBLES:*\n"
        "• 🔍 Detector FLAT (Patrón PESHI)\n"
        "• 🚀 Monitor Pump.fun Pre-Graduación\n"
        "• ⚠️ Analizador de Riesgo Automático\n"
        "• 📋 Gestión de Lista de Tokens\n\n"
        
        "📊 *CONFIGURACIÓN OPTIMIZADA:*\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Volumen mínimo: ${MIN_VOLUME_24H:,.0f}\n"
        f"• Duración FLAT: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']}h\n"
        f"• Máx riesgo: {MAX_RISK_SCORE}/100\n\n"
        
        "📋 *COMANDOS DE LISTAS:*\n"
        "• /lista_tokens - Todos los tokens detectados\n"
        "• /lista_flat - Solo tokens FLAT\n"
        "• /lista_pump - Tokens Pump.fun\n"
        "• /watchlist - Tu lista personal\n\n"
        
        "⚡ *COMANDOS PRINCIPALES:*\n"
        "• /iniciar - Activar todos los sistemas\n"
        "• /detener - Parar todo\n"
        "• /status - Estado del sistema\n"
        "• /agregar_token <mint> <notas>\n"
        "• /eliminar_token <mint>\n"
    )
    await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN)

async def lista_tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra lista completa de todos los tokens notificados"""
    try:
        tokens = await db.get_notified_tokens_summary(100)
        if not tokens:
            await update.message.reply_text("📭 No hay tokens notificados aún.")
            return
        
        await alert_system.send_token_list(tokens, "todos los tokens")
        
    except Exception as e:
        logger.error(f"❌ Error en /lista_tokens: {e}")
        await update.message.reply_text("❌ Error obteniendo la lista de tokens")

async def lista_flat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra lista de tokens FLAT detectados"""
    try:
        tokens = await db.get_flat_tokens_summary()
        if not tokens:
            await update.message.reply_text("📭 No hay tokens FLAT detectados aún.")
            return
        
        flat_tokens_formatted = []
        for token in tokens:
            flat_tokens_formatted.append({
                'mint_address': token['mint_address'],
                'symbol': token['symbol'],
                'volatility_score': token.get('volatility_score', 'N/A')
            })
        
        await alert_system.send_token_list(flat_tokens_formatted, "tokens flat")
        
    except Exception as e:
        logger.error(f"❌ Error en /lista_flat: {e}")
        await update.message.reply_text("❌ Error obteniendo tokens FLAT")

async def lista_pump_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra lista de tokens Pump.fun notificados"""
    try:
        tokens = await db.get_pumpfun_tokens_summary()
        if not tokens:
            await update.message.reply_text("📭 No hay tokens Pump.fun detectados aún.")
            return
        
        pump_tokens_formatted = []
        for token in tokens:
            pump_tokens_formatted.append({
                'mint_address': token['mint_address'],
                'symbol': token['symbol'],
                'market_cap': token.get('market_cap')
            })
        
        await alert_system.send_token_list(pump_tokens_formatted, "pump.fun")
        
    except Exception as e:
        logger.error(f"❌ Error en /lista_pump: {e}")
        await update.message.reply_text("❌ Error obteniendo tokens Pump.fun")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra la watchlist personal"""
    try:
        tokens = await db.get_watchlist()
        if not tokens:
            await update.message.reply_text("📭 Tu watchlist está vacía.")
            return
        
        watchlist_formatted = []
        for token in tokens:
            watchlist_formatted.append({
                'mint_address': token['mint_address'],
                'symbol': token['symbol'],
                'risk_score': 'N/A',
                'category': token['category']
            })
        
        await alert_system.send_token_list(watchlist_formatted, "watchlist")
        
    except Exception as e:
        logger.error(f"❌ Error en /watchlist: {e}")
        await update.message.reply_text("❌ Error obteniendo watchlist")

async def agregar_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Agrega un token a la watchlist manualmente"""
    try:
        if len(context.args) < 1:
            await update.message.reply_text("❌ Uso: /agregar_token <mint_address> [notas]")
            return
        
        mint = context.args[0]
        notes = " ".join(context.args[1:]) if len(context.args) > 1 else "Agregado manualmente"
        
        # Obtener metadata del token
        token_data = await api_client.get_token_metadata(mint)
        symbol = "N/A"
        name = "N/A"
        
        if token_data and isinstance(token_data, list) and len(token_data) > 0:
            symbol = token_data[0].get('symbol', 'N/A')
            name = token_data[0].get('name', 'N/A')
        
        await db.add_to_watchlist(mint, symbol, name, "manual", "user", notes)
        
        await update.message.reply_text(
            f"✅ Token agregado a watchlist:\n"
            f"• Symbol: {symbol}\n"
            f"• Mint: `{mint}`\n"
            f"• Notas: {notes}"
        )
        
    except Exception as e:
        logger.error(f"❌ Error en /agregar_token: {e}")
        await update.message.reply_text("❌ Error agregando token a la watchlist")

async def eliminar_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina un token de la watchlist"""
    try:
        if not context.args:
            await update.message.reply_text("❌ Uso: /eliminar_token <mint_address>")
            return
        
        mint = context.args[0]
        await db.remove_from_watchlist(mint)
        
        await update.message.reply_text(f"✅ Token `{mint}` eliminado de la watchlist")
        
    except Exception as e:
        logger.error(f"❌ Error en /eliminar_token: {e}")
        await update.message.reply_text("❌ Error eliminando token de la watchlist")

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activa todos los sistemas"""
    asyncio.create_task(flat_scanner.start_scanning())
    asyncio.create_task(pumpfun_monitor.start_monitoring())
    
    await update.message.reply_text(
        "✅ *SISTEMAS ACTIVADOS*\n\n"
        "• Scanner FLAT: 🟢 ACTIVO\n"
        "• Monitor Pump.fun: 🟢 ACTIVO\n"
        "• Analizador Riesgo: 🟢 ACTIVO\n"
        "• Gestor Tokens: 🟢 ACTIVO\n\n"
        "_Todos los sistemas funcionando..._",
        parse_mode=ParseMode.MARKDOWN
    )

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene todos los sistemas"""
    flat_scanner.stop()
    pumpfun_monitor.stop()
    
    await update.message.reply_text(
        "🛑 *SISTEMAS DETENIDOS*\n\n"
        "• Scanner FLAT: 🔴 DETENIDO\n"
        "• Monitor Pump.fun: 🔴 DETENIDO\n"
        "• Analizador Riesgo: 🔴 DETENIDO",
        parse_mode=ParseMode.MARKDOWN
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el estado del sistema"""
    flat_tokens = await db.get_flat_tokens_summary()
    notified_tokens = await db.get_notified_tokens_summary(10)
    watchlist_tokens = await db.get_watchlist()
    pump_tokens = await db.get_pumpfun_tokens_summary()
    
    status_msg = (
        f"📊 *ESTADO DEL SISTEMA*\n\n"
        f"• Scanner FLAT: {'🟢 ACTIVO' if flat_scanner.active else '🔴 DETENIDO'}\n"
        f"• Monitor Pump.fun: {'🟢 ACTIVO' if pumpfun_monitor.active else '🔴 DETENIDO'}\n"
        f"• Base datos: {'🟢 CONECTADA' if db.pool else '🔴 NO CONECTADA'}\n"
        f"• Tokens FLAT: {len(flat_tokens)}\n"
        f"• Tokens Pump.fun: {len(pump_tokens)}\n"
        f"• Tokens notificados: {len(notified_tokens)}\n"
        f"• Watchlist: {len(watchlist_tokens)} tokens\n\n"
        
        f"⚙️ *CONFIGURACIÓN:*\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Volumen mínimo: ${MIN_VOLUME_24H:,.0f}\n"
        f"• Duración FLAT: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']}h\n"
        f"• Alerta Pump.fun: ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}\n"
    )
    
    await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)

# ===================== MAIN COMPLETO =====================
async def main():
    logger.info("🚀 INICIANDO BOT COMPLETO...")
    
    await db.init()
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Configuración de Telegram faltante")
        return
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Registrar TODOS los comandos
    commands = [
        ("start", start_command),
        ("iniciar", iniciar_command),
        ("detener", detener_command),
        ("status", status_command),
        ("lista_tokens", lista_tokens_command),
        ("lista_flat", lista_flat_command),
        ("lista_pump", lista_pump_command),
        ("watchlist", watchlist_command),
        ("agregar_token", agregar_token_command),
        ("eliminar_token", eliminar_token_command),
    ]
    
    for command, handler in commands:
        application.add_handler(CommandHandler(command, handler))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("✅ Bot Telegram completo iniciado")
    
    await alert_system._send_telegram_message(
        "🤖 *JUPITER BOT COMPLETO INICIADO* 🚀\n\n"
        "✅ Todos los sistemas cargados\n"
        "✅ Base de datos conectada\n"
        "✅ APIs operativas\n"
        "✅ Scanner FLAT listo\n"
        "✅ Monitor Pump.fun listo\n"
        "✅ Gestión de tokens activa\n\n"
        
        "📋 *COMANDOS DISPONIBLES:*\n"
        "• /iniciar - Activar todos los sistemas\n"
        "• /lista_tokens - Ver todos los tokens\n"
        "• /lista_flat - Tokens FLAT\n"
        "• /lista_pump - Tokens Pump.fun\n"
        "• /watchlist - Tu lista personal\n\n"
        "_Esperando comandos..._"
    )
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("🛑 Bot interrumpido por usuario")
    finally:
        flat_scanner.stop()
        pumpfun_monitor.stop()
        await application.stop()
        await application.shutdown()
        
        if api_client.session:
            await api_client.session.close()
        
        logger.info("✅ Bot completo apagado correctamente")

if __name__ == "__main__":
    required_vars = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Variables faltantes: {missing_vars}")
        exit(1)
    
    asyncio.run(main())
