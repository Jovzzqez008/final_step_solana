# bot_jupiter_v2_pro.py - USANDO JUPITER LITE v2 CON MÚLTIPLES ENDPOINTS
import asyncio, json, os, time, logging, aiohttp
from statistics import pstdev, mean
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict, deque

# ===================== CONFIGURACIÓN =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 🛡️ FILTROS OPTIMIZADOS
MIN_VOLUME_24H = 50000.0          # Volumen 24h mínimo
MIN_LIQUIDITY = 25000.0           # Liquidez mínima
MIN_AGE_HOURS = 12                # Antigüedad mínima
FLAT_STD_THRESHOLD = 0.15         # Para tokens planos
BREAKOUT_STEP = 10.0              # Breakout al 30%
UPDATE_INTERVAL = 25              # Intervalo de actualización

# JUPITER LITE v2 ENDPOINTS
JUPITER_BASE_URL = "https://lite-api.jup.ag"
TOKENS_V2 = f"{JUPITER_BASE_URL}/tokens/v2"
TOP_ORGANIC_1H = f"{TOKENS_V2}/toporganicscore/1h?limit=80"
TOP_TRADED_1H = f"{TOKENS_V2}/toptraded/1h?limit=80"
RECENT_TOKENS = f"{TOKENS_V2}/recent?limit=50"
TAG_VERIFIED = f"{TOKENS_V2}/tag?query=verified"

# DEXSCREENER PARA DATOS ADICIONALES
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("jupiter_v2_pro")

# ===================== ESTADO =====================
price_histories = defaultdict(lambda: deque(maxlen=20))
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
            # Verificar cache
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
        
        # 1. Tokens con alto score orgánico (más confiable)
        logger.info("🔍 Obteniendo tokens con alto score orgánico...")
        organic_tokens = await self.make_jupiter_request(
            TOP_ORGANIC_1H, "top_organic", 600  # 10 min cache
        )
        if organic_tokens:
            sources.extend(organic_tokens)
            logger.info(f"✅ {len(organic_tokens)} tokens orgánicos")
        
        # 2. Tokens más tradeados (alto volumen)
        logger.info("🔍 Obteniendo tokens más tradeados...")
        traded_tokens = await self.make_jupiter_request(
            TOP_TRADED_1H, "top_traded", 300  # 5 min cache
        )
        if traded_tokens:
            sources.extend(traded_tokens)
            logger.info(f"✅ {len(traded_tokens)} tokens tradeados")
        
        # 3. Tokens verificados
        logger.info("🔍 Obteniendo tokens verificados...")
        verified_tokens = await self.make_jupiter_request(
            TAG_VERIFIED, "verified", 1800  # 30 min cache
        )
        if verified_tokens:
            sources.extend(verified_tokens)
            logger.info(f"✅ {len(verified_tokens)} tokens verificados")
        
        # 4. Tokens recientes (para oportunidades tempranas)
        logger.info("🔍 Obteniendo tokens recientes...")
        recent_tokens = await self.make_jupiter_request(
            RECENT_TOKENS, "recent", 600  # 10 min cache
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
            
            # Aplicar filtros de calidad
            quality_tokens = []
            for token in tokens_list:
                if self.is_quality_token(token):
                    quality_tokens.append(token)
            
            logger.info(f"🎯 Tokens de calidad: {len(quality_tokens)}")
            return quality_tokens[:60]  # Limitar a 60 tokens
            
        except Exception as e:
            logger.error(f"Error obteniendo tokens de calidad: {e}")
            return []
    
    def is_quality_token(self, token):
        """Determina si un token es de calidad usando datos de Jupiter v2"""
        try:
            # Datos básicos
            liquidity = token.get('liquidity', 0)
            volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                         token.get('stats24h', {}).get('sellVolume', 0))
            
            # Filtros principales
            if liquidity < MIN_LIQUIDITY:
                return False
            
            if volume_24h < MIN_VOLUME_24H:
                return False
            
            # Verificación y score orgánico
            is_verified = token.get('isVerified', False)
            organic_score = token.get('organicScore', 0)
            organic_label = token.get('organicScoreLabel', '')
            
            # Solo tokens verificados o con buen score orgánico
            if not is_verified and organic_score < 70:
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
        """Obtiene precio y datos desde Jupiter v2 (sin DexScreener)"""
        try:
            # Los datos ya están en el cache de tokens de calidad
            for token in self.cache.get('quality_tokens', []):
                if token.get('id') == token_id:
                    price = token.get('usdPrice', 0)
                    volume_24h = (token.get('stats24h', {}).get('buyVolume', 0) + 
                                 token.get('stats24h', {}).get('sellVolume', 0))
                    liquidity = token.get('liquidity', 0)
                    
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

