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

# ===================== CONFIGURACIÓN RAILWAY =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8066398402:AAEmj8mLhqM2yTyi7xO9Uf-GwAE3PTCjx1w")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8040288802")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:spskyTcDDlviBjMJCWjlvYbvpnheoULj@postgres-i7ys.railway.internal:5432/railway")
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=ac3de0dc-4108-489d-a8f8-96ab2f0ce341")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL", "wss://mainnet.helius-rpc.com/?api-key=ac3de0dc-4108-489d-a8f8-96ab2f0ce341")
DEXSCREENER_API = os.getenv("DEXSCREENER_API", "https://api.dexscreener.com/latest/dex")

# 🎯 CONFIGURACIÓN ESPECÍFICA
FLAT_CONFIG = {
    'MIN_FLAT_DURATION_HOURS': 1,
    'MAX_VOLATILITY': 1.5,
    'MAX_AVG_VOLUME_PER_CANDLE': 100,
    'MIN_LOW_VOLUME_CANDLES': 6,
    'CANDLES_TO_ANALYZE': 24,
}

# 🚀 PUMP.FUN - CONFIGURACIÓN REAL
PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_PRE_GRADUATION_THRESHOLD = 60000
PUMP_GRADUATION_TARGET = 69000

# ⚠️ FILTROS
MIN_LIQUIDITY = 30000
MIN_VOLUME_24H = 10000

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("pump_flat_bot")

# ===================== GESTIÓN DE DATOS =====================
class TokenManager:
    def __init__(self):
        self.pool = None
        self.recent_tokens = deque(maxlen=20)
    
    async def init(self):
        if DATABASE_URL:
            try:
                self.pool = await asyncpg.create_pool(DATABASE_URL)
                await self.create_tables()
                logger.info("✅ Base de datos conectada")
            except Exception as e:
                logger.error(f"❌ Error conectando a DB: {e}")
                self.pool = None
    
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
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute('''
                        INSERT INTO detected_tokens (mint_address, symbol, alert_type, metadata)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (mint_address) DO NOTHING
                    ''', mint, symbol, alert_type, json.dumps(metadata or {}))
            except Exception as e:
                logger.error(f"❌ Error guardando token en DB: {e}")
    
    async def get_recent_tokens(self, limit: int = 10, alert_type: str = None):
        if alert_type:
            return [t for t in list(self.recent_tokens) if t['type'] == alert_type][:limit]
        return list(self.recent_tokens)[:limit]
    
    async def is_duplicate(self, mint: str, alert_type: str) -> bool:
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
        """Obtener velas de DexScreener con mejor manejo de errores"""
        try:
            if not self.session:
                self.session = aiohttp.ClientSession()
            
            search_url = f"{DEXSCREENER_API}/search?q={mint}"
            async with self.session.get(search_url, timeout=8) as response:
                if response.status != 200:
                    return []
                
                data = await response.json()
                pairs = data.get('pairs', [])
                if not pairs:
                    return []
                
                # Buscar par de Solana
                pair = None
                for p in pairs:
                    if p.get('chainId') == 'solana':
                        pair = p
                        break
                
                if not pair:
                    return []
                
                # Obtener datos del par específico
                pair_address = pair.get('pairAddress')
                if not pair_address:
                    return []
                
                pair_url = f"{DEXSCREENER_API}/pairs/solana/{pair_address}"
                
                async with self.session.get(pair_url, timeout=8) as pair_response:
                    if pair_response.status != 200:
                        return []
                    
                    pair_data = await pair_response.json()
                    
                    # Manejar mejor la estructura de respuesta
                    if isinstance(pair_data, list) and len(pair_data) > 0:
                        pair_info = pair_data[0]
                    elif isinstance(pair_data, dict):
                        pair_info = pair_data
                    else:
                        return []
                    
                    # Obtener candles de manera segura
                    candles = pair_info.get('candles')
                    if not candles or not isinstance(candles, list):
                        return []
                    
                    normalized = []
                    for candle in candles[-limit:]:
                        if isinstance(candle, dict):
                            try:
                                normalized.append({
                                    'time': candle.get('timestamp', 0),
                                    'open': float(candle.get('open', 0)),
                                    'high': float(candle.get('high', 0)),
                                    'low': float(candle.get('low', 0)),
                                    'close': float(candle.get('close', 0)),
                                    'volume': float(candle.get('volume', 0))
                                })
                            except (ValueError, TypeError):
                                continue
                    
                    return normalized
                    
        except Exception as e:
            logger.error(f"❌ Error DexScreener {mint}: {e}")
            return []

