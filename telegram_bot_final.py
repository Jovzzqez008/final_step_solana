# bot_jupiter_v2_pro.py - MODIFICADO PARA TOKENS PLANOS + BREAKOUT 10%
import asyncio, json, os, time, logging, aiohttp
from statistics import pstdev, mean
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict, deque

# ===================== CONFIGURACIÓN MODIFICADA =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 🛡️ FILTROS OPTIMIZADOS - VOLUMEN BAJADO A 70K
MIN_VOLUME_24H = 70000.0          # Volumen 24h mínimo (antes 50000)
MIN_LIQUIDITY = 70000.0           # Liquidez mínima (antes 25000)
MIN_AGE_HOURS = 12                # Antigüedad mínima
FLAT_STD_THRESHOLD = 0.12         # Más estricto para tokens planos
BREAKOUT_STEP = 10.0              # Breakout al 10% (antes 30%)
MIN_FLAT_MINUTES = 22             # Mínimo 22 minutos en plano
UPDATE_INTERVAL = 25              # Intervalo de actualización

# JUPITER LITE v2 ENDPOINTS
JUPITER_BASE_URL = "https://lite-api.jup.ag"
TOKENS_V2 = f"{JUPITER_BASE_URL}/tokens/v2"
TOP_ORGANIC_1H = f"{TOKENS_V2}/toporganicscore/1h?limit=80"
TOP_TRADED_1H = f"{TOKENS_V2}/toptraded/1h?limit=80"
RECENT_TOKENS = f"{TOKENS_V2}/recent?limit=50"
TAG_VERIFIED = f"{TOKENS_V2}/tag?query=verified"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("jupiter_v2_pro")

# ===================== ESTADO =====================
price_histories = defaultdict(lambda: deque(maxlen=80))  # Más datos para 22+ minutos
flat_tokens = {}
watchlist = []
token_metadata = {}
bot_active = False
monitored_tokens = set()

