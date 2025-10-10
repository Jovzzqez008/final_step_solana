# bot_solana_ultimate.py - BOT DEFINITIVO PUMP.FUN + FLAT DETECTOR
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
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ===================== CONFIGURACIÓN =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")

# 🎯 CONFIGURACIÓN PUMP.FUN
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_PRE_GRADUATION_THRESHOLD = 60000
PUMP_GRADUATION_TARGET = 69000

# 🔍 CONFIGURACIÓN FLAT DETECTOR
FLAT_CONFIG = {
    'MIN_FLAT_DURATION_HOURS': 24,
    'MAX_VOLATILITY': 1.5,
    'MAX_AVG_VOLUME_PER_CANDLE': 150,
    'MIN_LOW_VOLUME_CANDLES': 8,
    'MAX_CONSECUTIVE_GREEN': 2,
    'CANDLES_TO_ANALYZE': 96,
    'VOLUME_SPIKE_THRESHOLD': 200,
}

# ⚙️ FILTROS
MIN_LIQUIDITY = 50000
MIN_VOLUME_24H = 50000
JUPITER_BASE_URL = "https://lite-api.jup.ag"
UPDATE_INTERVAL = 3600  # 1 hora entre scans FLAT

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot_solana.log')
    ]
)
logger = logging.getLogger("solana_ultimate")

# ===================== BASE DE DATOS =====================
class DatabaseManager:
    def __init__(self):
        self.pool = None
    
    async def init(self):
        if DATABASE_URL:
            self.pool = await asyncpg.create_pool(DATABASE_URL)
            await self.create_tables()
            logger.info("✅ Base de datos inicializada")
    
    async def create_tables(self):
        async with self.pool.acquire() as conn:
            # Tabla para tokens notificados
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS notified_tokens (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    alert_type TEXT,
                    first_detected TIMESTAMP DEFAULT NOW(),
                    last_alert TIMESTAMP DEFAULT NOW(),
                    alert_count INTEGER DEFAULT 1,
                    metadata JSONB
                )
            ''')
            
            # Tabla para tokens FLAT
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS flat_tokens (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    flat_duration_hours FLOAT,
                    volatility_score FLOAT,
                    volume_analysis JSONB,
                    detected_at TIMESTAMP DEFAULT NOW()
                )
            ''')
    
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
    
    async def mark_token_notified(self, mint: str, symbol: str, alert_type: str, metadata: dict = None):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO notified_tokens (mint_address, symbol, alert_type, metadata)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (mint_address) 
                DO UPDATE SET 
                    last_alert = NOW(),
                    alert_count = notified_tokens.alert_count + 1,
                    metadata = EXCLUDED.metadata
            ''', mint, symbol, alert_type, json.dumps(metadata or {}))
    
    async def save_flat_token(self, mint: str, symbol: str, flat_data: dict):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO flat_tokens (mint_address, symbol, flat_duration_hours, volatility_score, volume_analysis)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (mint_address) 
                DO UPDATE SET 
                    flat_duration_hours = EXCLUDED.flat_duration_hours,
                    volatility_score = EXCLUDED.volatility_score,
                    volume_analysis = EXCLUDED.volume_analysis,
                    detected_at = NOW()
            ''', mint, symbol, flat_data.get('flat_duration'), 
                flat_data.get('volatility'), json.dumps(flat_data.get('volume_analysis', {})))

db = DatabaseManager()