api_client = FastAPIClient()

# ===================== DETECTOR FLAT =====================
class FlatDetector:
    async def analyze_flat_pattern(self, mint: str) -> dict:
        try:
            candles = await api_client.get_dexscreener_candles(mint, FLAT_CONFIG['CANDLES_TO_ANALYZE'])
            
            if len(candles) < 12:
                return {'is_flat': False, 'reason': 'insufficient_data'}
            
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
            
            volumes = [c['volume'] for c in candles]
            avg_volume = mean(volumes) if volumes else 0
            low_volume_count = sum(1 for v in volumes if v < 20)
            
            is_flat = (
                std_dev < FLAT_CONFIG['MAX_VOLATILITY'] and
                price_range < 3.0 and
                avg_volume < FLAT_CONFIG['MAX_AVG_VOLUME_PER_CANDLE'] and
                low_volume_count >= FLAT_CONFIG['MIN_LOW_VOLUME_CANDLES']
            )
            
            if is_flat:
                estimated_hours = len(candles) * 5 / 60
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
        
        # Verificar que sea un mint válido (no programas del sistema)
        invalid_mints = [
            "ComputeBudget111111111111111111111111111111",
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "11111111111111111111111111111111"
        ]
        
        if mint in invalid_mints:
            logger.info(f"⏭️  Mint del sistema detectado, omitiendo: {mint}")
            return
        
        message = (
            f"🚀 *PUMP.FUN - PRE-GRADUACIÓN* 🚀\n\n"
            f"*Mint:* `{mint}`\n"
            f"*Market Cap Actual:* ${market_cap:,.0f}\n"
            f"*Graduación en:* ${max(0, PUMP_GRADUATION_TARGET - market_cap):,.0f}\n\n"
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

# ===================== MONITOR PUMP.FUN MEJORADO =====================
class PumpFunMonitor:
    def __init__(self):
        self.active = False
        self.websocket = None
    
    async def start_monitoring(self):
        self.active = True
        logger.info("🚀 Iniciando monitor Pump.fun con Helius...")
        
        if not HELIUS_WSS_URL:
            logger.error("❌ HELIUS_WSS_URL no configurado")
            return
        
        await alert_system._send_message(
            f"🔥 *MONITOR PUMP.FUN INICIADO*\n\n"
            f"• Conectado a Helius WebSocket\n"
            f"• Filtrado mejorado activado\n"
            f"• Alerta en: ${PUMP_PRE_GRADUATION_THRESHOLD:,.0f}+ MC\n\n"
            "_Buscando tokens reales de Pump.fun..._"
        )
        
        while self.active:
            try:
                async with websockets.connect(HELIUS_WSS_URL) as websocket:
                    logger.info("✅ Conectado a WebSocket Helius")
                    
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
                    await websocket.send(json.dumps(subscribe_msg))
                    logger.info("✅ Suscrito a logs de Pump.fun")
                    
                    while self.active:
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=30)
                            data = json.loads(message)
                            await self._process_pumpfun_event(data)
                            
                        except asyncio.TimeoutError:
                            # Keep-alive
                            await websocket.ping()
                        except Exception as e:
                            logger.error(f"❌ Error procesando mensaje WebSocket: {e}")
                            break
                            
            except Exception as e:
                logger.error(f"❌ Error conexión WebSocket: {e}")
                if self.active:
                    logger.info("🔄 Reconectando en 5 segundos...")
                    await asyncio.sleep(5)
    
    async def _process_pumpfun_event(self, event_data):
        try:
            # Filtro mejorado para detectar solo tokens reales de Pump.fun
            if 'params' not in event_data:
                return
                
            result = event_data['params'].get('result', {})
            logs = result.get('value', {}).get('logs', [])
            
            if not logs:
                return
            
            log_text = " ".join(logs)
            
            # Filtrar programas del sistema conocidos
            system_programs = [
                "ComputeBudget111111111111111111111111111111",
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", 
                "11111111111111111111111111111111",
                "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
                "SystemProgram1111111111111111111111111111111"
            ]
            
            # Buscar mint addresses válidos (excluyendo programas del sistema)
            import re
            mint_pattern = r"([1-9A-HJ-NP-Za-km-z]{32,44})"
            all_mints = re.findall(mint_pattern, log_text)
            
            valid_mints = []
            for mint in all_mints:
                if mint not in system_programs and mint != PUMPFUN_PROGRAM_ID:
                    valid_mints.append(mint)
            
            if not valid_mints:
                return
            
            # Buscar market cap realista
            mc_patterns = [
                r"market cap.*?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)",
                r"market_cap.*?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)",
                r"mc.*?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)",
                r"graduat.*?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)",
                r"price.*?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)"
            ]
            
            for pattern in mc_patterns:
                matches = re.findall(pattern, log_text, re.IGNORECASE)
                for match in matches:
                    try:
                        # Limpiar el número (remover comas)
                        clean_match = match.replace(',', '')
                        market_cap = float(clean_match)
                        
                        # Solo alertar si el market cap es realista para Pump.fun
                        if (PUMP_PRE_GRADUATION_THRESHOLD <= market_cap <= 1000000 and 
                            market_cap > 0):
                            
                            # Usar el primer mint válido encontrado
                            mint = valid_mints[0]
                            logger.info(f"🎯 Token Pump.fun detectado: {mint} - MC: ${market_cap:,.0f}")
                            await alert_system.send_pumpfun_alert(mint, market_cap)
                            return
                            
                    except (ValueError, TypeError):
                        continue
                        
        except Exception as e:
            logger.error(f"❌ Error procesando evento Pump.fun: {e}")

    def stop(self):
        self.active = False
        logger.info("🛑 Monitor Pump.fun detenido")

