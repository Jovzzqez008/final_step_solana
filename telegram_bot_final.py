import asyncio
import json
import os
import time
import logging
import aiohttp
import asyncpg
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict, deque
import websockets

# ===================== CONFIGURACIÓN COMPLETA =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")

# 🎯 FILTROS PARA TOKENS PLANOS
MIN_FLAT_DURATION_MINUTES = 120  # Mínimo 2 horas en plano
MAX_FLAT_VOLATILITY = 2.0       # Máxima volatilidad permitida (2%)
MIN_LIQUIDITY = 50000           # Liquidez mínima
BREAKOUT_THRESHOLD = 10.0       # Breakout del 10%

# 🚀 FILTROS PARA PUMP.FUN
PUMP_GRADUATION_THRESHOLD = 65000  # Alertar a $65k (antes de $69k)
MIN_HOLDERS = 50                   # Mínimo holders para considerar

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("advanced_scanner")

# ===================== BASE DE DATOS =====================
class DatabaseManager:
    def __init__(self):
        self.pool = None
    
    async def init(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self.create_tables()
    
    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS flat_tokens (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    first_detected TIMESTAMP,
                    flat_duration_minutes INTEGER,
                    base_price REAL,
                    breakout_price REAL,
                    status TEXT
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS pump_monitor (
                    mint_address TEXT PRIMARY KEY,
                    symbol TEXT,
                    detected_at TIMESTAMP,
                    market_cap REAL,
                    status TEXT
                )
            ''')
    
    async def save_flat_token(self, mint, symbol, base_price):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO flat_tokens (mint_address, symbol, first_detected, base_price, status)
                VALUES ($1, $2, $3, $4, 'monitoring')
                ON CONFLICT (mint_address) 
                DO UPDATE SET base_price = EXCLUDED.base_price, status = 'monitoring'
            ''', mint, symbol, datetime.now(), base_price)
    
    async def update_flat_token_breakout(self, mint, breakout_price, duration):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                UPDATE flat_tokens 
                SET breakout_price = $1, flat_duration_minutes = $2, status = 'breakout'
                WHERE mint_address = $3
            ''', breakout_price, duration, mint)
    
    async def save_pump_token(self, mint, symbol, market_cap):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO pump_monitor (mint_address, symbol, detected_at, market_cap, status)
                VALUES ($1, $2, $3, $4, 'monitoring')
                ON CONFLICT (mint_address) 
                DO UPDATE SET market_cap = EXCLUDED.market_cap, status = 'monitoring'
            ''', mint, symbol, datetime.now(), market_cap)

db = DatabaseManager()