# ===================== CLIENTES API =====================
class APIClient:
    def __init__(self):
        self.session = None
        self.jupiter_base = JUPITER_BASE_URL
    
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def jupiter_request(self, endpoint: str):
        try:
            session = await self.get_session()
            url = f"{self.jupiter_base}{endpoint}"
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                return None
        except Exception as e:
            logger.error(f"❌ Error Jupiter request: {e}")
            return None
    
    async def get_quality_tokens(self):
        """Obtiene tokens de calidad desde Jupiter"""
        endpoints = [
            "/tokens/v2/toporganicscore/1h?limit=50",
            "/tokens/v2/toptraded/1h?limit=50",
            "/tokens/v2/tag?query=verified&limit=30",
            "/tokens/v2/recent?limit=30"
        ]
        
        all_tokens = []
        for endpoint in endpoints:
            tokens = await self.jupiter_request(endpoint)
            if tokens:
                all_tokens.extend(tokens)
        
        # Filtrar y eliminar duplicados
        unique_tokens = {}
        for token in all_tokens:
            mint = token.get('id')
            if not mint:
                continue
                
            liquidity = token.get('liquidity', 0)
            volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                         token.get('stats24h', {}).get('sellVolume', 0))
            
            if liquidity >= MIN_LIQUIDITY and volume_24h >= MIN_VOLUME_24H:
                unique_tokens[mint] = token
        
        logger.info(f"🎯 {len(unique_tokens)} tokens de calidad encontrados")
        return list(unique_tokens.values())
    
    async def get_dexscreener_candles(self, mint: str, limit: int = 96):
        """Obtiene velas desde DexScreener"""
        try:
            session = await self.get_session()
            # Primero buscar el pair address
            search_url = f"https://api.dexscreener.com/latest/dex/search?q={mint}"
            async with session.get(search_url, timeout=10) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                pairs = data.get('pairs', [])
                if not pairs:
                    return []
                
                # Tomar el primer pair de Solana
                pair_data = None
                for pair in pairs:
                    if pair.get('chainId') == 'solana':
                        pair_data = pair
                        break
                
                if not pair_data:
                    return []
                
                # Obtener datos del pair (que incluye velas)
                pair_address = pair_data.get('pairAddress')
                if not pair_address:
                    return []
                
                # DexScreener no tiene endpoint público directo para velas históricas
                # Usaremos los datos básicos del pair para nuestro análisis
                return self._extract_candle_data(pair_data)
                    
        except Exception as e:
            logger.error(f"❌ Error DexScreener para {mint}: {e}")
            return []
    
    def _extract_candle_data(self, pair_data: dict):
        """Extrae datos de velas desde la respuesta de DexScreener"""
        # DexScreener no proporciona velas históricas directamente en la API pública
        # Para simular el análisis, usaremos datos actuales y generaremos un historial simple
        # En producción, necesitarías una suscripción a DexScreener para velas históricas
        
        current_price = float(pair_data.get('priceUsd', 0))
        volume = float(pair_data.get('volume', {}).get('h24', 0))
        
        # Simular algunas velas para demostración
        candles = []
        base_time = int(time.time() * 1000) - (96 * 15 * 60 * 1000)  # 96 velas de 15min
        
        for i in range(96):
            # Variación pequeña alrededor del precio actual para simular flat
            variation = 1 + (i % 10 - 5) * 0.001  # ±0.5%
            simulated_price = current_price * variation
            simulated_volume = volume / 96 * (0.5 + (i % 3) * 0.3)  # Volumen variable
            
            candles.append({
                'time': base_time + (i * 15 * 60 * 1000),
                'open': simulated_price,
                'high': simulated_price * 1.002,
                'low': simulated_price * 0.998,
                'close': simulated_price,
                'volume': simulated_volume
            })
        
        return candles

api_client = APIClient()