# ===================== SISTEMA DE MONITOREO =====================
async def initialize_quality_tokens():
    """Inicializa tokens de calidad"""
    try:
        tokens = await jupiter_api.get_quality_tokens()
        if not tokens:
            logger.error("❌ No se pudieron obtener tokens de calidad")
            return []
        
        # Guardar en cache para acceso rápido
        jupiter_api.cache['quality_tokens'] = tokens
        
        token_addresses = []
        for token in tokens:
            token_id = token.get('id')
            if token_id and token_id not in monitored_tokens:
                token_addresses.append(token_id)
                monitored_tokens.add(token_id)
                logger.info(f"✅ {token.get('symbol')}: Liq=${token.get('liquidity',0):,.0f}")
        
        logger.info(f"🚀 {len(token_addresses)} tokens de calidad listos")
        return token_addresses
        
    except Exception as e:
        logger.error(f"Error inicializando tokens: {e}")
        return []

# ===================== ANÁLISIS TÉCNICO =====================
def calculate_volatility(hist):
    """Calcula volatilidad del historial de precios"""
    if len(hist) < 5:
        return None
        
    prices = [point["price"] for point in hist if point["price"] > 0]
    if len(prices) < 4:
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
        'avg_move': mean([abs(x) for x in returns]) if returns else 0
    }

def is_flat_condition(hist):
    """Verifica si el token está en condición plana"""
    metrics = calculate_volatility(hist)
    if not metrics:
        return False
        
    return (metrics['std_dev'] < FLAT_STD_THRESHOLD and 
            metrics['max_move'] < 0.8)

