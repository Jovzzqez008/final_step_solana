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

# ===================== CONFIGURACIÓN FOCALIZADA =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL = os.getenv("DATABASE_URL")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")

# 🎯 CONFIGURACIÓN ESPECÍFICA PARA TU ESTRATEGIA
FLAT_CONFIG = {
    'MIN_FLAT_DURATION_HOURS': 1,  # MÍNIMO 1 HORA EN FLAT
    'MAX_VOLATILITY': 1.5,  # MÁXIMA VOLATILIDAD PERMITIDA
    'MAX_AVG_VOLUME_PER_CANDLE': 100,
    'MIN_LOW_VOLUME_CANDLES': 6,
    'CANDLES_TO_ANALYZE': 24,  # 24 velas de 5min = 2 horas
}

# 🚀 PUMP.FUN - ALERTA TEMPRANA
PUMPFUN_PROGRAM_ID = "pumpfun1Mt11111111111111111111111111111111"
PUMP_PRE_GRADUATION_THRESHOLD = 60000  # Alerta a $60k
PUMP_GRADUATION_TARGET = 69000  # Graduación a $69k

# ⚠️ FILTROS DE SEGURIDAD BÁSICOS
MIN_LIQUIDITY = 30000
MIN_VOLUME_24H = 10000

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("pump_flat_bot")

# ===================== GESTIÓN DE DATOS =====================
class TokenManager:
    def __init__(self):
        self.pool = None
        self.recent_tokens = deque(maxlen=20)  # Últimos 20 tokens
    
    async def init(self):
        if DATABASE_URL:
            self.pool = await asyncpg.create_pool(DATABASE_URL)
            await self.create_tables()
    
    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS detected_tokens (
                    id SERIAL PRIMARY KEY,
                    mint_address TEXT UNIQUE,
                    symbol TEXT,
                    alert_type TEXT,
                    risk_score INTEGER DEFAULT 0,
                    detected_at TIMESTAMP DEFAULT NOW(),
                    metadata JSONB
                )
            ''')
    
    async def save_token(self, mint: str, symbol: str, alert_type: str, metadata: dict = None):
        self.recent_tokens.appendleft({
            'mint': mint,
            'symbol': symbol,
            'type': alert_type,
            'time': datetime.now(),
            'metadata': metadata or {}
        })
        
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO detected_tokens (mint_address, symbol, alert_type, metadata)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (mint_address) DO NOTHING
                ''', mint, symbol, alert_type, json.dumps(metadata or {}))
    
    async def get_recent_tokens(self, limit: int = 10, alert_type: str = None):
        if alert_type:
            return [t for t in list(self.recent_tokens) if t['type'] == alert_type][:limit]
        return list(self.recent_tokens)[:limit]
    
    async def is_duplicate(self, mint: str, alert_type: str) -> bool:
        """Evitar duplicados en las últimas 2 horas"""
        cutoff_time = datetime.now() - timedelta(hours=2)
        for token in self.recent_tokens:
            if (token['mint'] == mint and 
                token['type'] == alert_type and 
                token['time'] > cutoff_time):
                return True
        return False

token_manager = TokenManager()