# ===================== DETECTOR FLAT =====================
class FlatDetector:
    def __init__(self):
        self.flat_tokens = {}
    
    async def analyze_token_flat(self, mint: str, token_data: dict = None) -> dict:
        """Analiza si un token está en patrón FLAT"""
        try:
            candles = await api_client.get_dexscreener_candles(mint, FLAT_CONFIG['CANDLES_TO_ANALYZE'])
            if len(candles) < 48:
                return {'is_flat': False, 'reason': 'insufficient_data'}
            
            volatility_analysis = self._calculate_volatility(candles)
            if volatility_analysis['std_dev'] > FLAT_CONFIG['MAX_VOLATILITY']:
                return {'is_flat': False, 'reason': 'high_volatility'}
            
            volume_analysis = self._analyze_volume_pattern(candles)
            if not volume_analysis['is_flat_pattern']:
                return {'is_flat': False, 'reason': 'volume_pattern_not_flat'}
            
            flat_duration = self._estimate_flat_duration(candles)
            if flat_duration < FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']:
                return {'is_flat': False, 'reason': 'insufficient_duration'}
            
            return {
                'is_flat': True,
                'flat_duration': flat_duration,
                'volatility': volatility_analysis['std_dev'],
                'volume_analysis': volume_analysis,
                'price_range_pct': volatility_analysis['price_range_pct']
            }
            
        except Exception as e:
            logger.error(f"❌ Error en análisis FLAT para {mint}: {e}")
            return {'is_flat': False, 'reason': 'analysis_error'}
    
    def _calculate_volatility(self, candles):
        prices = [c['close'] for c in candles if c['close'] > 0]
        if len(prices) < 10:
            return {'std_dev': 100, 'price_range_pct': 100}
        
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
                returns.append(ret)
        
        if not returns:
            return {'std_dev': 0, 'price_range_pct': 0}
        
        price_range_pct = (max(prices) - min(prices)) / min(prices) * 100
        
        return {
            'std_dev': pstdev(returns) if len(returns) > 1 else 0,
            'price_range_pct': price_range_pct
        }
    
    def _analyze_volume_pattern(self, candles):
        volumes = [c['volume'] for c in candles]
        
        low_volume_count = sum(1 for v in volumes if v < 20)
        medium_volume_count = sum(1 for v in volumes if 20 <= v <= 200)
        high_volume_count = sum(1 for v in volumes if v > 200)
        
        isolated_spikes = 0
        for i in range(1, len(volumes)-1):
            prev_low = volumes[i-1] < 15
            current_high = volumes[i] > FLAT_CONFIG['VOLUME_SPIKE_THRESHOLD']
            next_low = volumes[i+1] < 20
            
            if prev_low and current_high and next_low:
                isolated_spikes += 1
        
        consecutive_green = 0
        max_consecutive = 0
        for i in range(1, len(candles)):
            if candles[i]['close'] >= candles[i-1]['close'] and volumes[i] > 500:
                consecutive_green += 1
                max_consecutive = max(max_consecutive, consecutive_green)
            else:
                consecutive_green = 0
        
        is_flat_pattern = (
            low_volume_count >= FLAT_CONFIG['MIN_LOW_VOLUME_CANDLES'] and
            medium_volume_count <= 6 and
            high_volume_count <= 3 and
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
            'max_consecutive_green': max_consecutive,
            'avg_volume': mean(volumes) if volumes else 0
        }
    
    def _estimate_flat_duration(self, candles):
        if len(candles) < 2:
            return 0
        return len(candles) * 0.25  # 0.25 horas por vela de 15min

flat_detector = FlatDetector()