# ===================== MONITOREO PRINCIPAL =====================
async def monitor_quality_tokens(context: ContextTypes.DEFAULT_TYPE):
    """Monitoreo principal de tokens de calidad"""
    logger.info("🎯 Iniciando monitoreo con Jupiter v2...")
    
    tokens_to_monitor = await initialize_quality_tokens()
    
    if not tokens_to_monitor:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="❌ No se encontraron tokens de calidad para monitorear"
        )
        return
    
    await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text=f"✅ *Sistema Iniciado*\n\n{len(tokens_to_monitor)} tokens de calidad monitoreados\nBreakout mínimo: {BREAKOUT_STEP}%",
        parse_mode="Markdown"
    )
    
    iteration = 0
    while bot_active:
        try:
            iteration += 1
            start_time = time.time()
            
            processed = 0
            valid_tokens = 0
            
            for token_addr in tokens_to_monitor:
                if not bot_active:
                    break
                
                token_data = await jupiter_api.get_token_price_from_jupiter(token_addr)
                if token_data and token_data.get('valid'):
                    valid_tokens += 1
                    result = await process_token_monitoring(token_addr, token_data, context)
                    if result:
                        processed += 1
                
                # Pequeña pausa para no saturar
                await asyncio.sleep(0.05)
            
            # Log de progreso
            elapsed = time.time() - start_time
            logger.info(f"🔄 Iteración #{iteration}: {processed}/{len(tokens_to_monitor)} tokens en {elapsed:.1f}s")
            
            # Reporte periódico
            if iteration % 4 == 0:
                await send_progress_report(context, iteration, tokens_to_monitor, processed, valid_tokens)
            
            await asyncio.sleep(UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error en monitoreo: {e}")
            await asyncio.sleep(30)

async def process_token_monitoring(token_addr: str, token_data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Procesa el monitoreo de un token individual"""
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
        
        # Detectar condición plana
        if token_addr not in flat_tokens and is_flat_condition(hist):
            flat_tokens[token_addr] = {
                "first_price": current_price,
                "flat_since": time.time(),
                "max_alert": 0,
                "symbol": token_data.get('symbol', 'N/A'),
                "volume": token_data.get('volume24h', 0),
                "liquidity": token_data.get('liquidity', 0)
            }
            logger.info(f"📊 TOKEN PLANO: {token_data.get('symbol')} ({token_addr[:8]}...)")
        
        # Verificar breakout
        if token_addr in flat_tokens:
            base_info = flat_tokens[token_addr]
            base_price = base_info["first_price"]
            
            if base_price > 0:
                current_pct = (current_price - base_price) / base_price * 100
                last_alert = base_info["max_alert"]
                
                # Solo alertar si supera el último alerta + step
                if current_pct >= last_alert + BREAKOUT_STEP:
                    flat_tokens[token_addr]["max_alert"] = current_pct
                    await send_breakout_alert(context, token_addr, current_pct, token_data, base_info)
                    logger.info(f"🚀 BREAKOUT {current_pct:.1f}%: {token_data.get('symbol')}")
                    return True
        
        return False
        
    except Exception as e:
        logger.debug(f"Error procesando {token_addr[:8]}: {e}")
        return False

async def send_breakout_alert(context, token_addr, breakout_pct, token_data, base_info):
    """Envía alerta de breakout"""
    try:
        symbol = token_data.get('symbol', 'N/A')
        short_addr = token_addr[:8] + "..." + token_addr[-6:]
        
        # Determinar nivel de riesgo
        if breakout_pct > 50:
            emoji, risk = "🚀🚀", "MUY ALTO"
        elif breakout_pct > 30:
            emoji, risk = "🚀", "ALTO"
        else:
            emoji, risk = "📈", "MEDIO"
        
        msg = (
            f"{emoji} *BREAKOUT {breakout_pct:.1f}% DETECTADO* 🎯\n\n"
            f"*Token:* {symbol} (`{short_addr}`)\n"
            f"*Cambio:* +{breakout_pct:.2f}% (desde base plana)\n"
            f"*Precio Actual:* ${token_data['price']:.6f}\n"
            f"*Volumen 24h:* ${token_data['volume24h']:,.0f}\n"
            f"*Liquidez:* ${token_data['liquidity']:,.0f}\n"
            f"*Score Orgánico:* {token_data.get('organic_score', 'N/A')}\n"
            f"*Verificado:* {'✅' if token_data.get('is_verified') else '❌'}\n"
            f"*Nivel Riesgo:* {risk}\n\n"
            f"🔍 *Análisis Rápido:*\n"
            f"- [Jupiter Swap](https://jup.ag/swap/SOL-{token_addr})\n"
            f"- [DexScreener](https://dexscreener.com/solana/{token_addr})\n"
            f"- [Birdeye](https://birdeye.so/token/{token_addr}?chain=solana)"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        
    except Exception as e:
        logger.error(f"Error enviando alerta: {e}")

async def send_progress_report(context, iteration, tokens, processed, valid_tokens):
    """Envía reporte de progreso"""
    try:
        report_msg = (
            f"📊 *Reporte #{iteration} - Jupiter v2 Pro*\n\n"
            f"✅ Tokens monitoreados: {len(tokens)}\n"
            f"🔍 Tokens válidos: {valid_tokens}\n"
            f"📈 Tokens planos: {len(flat_tokens)}\n"
            f"🚀 Breakouts detectados: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}\n"
            f"📞 Requests API: {jupiter_api.request_count}\n\n"
            f"⚡ _Sistema optimizado con Jupiter Lite v2_"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=report_msg,
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error enviando reporte: {e}")

# ===================== COMANDOS TELEGRAM =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.effective_chat.id
    
    welcome_msg = (
        "🤖 *Jupiter v2 Pro Breakout Bot* 🚀\n\n"
        "✅ *Características avanzadas:*\n"
        "• Jupiter Lite v2 API (más rápido)\n"
        "• Múltiples fuentes de tokens\n"
        "• Filtros de calidad automáticos\n"
        "• Score orgánico integrado\n"
        "• Breakout detection 30%+\n\n"
        f"🎯 *Configuración actual:*\n"
        f"• Breakout: +{BREAKOUT_STEP}%\n"
        f"• Volumen: ${MIN_VOLUME_24H:,.0f}+\n"
        f"• Liquidez: ${MIN_LIQUIDITY:,.0f}+\n"
        f"• Intervalo: {UPDATE_INTERVAL}s\n\n"
        "📊 *Comandos:*\n"
        "• /cazar - Iniciar monitoreo\n"
        "• /parar - Detener\n"
        "• /status - Estado\n"
        "• /tokens - Lista tokens\n"
        "• /planos - Tokens planos"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el monitoreo"""
    global bot_active
    if bot_active:
        await update.message.reply_text("⚙️ Monitoreo ya activo")
        return
    
    bot_active = True
    await update.message.reply_text(
        "🚀 *ACTIVANDO JUPITER v2 PRO*\n\n"
        "✅ Inicializando sistema...\n"
        "🔍 Cargando tokens de calidad...\n"
        f"🎯 Buscando breakouts > {BREAKOUT_STEP}%\n"
        "📊 Múltiples fuentes activas\n\n"
        "_Sistema optimizado con Jupiter Lite v2..._",
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
    status_msg = (
        f"🚀 *JUPITER v2 PRO - ESTADO*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if bot_active else '🔴 DETENIDO'}\n"
        f"✅ Tokens calidad: {len(monitored_tokens)}\n"
        f"📊 Tokens planos: {len(flat_tokens)}\n"
        f"📈 Breakouts {BREAKOUT_STEP}%+: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}\n"
        f"📞 Requests: {jupiter_api.request_count}\n\n"
        f"💡 _Sistema multi-fuente con Jupiter v2_"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens monitoreados"""
    if not monitored_tokens:
        await update.message.reply_text("📭 No hay tokens monitoreados")
        return
        
    msg = "👁️ *Tokens de Calidad Monitoreados:*\n\n"
    for i, addr in enumerate(list(monitored_tokens)[:12], 1):
        short_addr = addr[:8] + "..." + addr[-6:]
        status = "📊" if addr in flat_tokens else "🔍"
        hist_len = len(price_histories.get(addr, []))
        msg += f"{i}. `{short_addr}` {status} ({hist_len} datos)\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens planos"""
    if not flat_tokens:
        await update.message.reply_text("📊 No hay tokens planos detectados")
        return
        
    msg = "📊 *Tokens en Condición Plana:*\n\n"
    for i, (addr, info) in enumerate(list(flat_tokens.items())[:8], 1):
        symbol = info.get('symbol', 'N/A')
        since = datetime.fromtimestamp(info["flat_since"]).strftime("%H:%M")
        alert_pct = info.get("max_alert", 0)
        status = f"🚀 +{alert_pct:.1f}%" if alert_pct > 0 else "⏳ Plano"
        msg += f"{i}. {symbol} (`{addr[:8]}...`) | {since} | {status}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

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
    
    logger.info("🚀 Jupiter v2 Pro Breakout Bot Iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
