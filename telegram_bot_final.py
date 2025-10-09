# bot_jupiter_optimized.py - VERSIÓN MEJORADA Y MÁS EFECTIVA
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
from collections import defaultdict, deque
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode

# ===================== CONFIGURACIÓN OPTIMIZADA =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")

# 🎯 CONFIGURACIÓN FLAT OPTIMIZADA (Más oportunidades, menos falsos positivos)
FLAT_CONFIG = {
    'MIN_FLAT_DURATION_HOURS': 12,           # 12+ horas en flat (más realista)
    'MAX_VOLATILITY': 2.5,                   # Hasta 2.5% de volatilidad
    'MAX_AVG_VOLUME_PER_CANDLE': 300,        # $300 promedio por vela 15m
    'MIN_LOW_VOLUME_CANDLES': 6,             # Mín 6 velas con volumen < $20
    'MAX_CONSECUTIVE_GREEN': 3,              # Máx 3 velas verdes consecutivas > $500
    'CANDLES_TO_ANALYZE': 72,                # 18 horas de velas 15m (antes 24h)
    'VOLUME_SPIKE_THRESHOLD': 300,           # Umbral más alto para picos
    'PRICE_CHANGE_THRESHOLD': 15,            # Máx 15% cambio precio durante flat
}

# 🚀 CONFIGURACIÓN PUMP.FUN MEJORADA
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # Actualizado
PUMP_PRE_GRADUATION_THRESHOLD = 55000        # Alertar a $55k MC (más temprano)
PUMP_GRADUATION_TARGET = 69000

# ⚠️ FILTROS DE SEGURIDAD OPTIMIZADOS
MIN_LIQUIDITY = 15000        # $15K en vez de $50K (más oportunidades)
MIN_VOLUME_24H = 25000       # $25K en vez de $50K
MAX_RISK_SCORE = 50          # 50/100 en vez de 40 (más flexible)

# 🔧 CONFIGURACIÓN OPERATIVA MEJORADA
UPDATE_INTERVAL = 1800                       # 30 minutos entre scans (más frecuente)
PUMP_MONITOR_INTERVAL = 15                   # 15 segundos para pump.fun
JUPITER_BASE_URL = "https://lite-api.jup.ag"

# Configuración de logging mejorada
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot_optimized.log'),
        logging.FileHandler('bot_debug.log', level=logging.DEBUG)  # Log detallado
    ]
)
logger = logging.getLogger("jupiter_optimized")

