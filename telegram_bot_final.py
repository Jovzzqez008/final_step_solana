# bot_jupiter_optimized_simple.py - VERSIÓN SIMPLIFICADA Y FUNCIONAL
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

# ===================== CONFIGURACIÓN DE LOGGING CORREGIDA =====================
logger = logging.getLogger("jupiter_optimized")
logger.setLevel(logging.DEBUG)

# Evitar logs duplicados
logger.propagate = False

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Handler para consola
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

# Handler para archivo principal
file_handler = logging.FileHandler('bot_optimized.log')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)

# Handler para debug
debug_handler = logging.FileHandler('bot_debug.log')
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)
logger.addHandler(debug_handler)

# ===================== CONFIGURACIÓN OPTIMIZADA =====================
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

# ⚠️ FILTROS MÁS FLEXIBLES
MIN_LIQUIDITY = 15000
MIN_VOLUME_24H = 25000
MAX_RISK_SCORE = 50

# 🔧 CONFIGURACIÓN OPERATIVA
UPDATE_INTERVAL = 1800
JUPITER_BASE_URL = "https://lite-api.jup.ag"

# ===================== CLASE DATABASE MANAGER (SIMPLIFICADA) =====================
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
            # Tablas esenciales
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS notified_tokens (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    alert_type TEXT,
                    risk_score INTEGER,
                    first_detected TIMESTAMP DEFAULT NOW(),
                    last_alert TIMESTAMP DEFAULT NOW()
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS flat_tokens (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    flat_duration_hours INTEGER,
                    volatility_score REAL,
                    detected_at TIMESTAMP DEFAULT NOW()
                )
            ''')
    
    async def mark_token_notified(self, mint: str, symbol: str, alert_type: str, risk_score: int = 0):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO notified_tokens (mint_address, symbol, alert_type, risk_score)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (mint_address) 
                DO UPDATE SET last_alert = NOW()
            ''', mint, symbol, alert_type, risk_score)
    
    async def is_token_notified(self, mint: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT 1 FROM notified_tokens WHERE mint_address = $1", mint)
            return bool(row)

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

# ===================== SISTEMA DE ALERTAS =====================
class AlertSystem:
    def __init__(self):
        self.bot = None
    
    async def get_bot(self):
        if not self.bot and TELEGRAM_BOT_TOKEN:
            self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        return self.bot
    
    async def send_flat_alert(self, mint: str, token_data: dict, flat_analysis: dict):
        if await db.is_token_notified(mint):
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
            f"*Score:* {flat_score}/100\n\n"
            
            f"📊 *ANÁLISIS FLAT:*\n"
            f"• Duración: {flat_analysis['flat_duration']:.1f} horas\n"
            f"• Volatilidad: {flat_analysis['volatility']:.2f}%\n"
            f"• Velas volumen bajo: {flat_analysis['volume_analysis']['low_volume_candles']}\n"
            f"• Picos aislados: {flat_analysis['volume_analysis']['isolated_spikes']}\n"
            f"• Liquidez: ${liquidity:,.0f}\n\n"
            
            f"🔍 *ENLACES:*\n"
            f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
            f"• [Birdeye](https://birdeye.so/token/{mint})\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n\n"
            
            f"💡 *ACCIÓN:*\n"
            f"{'🚀 OPORTUNIDAD FUERTE' if flat_score >= 70 else '✅ CONSIDERAR ANÁLISIS' if flat_score >= 50 else '⚠️ VERIFICAR MANUALMENTE'}"
        )
        
        await self._send_telegram_message(message)
        await db.mark_token_notified(mint, symbol, "FLAT_DETECTED", flat_score)
        
        logger.info(f"✅ Alerta FLAT enviada para {symbol} (Score: {flat_score})")
    
    async def _send_telegram_message(self, message: str):
        try:
            bot = await self.get_bot()
            if bot and TELEGRAM_CHAT_ID:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False
                )
        except Exception as e:
            logger.error(f"❌ Error enviando mensaje Telegram: {e}")

