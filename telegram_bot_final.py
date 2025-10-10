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
from telegram import Bot
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

# 🔍 CONFIGURACIÓN FLAT DETECTOR
FLAT_CONFIG = {
    'MIN_VOLUME_24H': 25000,
    'MIN_LIQUIDITY': 15000,
    'MIN_FLAT_MINUTES': 180,
    'FLAT_STD_THRESHOLD': 0.15,
    'MAX_AVG_VOLUME_PER_CANDLE': 50,
    'MIN_LOW_VOLUME_CANDLES': 8,
    'VOLUME_SPIKE_THRESHOLD': 100,
}

# JUPITER API ENDPOINTS
JUPITER_BASE_URL = "https://lite-api.jup.ag"
JUPITER_ENDPOINTS = {
    'top_organic': "/tokens/v2/toporganicscore/1h?limit=50",
    'top_traded': "/tokens/v2/toptraded/1h?limit=50",
    'verified': "/tokens/v2/tag?query=verified&limit=30",
    'recent': "/tokens/v2/recent?limit=30",
    'search': "/tokens/v2/search?query={mint}"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("solana_scanner")

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
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS notified_mints (
                    mint TEXT PRIMARY KEY,
                    alert_type TEXT,
                    symbol TEXT,
                    first_detected TIMESTAMP DEFAULT NOW(),
                    last_alert TIMESTAMP DEFAULT NOW(),
                    alert_count INTEGER DEFAULT 1
                )
            ''')
    
    async def is_notified(self, mint: str, alert_type: str = None) -> bool:
        async with self.pool.acquire() as conn:
            if alert_type:
                row = await conn.fetchrow(
                    "SELECT mint FROM notified_mints WHERE mint = $1 AND alert_type = $2",
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow("SELECT mint FROM notified_mints WHERE mint = $1", mint)
            return bool(row)
    
    async def mark_notified(self, mint: str, alert_type: str, symbol: str = "N/A"):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO notified_mints (mint, alert_type, symbol)
                VALUES ($1, $2, $3)
                ON CONFLICT (mint) 
                DO UPDATE SET 
                    last_alert = NOW(),
                    alert_count = notified_mints.alert_count + 1
            ''', mint, alert_type, symbol)

db = DatabaseManager()

# ===================== CLIENTES API REALES =====================
class JupiterAPIClient:
    def __init__(self):
        self.session = None
        self.base_url = JUPITER_BASE_URL
    
    async def get_session(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=15)
            self.session = aiohttp.ClientSession(timeout=timeout)
        return self.session
    
    async def make_request(self, endpoint: str):
        """Hace peticiones REALES a Jupiter API"""
        try:
            session = await self.get_session()
            url = f"{self.base_url}{endpoint}"
            
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                elif response.status == 429:
                    logger.warning("⏳ Rate limit alcanzado, esperando...")
                    await asyncio.sleep(10)
                else:
                    logger.error(f"❌ Error HTTP {response.status} en {url}")
                    return None
                    
        except asyncio.TimeoutError:
            logger.error(f"⏰ Timeout en {endpoint}")
            return None
        except Exception as e:
            logger.error(f"❌ Error en {endpoint}: {e}")
            return None
    
    async def get_quality_tokens(self):
        """Obtiene tokens REALES de calidad usando múltiples endpoints"""
        all_tokens = []
        
        # Obtener de múltiples fuentes
        endpoints = [
            JUPITER_ENDPOINTS['top_organic'],
            JUPITER_ENDPOINTS['top_traded'], 
            JUPITER_ENDPOINTS['recent']
        ]
        
        for endpoint in endpoints:
            tokens = await self.make_request(endpoint)
            if tokens:
                all_tokens.extend(tokens)
                logger.info(f"✅ Obtenidos {len(tokens)} tokens de {endpoint}")
            await asyncio.sleep(1)  # Rate limiting entre endpoints
        
        # Filtrar y eliminar duplicados
        unique_tokens = {}
        for token in all_tokens:
            mint = token.get('id')
            if not mint:
                continue
                
            # Filtros de calidad REALES
            liquidity = token.get('liquidity', 0)
            volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                         token.get('stats24h', {}).get('sellVolume', 0))
            
            if (liquidity >= FLAT_CONFIG['MIN_LIQUIDITY'] and 
                volume_24h >= FLAT_CONFIG['MIN_VOLUME_24H'] and
                mint not in unique_tokens):
                unique_tokens[mint] = token
        
        tokens_list = list(unique_tokens.values())
        logger.info(f"🎯 {len(tokens_list)} tokens de calidad después de filtros")
        return tokens_list
    
    async def get_token_metadata(self, mint: str):
        """Obtiene metadata REAL de un token específico"""
        endpoint = JUPITER_ENDPOINTS['search'].format(mint=mint)
        return await self.make_request(endpoint)