# ===================== JUPITER V2 CLIENT AVANZADO =====================
class JupiterV2ProAPI:
    def __init__(self):
        self.session = None
        self.request_count = 0
        self.cache = {}
        self.cache_times = {}
        
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def make_jupiter_request(self, url: str, cache_key: str = None, cache_ttl: int = 300):
        """Request genérico a Jupiter API con cache"""
        try:
            if cache_key and cache_key in self.cache:
                if time.time() - self.cache_times.get(cache_key, 0) < cache_ttl:
                    return self.cache[cache_key]
            
            session = await self.get_session()
            async with session.get(url, timeout=10) as response:
                self.request_count += 1
                
                if response.status == 200:
                    data = await response.json()
                    if cache_key:
                        self.cache[cache_key] = data
                        self.cache_times[cache_key] = time.time()
                    return data
                else:
                    logger.error(f"❌ Jupiter API error {response.status}: {url}")
                    return None
                    
        except Exception as e:
            logger.error(f"❌ Error Jupiter request: {e}")
            return None
    
    async def get_multiple_token_sources(self):
        """Obtiene tokens de múltiples fuentes de Jupiter v2"""
        sources = []
        
        # 1. Tokens con alto score orgánico
        logger.info("🔍 Obteniendo tokens con alto score orgánico...")
        organic_tokens = await self.make_jupiter_request(
            TOP_ORGANIC_1H, "top_organic", 600
        )
        if organic_tokens:
            sources.extend(organic_tokens)
            logger.info(f"✅ {len(organic_tokens)} tokens orgánicos")
        
        # 2. Tokens más tradeados
        logger.info("🔍 Obteniendo tokens más tradeados...")
        traded_tokens = await self.make_jupiter_request(
            TOP_TRADED_1H, "top_traded", 300
        )
        if traded_tokens:
            sources.extend(traded_tokens)
            logger.info(f"✅ {len(traded_tokens)} tokens tradeados")
        
        # 3. Tokens verificados
        logger.info("🔍 Obteniendo tokens verificados...")
        verified_tokens = await self.make_jupiter_request(
            TAG_VERIFIED, "verified", 1800
        )
        if verified_tokens:
            sources.extend(verified_tokens)
            logger.info(f"✅ {len(verified_tokens)} tokens verificados")
        
        # 4. Tokens recientes
        logger.info("🔍 Obteniendo tokens recientes...")
        recent_tokens = await self.make_jupiter_request(
            RECENT_TOKENS, "recent", 600
        )
        if recent_tokens:
            sources.extend(recent_tokens)
            logger.info(f"✅ {len(recent_tokens)} tokens recientes")
        
        return sources
    
    async def get_quality_tokens(self):
        """Filtra tokens de calidad usando datos de Jupiter v2"""
        try:
            all_tokens = await self.get_multiple_token_sources()
            if not all_tokens:
                return []
            
            # Eliminar duplicados
            unique_tokens = {}
            for token in all_tokens:
                token_id = token.get('id')
                if token_id and token_id not in unique_tokens:
                    unique_tokens[token_id] = token
            
            tokens_list = list(unique_tokens.values())
            logger.info(f"📊 Total tokens únicos: {len(tokens_list)}")
            
            # Aplicar filtros de calidad CON VOLUMEN REDUCIDO
            quality_tokens = []
            for token in tokens_list:
                if self.is_quality_token(token):
                    quality_tokens.append(token)
            
            logger.info(f"🎯 Tokens de calidad: {len(quality_tokens)}")
            return quality_tokens[:60]
            
        except Exception as e:
            logger.error(f"Error obteniendo tokens de calidad: {e}")
            return []
    
    def is_quality_token(self, token):
        """Determina si un token es de calidad - FILTROS MÁS FLEXIBLES"""
        try:
            # Datos básicos
            liquidity = token.get('liquidity', 0)
            volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                         token.get('stats24h', {}).get('sellVolume', 0))
            
            # FILTROS MÁS BAJOS - 70K en lugar de 100K+
            if liquidity < MIN_LIQUIDITY:
                return False
            
            if volume_24h < MIN_VOLUME_24H:
                return False
            
            # Verificación y score orgánico
            is_verified = token.get('isVerified', False)
            organic_score = token.get('organicScore', 0)
            
            # Más flexible con tokens no verificados pero con buen volumen
            if not is_verified and organic_score < 50 and volume_24h < 150000:
                return False
            
            # Excluir tokens con nombres sospechosos
            symbol = token.get('symbol', '').upper()
            name = token.get('name', '').upper()
            suspicious_words = ['TEST', 'FAKE', 'SCAM', 'RUG', 'DUMP', 'SHIT']
            
            if any(word in symbol or word in name for word in suspicious_words):
                return False
            
            return True
            
        except Exception as e:
            logger.debug(f"Error evaluando token calidad: {e}")
            return False
    
    async def get_token_price_from_jupiter(self, token_id: str):
        """Obtiene precio y datos desde Jupiter v2"""
        try:
            for token in self.cache.get('quality_tokens', []):
                if token.get('id') == token_id:
                    price = token.get('usdPrice', 0)
                    volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                                 token.get('stats24h', {}).get('sellVolume', 0))
                    liquidity = token.get('liquidity', 0)
                    
                    # USANDO FILTROS MÁS BAJOS
                    if price > 0 and volume_24h >= MIN_VOLUME_24H and liquidity >= MIN_LIQUIDITY:
                        return {
                            'price': price,
                            'volume24h': volume_24h,
                            'liquidity': liquidity,
                            'symbol': token.get('symbol'),
                            'name': token.get('name'),
                            'organic_score': token.get('organicScore'),
                            'is_verified': token.get('isVerified'),
                            'valid': True
                        }
            
            return None
            
        except Exception as e:
            logger.debug(f"Error obteniendo precio Jupiter: {e}")
            return None

jupiter_api = JupiterV2ProAPI()

# ===================== SISTEMA DE MONITOREO MEJORADO =====================
async def initialize_quality_tokens():
    """Inicializa tokens de calidad"""
    try:
        tokens = await jupiter_api.get_quality_tokens()
        if not tokens:
            logger.error("❌ No se pudieron obtener tokens de calidad")
            return []
        
        jupiter_api.cache['quality_tokens'] = tokens
        
        token_addresses = []
        for token in tokens:
            token_id = token.get('id')
            if token_id and token_id not in monitored_tokens:
                token_addresses.append(token_id)
                monitored_tokens.add(token_id)
                # Guardar metadata completa
                token_metadata[token_id] = {
                    'symbol': token.get('symbol', 'N/A'),
                    'name': token.get('name', 'N/A'),
                    'liquidity': token.get('liquidity', 0),
                    'volume24h': (token.get('stats24h', {}).get('buyVolume', 0) + 
                                 token.get('stats24h', {}).get('sellVolume', 0)),
                    'organic_score': token.get('organicScore', 0),
                    'is_verified': token.get('isVerified', False)
                }
                logger.info(f"✅ {token.get('symbol')}: Liq=${token.get('liquidity',0):,.0f}")
        
        logger.info(f"🚀 {len(token_addresses)} tokens de calidad listos (filtros: 70K vol)")
        return token_addresses
        
    except Exception as e:
        logger.error(f"Error inicializando tokens: {e}")
        return []