# ===================== CLIENTES API =====================
class AdvancedAPIClient:
    def __init__(self):
        self.session = None
        self.jupiter_base = "https://lite-api.jup.ag/tokens/v2"
    
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    # 🔍 DETECCIÓN DE TOKENS PLANOS CON DEXSCREENER
    async def get_flat_candidates(self):
        """Busca tokens con bajo volumen y precio estable usando DexScreener"""
        try:
            session = await self.get_session()
            
            # Buscar pares nuevos en Solana
            async with session.get(f"{DEXSCREENER_API}/pairs/solana?limit=100") as response:
                if response.status == 200:
                    data = await response.json()
                    pairs = data.get('pairs', [])
                    
                    candidates = []
                    for pair in pairs:
                        if self.is_potential_flat(pair):
                            candidates.append(pair)
                    
                    logger.info(f"🔍 Encontrados {len(candidates)} candidatos planos")
                    return candidates
                else:
                    logger.error(f"Error DexScreener: {response.status}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error buscando candidatos planos: {e}")
            return []
    
    def is_potential_flat(self, pair):
        """Determina si un par tiene características de token plano"""
        try:
            # Volumen bajo como PESHI ($3-258 por intervalo)
            volume_h24 = pair.get('volume', {}).get('h24', 0)
            price_change = pair.get('priceChange', {}).get('h24', 0)
            liquidity = pair.get('liquidity', {}).get('usd', 0)
            
            # Filtros similares a PESHI: volumen bajo, cambio de precio mínimo
            return (volume_h24 < 10000 and           # Volumen bajo
                    abs(price_change) < 5 and        # Precio estable
                    liquidity >= MIN_LIQUIDITY and   # Liquidez suficiente
                    pair.get('fdv', 0) < 5000000)    # Market cap no muy alto
                    
        except Exception as e:
            logger.debug(f"Error evaluando par: {e}")
            return False
    
    # 🚀 MONITOREO PUMP.FUN CON HELIUS
    async def monitor_pump_fun_websocket(self, callback):
        """Monitorea tokens de Pump.fun via WebSocket de Helius"""
        try:
            async with websockets.connect(HELIUS_WSS_URL) as websocket:
                # Suscribirse a eventos de nuevos tokens
                subscribe_message = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]},  # Pump.fun contract
                        {"commitment": "processed"}
                    ]
                }
                await websocket.send(json.dumps(subscribe_message))
                
                while True:
                    message = await websocket.recv()
                    data = json.loads(message)
                    
                    if 'params' in data and 'result' in data['params']:
                        log = data['params']['result']['value']['logs']
                        await self.process_pump_log(log, callback)
                        
        except Exception as e:
            logger.error(f"Error WebSocket Pump.fun: {e}")
            await asyncio.sleep(5)
            await self.monitor_pump_fun_websocket(callback)
    
    async def process_pump_log(self, logs, callback):
        """Procesa logs de Pump.fun para detectar tokens cerca de graduación"""
        try:
            for log in logs:
                if 'marketCap' in log or 'price' in log:
                    # Extraer datos del token (simplificado - necesitarías parsing específico)
                    token_data = await self.extract_token_data_from_log(log)
                    if token_data and token_data['market_cap'] > PUMP_GRADUATION_THRESHOLD:
                        await callback(token_data)
                        
        except Exception as e:
            logger.error(f"Error procesando log: {e}")
    
    async def extract_token_data_from_log(self, log):
        """Extrae datos del token del log (necesita implementación específica)"""
        # Esta función necesita parsing específico de los logs de Pump.fun
        # Retornar datos simulados por ahora
        return {
            'mint': 'SIMULATED_MINT',
            'symbol': 'TEST',
            'market_cap': 66000,
            'price': 0.001
        }
    
    # 📊 ANÁLISIS DE VOLATILIDAD PARA TOKENS PLANOS
    async def analyze_volatility(self, mint_address):
        """Analiza la volatilidad histórica del token"""
        try:
            session = await self.get_session()
            
            # Obtener datos históricos de DexScreener
            async with session.get(f"{DEXSCREENER_API}/tokens/{mint_address}") as response:
                if response.status == 200:
                    data = await response.json()
                    pairs = data.get('pairs', [])
                    
                    if pairs:
                        pair = pairs[0]
                        price_history = pair.get('priceHistory', [])
                        
                        if len(price_history) >= 10:  # Mínimo 10 puntos de datos
                            volatility = self.calculate_price_volatility(price_history)
                            return {
                                'volatility': volatility,
                                'current_price': pair.get('priceUsd', 0),
                                'volume': pair.get('volume', {}).get('h24', 0),
                                'liquidity': pair.get('liquidity', {}).get('usd', 0)
                            }
            
            return None
            
        except Exception as e:
            logger.error(f"Error analizando volatilidad: {e}")
            return None
    
    def calculate_price_volatility(self, price_history):
        """Calcula la volatilidad basado en el historial de precios"""
        if len(price_history) < 2:
            return 100  # Alta volatilidad si no hay datos
        
        prices = [point.get('price', 0) for point in price_history if point.get('price', 0) > 0]
        if len(prices) < 2:
            return 100
        
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = abs((prices[i] - prices[i-1]) / prices[i-1] * 100)
                returns.append(ret)
        
        if not returns:
            return 0
        
        return sum(returns) / len(returns)

api_client = AdvancedAPIClient()