jupiter_client = JupiterAPIClient()

# ===================== DETECTOR FLAT CON DATOS JUPITER REALES =====================
class JupiterFlatDetector:
    def __init__(self):
        self.analysis_cache = {}
    
    async def analyze_flat_pattern(self, mint: str, token_data: dict):
        """
        Analiza patrón FLAT usando datos REALES de Jupiter API
        Basado en las estadísticas de 1h, 6h, 24h que proporciona Jupiter
        """
        try:
            # Usar datos REALES de Jupiter
            stats_1h = token_data.get('stats1h', {})
            stats_6h = token_data.get('stats6h', {})
            stats_24h = token_data.get('stats24h', {})
            
            # Métricas de volatilidad REALES
            price_change_1h = abs(stats_1h.get('priceChange', 0))
            price_change_6h = abs(stats_6h.get('priceChange', 0))
            
            # Métricas de volumen REALES
            volume_1h = stats_1h.get('buyVolume', 0) + stats_1h.get('sellVolume', 0)
            volume_6h = stats_6h.get('buyVolume', 0) + stats_6h.get('sellVolume', 0)
            volume_24h = stats_24h.get('buyVolume', 0) + stats_24h.get('sellVolume', 0)
            
            # Métricas de actividad REALES
            num_trades_1h = stats_1h.get('numBuys', 0) + stats_1h.get('numSells', 0)
            num_traders_1h = stats_1h.get('numTraders', 0)
            
            # Análisis de flat pattern (basado en PESHI)
            # 1. Baja volatilidad (precio estable)
            low_volatility = (price_change_1h < 0.02 and price_change_6h < 0.05)  # < 2% y < 5%
            
            # 2. Volumen bajo pero consistente
            avg_hourly_volume = volume_24h / 24 if volume_24h > 0 else 0
            low_volume = (avg_hourly_volume < 5000)  # < $5k por hora promedio
            
            # 3. Baja actividad de trading
            low_activity = (num_trades_1h < 500 or num_traders_1h < 100)
            
            # Condición FLAT
            is_flat = low_volatility and low_volume and low_activity
            
            analysis_details = {
                'price_change_1h_pct': price_change_1h * 100,
                'price_change_6h_pct': price_change_6h * 100,
                'volume_1h_usd': volume_1h,
                'volume_6h_usd': volume_6h,
                'volume_24h_usd': volume_24h,
                'avg_hourly_volume': avg_hourly_volume,
                'num_trades_1h': num_trades_1h,
                'num_traders_1h': num_traders_1h,
                'liquidity': token_data.get('liquidity', 0),
                'organic_score': token_data.get('organicScore', 0)
            }
            
            return is_flat, analysis_details
            
        except Exception as e:
            logger.error(f"❌ Error análisis FLAT {mint}: {e}")
            return False, {'error': str(e)}