# ===================== ANÁLISIS TÉCNICO MEJORADO =====================
def calculate_volatility(hist):
    """Calcula volatilidad del historial de precios"""
    if len(hist) < 10:  # Mínimo 10 puntos para análisis
        return None
        
    prices = [point["price"] for point in hist if point["price"] > 0]
    if len(prices) < 8:
        return None
        
    returns = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
            returns.append(ret)
    
    if not returns:
        return None
        
    return {
        'std_dev': pstdev(returns) if len(returns) > 1 else 0,
        'max_move': max(abs(x) for x in returns) if returns else 0,
        'avg_move': mean([abs(x) for x in returns]) if returns else 0,
        'min_price': min(prices) if prices else 0,
        'max_price': max(prices) if prices else 0
    }

def is_flat_condition(hist):
    """Verifica si el token está en condición plana por más de 22 minutos"""
    if len(hist) < 53:  # ~22 minutos con intervalos de 25 segundos
        return False

    metrics = calculate_volatility(hist)
    if not metrics:
        return False
        
    # CONDICIÓN MÁS ESTRICTA PARA TOKENS PLANOS
    return (metrics['std_dev'] < FLAT_STD_THRESHOLD and 
            metrics['max_move'] < 0.6 and  # Máximo movimiento muy pequeño
            (metrics['max_price'] - metrics['min_price']) / metrics['min_price'] * 100 < 1.5)

def get_flat_duration_minutes(flat_since):
    """Calcula cuántos minutos ha estado el token en condición plana"""
    return (time.time() - flat_since) / 60