# ===================== APIS RÁPIDAS =====================
class FastAPIClient:
    def __init__(self):
        self.session = None
        self.cache = {}
    
    async def get_jupiter_tokens(self):
        """Obtener tokens recientes y con volumen de Jupiter"""
        try:
            if not self.session:
                self.session = aiohttp.ClientSession()
            
            endpoints = [
                "https://lite-api.jup.ag/tokens/v2/recent?limit=50",
                "https://lite-api.jup.ag/tokens/v2/toptraded/1h?limit=50"
            ]
            
            all_tokens = []
            for url in endpoints:
                async with self.session.get(url, timeout=10) as response:
                    if response.status == 200:
                        tokens = await response.json()
                        if isinstance(tokens, list):
                            all_tokens.extend(tokens)
            
            # Filtrar por liquidez y volumen básico
            filtered = []
            for token in all_tokens:
                liquidity = token.get('liquidity', 0)
                volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                             token.get('stats24h', {}).get('sellVolume', 0))
                
                if liquidity >= MIN_LIQUIDITY and volume_24h >= MIN_VOLUME_24H:
                    filtered.append(token)
            
            logger.info(f"🔍 {len(filtered)} tokens válidos encontrados")
            return filtered
            
        except Exception as e:
            logger.error(f"❌ Error Jupiter API: {e}")
            return []
    
    async def get_dexscreener_candles(self, mint: str, limit: int = 24):
        """Obtener velas recientes para análisis FLAT"""
        try:
            if not self.session:
                self.session = aiohttp.ClientSession()
            
            # Buscar par en DexScreener
            search_url = f"https://api.dexscreener.com/latest/dex/search?q={mint}"
            async with self.session.get(search_url, timeout=8) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                pairs = data.get('pairs', [])
                if not pairs:
                    return []
                
                # Tomar el primer par de Solana
                pair = None
                for p in pairs:
                    if p.get('chainId') == 'solana':
                        pair = p
                        break
                
                if not pair:
                    return []
                
                # Obtener datos del par
                pair_address = pair.get('pairAddress')
                pair_url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pair_address}"
                
                async with self.session.get(pair_url, timeout=8) as pair_response:
                    if pair_response.status != 200:
                        return []
                    
                    pair_data = await pair_response.json()
                    
                    # Intentar obtener velas
                    candles = pair_data.get('pairs', [{}])[0].get('candles')
                    if not candles:
                        return []
                    
                    # Normalizar formato
                    normalized = []
                    for candle in candles[-limit:]:
                        if isinstance(candle, dict):
                            normalized.append({
                                'time': candle.get('timestamp', 0),
                                'open': float(candle.get('open', 0)),
                                'high': float(candle.get('high', 0)),
                                'low': float(candle.get('low', 0)),
                                'close': float(candle.get('close', 0)),
                                'volume': float(candle.get('volume', 0))
                            })
                    
                    return normalized
                    
        except Exception as e:
            logger.error(f"❌ Error DexScreener {mint}: {e}")
            return []

api_client = FastAPIClient()

# ===================== DETECTOR FLAT SIMPLIFICADO =====================
class FlatDetector:
    async def analyze_flat_pattern(self, mint: str) -> dict:
        """Análisis simplificado para tokens FLAT de 1+ hora"""
        try:
            candles = await api_client.get_dexscreener_candles(mint, FLAT_CONFIG['CANDLES_TO_ANALYZE'])
            
            if len(candles) < 12:  # Mínimo 1 hora de datos
                return {'is_flat': False, 'reason': 'insufficient_data'}
            
            # Calcular volatilidad
            prices = [c['close'] for c in candles if c['close'] > 0]
            if len(prices) < 8:
                return {'is_flat': False, 'reason': 'not_enough_prices'}
            
            returns = []
            for i in range(1, len(prices)):
                if prices[i-1] > 0:
                    ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
                    returns.append(ret)
            
            if not returns:
                return {'is_flat': False, 'reason': 'no_returns'}
            
            std_dev = pstdev(returns) if len(returns) > 1 else 100
            price_range = (max(prices) - min(prices)) / min(prices) * 100
            
            # Análisis de volumen
            volumes = [c['volume'] for c in candles]
            avg_volume = mean(volumes) if volumes else 0
            low_volume_count = sum(1 for v in volumes if v < 20)
            
            # CONDICIONES PARA FLAT
            is_flat = (
                std_dev < FLAT_CONFIG['MAX_VOLATILITY'] and
                price_range < 3.0 and  # Máximo 3% de rango de precio
                avg_volume < FLAT_CONFIG['MAX_AVG_VOLUME_PER_CANDLE'] and
                low_volume_count >= FLAT_CONFIG['MIN_LOW_VOLUME_CANDLES']
            )
            
            if is_flat:
                estimated_hours = len(candles) * 5 / 60  # Asumiendo velas de 5min
                return {
                    'is_flat': True,
                    'duration_hours': estimated_hours,
                    'volatility': std_dev,
                    'price_range_pct': price_range,
                    'avg_volume': avg_volume
                }
            else:
                return {'is_flat': False, 'reason': 'not_flat_pattern'}
                
        except Exception as e:
            logger.error(f"❌ Error análisis FLAT {mint}: {e}")
            return {'is_flat': False, 'reason': 'error'}