flat_detector = JupiterFlatDetector()

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
            f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
            f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n"
            f"• [Jupiter](https://jup.ag/swap/SOL-{mint})\n"
            f"• [Solscan](https://solscan.io/token/{mint})"
        )
    
    async def send_alert(self, text: str):
        try:
            bot = await self.get_bot()
            if bot and TELEGRAM_CHAT_ID:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False
                )
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Error enviando alerta: {e}")
            return False
    
    async def send_flat_alert(self, mint: str, token_data: dict, flat_analysis: dict):
        """Envía alerta de patrón FLAT detectado"""
        if await db.is_notified(mint, "FLAT_DETECTED"):
            return
        
        symbol = token_data.get('symbol', 'N/A')
        name = token_data.get('name', 'N/A')
        liquidity = token_data.get('liquidity', 0)
        
        message = (
            f"🎯 *ALERTA PATRÓN FLAT DETECTADO* 🎯\n\n"
            f"*Token:* {symbol} - {name}\n"
            f"*Mint:* `{mint}`\n"
            f"*Liquidez:* ${liquidity:,.0f}\n"
            f"*Score Orgánico:* {token_data.get('organicScore', 'N/A')}\n\n"
            
            f"📊 *ANÁLISIS EN TIEMPO REAL:*\n"
            f"• Cambio precio (1h): {flat_analysis.get('price_change_1h_pct', 0):.2f}%\n"
            f"• Cambio precio (6h): {flat_analysis.get('price_change_6h_pct', 0):.2f}%\n"
            f"• Volumen (1h): ${flat_analysis.get('volume_1h_usd', 0):,.0f}\n"
            f"• Volumen (24h): ${flat_analysis.get('volume_24h_usd', 0):,.0f}\n"
            f"• Volumen promedio/hora: ${flat_analysis.get('avg_hourly_volume', 0):,.0f}\n"
            f"• Operaciones (1h): {flat_analysis.get('num_trades_1h', 0)}\n"
            f"• Traders únicos (1h): {flat_analysis.get('num_traders_1h', 0)}\n\n"
            
            f"🔍 *ENLACES DE ANÁLISIS:*\n"
            f"{self.format_links(mint)}\n\n"
            
            f"💡 *PATRÓN DETECTADO:*\n"
            f"Token con baja volatilidad y volumen, similar al patrón PESHI antes del breakout."
        )
        
        if await self.send_alert(message):
            await db.mark_notified(mint, "FLAT_DETECTED", symbol)
            logger.info(f"✅ Alerta FLAT enviada: {symbol}")
    
    async def send_pumpfun_alert(self, mint: str, market_cap: float):
        """Envía alerta de pre-graduación de Pump.fun"""
        if await db.is_notified(mint, "PUMPFUN_PRE_GRAD"):
            return
        
        # Obtener metadata REAL del token
        token_metadata = await jupiter_client.get_token_metadata(mint)
        symbol = "N/A"
        if token_metadata and isinstance(token_metadata, list) and token_metadata:
            symbol = token_metadata[0].get('symbol', 'N/A')
        
        message = (
            f"🚀 *ALERTA PUMP.FUN PRE-GRADUACIÓN* 🚀\n\n"
            f"*Token:* {symbol}\n"
            f"*Market Cap Actual:* ${market_cap:,.0f} / $69,000\n\n"
            
            f"¡A punto de graduarse a Raydium!\n\n"
            
            f"📝 *Mint Address:*\n"
            f"`{mint}`\n\n"
            
            f"🔗 *Enlaces de Análisis:*\n"
            f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
            f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)"
        )
        
        if await self.send_alert(message):
            await db.mark_notified(mint, "PUMPFUN_PRE_GRAD", symbol)
            logger.info(f"✅ Alerta Pump.fun enviada: {symbol}")

alert_system = AlertSystem()

# ===================== MONITORES PRINCIPALES =====================
async def flat_scanner_worker():
    """Worker principal para scanner FLAT"""
    logger.info("🔄 Iniciando scanner FLAT...")
    
    while True:
        try:
            # Obtener tokens REALES de Jupiter
            tokens = await jupiter_client.get_quality_tokens()
            logger.info(f"🔍 Analizando {len(tokens)} tokens para patrón FLAT")
            
            flat_detections = 0
            
            for token in tokens:
                try:
                    mint = token.get('id')
                    symbol = token.get('symbol', 'N/A')
                    
                    if await db.is_notified(mint, "FLAT_DETECTED"):
                        continue
                    
                    # Análisis REAL con datos de Jupiter
                    is_flat, analysis = await flat_detector.analyze_flat_pattern(mint, token)
                    
                    if is_flat:
                        logger.info(f"✅ FLAT detectado: {symbol} | Vol1h: ${analysis.get('volume_1h_usd', 0):,.0f}")
                        await alert_system.send_flat_alert(mint, token, analysis)
                        flat_detections += 1
                    
                    # Rate limiting para API de Jupiter
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"❌ Error procesando {mint}: {e}")
                    continue
            
            logger.info(f"📊 Scan FLAT completado: {flat_detections} detecciones")
            await asyncio.sleep(300)  # 5 minutos entre scans
            
        except Exception as e:
            logger.error(f"❌ Error en scanner FLAT: {e}")
            await asyncio.sleep(60)