pump_monitor = PumpFunMonitor()

# ===================== SCANNER FLAT =====================
class FlatScanner:
    def __init__(self):
        self.active = False
    
    async def start_scanning(self):
        self.active = True
        logger.info("🔄 Iniciando scanner FLAT...")
        
        await alert_system._send_message(
            "🎯 *SCANNER FLAT INICIADO*\n\n"
            f"• Duración mínima: {FLAT_CONFIG['MIN_FLAT_DURATION_HOURS']} hora\n"
            f"• Volatilidad máxima: {FLAT_CONFIG['MAX_VOLATILITY']}%\n"
            f"• Intervalo: 60 segundos\n\n"
            "_Buscando tokens en fase de acumulación..._"
        )
        
        while self.active:
            try:
                tokens = await api_client.get_jupiter_tokens()
                logger.info(f"🔍 Analizando {len(tokens)} tokens para FLAT")
                
                flat_detections = 0
                
                for token in tokens[:30]:
                    if not self.active:
                        break
                    
                    mint = token.get('id')
                    symbol = token.get('symbol', 'N/A')
                    
                    if not mint:
                        continue
                    
                    # Saltar tokens con símbolos muy genéricos
                    generic_symbols = ['SOL', 'USDC', 'USDT', 'BONK', 'JUP', 'RAY', 'ORCA']
                    if symbol in generic_symbols:
                        continue
                    
                    try:
                        flat_analysis = await flat_detector.analyze_flat_pattern(mint)
                        
                        if flat_analysis['is_flat']:
                            logger.info(f"✅ FLAT detectado: {symbol} - {flat_analysis['duration_hours']:.1f}h")
                            await alert_system.send_flat_alert(mint, token, flat_analysis)
                            flat_detections += 1
                    except Exception as e:
                        logger.error(f"❌ Error analizando {mint}: {e}")
                        continue
                    
                    await asyncio.sleep(1)  # Rate limiting
                
                logger.info(f"📊 Scan completado: {flat_detections} tokens FLAT")
                await asyncio.sleep(60)  # Esperar 1 minuto entre scans
                
            except Exception as e:
                logger.error(f"❌ Error en scanner FLAT: {e}")
                await asyncio.sleep(30)
    
    def stop(self):
        self.active = False