# ===================== MEJORAS EN BASE DE DATOS =====================
class DatabaseManager:
    def __init__(self):
        self.pool = None
        self.retry_count = 0
        self.max_retries = 3
    
    async def init(self):
        """Inicialización con reintentos"""
        for attempt in range(self.max_retries):
            try:
                if DATABASE_URL:
                    self.pool = await asyncpg.create_pool(
                        DATABASE_URL, 
                        min_size=2, 
                        max_size=10,
                        command_timeout=60
                    )
                    await self.create_tables()
                    logger.info("✅ Base de datos PostgreSQL inicializada")
                    return
            except Exception as e:
                logger.warning(f"⚠️ Intento {attempt + 1} de conexión a BD falló: {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)  # Backoff exponencial
                else:
                    logger.error("❌ No se pudo conectar a la base de datos después de múltiples intentos")
    
    # ... (el resto de métodos se mantienen similares pero con mejor manejo de errores)

# ===================== CLIENTE API MEJORADO =====================
class RobustAPIClient:
    def __init__(self):
        self.session = None
        self.jupiter_base = JUPITER_BASE_URL
        self.cache = {}
        self.request_timeout = 15
        self.max_retries = 3
    
    async def get_session(self):
        if not self.session or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.request_timeout)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session
    
    async def robust_request(self, url: str, cache_key: str = None, ttl: int = 300):
        """Petición HTTP con reintentos y manejo robusto de errores"""
        for attempt in range(self.max_retries):
            try:
                session = await self.get_session()
                
                # Verificar cache primero
                if cache_key and cache_key in self.cache:
                    cached_data, timestamp = self.cache[cache_key]
                    if time.time() - timestamp < ttl:
                        return cached_data
                
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if cache_key:
                            self.cache[cache_key] = (data, time.time())
                        return data
                    elif response.status == 429:  # Rate limit
                        wait_time = (2 ** attempt) * 5
                        logger.warning(f"⏳ Rate limit detectado, esperando {wait_time}s")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"❌ Error HTTP {response.status} en {url}")
                        if attempt == self.max_retries - 1:
                            return None
                        
            except asyncio.TimeoutError:
                logger.warning(f"⏰ Timeout en intento {attempt + 1} para {url}")
                if attempt == self.max_retries - 1:
                    return None
            except Exception as e:
                logger.error(f"❌ Error en petición {url}: {e}")
                if attempt == self.max_retries - 1:
                    return None
            
            await asyncio.sleep(1)  # Espera entre reintentos
        
        return None
    
    async def get_quality_tokens_optimized(self):
        """Obtiene tokens de calidad con criterios más amplios"""
        endpoints = [
            "/tokens/v2/toporganicscore/1h?limit=100",  # Más tokens
            "/tokens/v2/toptraded/1h?limit=100", 
            "/tokens/v2/tag?query=verified&limit=50",
            "/tokens/v2/recent?limit=75",  # Más tokens recientes
            "/tokens/v2/trending?limit=50"  # Nuevo: tokens en tendencia
        ]
        
        all_tokens = []
        for endpoint in endpoints:
            tokens = await self.robust_request(
                f"{self.jupiter_base}{endpoint}", 
                f"jupiter_{endpoint}", 
                300  # Cache más corto para datos frescos
            )
            if tokens:
                all_tokens.extend(tokens)
        
        # Filtrar con criterios más flexibles
        unique_tokens = {}
        for token in all_tokens:
            mint = token.get('id')
            if not mint:
                continue
                
            liquidity = token.get('liquidity', 0)
            volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                         token.get('stats24h', {}).get('sellVolume', 0))
            
            # Criterios más flexibles
            if (liquidity >= MIN_LIQUIDITY and 
                volume_24h >= MIN_VOLUME_24H and
                mint not in unique_tokens):
                unique_tokens[mint] = token
        
        logger.info(f"🎯 {len(unique_tokens)} tokens de calidad encontrados (criterios optimizados)")
        return list(unique_tokens.values())