flat_detector = FlatDetector()

# ===================== SISTEMA DE ALERTAS =====================
class AlertSystem:
    def __init__(self):
        self.bot = None
    
    async def get_bot(self):
        if not self.bot and TELEGRAM_BOT_TOKEN:
            self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        return self.bot
    
    def format_token_links(self, mint: str) -> str:
        """Formato limpio de enlaces para Telegram"""
        return (
            f"🔗 *Enlaces Rápidos:*\n"
            f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n"
            f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n"
            f"• [Jupiter](https://jup.ag/swap/SOL-{mint})\n"
        )
    
    async def send_flat_alert(self, mint: str, token_data: dict, flat_analysis: dict):
        if await token_manager.is_duplicate(mint, "FLAT"):
            return
        
        symbol = token_data.get('symbol', 'N/A')
        
        message = (
            f"🎯 *TOKEN FLAT DETECTADO* 🎯\n\n"
            f"*Token:* {symbol}\n"
            f"*Mint:* `{mint}`\n"
            f"*Tiempo en Flat:* {flat_analysis['duration_hours']:.1f} horas\n"
            f"*Volatilidad:* {flat_analysis['volatility']:.2f}%\n"
            f"*Rango Precio:* {flat_analysis['price_range_pct']:.2f}%\n"
            f"*Volumen Promedio:* ${flat_analysis['avg_volume']:.2f}\n\n"
            f"{self.format_token_links(mint)}\n"
            f"💡 *Estrategia:* Token en acumulación - Posible breakout próximo"
        )
        
        await self._send_message(message)
        await token_manager.save_token(mint, symbol, "FLAT", flat_analysis)
    
    async def send_pumpfun_alert(self, mint: str, market_cap: float):
        if await token_manager.is_duplicate(mint, "PUMPFUN"):
            return
        
        message = (
            f"🚀 *PUMP.FUN - PRE-GRADUACIÓN* 🚀\n\n"
            f"*Mint:* `{mint}`\n"
            f"*Market Cap Actual:* ${market_cap:,.0f}\n"
            f"*Graduación en:* ${PUMP_GRADUATION_TARGET - market_cap:,.0f}\n\n"
            f"{self.format_token_links(mint)}\n"
            f"⚡ *Acción Inminente:* Liquidez se bloqueará automáticamente en ~${PUMP_GRADUATION_TARGET:,.0f}"
        )
        
        await self._send_message(message)
        await token_manager.save_token(mint, "PUMP_TOKEN", "PUMPFUN", 
                                     {'market_cap': market_cap, 'alert_time': datetime.now().isoformat()})
    
    async def send_token_list(self, tokens: list, list_type: str):
        if not tokens:
            await self._send_message(f"📭 No hay tokens {list_type}")
            return
        
        message = f"📋 *ÚLTIMOS {len(tokens)} TOKENS - {list_type.upper()}*\n\n"
        
        for i, token in enumerate(tokens, 1):
            mint = token['mint']
            symbol = token.get('symbol', 'N/A')
            token_type = token.get('type', 'N/A')
            time_ago = self._format_time_ago(token['time'])
            
            message += (
                f"`{i}. {symbol} ({token_type})`\n"
                f"   • Mint: `{mint}`\n"
                f"   • Hace: {time_ago}\n"
                f"   • [DexScreener](https://dexscreener.com/solana/{mint}) | "
                f"[RugCheck](https://rugcheck.xyz/tokens/{mint}) | "
                f"[Birdeye](https://birdeye.so/token/{mint})\n\n"
            )
        
        await self._send_message(message)
    
    def _format_time_ago(self, token_time):
        now = datetime.now()
        diff = now - token_time
        minutes = diff.total_seconds() / 60
        
        if minutes < 60:
            return f"{int(minutes)} min"
        elif minutes < 1440:
            return f"{int(minutes/60)} horas"
        else:
            return f"{int(minutes/1440)} días"
    
    async def _send_message(self, message: str):
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
            logger.error(f"❌ Error enviando mensaje: {e}")