# ===================== SISTEMA DE DETECCIÓN =====================
class AdvancedTokenScanner:
    def __init__(self):
        self.flat_tokens = {}
        self.pump_tokens = {}
        self.price_history = defaultdict(lambda: deque(maxlen=100))
        self.bot_active = False
    
    async def start_flat_detection(self, context):
        """Inicia la detección de tokens planos"""
        logger.info("🎯 Iniciando detección de tokens planos...")
        
        while self.bot_active:
            try:
                candidates = await api_client.get_flat_candidates()
                
                for candidate in candidates:
                    if not self.bot_active:
                        break
                    
                    mint = candidate.get('baseToken', {}).get('address')
                    symbol = candidate.get('baseToken', {}).get('symbol', 'N/A')
                    
                    if mint and mint not in self.flat_tokens:
                        # Analizar volatilidad
                        analysis = await api_client.analyze_volatility(mint)
                        
                        if analysis and analysis['volatility'] < MAX_FLAT_VOLATILITY:
                            # TOKEN PLANO DETECTADO
                            self.flat_tokens[mint] = {
                                'symbol': symbol,
                                'base_price': analysis['current_price'],
                                'detected_at': time.time(),
                                'volatility': analysis['volatility'],
                                'volume': analysis['volume']
                            }
                            
                            await db.save_flat_token(mint, symbol, analysis['current_price'])
                            
                            # Enviar alerta inicial
                            await self.send_flat_detection_alert(context, mint, candidate, analysis)
                    
                    # Verificar breakouts en tokens planos existentes
                    await self.check_breakouts(context, mint, candidate)
                
                await asyncio.sleep(60)  # Revisar cada minuto
                
            except Exception as e:
                logger.error(f"Error en detección planos: {e}")
                await asyncio.sleep(30)
    
    async def check_breakouts(self, context, mint, candidate):
        """Verifica si un token plano ha tenido breakout"""
        if mint in self.flat_tokens:
            flat_info = self.flat_tokens[mint]
            current_price = candidate.get('priceUsd', 0)
            base_price = flat_info['base_price']
            
            if base_price > 0:
                price_change = (current_price - base_price) / base_price * 100
                
                if price_change >= BREAKOUT_THRESHOLD:
                    flat_duration = (time.time() - flat_info['detected_at']) / 60
                    
                    # BREAKOUT DETECTADO
                    await db.update_flat_token_breakout(mint, current_price, flat_duration)
                    await self.send_breakout_alert(context, mint, candidate, price_change, flat_duration)
                    
                    # Remover de monitoreo
                    self.flat_tokens.pop(mint, None)
    
    async def start_pump_fun_monitoring(self, context):
        """Inicia el monitoreo de Pump.fun"""
        logger.info("🚀 Iniciando monitoreo Pump.fun...")
        await api_client.monitor_pump_fun_websocket(
            lambda token_data: self.handle_pump_token(context, token_data)
        )
    
    async def handle_pump_token(self, context, token_data):
        """Maneja tokens de Pump.fun cerca de graduación"""
        try:
            mint = token_data['mint']
            symbol = token_data['symbol']
            market_cap = token_data['market_cap']
            
            if market_cap >= PUMP_GRADUATION_THRESHOLD and mint not in self.pump_tokens:
                self.pump_tokens[mint] = token_data
                await db.save_pump_token(mint, symbol, market_cap)
                await self.send_pump_alert(context, token_data)
                
        except Exception as e:
            logger.error(f"Error manejando token Pump.fun: {e}")

    # ===================== SISTEMA DE ALERTAS =====================
    async def send_flat_detection_alert(self, context, mint, candidate, analysis):
        """Envía alerta de detección de token plano"""
        try:
            symbol = candidate.get('baseToken', {}).get('symbol', 'N/A')
            price = candidate.get('priceUsd', 0)
            volume = candidate.get('volume', {}).get('h24', 0)
            liquidity = candidate.get('liquidity', {}).get('usd', 0)
            
            msg = (
                f"📊 *TOKEN PLANO DETECTADO* 📊\n\n"
                f"*Token:* {symbol}\n"
                f"*Dirección:* `{mint}`\n"
                f"*Precio Actual:* ${price:.6f}\n"
                f"*Volumen 24h:* ${volume:,.0f}\n"
                f"*Liquidez:* ${liquidity:,.0f}\n"
                f"*Volatilidad:* {analysis['volatility']:.2f}%\n"
                f"*Condición:* ESTABLE (similar a PESHI)\n\n"
                f"🔍 *ENLACES DE ANÁLISIS:*\n"
                f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
                f"• [Jupiter Swap](https://jup.ag/swap/SOL-{mint})\n"
                f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n"
                f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n\n"
                f"⚡ *Patrón:* Volumen bajo + Precio estable\n"
                f"🎯 *Objetivo:* Breakout > {BREAKOUT_THRESHOLD}%"
            )
            
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=False
            )
            
        except Exception as e:
            logger.error(f"Error enviando alerta plano: {e}")
    
    async def send_breakout_alert(self, context, mint, candidate, breakout_pct, flat_duration):
        """Envía alerta de breakout"""
        try:
            symbol = candidate.get('baseToken', {}).get('symbol', 'N/A')
            price = candidate.get('priceUsd', 0)
            
            msg = (
                f"🚀 *BREAKOUT DETECTADO* 🚀\n\n"
                f"*Token:* {symbol}\n"
                f"*Dirección:* `{mint}`\n"
                f"*Breakout:* +{breakout_pct:.1f}%\n"
                f"*Tiempo en Plano:* {flat_duration:.1f} minutos\n"
                f"*Precio Actual:* ${price:.6f}\n\n"
                f"🔍 *ENLACES RÁPIDOS:*\n"
                f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
                f"• [Jupiter Swap](https://jup.ag/swap/SOL-{mint})\n"
                f"• [Birdeye](https://birdeye.so/token/{mint}?chain=solana)\n\n"
                f"✅ *Patrón confirmado:* Flat {flat_duration:.0f}min → Breakout"
            )
            
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=msg,
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Error enviando alerta breakout: {e}")
    
    async def send_pump_alert(self, context, token_data):
        """Envía alerta de token Pump.fun cerca de graduación"""
        try:
            mint = token_data['mint']
            symbol = token_data['symbol']
            market_cap = token_data['market_cap']
            
            msg = (
                f"🎯 *PUMP.FUN - CERCA DE GRADUACIÓN* 🎯\n\n"
                f"*Token:* {symbol}\n"
                f"*Dirección:* `{mint}`\n"
                f"*Market Cap Actual:* ${market_cap:,.0f}\n"
                f"*Umbral Graduación:* $69,000\n"
                f"*Diferencia:* ${69000 - market_cap:,.0f}\n\n"
                f"🔍 *ENLACES CRÍTICOS:*\n"
                f"• [Pump.fun](https://pump.fun/coin/{mint})\n"
                f"• [DexScreener](https://dexscreener.com/solana/{mint})\n"
                f"• [Jupiter Swap](https://jup.ag/swap/SOL-{mint})\n"
                f"• [RugCheck](https://rugcheck.xyz/tokens/{mint})\n\n"
                f"⚡ *ACCIÓN INMINENTE:* Liquidez se bloqueará automáticamente"
            )
            
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=msg,
                parse_mode="Markdown",
                disable_web_page_preview=False
            )
            
        except Exception as e:
            logger.error(f"Error enviando alerta Pump.fun: {e}")