# ===================== DETECTOR FLAT MEJORADO =====================
class EnhancedFlatDetector:
    def __init__(self):
        self.flat_tokens = {}
        self.analysis_cache = {}
    
    async def analyze_token_flat_enhanced(self, mint: str, token_data: dict = None) -> dict:
        """Análisis FLAT mejorado con más métricas"""
        try:
            # Obtener velas con manejo de errores
            candles = await api_client.get_dexscreener_candles(mint, FLAT_CONFIG['CANDLES_TO_ANALYZE'])
            if len(candles) < 36:  # Mínimo 9 horas de datos (antes 12)
                return {'is_flat': False, 'reason': 'insufficient_data', 'confidence': 0}
            
            # Análisis múltiple
            volatility_analysis = self._calculate_enhanced_volatility(candles)
            volume_analysis = self._analyze_volume_pattern_enhanced(candles)
            price_analysis = self._analyze_price_action(candles)
            trend_analysis = self._analyze_trend_context(candles)
            
            # Puntuación compuesta (0-100)
            flat_score = self._calculate_flat_score(
                volatility_analysis, 
                volume_analysis, 
                price_analysis,
                trend_analysis
            )
            
            # Umbral dinámico basado en confianza
            confidence_threshold = 65  # 65/100 en vez de binario
            
            if flat_score >= confidence_threshold:
                flat_duration = self._estimate_flat_duration_enhanced(candles)
                
                return {
                    'is_flat': True,
                    'flat_score': flat_score,
                    'flat_duration': flat_duration,
                    'volatility': volatility_analysis['std_dev'],
                    'volume_analysis': volume_analysis,
                    'price_analysis': price_analysis,
                    'trend_analysis': trend_analysis,
                    'confidence': min(flat_score / 100, 0.95)
                }
            else:
                return {
                    'is_flat': False, 
                    'reason': 'low_flat_score', 
                    'flat_score': flat_score,
                    'confidence': flat_score / 100
                }
                
        except Exception as e:
            logger.error(f"❌ Error en análisis FLAT mejorado para {mint}: {e}")
            return {'is_flat': False, 'reason': 'analysis_error', 'confidence': 0}
    
    def _calculate_flat_score(self, volatility_analysis, volume_analysis, price_analysis, trend_analysis):
        """Calcula puntuación FLAT compuesta"""
        score = 0
        max_score = 100
        
        # Volatilidad (30 puntos)
        volatility_score = max(0, 30 - (volatility_analysis['std_dev'] * 10))
        score += volatility_score
        
        # Patrón de volumen (30 puntos)
        volume_score = 0
        if volume_analysis['is_flat_pattern']:
            volume_score += 20
        volume_score += min(10, volume_analysis['low_volume_candles'] / 2)
        score += volume_score
        
        # Acción de precio (25 puntos)
        price_score = 25 - (price_analysis['price_range_pct'] / 2)
        score += max(0, price_score)
        
        # Contexto de tendencia (15 puntos)
        if trend_analysis['prev_trend'] == 'up':
            trend_score = 15
        else:
            trend_score = 5
        score += trend_score
        
        return min(max_score, score)
    
    def _analyze_trend_context(self, candles):
        """Analiza el contexto de tendencia previo al flat"""
        if len(candles) < 20:
            return {'prev_trend': 'unknown', 'trend_strength': 0}
        
        # Dividir velas en segmentos
        early_prices = [c['close'] for c in candles[:20]]  # Primeras 5 horas
        late_prices = [c['close'] for c in candles[-20:]]  # Últimas 5 horas
        
        early_avg = mean(early_prices) if early_prices else 0
        late_avg = mean(late_prices) if late_prices else 0
        
        if early_avg == 0:
            return {'prev_trend': 'unknown', 'trend_strength': 0}
        
        trend_pct = ((late_avg - early_avg) / early_avg) * 100
        
        if trend_pct > 10:
            return {'prev_trend': 'up', 'trend_strength': trend_pct}
        elif trend_pct < -10:
            return {'prev_trend': 'down', 'trend_strength': abs(trend_pct)}
        else:
            return {'prev_trend': 'sideways', 'trend_strength': 0}
    
    def _analyze_price_action(self, candles):
        """Análisis mejorado de acción del precio"""
        prices = [c['close'] for c in candles if c['close'] > 0]
        if not prices:
            return {'price_range_pct': 100, 'consolidation': False}
        
        min_price = min(prices)
        max_price = max(prices)
        price_range_pct = ((max_price - min_price) / min_price) * 100
        
        # Verificar consolidación (precio en rango estrecho)
        recent_prices = prices[-24:]  # Últimas 6 horas
        if len(recent_prices) >= 12:
            recent_min = min(recent_prices)
            recent_max = max(recent_prices)
            recent_range = ((recent_max - recent_min) / recent_min) * 100
            consolidation = recent_range < 5  # Menos del 5% de rango en 6 horas
        else:
            consolidation = False
        
        return {
            'price_range_pct': price_range_pct,
            'consolidation': consolidation,
            'support_level': min_price,
            'resistance_level': max_price
        }
    
    def _analyze_volume_pattern_enhanced(self, candles):
        """Análisis de volumen mejorado"""
        volumes = [c['volume'] for c in candles]
        
        # Métricas básicas
        low_volume_count = sum(1 for v in volumes if v < 25)  # $25 en vez de $20
        medium_volume_count = sum(1 for v in volumes if 25 <= v <= 350)  # Rango ampliado
        high_volume_count = sum(1 for v in volumes if v > 350)
        
        # Detectar picos de volumen más flexible
        isolated_spikes = 0
        volume_spikes = []
        
        for i in range(1, len(volumes)-1):
            prev_avg = mean(volumes[max(0, i-3):i]) if i >= 3 else volumes[i-1]
            current_volume = volumes[i]
            next_avg = mean(volumes[i+1:min(len(volumes), i+4)]) if i < len(volumes)-3 else volumes[i+1]
            
            # Pico si volumen actual es 3x el promedio circundante
            if (current_volume > FLAT_CONFIG['VOLUME_SPIKE_THRESHOLD'] and 
                current_volume > prev_avg * 3 and 
                current_volume > next_avg * 3):
                isolated_spikes += 1
                volume_spikes.append({
                    'position': i,
                    'volume': current_volume,
                    'multiplier': current_volume / max(prev_avg, next_avg)
                })
        
        # Patrón de velas verdes consecutivas más flexible
        consecutive_green = 0
        max_consecutive = 0
        green_sequences = []
        
        current_sequence = 0
        for i in range(1, len(candles)):
            if candles[i]['close'] >= candles[i-1]['close'] and volumes[i] > 400:  # $400 en vez de $500
                current_sequence += 1
            else:
                if current_sequence > 0:
                    green_sequences.append(current_sequence)
                    max_consecutive = max(max_consecutive, current_sequence)
                current_sequence = 0
        
        max_consecutive = max(max_consecutive, current_sequence)
        
        # Condiciones FLAT más flexibles
        is_flat_pattern = (
            low_volume_count >= FLAT_CONFIG['MIN_LOW_VOLUME_CANDLES'] and
            medium_volume_count <= 10 and  # Más flexible
            high_volume_count <= 5 and     # Más flexible
            isolated_spikes >= 1 and
            max_consecutive <= FLAT_CONFIG['MAX_CONSECUTIVE_GREEN'] and
            mean(volumes) < FLAT_CONFIG['MAX_AVG_VOLUME_PER_CANDLE']
        )
        
        return {
            'is_flat_pattern': is_flat_pattern,
            'low_volume_candles': low_volume_count,
            'medium_volume_candles': medium_volume_count,
            'high_volume_candles': high_volume_count,
            'isolated_spikes': isolated_spikes,
            'volume_spikes': volume_spikes,
            'max_consecutive_green': max_consecutive,
            'green_sequences': green_sequences,
            'avg_volume': mean(volumes) if volumes else 0,
            'volume_std_dev': pstdev(volumes) if len(volumes) > 1 else 0
        }
    
    def _estimate_flat_duration_enhanced(self, candles):
        """Estima duración mejorada del flat"""
        if len(candles) < 4:
            return 0
        
        # Buscar el inicio del patrón flat
        volumes = [c['volume'] for c in candles]
        prices = [c['close'] for c in candles]
        
        # Encontrar punto donde el volumen se estabiliza
        for i in range(20, len(volumes)):
            recent_volumes = volumes[i-10:i]
            if len(recent_volumes) >= 10 and max(recent_volumes) < 100:
                start_index = i - 10
                duration_hours = (len(candles) - start_index) * 0.25
                return max(duration_hours, FLAT_CONFIG['MIN_FLAT_DURATION_HOURS'])
        
        return len(candles) * 0.25