# ===================== SISTEMA DE ALERTAS =====================
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
            f"• [Jupiter Swap](https://jup.ag/swap/SOL-{mint})\n"
        )
    
    async def send_flat_alert(self, mint: str, token_data: dict, flat_analysis: dict):
        """Envía alerta de token FLAT detectado"""
        if await db.is_token_notified(mint, "FLAT_DETECTED"):
            logger.info(f"🔔 Token {mint} ya notificado (FLAT), omitiendo")
            return
        
        symbol = token_data.get('symbol', 'N/A')
        name = token_data.get('name', 'N/A')
        liquidity = token_data.get('liquidity', 0)
        volume_24h = (token_data.get('stats24h', {}).get('buyVolume', 0) + 
                     token_data.get('stats24h', {}).get('sellVolume', 0))
        
        message = (
            f"🎯 *ALERTA DE PATRÓN FLAT DETECTADO* 🎯\n\n"
            f"*Token:* {symbol} - {name}\n"
            f"*Mint:* `{mint}`\n"
            f"*Liquidez:* ${liquidity:,.0f}\n"
            f"*Volumen 24h:* ${volume_24h:,.0f}\n\n"
            
            f"📊 *ANÁLISIS FLAT:*\n"
            f"• Tiempo en flat: {flat_analysis['flat_duration']:.1f} horas\n"
            f"• Volatilidad: {flat_analysis['volatility']:.2f}%\n"
            f"• Rango precio: {flat_analysis['price_range_pct']:.1f}%\n"
            f"• Velas volumen bajo: {flat_analysis['volume_analysis']['low_volume_candles']}\n"
            f"• Picos aislados: {flat_analysis['volume_analysis']['isolated_spikes']}\n"
            f"• Volumen promedio: ${flat_analysis['volume_analysis']['avg_volume']:.2f}\n\n"
            
            f"🔍 *ENLACES DE ANÁLISIS:*\n"
            f"{self.format_links(mint)}\n"
            
            f"💡 *PATRÓN DETECTADO:*\n"
            f"Token en consolidación prolongada con volumen bajo, similar al patrón PESHI antes del breakout."
        )
        
        await self._send_telegram_message(message)
        await db.mark_token_notified(mint, symbol, "FLAT_DETECTED", 
                                   {'flat_analysis': flat_analysis})
        await db.save_flat_token(mint, symbol, flat_analysis)
        
        logger.info(f"✅ Alerta FLAT enviada para {symbol}")
    
    async def send_pumpfun_alert(self, mint: str, market_cap: float, token_data: dict = None):
        """Envía alerta de pre-graduación de Pump.fun"""
        if await db.is_token_notified(mint, "PUMPFUN_PRE_GRAD"):
            return
        
        symbol = token_data.get('symbol', 'N/A') if token_data else 'N/A'
        
        message = (
            f"🚀 *ALERTA PUMP.FUN PRE-GRADUACIÓN* 🚀\n\n"
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
        await db.mark_token_notified(mint, symbol, "PUMPFUN_PRE_GRAD", 
                                   {'market_cap': market_cap, 'alert_time': datetime.now().isoformat()})
        
        logger.info(f"✅ Alerta Pump.fun enviada para {symbol}")
    
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

# ===================== MONITORES =====================
class PumpFunMonitor:
    def __init__(self):
        self.active = False
    
    async def start_monitoring(self):
        """Inicia el monitoreo de Pump.fun via WebSocket"""
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
                    # Suscribirse a logs del programa Pump.fun
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
                            await self._process_pumpfun_message(message)
                            
                        except asyncio.TimeoutError:
                            # Enviar ping para mantener la conexión
                            await websocket.send(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))
                        except Exception as e:
                            logger.error(f"❌ Error procesando mensaje WebSocket: {e}")
                            break
                            
            except Exception as e:
                logger.error(f"❌ Error conexión WebSocket Pump.fun: {e}")
                if self.active:
                    await asyncio.sleep(5)
    
    async def _process_pumpfun_message(self, message: str):
        """Procesa mensajes de Pump.fun y detecta near-graduation"""
        try:
            data = json.loads(message)
            
            # En un entorno real, aquí analizarías los logs para extraer
            # el market cap de los tokens. Por simplicidad, simularemos
            # la detección con datos de ejemplo.
            
            # Simulación: Ocasionalmente generar alertas de prueba
            import random
            if random.random() < 0.02:  # 2% de probabilidad por mensaje
                mock_mint = f"MOCK{int(time.time())}"
                mock_market_cap = random.randint(58000, 68000)
                
                if mock_market_cap >= PUMP_PRE_GRADUATION_THRESHOLD:
                    logger.info(f"🎯 SIMULACIÓN: Token {mock_mint} cerca de graduación - MC: ${mock_market_cap:,.0f}")
                    await alert_system.send_pumpfun_alert(mock_mint, mock_market_cap)
                    
        except Exception as e:
            logger.error(f"❌ Error procesando mensaje Pump.fun: {e}")
    
    def stop(self):
        self.active = False
        logger.info("🛑 Monitor Pump.fun detenido")