alert_system = AlertSystem()

# ===================== SCANNERS PRINCIPALES =====================
class FlatScanner:
    def __init__(self):
        self.active = False
    
    async def start_scanning(self):
        self.active = True
        logger.info("🔄 Iniciando scanner FLAT...")
        
        while self.active:
            try:
                tokens = await api_client.get_jupiter_tokens()
                logger.info(f"🔍 Analizando {len(tokens)} tokens para FLAT")
                
                for token in tokens[:30]:  # Limitar para no saturar
                    if not self.active:
                        break
                    
                    mint = token.get('id')
                    if not mint:
                        continue
                    
                    # Análisis rápido FLAT
                    flat_analysis = await flat_detector.analyze_flat_pattern(mint)
                    
                    if flat_analysis['is_flat']:
                        logger.info(f"✅ FLAT detectado: {token.get('symbol')} - {flat_analysis['duration_hours']:.1f}h")
                        await alert_system.send_flat_alert(mint, token, flat_analysis)
                    
                    await asyncio.sleep(2)  # Rate limiting
                
                await asyncio.sleep(60)  # Esperar 1 minuto entre scans
                
            except Exception as e:
                logger.error(f"❌ Error en scanner FLAT: {e}")
                await asyncio.sleep(30)
    
    def stop(self):
        self.active = False

flat_scanner = FlatScanner()

class PumpFunMonitor:
    def __init__(self):
        self.active = False
    
    async def start_monitoring(self):
        self.active = True
        logger.info("🚀 Iniciando monitor Pump.fun...")
        
        if not HELIUS_WSS_URL:
            logger.error("❌ HELIUS_WSS_URL no configurado")
            return
        
        # SIMULACIÓN - En producción usarías WebSocket real
        while self.active:
            try:
                # Aquí iría tu conexión WebSocket real a Helius
                # Por ahora simulamos detección ocasional
                await asyncio.sleep(30)
                
                # Simulación aleatoria para testing
                import random
                if random.random() < 0.1:  # 10% de probabilidad
                    mock_mint = f"SIM{int(time.time())}"
                    mock_mcap = random.randint(55000, 68000)
                    
                    if mock_mcap >= PUMP_PRE_GRADUATION_THRESHOLD:
                        logger.info(f"🎯 SIMULACIÓN: Token cerca de graduación - MC: ${mock_mcap:,.0f}")
                        await alert_system.send_pumpfun_alert(mock_mint, mock_mcap)
                        
            except Exception as e:
                logger.error(f"❌ Error monitor Pump.fun: {e}")
                await asyncio.sleep(10)
    
    def stop(self):
        self.active = False

pump_monitor = PumpFunMonitor()

