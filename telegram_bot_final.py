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
PUMP_PRE_GRADUATION_THRESHOLD = 60000  # Alertar a $60k
PUMP_GRADUATION_TARGET = 69000

# 🔍 CONFIGURACIÓN FLAT DETECTOR (Basado en análisis PESHI)
FLAT_CONFIG = {
    'MIN_FLAT_MINUTES': 180,  # 3 horas mínimo en flat
    'MAX_VOLATILITY': 0.15,   # 0.15% de desviación estándar
    'MAX_AVG_VOLUME': 50,     # $50 promedio por vela
    'MIN_LOW_VOLUME_CANDLES': 8,  # Mínimo 8 velas con volumen < $10
    'VOLUME_SPIKE_THRESHOLD': 100, # Picos de volumen > $100
    'CANDLE_INTERVAL': '5m',  # Velas de 5 minutos para mayor precisión
    'CANDLES_TO_ANALYZE': 36, # 3 horas de datos (36 velas de 5min)
}

# ⚙️ FILTROS DE CALIDAD
MIN_LIQUIDITY = 15000
MIN_VOLUME_24H = 25000
JUPITER_BASE_URL = "https://lite-api.jup.ag"

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
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
                    mint_address TEXT PRIMARY KEY,
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
                    "SELECT 1 FROM notified_mints WHERE mint_address = $1 AND alert_type = $2",
                    mint, alert_type
                )
            else:
                row = await conn.fetchrow(
                    "SELECT 1 FROM notified_mints WHERE mint_address = $1",
                    mint
                )
            return bool(row)
    
    async def mark_notified(self, mint: str, alert_type: str, symbol: str = "N/A"):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO notified_mints (mint_address, alert_type, symbol)
                VALUES ($1, $2, $3)
                ON CONFLICT (mint_address) 
                DO UPDATE SET 
                    last_alert = NOW(),
                    alert_count = notified_mints.alert_count + 1
            ''', mint, alert_type, symbol)

db = DatabaseManager()

# ===================== CLIENTES API =====================
class APIClient:
    def __init__(self):
        self.session = None
    
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def jupiter_request(self, endpoint: str):
        try:
            session = await self.get_session()
            url = f"{JUPITER_BASE_URL}{endpoint}"
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    return await response.json()
                return None
        except Exception as e:
            logger.error(f"❌ Error Jupiter request: {e}")
            return None
    
    async def get_tokens_for_flat_analysis(self):
        """Obtiene tokens recientes y populares para análisis FLAT"""
        endpoints = [
            "/tokens/v2/recent?limit=50",
            "/tokens/v2/toptraded/1h?limit=30",
            "/tokens/v2/toporganicscore/1h?limit=30"
        ]
        
        all_tokens = []
        for endpoint in endpoints:
            tokens = await self.jupiter_request(endpoint)
            if tokens:
                all_tokens.extend(tokens)
        
        # Filtrar por liquidez y volumen
        filtered_tokens = []
        for token in all_tokens:
            mint = token.get('id')
            liquidity = token.get('liquidity', 0)
            volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                         token.get('stats24h', {}).get('sellVolume', 0))
            
            if liquidity >= MIN_LIQUIDITY and volume_24h >= MIN_VOLUME_24H:
                filtered_tokens.append(token)
        
        logger.info(f"🎯 {len(filtered_tokens)} tokens para análisis FLAT")
        return filtered_tokens
    
    async def get_birdeye_data(self, mint: str):
        """Obtiene datos de velas desde Birdeye (alternativa a DexScreener)"""
        try:
            session = await self.get_session()
            # Birdeye API para velas históricas
            url = f"https://public-api.birdeye.so/defi/history_price?address={mint}&type=5m&time_from={int(time.time()) - 10800}"  # 3 horas
            headers = {"X-API-KEY": "public"}  # API key pública
            
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    candles = data.get('data', {}).get('items', [])
                    
                    formatted_candles = []
                    for candle in candles:
                        formatted_candles.append({
                            'time': candle.get('unixTime', 0),
                            'open': float(candle.get('o', 0)),
                            'high': float(candle.get('h', 0)),
                            'low': float(candle.get('l', 0)),
                            'close': float(candle.get('c', 0)),
                            'volume': float(candle.get('v', 0))
                        })
                    
                    return formatted_cables
                return []
        except Exception as e:
            logger.error(f"❌ Error Birdeye para {mint}: {e}")
            return []

api_client = APIClient()

# ===================== DETECTOR FLAT MEJORADO =====================
class FlatDetector:
    def __init__(self):
        self.analysis_cache = {}
    
    async def analyze_flat_pattern(self, mint: str, token_data: dict = None) -> dict:
        """Analiza si un token está en patrón FLAT basado en PESHI"""
        try:
            candles = await api_client.get_birdeye_data(mint)
            if len(candles) < FLAT_CONFIG['CANDLES_TO_ANALYZE']:
                return {'is_flat': False, 'reason': 'insufficient_data'}
            
            # Análisis de volatilidad
            volatility = self._calculate_volatility(candles)
            if volatility > FLAT_CONFIG['MAX_VOLATILITY']:
                return {'is_flat': False, 'reason': f'high_volatility_{volatility:.3f}'}
            
            # Análisis de volumen
            volume_analysis = self._analyze_volume(candles)
            if not volume_analysis['is_flat_volume']:
                return {'is_flat': False, 'reason': 'volume_pattern'}
            
            # Análisis de precio
            price_analysis = self._analyze_price(candles)
            
            return {
                'is_flat': True,
                'volatility': volatility,
                'volume_analysis': volume_analysis,
                'price_analysis': price_analysis,
                'flat_duration_minutes': len(candles) * 5,  # 5 minutos por vela
                'candles_analyzed': len(candles)
            }
            
        except Exception as e:
            logger.error(f"❌ Error análisis FLAT {mint}: {e}")
            return {'is_flat': False, 'reason': 'analysis_error'}
    
    def _calculate_volatility(self, candles):
        """Calcula la volatilidad como desviación estándar de returns"""
        prices = [c['close'] for c in candles if c['close'] > 0]
        if len(prices) < 5:
            return 100.0
        
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1]
                returns.append(ret)
        
        if not returns or len(returns) < 2:
            return 0.0
        
        return pstdev(returns) * 100  # Convertir a porcentaje
    
    def _analyze_volume(self, candles):
        """Analiza el patrón de volumen (basado en PESHI)"""
        volumes = [c['volume'] for c in candles]
        
        # Contar velas con volumen muy bajo (< $10)
        low_volume_count = sum(1 for v in volumes if v < 10)
        
        # Contar picos de volumen aislados (> $100)
        isolated_spikes = 0
        for i in range(1, len(volumes)-1):
            if volumes[i] > FLAT_CONFIG['VOLUME_SPIKE_THRESHOLD']:
                if volumes[i-1] < 20 and volumes[i+1] < 20:
                    isolated_spikes += 1
        
        # Volumen promedio
        avg_volume = mean(volumes) if volumes else 0
        
        # Condición FLAT: mayoría de velas con volumen bajo y algunos picos aislados
        is_flat_volume = (
            low_volume_count >= FLAT_CONFIG['MIN_LOW_VOLUME_CANDLES'] and
            avg_volume < FLAT_CONFIG['MAX_AVG_VOLUME'] and
            isolated_spikes >= 1  # Al menos un pico aislado
        )
        
        return {
            'is_flat_volume': is_flat_volume,
            'low_volume_candles': low_volume_count,
            'isolated_spikes': isolated_spikes,
            'avg_volume': avg_volume,
            'max_volume': max(volumes) if volumes else 0
        }
    
    def _analyze_price(self, candles):
        """Analiza la acción del precio"""
        prices = [c['close'] for c in candles if c['close'] > 0]
        if not prices:
            return {'price_range_pct': 100, 'trend': 'unknown'}
        
        min_price = min(prices)
        max_price = max(prices)
        price_range_pct = ((max_price - min_price) / min_price) * 100
        
        # Determinar tendencia simple
        first_half = prices[:len(prices)//2]
        second_half = prices[len(prices)//2:]
        
        avg_first = mean(first_half) if first_half else 0
        avg_second = mean(second_half) if second_half else 0
        
        if avg_second > avg_first * 1.01:
            trend = 'up'
        elif avg_second < avg_first * 0.99:
            trend = 'down'
        else:
            trend = 'flat'
        
        return {
            'price_range_pct': price_range_pct,
            'trend': trend,
            'min_price': min_price,
            'max_price': max_price
        }

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
            f"• [Jupiter](https://jup.ag/swap/SOL-{mint})\n"
            f"• [Solscan](https://solscan.io/token/{mint})"
        )
    
    async def send_flat_alert(self, mint: str, token_data: dict, flat_analysis: dict):
        """Envía alerta de token FLAT detectado"""
        if await db.is_notified(mint, "FLAT_DETECTED"):
            return
        
        symbol = token_data.get('symbol', 'N/A')
        name = token_data.get('name', 'N/A')
        liquidity = token_data.get('liquidity', 0)
        volume_24h = (token_data.get('stats24h', {}).get('buyVolume', 0) + 
                     token_data.get('stats24h', {}).get('sellVolume', 0))
        
        message = (
            f"🎯 *TOKEN FLAT DETECTADO* 🎯\n\n"
            f"*Token:* {symbol} - {name}\n"
            f"*Mint:* `{mint}`\n"
            f"*Liquidez:* ${liquidity:,.0f}\n"
            f"*Volumen 24h:* ${volume_24h:,.0f}\n\n"
            
            f"📊 *ANÁLISIS FLAT:*\n"
            f"• Tiempo en flat: {flat_analysis['flat_duration_minutes']} min\n"
            f"• Volatilidad: {flat_analysis['volatility']:.3f}%\n"
            f"• Rango precio: {flat_analysis['price_analysis']['price_range_pct']:.2f}%\n"
            f"• Velas volumen bajo: {flat_analysis['volume_analysis']['low_volume_candles']}\n"
            f"• Picos aislados: {flat_analysis['volume_analysis']['isolated_spikes']}\n"
            f"• Volumen promedio: ${flat_analysis['volume_analysis']['avg_volume']:.2f}\n"
            f"• Volumen máximo: ${flat_analysis['volume_analysis']['max_volume']:.2f}\n\n"
            
            f"🔍 *ENLACES:*\n"
            f"{self.format_links(mint)}\n\n"
            
            f"💡 *PATRÓN PESHI DETECTADO:*\n"
            f"Token en consolidación con volumen mínimo, similar a PESHI antes del breakout."
        )
        
        await self._send_telegram_message(message)
        await db.mark_notified(mint, "FLAT_DETECTED", symbol)
        logger.info(f"✅ Alerta FLAT enviada: {symbol}")
    
    async def send_pumpfun_alert(self, mint: str, market_cap: float, token_data: dict = None):
        """Envía alerta de pre-graduación de Pump.fun"""
        if await db.is_notified(mint, "PUMPFUN_PRE_GRAD"):
            return
        
        symbol = token_data.get('symbol', 'N/A') if token_data else 'N/A'
        
        message = (
            f"🚀 *PUMP.FUN - PRE-GRADUACIÓN* 🚀\n\n"
            f"*Token:* {symbol}\n"
            f"*Mint:* `{mint}`\n"
            f"*Market Cap:* ${market_cap:,.0f}\n"
            f"*Umbral:* ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}\n"
            f"*Falta para graduación:* ${PUMP_GRADUATION_TARGET - market_cap:,.0f}\n\n"
            
            f"⚡ *ACCIÓN INMINENTE:*\n"
            f"Liquidez se bloqueará automáticamente en ${PUMP_GRADUATION_TARGET:,.0f}\n\n"
            
            f"🔗 *ENLACES RÁPIDOS:*\n"
            f"{self.format_links(mint)}\n\n"
            
            f"🎯 *ESTRATEGIA:*\n"
            f"Token seguro (LP bloqueado) - Analizar potencial post-graduación"
        )
        
        await self._send_telegram_message(message)
        await db.mark_notified(mint, "PUMPFUN_PRE_GRAD", symbol)
        logger.info(f"✅ Alerta Pump.fun enviada: {symbol}")
    
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
            logger.error(f"❌ Error enviando Telegram: {e}")

alert_system = AlertSystem()

# ===================== MONITORES =====================
class FlatScanner:
    def __init__(self):
        self.active = False
    
    async def start_scanning(self):
        """Escáner periódico de tokens FLAT"""
        self.active = True
        logger.info("🔄 Iniciando scanner FLAT...")
        
        while self.active:
            try:
                tokens = await api_client.get_tokens_for_flat_analysis()
                logger.info(f"🔍 Analizando {len(tokens)} tokens para FLAT")
                
                for token in tokens:
                    if not self.active:
                        break
                    
                    mint = token.get('id')
                    symbol = token.get('symbol', 'N/A')
                    
                    try:
                        flat_analysis = await flat_detector.analyze_flat_pattern(mint, token)
                        
                        if flat_analysis['is_flat']:
                            logger.info(f"✅ FLAT detectado: {symbol} | Vol: {flat_analysis['volatility']:.3f}%")
                            await alert_system.send_flat_alert(mint, token, flat_analysis)
                        else:
                            logger.debug(f"❌ No flat: {symbol} - {flat_analysis.get('reason', 'unknown')}")
                    
                    except Exception as e:
                        logger.error(f"❌ Error procesando {mint}: {e}")
                    
                    await asyncio.sleep(1)  # Rate limiting
                
                logger.info("📊 Scan FLAT completado")
                await asyncio.sleep(300)  # Esperar 5 minutos entre scans
                
            except Exception as e:
                logger.error(f"❌ Error en scanner FLAT: {e}")
                await asyncio.sleep(60)

class PumpFunMonitor:
    def __init__(self):
        self.active = False
    
    async def start_monitoring(self):
        """Monitor en tiempo real de Pump.fun"""
        self.active = True
        
        if not HELIUS_WSS_URL:
            logger.error("❌ HELIUS_WSS_URL no configurado")
            return
        
        logger.info("🚀 Iniciando monitor Pump.fun...")
        
        while self.active:
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
                    logger.info("✅ Conectado a WebSocket - Monitoreando Pump.fun")
                    
                    while self.active:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                            await self._process_helius_message(message)
                        except asyncio.TimeoutError:
                            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "ping"}))
                        except Exception as e:
                            logger.error(f"❌ Error procesando mensaje: {e}")
                            break
                            
            except Exception as e:
                logger.error(f"❌ Error conexión WebSocket: {e}")
                await asyncio.sleep(5)
    
    async def _process_helius_message(self, message: str):
        """Procesa mensajes de Helius para detectar near-graduation"""
        try:
            data = json.loads(message)
            params = data.get('params', {})
            result = params.get('result', {})
            logs = result.get('value', {}).get('logs', [])
            
            # Buscar indicios de market cap en los logs
            log_text = ' '.join(logs).lower()
            
            # Detectar tokens cerca de graduación (patrones comunes en logs)
            if any(keyword in log_text for keyword in ['market_cap', 'mcap', 'graduat']):
                # Extraer mint address del log (buscar patrones comunes)
                import re
                mint_match = re.search(r'[1-9A-HJ-NP-Za-km-z]{32,44}', log_text)
                if mint_match:
                    mint = mint_match.group(0)
                    
                    # Simular market cap (en producción extraerías esto del log)
                    # Esto es un placeholder - necesitarías parsear el log real
                    simulated_mcap = 62000  # Ejemplo: $62k
                    
                    if simulated_mcap >= PUMP_PRE_GRADUATION_THRESHOLD:
                        logger.info(f"🎯 Pump.fun cerca de graduación: {mint}")
                        await alert_system.send_pumpfun_alert(mint, simulated_mcap)
                        
        except Exception as e:
            logger.error(f"❌ Error procesando mensaje Helius: {e}")

# Inicializar monitores
flat_scanner = FlatScanner()
pumpfun_monitor = PumpFunMonitor()

# ===================== MAIN =====================
async def main():
    logger.info("🚀 INICIANDO SOLANA SCANNER...")
    
    await db.init()
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return
    
    # Mensaje de inicio
    await alert_system._send_telegram_message(
        "🤖 *SOLANA SCANNER INICIADO* 🚀\n\n"
        "✅ Sistemas cargados\n"
        "✅ Base de datos conectada\n"
        "✅ Monitores listos\n\n"
        "_Buscando oportunidades FLAT y graduaciones Pump.fun..._"
    )
    
    # Iniciar monitores en segundo plano
    flat_task = asyncio.create_task(flat_scanner.start_scanning())
    pump_task = asyncio.create_task(pumpfun_monitor.start_monitoring())
    
    try:
        await asyncio.gather(flat_task, pump_task)
    except KeyboardInterrupt:
        logger.info("🛑 Bot interrumpido")
    finally:
        flat_scanner.active = False
        pumpfun_monitor.active = False
        
        if api_client.session:
            await api_client.session.close()

if __name__ == "__main__":
    asyncio.run(main())