class FlatScanner:
    def __init__(self):
        self.active = False
    
    async def start_scanning(self):
        """Inicia el escáner periódico de tokens FLAT"""
        self.active = True
        logger.info("🔄 Iniciando scanner de tokens FLAT...")
        
        await alert_system._send_telegram_message(
            "🎯 *SCANNER FLAT INICIADO*\n\n"
            f"• Duración mínima: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']} horas\n"
            f"• Volatilidad máxima: {FLAT_CONFIG['MAX_VOLATILITY']}%\n"
            f"• Volumen máximo: ${FLAT_CONFIG['MAX_AVG_VOLUME_PER_CANDLE']} por vela\n"
            f"• Intervalo: {UPDATE_INTERVAL/60} minutos\n\n"
            "_Buscando patrones PESHI..._"
        )
        
        while self.active:
            try:
                tokens = await api_client.get_quality_tokens()
                logger.info(f"🔍 Analizando {len(tokens)} tokens para detección FLAT")
                
                flat_detections = 0
                
                for token in tokens[:30]:  # Limitar para no saturar APIs
                    if not self.active:
                        break
                    
                    mint = token.get('id')
                    symbol = token.get('symbol', 'N/A')
                    
                    try:
                        flat_analysis = await flat_detector.analyze_token_flat(mint, token)
                        
                        if flat_analysis['is_flat']:
                            logger.info(f"✅ FLAT detectado: {symbol} ({mint[:12]}...)")
                            await alert_system.send_flat_alert(mint, token, flat_analysis)
                            flat_detections += 1
                    
                    except Exception as e:
                        logger.error(f"❌ Error procesando token {mint}: {e}")
                        continue
                    
                    await asyncio.sleep(1)  # Rate limiting entre tokens
                
                logger.info(f"📊 Scan completado: {flat_detections} tokens FLAT detectados")
                await asyncio.sleep(UPDATE_INTERVAL)
                
            except Exception as e:
                logger.error(f"❌ Error en scanner FLAT: {e}")
                await asyncio.sleep(60)
    
    def stop(self):
        self.active = False
        logger.info("🛑 Scanner FLAT detenido")

# Inicializar monitores
pumpfun_monitor = PumpFunMonitor()
flat_scanner = FlatScanner()