scanner = AdvancedTokenScanner()

# ===================== COMANDOS TELEGRAM =====================
async def start_detection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia ambos sistemas de detección"""
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.effective_chat.id
    
    scanner.bot_active = True
    
    welcome_msg = (
        "🤖 *SCANNER AVANZADO ACTIVADO* 🚀\n\n"
        "🎯 *SISTEMAS INICIADOS:*\n"
        "• 📊 Detector de Tokens Planos\n"
        "• 🚀 Monitor Pump.fun\n\n"
        "⚙️ *CONFIGURACIÓN:*\n"
        f"• Flat Min: {MIN_FLAT_DURATION_MINUTES}min\n"
        f"• Volatilidad Max: {MAX_FLAT_VOLATILITY}%\n"
        f"• Breakout: {BREAKOUT_THRESHOLD}%\n"
        f"• Pump Alert: ${PUMP_GRADUATION_THRESHOLD:,.0f}\n\n"
        "🔍 *BUSCANDO PATRÓN PESHI:*\n"
        "Volumen bajo + Precio estable + Breakout potencial"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")
    
    # Iniciar ambos sistemas
    asyncio.create_task(scanner.start_flat_detection(context))
    asyncio.create_task(scanner.start_pump_fun_monitoring(context))

async def stop_detection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene los sistemas"""
    scanner.bot_active = False
    await update.message.reply_text("🛑 Sistemas de detección detenidos")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra estado actual"""
    status_msg = (
        f"📊 *ESTADO SISTEMA*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if scanner.bot_active else '🔴 DETENIDO'}\n"
        f"📈 Tokens Planos: {len(scanner.flat_tokens)}\n"
        f"🚀 Tokens Pump: {len(scanner.pump_tokens)}\n"
        f"💾 DB Conectada: {'✅' if db.pool else '❌'}\n\n"
        f"🎯 *ESTRATEGIAS ACTIVAS:*\n"
        f"• Patrón PESHI (Flat + Breakout)\n"
        f"• Alertas Pump.fun pre-graduación"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

# ===================== MAIN =====================
async def main():
    """Función principal"""
    await db.init()
    
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Comandos
    app.add_handler(CommandHandler("start", start_detection))
    app.add_handler(CommandHandler("stop", stop_detection))
    app.add_handler(CommandHandler("status", status))
    
    logger.info("🚀 Scanner Avanzado - Listo para detectar patrones PESHI")
    logger.info(f"🎯 Configuración: Flat {MIN_FLAT_DURATION_MINUTES}min + Volatility {MAX_FLAT_VOLATILITY}%")
    
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