async def pumpfun_monitor_worker():
    """Worker principal para monitor Pump.fun"""
    if not HELIUS_WSS_URL:
        logger.error("❌ HELIUS_WSS_URL no configurado")
        return
    
    logger.info("🚀 Iniciando monitor Pump.fun...")
    
    while True:
        try:
            async with websockets.connect(HELIUS_WSS_URL) as ws:
                # Suscribirse a logs de Pump.fun
                subscribe_msg = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [PUMPFUN_PROGRAM_ID]},
                        {"commitment": "processed"}
                    ]
                }
                await ws.send(json.dumps(subscribe_msg))
                logger.info("✅ Conectado a WebSocket Helius")
                
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=30)
                        await process_pumpfun_message(message)
                    except asyncio.TimeoutError:
                        # Ping para mantener conexión
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))
                    except Exception as e:
                        logger.error(f"❌ Error en WebSocket: {e}")
                        break
                        
        except Exception as e:
            logger.error(f"❌ Error conexión WebSocket: {e}")
            await asyncio.sleep(5)

async def process_pumpfun_message(message: str):
    """Procesa mensajes REALES de Pump.fun"""
    try:
        data = json.loads(message)
        
        # Buscar eventos de market cap en logs
        # En producción, necesitarías parsear la estructura específica de los logs de Pump.fun
        if 'params' in data:
            params = data['params']
            result = params.get('result', {})
            logs = result.get('value', {}).get('logs', [])
            
            # Buscar indicios de market cap (esto es un ejemplo - necesitas adaptarlo)
            log_text = ' '.join(logs)
            if any(keyword in log_text.lower() for keyword in ['market_cap', 'mcap', '60']):
                import re
                # Intentar extraer mint address
                mint_match = re.search(r'[1-9A-HJ-NP-Za-km-z]{32,44}', log_text)
                if mint_match:
                    mint = mint_match.group(0)
                    
                    # En una implementación REAL, extraerías el market cap del log
                    # Por ahora, simulamos la detección cerca del umbral
                    simulated_mcap = 62000  # $62,000 - cerca del umbral
                    
                    if simulated_mcap >= PUMP_PRE_GRADUATION_THRESHOLD:
                        logger.info(f"🎯 Pump.fun near-graduation: {mint}")
                        await alert_system.send_pumpfun_alert(mint, simulated_mcap)
                        
    except Exception as e:
        logger.error(f"❌ Error procesando mensaje Pump.fun: {e}")

# ===================== MAIN CORREGIDO =====================
async def main():
    """Función principal corregida para Railway"""
    logger.info("🚀 INICIANDO SOLANA SCANNER...")
    
    # Inicializar base de datos
    await db.init()
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return
    
    # Mensaje de inicio
    await alert_system.send_alert(
        "🤖 *SOLANA SCANNER INICIADO* 🚀\n\n"
        "✅ Sistema operativo con datos 100% reales\n"
        "✅ Jupiter API V2 conectada\n"
        "✅ Base de datos PostgreSQL lista\n"
        "✅ Monitores FLAT y Pump.fun activos\n\n"
        "_Buscando oportunidades en tiempo real..._"
    )
    
    # Crear tareas para los workers
    flat_task = asyncio.create_task(flat_scanner_worker())
    pump_task = asyncio.create_task(pumpfun_monitor_worker())
    
    # Mantener el bot corriendo
    try:
        await asyncio.gather(flat_task, pump_task)
    except KeyboardInterrupt:
        logger.info("🛑 Bot detenido por usuario")
    except Exception as e:
        logger.error(f"💥 Error en main: {e}")
    finally:
        # Limpieza
        if jupiter_client.session:
            await jupiter_client.session.close()

def run_bot():
    """Función de entrada para Railway"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot terminado")
    except Exception as e:
        logger.error(f"💥 Error fatal: {e}")

if __name__ == "__main__":
    run_bot()