# ===================== MONITOREO PRINCIPAL MEJORADO =====================
async def monitor_quality_tokens(context: ContextTypes.DEFAULT_TYPE):
    """Monitoreo principal enfocado en tokens planos + breakout 10%"""
    logger.info("🎯 Iniciando monitoreo MEJORADO - Tokens planos +10% breakout...")
    
    tokens_to_monitor = await initialize_quality_tokens()
    
    if not tokens_to_monitor:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="❌ No se encontraron tokens de calidad para monitorear"
        )
        return
    
    await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text=f"✅ *Sistema MEJORADO Iniciado*\n\n"
             f"🎯 {len(tokens_to_monitor)} tokens monitoreados\n"
             f"📊 Breakout mínimo: +{BREAKOUT_STEP}%\n"
             f"⏱️  Mínimo en plano: {MIN_FLAT_MINUTES} minutos\n"
             f"💰 Filtros volumen: ${MIN_VOLUME_24H:,.0f}+\n\n"
             f"_Buscando tokens planos que explotan..._",
        parse_mode="Markdown"
    )
    
    iteration = 0
    while bot_active:
        try:
            iteration += 1
            start_time = time.time()
            
            processed = 0
            valid_tokens = 0
            breakout_detected = 0

            for token_addr in tokens_to_monitor:
                if not bot_active:
                    break
                
                token_data = await jupiter_api.get_token_price_from_jupiter(token_addr)
                if token_data and token_data.get('valid'):
                    valid_tokens += 1
                    result = await process_token_monitoring(token_addr, token_data, context)
                    if result:
                        processed += 1
                        breakout_detected += 1
                
                await asyncio.sleep(0.05)
            
            # Limpiar tokens que ya no están planos o tienen mucho tiempo sin movimiento
            await cleanup_flat_tokens()
            
            elapsed = time.time() - start_time
            logger.info(f"🔄 Iteración #{iteration}: {processed} procesados, {breakout_detected} breakouts en {elapsed:.1f}s")
            
            if iteration % 4 == 0:
                await send_progress_report(context, iteration, tokens_to_monitor, processed, valid_tokens)
            
            await asyncio.sleep(UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error en monitoreo: {e}")
            await asyncio.sleep(30)

async def cleanup_flat_tokens():
    """Limpia tokens que ya no están en condición plana o han caído mucho"""
    current_time = time.time()
    tokens_to_remove = []
    
    for token_addr, flat_info in list(flat_tokens.items()):
        flat_duration = current_time - flat_info["flat_since"]
        
        # Eliminar si ha estado más de 2 horas en plano sin breakout
        if flat_duration > 7200:  # 2 horas
            tokens_to_remove.append(token_addr)
            logger.info(f"🧹 Eliminando token plano viejo: {flat_info.get('symbol')}")
        
        # También eliminar si el volumen ha caído demasiado
        elif flat_info.get("volume", 0) < MIN_VOLUME_24H * 0.5:
            tokens_to_remove.append(token_addr)
            logger.info(f"🧹 Eliminando token con volumen bajo: {flat_info.get('symbol')}")
    
    for token_addr in tokens_to_remove:
        flat_tokens.pop(token_addr, None)

async def process_token_monitoring(token_addr: str, token_data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Procesa el monitoreo de un token individual - ENFOCADO EN PLANOS + BREAKOUT"""
    try:
        current_price = token_data['price']
        
        # Actualizar historial de precios
        hist = price_histories[token_addr]
        hist.append({
            "ts": time.time(), 
            "price": current_price,
            "volume": token_data.get('volume24h', 0)
        })
        
        # Añadir a watchlist
        if token_addr not in watchlist:
            watchlist.append(token_addr)
            if len(watchlist) > 50:
                watchlist.pop(0)
        
        # DETECTAR CONDICIÓN PLANA (más de 22 minutos)
        if token_addr not in flat_tokens and is_flat_condition(hist):
            flat_duration = get_flat_duration_minutes(time.time() - (len(hist) * UPDATE_INTERVAL))
            
            if flat_duration >= MIN_FLAT_MINUTES:
                flat_tokens[token_addr] = {
                    "first_price": current_price,
                    "flat_since": time.time(),
                    "max_alert": 0,
                    "symbol": token_data.get('symbol', 'N/A'),
                    "volume": token_data.get('volume24h', 0),
                    "liquidity": token_data.get('liquidity', 0),
                    "flat_duration_minutes": flat_duration
                }
                logger.info(f"📊 TOKEN PLANO DETECTADO: {token_data.get('symbol')} "
                           f"({flat_duration:.1f} minutos en plano)")
        
        # VERIFICAR BREAKOUT +10% DESDE PRECIO PLANO
        if token_addr in flat_tokens:
            base_info = flat_tokens[token_addr]
            base_price = base_info["first_price"]
            
            if base_price > 0:
                current_pct = (current_price - base_price) / base_price * 100
                last_alert = base_info["max_alert"]
                
                # DETECTAR BREAKOUT DE +10% O MÁS
                if current_pct >= BREAKOUT_STEP and current_pct >= last_alert + BREAKOUT_STEP:
                    flat_tokens[token_addr]["max_alert"] = current_pct
                    
                    # Solo alertar si ha estado suficiente tiempo en plano
                    flat_minutes = get_flat_duration_minutes(base_info["flat_since"])
                    if flat_minutes >= MIN_FLAT_MINUTES:
                        await send_breakout_alert(context, token_addr, current_pct, token_data, base_info)
                        logger.info(f"🚀 BREAKOUT {current_pct:.1f}%: {token_data.get('symbol')} "
                                   f"(después de {flat_minutes:.1f} minutos plano)")
                        return True
        
        return False
        
    except Exception as e:
        logger.debug(f"Error procesando {token_addr[:8]}: {e}")
        return False

async def send_breakout_alert(context, token_addr, breakout_pct, token_data, base_info):
    """Envía alerta de breakout CON ENLACES COMPLETOS"""
    try:
        symbol = token_data.get('symbol', 'N/A')
        
        # DIRECCIÓN COMPLETA para los enlaces
        full_address = token_addr
        
        # Determinar nivel de breakout
        if breakout_pct > 30:
            emoji, risk = "🚀🚀🚀", "EXPLOSIÓN"
        elif breakout_pct > 20:
            emoji, risk = "🚀🚀", "FUERTE"
        else:
            emoji, risk = "🚀", "MODERADO"
        
        flat_minutes = base_info.get("flat_duration_minutes", 0)
        
        msg = (
            f"{emoji} *BREAKOUT {breakout_pct:.1f}% DETECTADO* {emoji}\n\n"
            f"*Token:* {symbol}\n"
            f"*Dirección:* `{full_address}`\n"
            f"*Breakout:* +{breakout_pct:.2f}% desde base plana\n"
            f"*Tiempo en plano:* {flat_minutes:.1f} minutos\n"
            f"*Precio Actual:* ${token_data['price']:.6f}\n"
            f"*Volumen 24h:* ${token_data['volume24h']:,.0f}\n"
            f"*Liquidez:* ${token_data['liquidity']:,.0f}\n"
            f"*Score Orgánico:* {token_data.get('organic_score', 'N/A')}\n"
            f"*Verificado:* {'✅' if token_data.get('is_verified') else '❌'}\n"
            f"*Nivel:* {risk}\n\n"
            f"🔍 *ENLACES COMPLETOS:*\n"
            f"• [Jupiter Swap](https://jup.ag/swap/SOL-{full_address})\n"
            f"• [DexScreener](https://dexscreener.com/solana/{full_address})\n"
            f"• [Birdeye](https://birdeye.so/token/{full_address}?chain=solana)\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{full_address})\n\n"
            f"⚡ *Patrón: Token plano +{flat_minutes:.0f}min → Breakout +{breakout_pct:.1f}%*"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=False,  # Permitir preview para ver enlaces
        )
        
    except Exception as e:
        logger.error(f"Error enviando alerta: {e}")

async def send_progress_report(context, iteration, tokens, processed, valid_tokens):
    """Envía reporte de progreso MEJORADO"""
    try:
        # Contar tokens con diferentes tiempos en plano
        flat_stats = {"15-22min": 0, "22-30min": 0, "30+min": 0}
        current_time = time.time()
        
        for flat_info in flat_tokens.values():
            flat_minutes = (current_time - flat_info["flat_since"]) / 60
            if flat_minutes >= 30:
                flat_stats["30+min"] += 1
            elif flat_minutes >= 22:
                flat_stats["22-30min"] += 1
            elif flat_minutes >= 15:
                flat_stats["15-22min"] += 1
        
        report_msg = (
            f"📊 *Reporte #{iteration} - Sistema Mejorado*\n\n"
            f"✅ Tokens monitoreados: {len(tokens)}\n"
            f"🔍 Tokens válidos: {valid_tokens}\n"
            f"📈 Tokens planos: {len(flat_tokens)}\n"
            f"  ├── 15-22min: {flat_stats['15-22min']}\n"
            f"  ├── 22-30min: {flat_stats['22-30min']}\n"
            f"  └── 30+min: {flat_stats['30+min']}\n"
            f"🚀 Breakouts +{BREAKOUT_STEP}%: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}\n"
            f"📞 Requests API: {jupiter_api.request_count}\n\n"
            f"⚡ _Buscando tokens planos >22min + breakout {BREAKOUT_STEP}%_"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=report_msg,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error enviando reporte: {e}")

# ===================== COMANDOS TELEGRAM MEJORADOS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.effective_chat.id
    
    welcome_msg = (
        "🤖 *Jupiter v2 Pro - SISTEMA MEJORADO* 🚀\n\n"
        "🎯 *ENFOQUE PRINCIPAL:*\n"
        "• Tokens en rango plano >22 minutos\n"
        "• Breakout mínimo: +10%\n"
        "• Filtros volumen: $70K+\n"
        "• Enlaces completos incluidos\n\n"
        f"⚙️ *Configuración actual:*\n"
        f"• Breakout: +{BREAKOUT_STEP}%\n"
        f"• Mínimo plano: {MIN_FLAT_MINUTES}min\n"
        f"• Volumen: ${MIN_VOLUME_24H:,.0f}+\n"
        f"• Liquidez: ${MIN_LIQUIDITY:,.0f}+\n"
        f"• Intervalo: {UPDATE_INTERVAL}s\n\n"
        "📊 *Comandos:*\n"
        "• /cazar - Iniciar monitoreo\n"
        "• /parar - Detener\n"
        "• /status - Estado\n"
        "• /tokens - Lista tokens CON ENLACES\n"
        "• /planos - Tokens planos"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens monitoreados CON ENLACES COMPLETOS"""
    if not monitored_tokens:
        await update.message.reply_text("📭 No hay tokens monitoreados")
        return
        
    # Mostrar máximo 8 tokens para no saturar
    tokens_to_show = list(monitored_tokens)[:8]
    
    for i, addr in enumerate(tokens_to_show, 1):
        metadata = token_metadata.get(addr, {})
        symbol = metadata.get('symbol', 'N/A')
        
        msg = (
            f"🔍 *Token #{i}: {symbol}*\n"
            f"📍 *Dirección completa:*\n`{addr}`\n\n"
            f"🔗 *Enlaces directos:*\n"
            f"• [Jupiter Swap](https://jup.ag/swap/SOL-{addr})\n"
            f"• [DexScreener](https://dexscreener.com/solana/{addr})\n"
            f"• [Birdeye](https://birdeye.so/token/{addr}?chain=solana)\n"
            f"• [RugCheck](https://rugcheck.xyz/tokens/{addr})\n\n"
            f"📊 *Estadísticas:*\n"
            f"• Liquidez: ${metadata.get('liquidity', 0):,.0f}\n"
            f"• Volumen 24h: ${metadata.get('volume24h', 0):,.0f}\n"
            f"• Score: {metadata.get('organic_score', 'N/A')}\n"
            f"• Verificado: {'✅' if metadata.get('is_verified') else '❌'}"
        )
        
        await update.message.reply_text(
            msg, 
            parse_mode="Markdown",
            disable_web_page_preview=False
        )
    
    if len(monitored_tokens) > 8:
        await update.message.reply_text(
            f"📋 ... y {len(monitored_tokens) - 8} tokens más monitoreados",
            parse_mode="Markdown"
        )

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens planos CON DETALLES MEJORADOS"""
    if not flat_tokens:
        await update.message.reply_text("📊 No hay tokens planos detectados")
        return
        
    msg = "📊 *Tokens en Condición Plana:*\n\n"
    current_time = time.time()
    
    for i, (addr, info) in enumerate(list(flat_tokens.items())[:10], 1):
        symbol = info.get('symbol', 'N/A')
        since_minutes = (current_time - info["flat_since"]) / 60
        alert_pct = info.get("max_alert", 0)
        
        if alert_pct > 0:
            status = f"🚀 +{alert_pct:.1f}%"
        elif since_minutes >= 30:
            status = "⏰ 30+min"
        elif since_minutes >= 22:
            status = "✅ 22+min"
        else:
            status = "⏳ En desarrollo"
            
        msg += (f"{i}. {symbol} (`{addr[:12]}...`)\n"
                f"   └── {since_minutes:.1f}min | {status}\n")
    
    if len(flat_tokens) > 10:
        msg += f"\n📋 ... y {len(flat_tokens) - 10} tokens planos más"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# Los demás comandos (cazar, parar, status) se mantienen igual
async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el monitoreo"""
    global bot_active
    if bot_active:
        await update.message.reply_text("⚙️ Monitoreo ya activo")
        return
    
    bot_active = True
    await update.message.reply_text(
        "🚀 *ACTIVANDO SISTEMA MEJORADO*\n\n"
        "✅ Buscando tokens planos >22min\n"
        f"🎯 Breakout mínimo: +{BREAKOUT_STEP}%\n"
        f"💰 Filtros volumen: ${MIN_VOLUME_24H:,.0f}+\n"
        "📊 Enlaces completos activos\n\n"
        "_Iniciando monitoreo de tokens planos..._",
        parse_mode="Markdown"
    )
    
    asyncio.create_task(monitor_quality_tokens(context))

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el monitoreo"""
    global bot_active
    bot_active = False
    await update.message.reply_text("🛑 Monitoreo detenido")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado del sistema"""
    flat_count_22plus = 0
    current_time = time.time()
    
    for flat_info in flat_tokens.values():
        if (current_time - flat_info["flat_since"]) / 60 >= 22:
            flat_count_22plus += 1
    
    status_msg = (
        f"🚀 *JUPITER v2 PRO - SISTEMA MEJORADO*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if bot_active else '🔴 DETENIDO'}\n"
        f"✅ Tokens calidad: {len(monitored_tokens)}\n"
        f"📊 Tokens planos: {len(flat_tokens)}\n"
        f"⏰ Planos >22min: {flat_count_22plus}\n"
        f"📈 Breakouts +{BREAKOUT_STEP}%: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}\n"
        f"📞 Requests: {jupiter_api.request_count}\n\n"
        f"💡 _Especializado en tokens planos + breakout_"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

# ===================== MAIN =====================
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    commands = [
        ("start", cmd_start),
        ("cazar", cmd_cazar),
        ("parar", cmd_parar),
        ("status", cmd_status),
        ("tokens", cmd_tokens),
        ("planos", cmd_planos),
    ]
    
    for command, handler in commands:
        app.add_handler(CommandHandler(command, handler))
    
    logger.info("🚀 Jupiter v2 Pro - SISTEMA MEJORADO Iniciado")
    logger.info(f"🎯 Enfoque: Tokens planos >{MIN_FLAT_MINUTES}min + breakout {BREAKOUT_STEP}%")
    logger.info(f"💰 Filtros: Volumen ${MIN_VOLUME_24H:,.0f}+")
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