# ===================== SISTEMA DE ALERTAS MEJORADO =====================
class EnhancedAlertSystem:
    def __init__(self):
        self.bot = None
        self.alert_cooldown = {}  # Prevenir spam de alertas
    
    async def send_enhanced_flat_alert(self, mint: str, token_data: dict, flat_analysis: dict, risk_analysis: dict):
        """Alerta FLAT mejorada con más información"""
        # Cooldown para evitar spam (5 minutos)
        if mint in self.alert_cooldown:
            if time.time() - self.alert_cooldown[mint] < 300:
                logger.info(f"⏳ Alerta en cooldown para {mint}")
                return
        
        if await db.is_token_notified(mint, "FLAT_DETECTED"):
            logger.info(f"🔔 Token {mint} ya notificado (FLAT), omitiendo")
            return
        
        symbol = token_data.get('symbol', 'N/A')
        name = token_data.get('name', 'N/A')
        liquidity = token_data.get('liquidity', 0)
        volume_24h = (token_data.get('stats24h', {}).get('buyVolume', 0) + 
                     token_data.get('stats24h', {}).get('sellVolume', 0))
        
        # Determinar calidad de la señal
        confidence = flat_analysis.get('confidence', 0.5)
        if confidence > 0.8:
            signal_quality = "🎯 ALTA CALIDAD"
        elif confidence > 0.6:
            signal_quality = "✅ BUENA CALIDAD" 
        else:
            signal_quality = "⚠️ CALIDAD MEDIA"
        
        message = (
            f"🎯 *TOKEN FLAT DETECTADO* 🎯\n\n"
            f"*Token:* {symbol} - {name}\n"
            f"*Mint:* `{mint}`\n"
            f"*Calidad Señal:* {signal_quality}\n"
            f"*Confianza:* {flat_analysis.get('flat_score', 0)}/100\n\n"
            
            f"📊 *DATOS MERCADO:*\n"
            f"• Liquidez: ${liquidity:,.0f}\n"
            f"• Volumen 24h: ${volume_24h:,.0f}\n"
            f"• Edad: {self._get_token_age(token_data)}\n\n"
            
            f"📈 *ANÁLISIS FLAT:*\n"
            f"• Tiempo en flat: {flat_analysis['flat_duration']:.1f} horas\n"
            f"• Volatilidad: {flat_analysis['volatility']:.2f}%\n"
            f"• Rango precio: {flat_analysis['price_analysis']['price_range_pct']:.1f}%\n"
            f"• Tendencia previa: {flat_analysis['trend_analysis']['prev_trend']}\n"
            f"• Velas volumen bajo: {flat_analysis['volume_analysis']['low_volume_candles']}\n"
            f"• Picos volumen: {flat_analysis['volume_analysis']['isolated_spikes']}\n\n"
            
            f"⚠️ *ANÁLISIS DE RIESGO:*\n"
            f"• Puntaje: {risk_analysis['score']}/100 ({risk_analysis['risk_level']})\n"
            f"• Señales riesgo: {len(risk_analysis['red_flags'])}\n"
            f"• Señales positivas: {len(risk_analysis['green_flags'])}\n\n"
            
            f"🔍 *ENLACES RÁPIDOS:*\n"
            f"{self.format_links(mint)}\n"
            
            f"💡 *RECOMENDACIÓN:*\n"
            f"{self._get_recommendation(risk_analysis['score'], flat_analysis.get('flat_score', 0))}"
        )
        
        # Actualizar cooldown
        self.alert_cooldown[mint] = time.time()
        
        # Añadir a watchlist automáticamente
        await db.add_to_watchlist(
            mint, symbol, name, "flat", "system", 
            f"Flat detectado - Score: {flat_analysis.get('flat_score', 0)} - Riesgo: {risk_analysis['score']}"
        )
        
        await self._send_telegram_message(message)
        await db.mark_token_notified(
            mint, symbol, "FLAT_DETECTED", 
            risk_analysis['score'], 
            {
                'flat_analysis': flat_analysis, 
                'risk_analysis': risk_analysis,
                'confidence': confidence
            }
        )
        
        logger.info(f"✅ Alerta FLAT mejorada enviada para {symbol} (Score: {flat_analysis.get('flat_score', 0)})")
    
    def _get_token_age(self, token_data: dict) -> str:
        """Obtiene la edad del token formateada"""
        try:
            first_pool = token_data.get('firstPool', {})
            created_at = first_pool.get('createdAt')
            
            if created_at:
                created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                age_hours = (datetime.now().astimezone() - created_dt).total_seconds() / 3600
                
                if age_hours < 24:
                    return f"{age_hours:.1f} horas"
                else:
                    return f"{age_hours/24:.1f} días"
        except:
            pass
        
        return "Desconocida"
    
    def _get_recommendation(self, risk_score: int, flat_score: int) -> str:
        """Genera recomendación basada en scores"""
        if risk_score <= 30 and flat_score >= 70:
            return "🚀 OPORTUNIDAD FUERTE - Buen riesgo/recompensa"
        elif risk_score <= 40 and flat_score >= 60:
            return "✅ OPORTUNIDAD SÓLIDA - Considerar entrada"
        elif risk_score <= 50 and flat_score >= 50:
            return "⚠️ OPORTUNIDAD MODERADA - Verificar manualmente"
        else:
            return "🔍 REQUIERE ANÁLISIS - Revisar cuidadosamente"