alert_system = AlertSystem()

# ===================== SCANNER OPTIMIZADO =====================
class OptimizedScanner:
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
            "_Buscando oportunidades..._"
        )
        
        while self.active:
            try:
                tokens = await api_client.get_quality_tokens()
                logger.info(f"🔍 Analizando {len(tokens)} tokens")
                
                flat_detections = 0
                
                for token in tokens[:30]:  # Limitar para no saturar
                    if not self.active:
                        break
                    
                    mint = token.get('id')
                    symbol = token.get('symbol', 'N/A')
                    
                    try:
                        flat_analysis = await flat_detector.analyze_token_flat(mint, token)
                        
                        if flat_analysis['is_flat'] and flat_analysis.get('flat_score', 0) >= 50:
                            logger.info(f"✅ FLAT: {symbol} (Score: {flat_analysis['flat_score']})")
                            await alert_system.send_flat_alert(mint, token, flat_analysis)
                            flat_detections += 1
                        
                        await asyncio.sleep(0.5)  # Rate limiting
                        
                    except Exception as e:
                        logger.error(f"❌ Error con {mint}: {e}")
                        continue
                
                logger.info(f"📊 Scan completado: {flat_detections} detecciones")
                await asyncio.sleep(UPDATE_INTERVAL)
                
            except Exception as e:
                logger.error(f"❌ Error en scanner: {e}")
                await asyncio.sleep(60)
    
    def stop(self):
        self.active = False

scanner = OptimizedScanner()

# ===================== COMANDOS TELEGRAM =====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = (
        "🤖 *JUPITER BOT OPTIMIZADO* 🚀\n\n"
        "✅ *CONFIGURACIÓN MEJORADA:*\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Volumen mínimo: ${MIN_VOLUME_24H:,.0f}\n"
        f"• Duración FLAT: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']}h\n"
        f"• Máx riesgo: {MAX_RISK_SCORE}/100\n\n"
        
        "⚡ *COMANDOS:*\n"
        "• /iniciar - Activar scanner\n"
        "• /detener - Parar scanner\n"
        "• /status - Estado del sistema\n"
    )
    await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN)

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(scanner.start_scanning())
    await update.message.reply_text("✅ *Scanner activado*", parse_mode=ParseMode.MARKDOWN)

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    scanner.stop()
    await update.message.reply_text("🛑 *Scanner detenido*", parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = (
        f"📊 *ESTADO DEL SISTEMA*\n\n"
        f"• Scanner: {'🟢 ACTIVO' if scanner.active else '🔴 DETENIDO'}\n"
        f"• Base datos: {'🟢 CONECTADA' if db.pool else '🔴 NO CONECTADA'}\n"
        f"• APIs: 🟢 OPERATIVAS\n\n"
        f"⚙️ *CONFIGURACIÓN:*\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Volumen mínimo: ${MIN_VOLUME_24H:,.0f}\n"
    )
    await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)

# ===================== MAIN =====================
async def main():
    logger.info("🚀 INICIANDO BOT OPTIMIZADO...")
    
    await db.init()
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ Configuración de Telegram faltante")
        return
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Comandos básicos
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("iniciar", iniciar_command))
    application.add_handler(CommandHandler("detener", detener_command))
    application.add_handler(CommandHandler("status", status_command))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("✅ Bot Telegram listo")
    
    await alert_system._send_telegram_message(
        "🤖 *BOT OPTIMIZADO INICIADO*\n\n"
        "✅ Filtros flexibles activados\n"
        "✅ Scanner listo\n"
        "✅ Use /iniciar para comenzar"
    )
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("👋 Bot terminado")
    finally:
        scanner.stop()
        await application.stop()
        if api_client.session:
            await api_client.session.close()

if __name__ == "__main__":
    # Verificar variables requeridas
    required_vars = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Variables faltantes: {missing_vars}")
        exit(1)
    
    asyncio.run(main())