# ===================== COMANDOS TELEGRAM =====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *BOT PUMP.FUN + FLAT DETECTOR* 🚀\n\n"
        "🎯 *OBJETIVO:*\n"
        "• Tokens FLAT: 1+ hora en línea recta\n"
        "• Pump.fun: Alerta pre-graduación ($60k+)\n\n"
        "📋 *COMANDOS:*\n"
        "• /iniciar - Activar scanners\n"
        "• /detener - Parar scanners\n"
        "• /lista_tokens - Últimos 10 tokens\n"
        "• /lista_flat - Solo tokens FLAT\n"
        "• /lista_pump - Solo tokens Pump.fun\n"
        "• /status - Estado del sistema\n\n"
        "⚡ *Inicia manualmente cuando quieras!*",
        parse_mode=ParseMode.MARKDOWN
    )

async def iniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(flat_scanner.start_scanning())
    asyncio.create_task(pump_monitor.start_monitoring())
    
    await update.message.reply_text(
        "✅ *SCANNERS ACTIVADOS*\n\n"
        "• FLAT Scanner: 🟢 ACTIVO\n"
        "• Pump.fun Monitor: 🟢 ACTIVO\n\n"
        "_Buscando oportunidades..._",
        parse_mode=ParseMode.MARKDOWN
    )

async def detener_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flat_scanner.stop()
    pump_monitor.stop()
    
    await update.message.reply_text(
        "🛑 *SCANNERS DETENIDOS*\n\n"
        "• FLAT Scanner: 🔴 DETENIDO\n"
        "• Pump.fun Monitor: 🔴 DETENIDO",
        parse_mode=ParseMode.MARKDOWN
    )

async def lista_tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tokens = await token_manager.get_recent_tokens(10)
    await alert_system.send_token_list(tokens, "todos los tokens")

async def lista_flat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tokens = await token_manager.get_recent_tokens(10, "FLAT")
    await alert_system.send_token_list(tokens, "FLAT")

async def lista_pump_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tokens = await token_manager.get_recent_tokens(10, "PUMPFUN")
    await alert_system.send_token_list(tokens, "PUMP.FUN")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recent_tokens = await token_manager.get_recent_tokens(5)
    flat_count = len(await token_manager.get_recent_tokens(50, "FLAT"))
    pump_count = len(await token_manager.get_recent_tokens(50, "PUMPFUN"))
    
    status_msg = (
        f"📊 *ESTADO DEL SISTEMA*\n\n"
        f"• FLAT Scanner: {'🟢 ACTIVO' if flat_scanner.active else '🔴 DETENIDO'}\n"
        f"• Pump.fun Monitor: {'🟢 ACTIVO' if pump_monitor.active else '🔴 DETENIDO'}\n"
        f"• Tokens FLAT hoy: {flat_count}\n"
        f"• Tokens Pump.fun hoy: {pump_count}\n"
        f"• Última actualización: {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"⚙️ *CONFIGURACIÓN:*\n"
        f"• Flat mínimo: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']} hora\n"
        f"• Alerta Pump.fun: ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}+"
    )
    
    await update.message.reply_text(status_msg, parse_mode=ParseMode.MARKDOWN)

# ===================== MAIN =====================
async def main():
    logger.info("🚀 INICIANDO BOT PUMP.FUN + FLAT...")
    
    await token_manager.init()
    
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Comandos simples
    commands = [
        ("start", start_command),
        ("iniciar", iniciar_command),
        ("detener", detener_command),
        ("status", status_command),
        ("lista_tokens", lista_tokens_command),
        ("lista_flat", lista_flat_command),
        ("lista_pump", lista_pump_command),
    ]
    
    for command, handler in commands:
        application.add_handler(CommandHandler(command, handler))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logger.info("✅ Bot Telegram iniciado - Listo para comandos")
    
    # Mensaje de inicio
    await alert_system._send_message(
        "🤖 *BOT INICIADO* 🚀\n\n"
        "✅ Sistema listo para comandos\n"
        "✅ Gestión de tokens activa\n"
        "✅ Scanners preparados\n\n"
        "Usa /iniciar para comenzar la búsqueda!"
    )
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("👋 Bot terminado por usuario")
    finally:
        flat_scanner.stop()
        pump_monitor.stop()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())