# ===================== SCANNER OPTIMIZADO =====================
class OptimizedFlatScanner:
    def __init__(self):
        self.active = False
        self.scan_stats = {
            'total_scans': 0,
            'tokens_analyzed': 0,
            'flat_detections': 0,
            'avg_scan_time': 0
        }
    
    async def start_optimized_scanning(self):
        """Scanner optimizado con mejor rendimiento"""
        self.active = True
        logger.info("🔄 Iniciando scanner FLAT optimizado...")
        
        await alert_system._send_telegram_message(
            "🎯 *SCANNER FLAT OPTIMIZADO INICIADO*\n\n"
            f"• Duración mínima: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']} horas\n"
            f"• Volatilidad máxima: {FLAT_CONFIG['MAX_VOLATILITY']}%\n"
            f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
            f"• Intervalo: {UPDATE_INTERVAL/60} minutos\n\n"
            "_Buscando oportunidades con criterios optimizados..._"
        )
        
        while self.active:
            scan_start = time.time()
            try:
                # Obtener tokens con criterios más amplios
                tokens = await api_client.get_quality_tokens_optimized()
                logger.info(f"🔍 Analizando {len(tokens)} tokens para detección FLAT")
                
                flat_detections = 0
                analyzed_tokens = 0
                
                # Procesar tokens en lote con semáforo para no saturar APIs
                semaphore = asyncio.Semaphore(5)  # Máximo 5 concurrentes
                
                async def process_token(token):
                    nonlocal flat_detections, analyzed_tokens
                    async with semaphore:
                        if not self.active:
                            return
                        
                        mint = token.get('id')
                        symbol = token.get('symbol', 'N/A')
                        
                        try:
                            analyzed_tokens += 1
                            
                            # Análisis de riesgo primero
                            risk_analysis = await risk_analyzer.analyze_token_risk(mint, token)
                            
                            # Criterio de riesgo más flexible
                            if risk_analysis['score'] <= MAX_RISK_SCORE:
                                # Análisis FLAT mejorado
                                flat_analysis = await enhanced_flat_detector.analyze_token_flat_enhanced(mint, token)
                                
                                if flat_analysis['is_flat']:
                                    logger.info(
                                        f"✅ FLAT detectado: {symbol} "
                                        f"(Score: {flat_analysis.get('flat_score', 0)}/"
                                        f"Riesgo: {risk_analysis['score']})"
                                    )
                                    
                                    # Solo alertar si confianza es suficiente
                                    if flat_analysis.get('confidence', 0) > 0.6:
                                        await db.save_flat_token(mint, symbol, flat_analysis)
                                        await db.save_risk_analysis(mint, risk_analysis)
                                        await enhanced_alert_system.send_enhanced_flat_alert(
                                            mint, token, flat_analysis, risk_analysis
                                        )
                                        flat_detections += 1
                            
                            await asyncio.sleep(0.5)  # Rate limiting entre tokens
                            
                        except Exception as e:
                            logger.error(f"❌ Error procesando token {mint}: {e}")
                
                # Procesar tokens concurrentemente
                tasks = [process_token(token) for token in tokens[:40]]  # Más tokens
                await asyncio.gather(*tasks, return_exceptions=True)
                
                # Actualizar estadísticas
                scan_time = time.time() - scan_start
                self.scan_stats['total_scans'] += 1
                self.scan_stats['tokens_analyzed'] += analyzed_tokens
                self.scan_stats['flat_detections'] += flat_detections
                self.scan_stats['avg_scan_time'] = (
                    (self.scan_stats['avg_scan_time'] * (self.scan_stats['total_scans'] - 1) + scan_time) / 
                    self.scan_stats['total_scans']
                )
                
                logger.info(
                    f"📊 Scan completado: {flat_detections} FLAT detectados, "
                    f"{analyzed_tokens} tokens analizados, "
                    f"{scan_time:.1f}s total"
                )
                
                await asyncio.sleep(UPDATE_INTERVAL)
                
            except Exception as e:
                logger.error(f"❌ Error en scanner FLAT optimizado: {e}")
                await asyncio.sleep(60)  # Esperar antes de reintentar
    
    async def get_stats(self):
        """Obtiene estadísticas del scanner"""
        return self.scan_stats