flat_scanner = FlatScanner()

# ===================== COMANDOS MANUALES =====================
async def handle_telegram_commands():
    """Manejo simple de comandos via mensajes directos"""
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    
    async def send_menu():
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=(
                "🤖 *BOT PUMP.FUN + FLAT DETECTOR* 🚀\n\n"
                "📋 *COMANDOS DISPONIBLES:*\n"
                "• `iniciar` - Activar scanners\n"
                "• `detener` - Parar scanners\n"
                "• `tokens` - Últimos 10 tokens\n"
                "• `flat` - Solo tokens FLAT\n"
                "• `pump` - Solo tokens Pump.fun\n"
                "• `status` - Estado del sistema\n\n"
                "⚡ *Envía el comando como mensaje*"
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    
    # Enviar menú inicial
    await send_menu()
    
    last_update_id = None
    
    while True:
        try:
            updates = await bot.get_updates(offset=last_update_id, timeout=30)
            
            for update in updates:
                if update.message and update.message.text:
                    command = update.message.text.lower().strip()
                    chat_id = update.message.chat_id
                    
                    if chat_id != int(TELEGRAM_CHAT_ID):
                        continue
                    
                    if command == 'iniciar':
                        asyncio.create_task(flat_scanner.start_scanning())
                        asyncio.create_task(pump_monitor.start_monitoring())
                        await bot.send_message(chat_id, "✅ Scanners activados")
                    
                    elif command == 'detener':
                        flat_scanner.stop()
                        pump_monitor.stop()
                        await bot.send_message(chat_id, "🛑 Scanners detenidos")
                    
                    elif command == 'tokens':
                        tokens = await token_manager.get_recent_tokens(10)
                        await alert_system.send_token_list(tokens, "todos los tokens")
                    
                    elif command == 'flat':
                        tokens = await token_manager.get_recent_tokens(10, "FLAT")
                        await alert_system.send_token_list(tokens, "FLAT")
                    
                    elif command == 'pump':
                        tokens = await token_manager.get_recent_tokens(10, "PUMPFUN")
                        await alert_system.send_token_list(tokens, "PUMP.FUN")
                    
                    elif command == 'status':
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
                        await bot.send_message(chat_id, status_msg, parse_mode=ParseMode.MARKDOWN)
                    
                    elif command in ['help', 'start', 'menu']:
                        await send_menu()
                    
                    else:
                        await bot.send_message(chat_id, "❌ Comando no reconocido. Usa `help` para ver opciones.")
                
                last_update_id = update.update_id + 1
                
        except Exception as e:
            logger.error(f"❌ Error en manejo de comandos: {e}")
            await asyncio.sleep(5)

# ===================== MAIN =====================
async def main():
    logger.info("🚀 INICIANDO BOT PUMP.FUN + FLAT...")
    
    await token_manager.init()
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return
    
    # Mensaje de inicio
    await alert_system._send_message(
        "🤖 *BOT INICIADO* 🚀\n\n"
        "✅ Sistema listo para comandos\n"
        "✅ Base de datos conectada\n" 
        "✅ Helius configurado\n"
        "✅ Filtros mejorados activos\n\n"
        "📋 *Comandos disponibles:*\n"
        "• `iniciar` - Activar scanners\n"
        "• `detener` - Parar scanners\n"  
        "• `tokens` - Últimos 10 tokens\n"
        "• `flat` - Solo tokens FLAT\n"
        "• `pump` - Solo tokens Pump.fun\n"
        "• `status` - Estado del sistema\n\n"
        "⚡ *Envía el comando como mensaje*"
    )
    
    # Iniciar manejador de comandos
    asyncio.create_task(handle_telegram_commands())
    
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("👋 Bot terminado por usuario")
    finally:
        flat_scanner.stop()
        pump_monitor.stop()

if __name__ == "__main__":
    asyncio.run(main())