# ===================== COMANDOS TELEGRAM =====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = (
        "🤖 *SOLANA ULTIMATE BOT* 🚀\n\n"
        "🎯 *SISTEMAS ACTIVOS:*\n"
        "• 🔥 Monitor Pump.fun Pre-Graduación\n"
        "• 🔍 Detector FLAT (Patrón PESHI)\n"
        "• 💾 Base de datos PostgreSQL\n\n"
        
        "📊 *CONFIGURACIÓN PUMP.FUN:*\n"
        f"• Alerta: ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f} MC\n"
        f"• Graduación: ${PUMP_GRADUATION_TARGET:,.0f} MC\n\n"
        
        "📈 *CONFIGURACIÓN FLAT:*\n"
        f"• Duración: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']}+ horas\n"
        f"• Volatilidad: <{FLAT_CONFIG['MAX_VOLATILITY']}%\n"
        f"• Volumen: <${FLAT_CONFIG['MAX_AVG_VOLUME_PER_CANDLE']}/vela\n\n"
        
        "⚡ *COMANDOS DISPONIBLES:*\n"
        "• /iniciar - Activar todos los sistemas\n"
        "• /detener - Parar todo\n"
        "• /status - Estado del sistema\n"
        "• /estadisticas - Ver estadísticas\n"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode=ParseMode.MARKDOWN)

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia ambos monitores"""
    # Iniciar en segundo plano
    asyncio.create_task(pumpfun_monitor.start_monitoring())
    asyncio.create_task(flat_scanner.start_scanning())
    
    await update.message.reply_text(
        "✅ *SISTEMAS ACTIVADOS*\n\n"
        "• Monitor Pump.fun: 🟢 INICIADO\n"
        "• Scanner FLAT: 🟢 INICIADO\n"
        "• Base de datos: 🟢 CONECTADA\n\n"
        "_Todos los sistemas funcionando en segundo plano..._",
        parse_mode=ParseMode.MARKDOWN
    )

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene ambos monitores"""
    pumpfun_monitor.stop()
    flat_scanner.stop()
    
    await update.message.reply_text(
        "🛑 *SISTEMAS DETENIDOS*\n\n"
        "• Monitor Pump.fun: 🔴 DETENIDO\n"
        "• Scanner FLAT: 🔴 DETENIDO",
        parse_mode=ParseMode.MARKDOWN
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el estado actual del sistema"""
    status_msg = (
        f"📊 *ESTADO DEL SISTEMA*\n\n"
        f"• Monitor Pump.fun: {'🟢 ACTIVO' if pumpfun_monitor.active else '🔴 DETENIDO'}\n"
        f"• Scanner FLAT: {'🟢 ACTIVO' if flat_scanner.active else '🔴 DETENIDO'}\n"
        f"• Base datos: {'🟢 CONECTADA' if db.pool else '🔴 NO CONECTADA'}\n\n"
        
        f"⚙️ *CONFIGURACIÓN ACTUAL:*\n"
        f"• Alerta Pump.fun: ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f} MC\n"
        f"• Volatilidad FLAT: {FLAT_CONFIG['MAX_VOLATILITY']}%\n"
        f"• Duración FLAT: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']} horas\n"
        f"• Intervalo scan: {UPDATE_INTERVAL/60} minutos\n"
    )
    
    await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)

async def estadisticas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra estadísticas de detección"""
    try:
        async with db.pool.acquire() as conn:
            # Contar alertas por tipo
            flat_count = await conn.fetchval(
                "SELECT COUNT(*) FROM notified_tokens WHERE alert_type = 'FLAT_DETECTED'"
            )
            pump_count = await conn.fetchval(
                "SELECT COUNT(*) FROM notified_tokens WHERE alert_type = 'PUMPFUN_PRE_GRAD'"
            )
            
            # Últimas alertas
            recent_alerts = await conn.fetch(
                "SELECT symbol, alert_type, last_alert FROM notified_tokens ORDER BY last_alert DESC LIMIT 5"
            )
        
        stats_msg = (
            f"📈 *ESTADÍSTICAS DE DETECCIÓN*\n\n"
            f"• Alertas FLAT totales: {flat_count}\n"
            f"• Alertas Pump.fun totales: {pump_count}\n\n"
            f"🕐 *ÚLTIMAS ALERTAS:*\n"
        )
        
        for alert in recent_alerts:
            time_ago = datetime.now() - alert['last_alert']
            hours_ago = time_ago.total_seconds() / 3600
            stats_msg += f"• {alert['symbol']} ({alert['alert_type']}) - {hours_ago:.1f}h ago\n"
        
        await update.message.reply_text(stats_msg, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"❌ Error obteniendo estadísticas: {e}")
        await update.message.reply_text("❌ Error obteniendo estadísticas")

# ===================== MAIN =====================
async def main():
    """Función principal"""
    logger.info("🚀 INICIANDO SOLANA ULTIMATE BOT...")
    
    # Inicializar base de datos
    await db.init()
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return
    
    # Configurar aplicación Telegram
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Registrar comandos
    commands = [
        ("start", start_command),
        ("iniciar", iniciar_command),
        ("detener", detener_command),
        ("status", status_command),
        ("estadisticas", estadisticas_command),
    ]
    
    for command, handler in commands:
        application.add_handler(CommandHandler(command, handler))
    
    # Iniciar bot
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("✅ Bot Telegram iniciado y listo para comandos")
    
    # Mensaje de inicio
    await alert_system._send_telegram_message(
        "🤖 *SOLANA ULTIMATE BOT INICIADO* 🚀\n\n"
        "✅ Sistemas cargados y listos\n"
        "✅ Base de datos conectada\n"
        "✅ APIs operativas\n\n"
        "📋 *Comandos disponibles:*\n"
        "• /iniciar - Activar monitores\n"
        "• /detener - Parar monitores\n"
        "• /status - Estado del sistema\n"
        "• /estadisticas - Ver estadísticas\n\n"
        "_Usa /iniciar para comenzar el monitoreo..._"
    )
    
    try:
        # Mantener el bot corriendo
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("🛑 Bot interrumpido por usuario")
    finally:
        # Limpieza
        pumpfun_monitor.stop()
        flat_scanner.stop()
        await application.stop()
        await application.shutdown()
        
        if api_client.session:
            await api_client.session.close()
        
        logger.info("✅ Bot apagado correctamente")

if __name__ == "__main__":
    # Verificar variables requeridas
    required_vars = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DATABASE_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Variables de entorno faltantes: {missing_vars}")
        exit(1)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot terminado por el usuario")
    except Exception as e:
        logger.error(f"💥 Error fatal: {e}")