# ===================== INICIALIZACIÓN DE COMPONENTES MEJORADOS =====================
db = DatabaseManager()
api_client = RobustAPIClient()
risk_analyzer = RiskAnalyzer()
enhanced_flat_detector = EnhancedFlatDetector()
enhanced_alert_system = EnhancedAlertSystem()
optimized_scanner = OptimizedFlatScanner()

# ... (los comandos de Telegram se mantienen similares pero usando los nuevos componentes)

# ===================== COMANDOS ADICIONALES =====================
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra estadísticas detalladas del scanner"""
    stats = await optimized_scanner.get_stats()
    
    stats_msg = (
        f"📊 *ESTADÍSTICAS DETALLADAS*\n\n"
        f"• Total escaneos: {stats['total_scans']}\n"
        f"• Tokens analizados: {stats['tokens_analyzed']}\n"
        f"• Detecciones FLAT: {stats['flat_detections']}\n"
        f"• Tiempo promedio escaneo: {stats['avg_scan_time']:.1f}s\n"
        f"• Ratio detección: {(stats['flat_detections']/max(1, stats['tokens_analyzed']))*100:.1f}%\n\n"
        
        f"⚙️ *CONFIGURACIÓN ACTUAL:*\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Volumen mínimo: ${MIN_VOLUME_24H:,.0f}\n"
        f"• Riesgo máximo: {MAX_RISK_SCORE}/100\n"
        f"• Volatilidad FLAT: {FLAT_CONFIG['MAX_VOLATILITY']}%\n"
        f"• Duración FLAT: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']}+ horas\n"
    )
    
    await update.message.reply_text(stats_msg, parse_mode=ParseMode.MARKDOWN)

async def adjust_filters_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ajusta filtros en tiempo real"""
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                "❌ Uso: /ajustar_filtros <filtro> <valor>\n\n"
                "Filtros disponibles:\n"
                "• liquidez <valor>\n"
                "• volumen <valor>\n" 
                "• riesgo <valor>\n"
                "• volatilidad <valor>\n"
                "• duracion <valor>"
            )
            return
        
        filter_name = context.args[0].lower()
        new_value = float(context.args[1])
        
        global MIN_LIQUIDITY, MIN_VOLUME_24H, MAX_RISK_SCORE
        changes = []
        
        if filter_name == "liquidez":
            old_value = MIN_LIQUIDITY
            MIN_LIQUIDITY = new_value
            changes.append(f"Liquidez: ${old_value:,.0f} → ${new_value:,.0f}")
        
        elif filter_name == "volumen":
            old_value = MIN_VOLUME_24H
            MIN_VOLUME_24H = new_value
            changes.append(f"Volumen: ${old_value:,.0f} → ${new_value:,.0f}")
        
        elif filter_name == "riesgo":
            old_value = MAX_RISK_SCORE
            MAX_RISK_SCORE = new_value
            changes.append(f"Riesgo máximo: {old_value} → {new_value}")
        
        elif filter_name == "volatilidad":
            old_value = FLAT_CONFIG['MAX_VOLATILITY']
            FLAT_CONFIG['MAX_VOLATILITY'] = new_value
            changes.append(f"Volatilidad: {old_value}% → {new_value}%")
        
        elif filter_name == "duracion":
            old_value = FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']
            FLAT_CONFIG['MIN_FLAT_DURATION_HOURS'] = new_value
            changes.append(f"Duración: {old_value}h → {new_value}h")
        
        else:
            await update.message.reply_text("❌ Filtro no reconocido")
            return
        
        change_text = "\n".join(changes)
        await update.message.reply_text(
            f"✅ *FILTROS ACTUALIZADOS*\n\n{change_text}",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except ValueError:
        await update.message.reply_text("❌ El valor debe ser un número")
    except Exception as e:
        logger.error(f"❌ Error ajustando filtros: {e}")
        await update.message.reply_text("❌ Error ajustando filtros")

# ===================== MAIN OPTIMIZADO =====================
async def main_optimized():
    """Función principal optimizada"""
    logger.info("🚀 INICIANDO JUPITER BOT OPTIMIZADO...")
    
    # Inicializar componentes
    await db.init()
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return
    
    # Configurar aplicación Telegram
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Registrar comandos (incluyendo los nuevos)
    commands = [
        ("start", start_command),
        ("iniciar", iniciar_command),
        ("detener", detener_command),
        ("status", status_command),
        ("stats", stats_command),
        ("ajustar_filtros", adjust_filters_command),
        ("ajustar_std", ajustar_std_command),
        ("ajustar_vol", ajustar_vol_command),
        ("lista_tokens", lista_tokens_command),
        ("lista_flat", lista_flat_command),
        ("lista_pump", lista_pump_command),
        ("watchlist", watchlist_command),
        ("agregar_token", agregar_token_command),
        ("eliminar_token", eliminar_token_command),
    ]
    
    for command, handler in commands:
        application.add_handler(CommandHandler(command, handler))
    
    # Iniciar bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("✅ Bot Telegram optimizado iniciado")
    
    await enhanced_alert_system._send_telegram_message(
        "🤖 *JUPITER BOT OPTIMIZADO INICIADO* 🚀\n\n"
        "✅ Sistemas mejorados cargados\n"
        "✅ Filtros optimizados para más oportunidades\n"
        "✅ Detector FLAT mejorado\n"
        "✅ Análisis de riesgo más preciso\n\n"
        
        "🎯 *CONFIGURACIÓN OPTIMIZADA:*\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Volumen mínimo: ${MIN_VOLUME_24H:,.0f}\n"
        f"• Duración FLAT: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']}h\n"
        f"• Volatilidad máxima: {FLAT_CONFIG['MAX_VOLATILITY']}%\n\n"
        
        "📊 *NUEVOS COMANDOS:*\n"
        "• /stats - Estadísticas detalladas\n"
        "• /ajustar_filtros - Modificar configuración\n\n"
        "_Listo para detectar oportunidades..._"
    )
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("🛑 Bot optimizado interrumpido por usuario")
    finally:
        # Limpieza
        optimized_scanner.stop()
        await application.stop()
        await application.shutdown()
        
        if api_client.session:
            await api_client.session.close()
        
        logger.info("✅ Bot optimizado apagado correctamente")

if __name__ == "__main__":
    required_vars = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DATABASE_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Variables de entorno faltantes: {missing_vars}")
        exit(1)
    
    try:
        asyncio.run(main_optimized())
    except KeyboardInterrupt:
        logger.info("👋 Bot optimizado terminado por el usuario")
    except Exception as e:
        logger.error(f"💥 Error fatal en bot optimizado: {e}")
