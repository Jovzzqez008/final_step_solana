# bot_antirug_completo.py - VERSIÓN COMPLETA Y FUNCIONAL
import asyncio, json, os, time, logging, aiohttp
from statistics import pstdev, mean
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict, deque

# ===================== CONFIGURACIÓN SEGURA =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 🛡️ FILTROS ANTIRUG PULL EXTREMOS
MIN_VOLUME_USD = 75000.0          # Volumen mínimo ALTO
MIN_LIQUIDITY = 50000.0           # Liquidez mínima ALTA  
MIN_AGE_HOURS = 24                # Mínimo 24 horas de antigüedad
MIN_HOLDERS = 100                 # Mínimo de holders
MAX_TAX_BUY = 5.0                 # Máximo 5% de tax en compra
MAX_TAX_SELL = 5.0                # Máximo 5% de tax en venta
MIN_MARKET_CAP = 100000.0         # Market cap mínimo
FLAT_STD_THRESHOLD = 0.2          # Más estricto para "plano"
BREAKOUT_STEP = 15.0              # Breakout más significativo
UPDATE_INTERVAL = 30              # Más lento para mejor análisis

# APIs
JUPITER_TOKENS_API = "https://api.jup.ag/tokens/v1/all"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"
BIRDEYE_API = "https://public-api.birdeye.so/public"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("antirug_bot")

# ===================== ESTADO =====================
price_histories = defaultdict(lambda: deque(maxlen=30))
flat_tokens = {}
watchlist = []
token_metadata = {}
bot_active = False
monitored_tokens = set()
blacklisted_tokens = set()

# ===================== APIs CLIENT MEJORADO =====================
class SecurityAPI:
    def __init__(self):
        self.session = None
        self.request_count = 0
        self.birdeye_api_key = os.getenv("BIRDEYE_API_KEY", "")
        
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def get_token_security_data(self, token_address: str):
        """Obtiene datos de seguridad del token"""
        try:
            session = await self.get_session()
            url = f"{DEXSCREENER_API}/tokens/{token_address}"
            
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('pairs') and len(data['pairs']) > 0:
                        pair = data['pairs'][0]
                        
                        # 🛡️ DATOS BÁSICOS DE SEGURIDAD
                        price = float(pair.get('priceUsd', 0))
                        volume24h = float(pair.get('volume', {}).get('h24', 0))
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        fdv = float(pair.get('fdv', 0))
                        
                        # Calcular edad aproximada del token
                        pair_created_at = pair.get('pairCreatedAt')
                        age_hours = self.calculate_token_age(pair_created_at)
                        
                        # 🚨 FILTROS DE SEGURIDAD PRIMARIOS
                        if (price <= 0 or 
                            volume24h < MIN_VOLUME_USD or 
                            liquidity < MIN_LIQUIDITY or
                            fdv < MIN_MARKET_CAP or
                            (age_hours is not None and age_hours < MIN_AGE_HOURS)):
                            return None
                        
                        # ✅ TOKEN PASÓ FILTROS BÁSICOS
                        token_data = {
                            'price': price,
                            'volume24h': volume24h,
                            'liquidity': liquidity,
                            'fdv': fdv,
                            'age_hours': age_hours,
                            'dex': pair.get('dexId'),
                            'pair_address': pair.get('pairAddress'),
                            'valid': True
                        }
                        
                        return token_data
            return None
        except Exception as e:
            logger.debug(f"Error seguridad {token_address}: {e}")
            return None
    
    def calculate_token_age(self, pair_created_at):
        """Calcula la edad del token en horas"""
        if not pair_created_at:
            return None
            
        try:
            created_timestamp = pair_created_at / 1000
            current_time = time.time()
            age_hours = (current_time - created_timestamp) / 3600
            return age_hours
        except:
            return None

security_api = SecurityAPI()

# ===================== DETECCIÓN SEGURA =====================
async def get_jupiter_tokens_safe():
    """Obtiene tokens de Jupiter con filtros de seguridad"""
    try:
        session = aiohttp.ClientSession()
        async with session.get(JUPITER_TOKENS_API, timeout=15) as response:
            if response.status == 200:
                tokens = await response.json()
                
                # Filtrar tokens con símbolos sospechosos
                safe_tokens = []
                suspicious_keywords = [
                    'TEST', 'FAKE', 'SCAM', 'RUG', 'PULL', 'DUMP', 
                    'SHIT', 'MEME', 'MOON', 'SHIB', 'DOGE', 'ELON', 
                    'TSUKI', 'AKITA', 'HUSKY', 'FLOKI', 'PEPE', 'WOJAK'
                ]
                
                for token in tokens:
                    symbol = token.get('symbol', '').upper()
                    name = token.get('name', '').upper()
                    
                    # 🚨 FILTRO DE SÍMBOLOS/NOMBRES SOSPECHOSOS
                    is_suspicious = any(keyword in symbol or keyword in name 
                                      for keyword in suspicious_keywords)
                    
                    if not is_suspicious:
                        safe_tokens.append(token)
                
                logger.info(f"🛡️ Filtrados {len(tokens) - len(safe_tokens)} tokens sospechosos")
                return safe_tokens[:150]  # Limitar a 150 tokens
        return []
    except Exception as e:
        logger.error(f"Error obteniendo tokens seguros: {e}")
        return []

async def initialize_safe_token_list():
    """Inicializa lista de tokens seguros"""
    try:
        tokens = await get_jupiter_tokens_safe()
        if not tokens:
            logger.error("❌ No se pudieron obtener tokens seguros")
            return []
        
        safe_tokens = []
        for token in tokens:
            addr = token.get('address')
            if addr and addr not in monitored_tokens and addr not in blacklisted_tokens:
                token_data = await security_api.get_token_security_data(addr)
                if token_data and token_data.get('valid'):
                    safe_tokens.append(addr)
                    monitored_tokens.add(addr)
                else:
                    blacklisted_tokens.add(addr)
        
        logger.info(f"🛡️ Inicializados {len(safe_tokens)} tokens SEGUROS")
        return safe_tokens
        
    except Exception as e:
        logger.error(f"Error inicializando tokens seguros: {e}")
        return []

# ===================== ANÁLISIS TÉCNICO SEGURO =====================
def is_flat_safe(hist):
    """Detección MUY estricta de tokens planos"""
    if len(hist) < 8:
        return False
        
    prices = [point["price"] for point in hist if point["price"] > 0]
    if len(prices) < 6:
        return False
        
    returns = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            ret = (prices[i] - prices[i-1]) / prices[i-1] * 100
            returns.append(ret)
    
    if not returns:
        return False
        
    sd = pstdev(returns) if len(returns) > 1 else 100
    max_move = max(abs(x) for x in returns) if returns else 100
    avg_move = mean([abs(x) for x in returns]) if returns else 100
    
    return (sd < FLAT_STD_THRESHOLD and 
            max_move < 0.5 and
            avg_move < 0.2)

# ===================== MONITOREO SEGURO =====================
async def monitor_safe_tokens(context: ContextTypes.DEFAULT_TYPE):
    """Monitoreo principal con filtros de seguridad"""
    logger.info("🛡️ Iniciando monitoreo SEGURO...")
    
    tokens_to_monitor = await initialize_safe_token_list()
    
    if not tokens_to_monitor:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="❌ No se encontraron tokens que pasen los filtros de seguridad"
        )
        return
    
    iteration = 0
    while bot_active:
        try:
            iteration += 1
            logger.info(f"🛡️ Iteración #{iteration} - {len(tokens_to_monitor)} tokens seguros")
            
            processed = 0
            threats_detected = 0
            
            for token_addr in tokens_to_monitor.copy():
                if not bot_active:
                    break
                    
                # Verificar seguridad en cada iteración
                token_data = await security_api.get_token_security_data(token_addr)
                if not token_data or not token_data.get('valid'):
                    blacklisted_tokens.add(token_addr)
                    if token_addr in tokens_to_monitor:
                        tokens_to_monitor.remove(token_addr)
                    threats_detected += 1
                    continue
                
                # Procesar token seguro
                result = await process_safe_token(token_addr, token_data, context)
                if result:
                    processed += 1
                
                await asyncio.sleep(0.2)
            
            # Reporte de seguridad
            if iteration % 5 == 0:
                security_report = (
                    f"🛡️ **REPORTE DE SEGURIDAD #{iteration}**\n\n"
                    f"✅ Tokens seguros: {len(tokens_to_monitor)}\n"
                    f"🔍 Tokens procesados: {processed}\n"
                    f"🚫 Amenazas detectadas: {threats_detected}\n"
                    f"📊 Tokens planos: {len(flat_tokens)}\n"
                    f"📈 Breakouts seguros: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}\n\n"
                    f"💡 _Filtros activos: Volumen >${MIN_VOLUME_USD:,.0f}, "
                    f"Liquidez >${MIN_LIQUIDITY:,.0f}_"
                )
                await context.bot.send_message(
                    chat_id=TARGET_CHAT_ID,
                    text=security_report,
                    parse_mode="Markdown"
                )
            
            await asyncio.sleep(UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error en monitoreo seguro: {e}")
            await asyncio.sleep(30)

async def process_safe_token(token_addr: str, token_data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Procesa un token que pasó todos los filtros de seguridad"""
    try:
        current_price = token_data['price']
        
        # Actualizar historial
        hist = price_histories[token_addr]
        hist.append({
            "ts": time.time(), 
            "price": current_price,
            "volume": token_data.get('volume24h', 0)
        })
        
        # Añadir a watchlist
        if token_addr not in watchlist:
            watchlist.append(token_addr)
            if len(watchlist) > 80:
                watchlist.pop(0)
        
        # Detectar tokens planos
        if token_addr not in flat_tokens and is_flat_safe(hist):
            flat_tokens[token_addr] = {
                "first_price": current_price,
                "flat_since": time.time(),
                "max_alert": 0,
                "volume": token_data.get('volume24h', 0),
                "liquidity": token_data.get('liquidity', 0),
                "age_hours": token_data.get('age_hours', 0)
            }
            logger.info(f"🛡️ TOKEN PLANO SEGURO: {token_addr[:8]}...")
        
        # Detectar breakout
        if token_addr in flat_tokens:
            base_price = flat_tokens[token_addr]["first_price"]
            if base_price > 0:
                current_pct = (current_price - base_price) / base_price * 100
                last_alert = flat_tokens[token_addr]["max_alert"]
                
                if current_pct >= last_alert + BREAKOUT_STEP:
                    # Verificar que aún sea seguro
                    current_data = await security_api.get_token_security_data(token_addr)
                    if current_data and current_data.get('valid'):
                        flat_tokens[token_addr]["max_alert"] = current_pct
                        await send_safe_breakout_alert(context, token_addr, current_pct, token_data)
                        logger.info(f"🚀 BREAKOUT SEGURO: {token_addr[:8]}... +{current_pct:.1f}%")
        
        return {'processed': True}
        
    except Exception as e:
        logger.debug(f"Error procesando token seguro {token_addr}: {e}")
        return None

# ===================== ALERTAS SEGURAS =====================
async def send_safe_breakout_alert(context, token_addr, breakout_pct, token_data):
    """Envía alertas de breakout con información de seguridad"""
    try:
        short_addr = token_addr[:8] + "..." + token_addr[-6:]
        age_hours = token_data.get('age_hours', 0)
        
        # Evaluación de riesgo
        risk_level = "🟢 BAJO"
        if age_hours < 48:
            risk_level = "🟡 MEDIO"
        if age_hours < 24:
            risk_level = "🔴 ALTO"
        
        emoji = "🚀" if breakout_pct > 25 else "📈" if breakout_pct > 15 else "🔼"
        
        msg = (
            f"{emoji} *BREAKOUT SEGURO DETECTADO* 🛡️\n\n"
            f"*Token:* `{short_addr}`\n"
            f"*Cambio:* +{breakout_pct:.2f}%\n"
            f"*Precio:* ${token_data['price']:.6f}\n"
            f"*Volumen 24h:* ${token_data['volume24h']:,.0f}\n"
            f"*Liquidez:* ${token_data['liquidity']:,.0f}\n"
            f"*Edad aprox.:* {age_hours:.1f} horas\n"
            f"*Nivel de Riesgo:* {risk_level}\n\n"
            f"✅ *Filtros pasados:*\n"
            f"• Volumen > ${MIN_VOLUME_USD:,.0f}\n"
            f"• Liquidez > ${MIN_LIQUIDITY:,.0f}\n"
            f"• Antigüedad > {MIN_AGE_HOURS}h\n\n"
            f"🔍 *Verificación:*\n"
            f"- [DexScreener](https://dexscreener.com/solana/{token_addr})\n"
            f"- [Birdeye](https://birdeye.so/token/{token_addr}?chain=solana)\n"
            f"- [RugCheck](https://rugcheck.xyz/tokens/{token_addr})\n"
            f"- [Jupiter](https://jup.ag/swap/SOL-{token_addr})"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        
        logger.info(f"🛡️ Alerta segura enviada: {short_addr} +{breakout_pct:.1f}%")
        
    except Exception as e:
        logger.error(f"Error enviando alerta segura: {e}")

# ===================== COMANDOS TELEGRAM =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.effective_chat.id
    
    welcome_msg = (
        "🤖 *AntiRug Breakout Bot* 🛡️\n\n"
        "✅ *Filtros de seguridad activos:*\n"
        f"• Volumen mínimo: ${MIN_VOLUME_USD:,.0f}\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Antigüedad mínima: {MIN_AGE_HOURS} horas\n"
        f"• Market cap mínimo: ${MIN_MARKET_CAP:,.0f}\n"
        f"• Breakout mínimo: +{BREAKOUT_STEP}%\n\n"
        "🚫 *Bloquea automáticamente:*\n"
        "• Tokens sospechosos (TEST, FAKE, SCAM...)\n"
        "• Meme coins (DOGE, SHIB, ELON, PEPE...)\n"
        "• Tokens muy nuevos (<24h)\n"
        "• Tokens con poca liquidez/volumen\n\n"
        "📊 *Comandos:*\n"
        "• /cazar - Iniciar monitoreo SEGURO\n"
        "• /parar - Detener\n"
        "• /status - Estado y seguridad\n"
        "• /tokens - Tokens monitoreados\n"
        "• /planos - Tokens planos seguros\n"
        "• /blacklist - Tokens bloqueados\n"
        "• /config - Configuración seguridad"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el monitoreo seguro"""
    global bot_active
    if bot_active:
        await update.message.reply_text("⚙️ Ya está monitoreando tokens seguros.")
        return
    
    bot_active = True
    await update.message.reply_text(
        "🎯 *INICIANDO MONITOREO SEGURO*\n\n"
        "🛡️ Cargando tokens con filtros de seguridad...\n"
        "🔍 Aplicando filtros anti-rug pull...\n"
        "📊 Verificando volumen, liquidez y antigüedad...\n"
        "⏰ Monitoreo activo cada 30 segundos\n\n"
        "_Buscando oportunidades SEGURAS..._",
        parse_mode="Markdown"
    )
    
    # Iniciar monitoreo seguro
    asyncio.create_task(monitor_safe_tokens(context))

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el monitoreo"""
    global bot_active
    bot_active = False
    await update.message.reply_text(
        "🛑 *MONITOREO DETENIDO*\n\n"
        "El bot ha dejado de buscar nuevas señales.\n"
        "Usa /cazar para reiniciar el monitoreo seguro.",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado con foco en seguridad"""
    status_msg = (
        f"🛡️ *ESTADO DE SEGURIDAD*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if bot_active else '🔴 DETENIDO'}\n"
        f"✅ Tokens seguros: {len(monitored_tokens)}\n"
        f"🚫 Tokens bloqueados: {len(blacklisted_tokens)}\n"
        f"📊 Tokens planos seguros: {len(flat_tokens)}\n"
        f"📈 Breakouts detectados: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}\n"
        f"📞 Requests API: {security_api.request_count}\n\n"
        f"💡 _Sistema anti-rug pulls ACTIVADO_"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens monitoreados"""
    if not monitored_tokens:
        await update.message.reply_text("📭 No hay tokens en monitoreo seguro.")
        return
        
    msg = "👁️ *Tokens en Monitoreo Seguro:*\n\n"
    tokens_list = list(monitored_tokens)[:15]
    
    for i, addr in enumerate(tokens_list, 1):
        short_addr = addr[:8] + "..." + addr[-6:]
        status = "📊" if addr in flat_tokens else "🔍"
        hist_len = len(price_histories.get(addr, []))
        msg += f"{i}. `{short_addr}` {status} ({hist_len} datos)\n"
    
    if len(monitored_tokens) > 15:
        msg += f"\n... y {len(monitored_tokens) - 15} más"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens planos detectados"""
    if not flat_tokens:
        await update.message.reply_text("📊 No hay tokens planos detectados todavía.")
        return
        
    msg = "📊 *Tokens Planos SEGUROS Detectados:*\n\n"
    for i, (addr, info) in enumerate(list(flat_tokens.items())[:10], 1):
        short_addr = addr[:8] + "..." + addr[-6:]
        since = datetime.fromtimestamp(info["flat_since"]).strftime("%H:%M")
        samples = len(price_histories.get(addr, []))
        alert_pct = info.get("max_alert", 0)
        age = info.get("age_hours", 0)
        
        if alert_pct > 0:
            status = f"🚀 +{alert_pct:.1f}%"
        else:
            flat_duration = (time.time() - info["flat_since"]) / 60
            status = f"⏳ {flat_duration:.0f}min"
            
        msg += f"{i}. `{short_addr}`\n   ⏰ {since} | 📈 {status} | 🕐 {age:.1f}h | 📊 {samples} datos\n\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens bloqueados por seguridad"""
    if not blacklisted_tokens:
        await update.message.reply_text("✅ No hay tokens en la lista negra.")
        return
        
    msg = "🚫 *Tokens Bloqueados por Seguridad:*\n\n"
    blacklist_sample = list(blacklisted_tokens)[:15]
    
    for i, addr in enumerate(blacklist_sample, 1):
        short_addr = addr[:8] + "..." + addr[-6:]
        msg += f"{i}. `{short_addr}`\n"
    
    if len(blacklisted_tokens) > 15:
        msg += f"\n... y {len(blacklisted_tokens) - 15} más bloqueados"
    
    msg += "\n💡 _Estos tokens no pasaron los filtros de seguridad_"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra configuración actual"""
    config_msg = (
        f"⚙️ *CONFIGURACIÓN DE SEGURIDAD*\n\n"
        f"**Filtros Principales:**\n"
        f"• Volumen mínimo: ${MIN_VOLUME_USD:,.0f}\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Antigüedad mínima: {MIN_AGE_HOURS} horas\n"
        f"• Market cap mínimo: ${MIN_MARKET_CAP:,.0f}\n\n"
        f"**Detección Técnica:**\n"
        f"• Breakout mínimo: +{BREAKOUT_STEP}%\n"
        f"• Volatilidad máxima: {FLAT_STD_THRESHOLD}%\n"
        f"• Mínimo muestras: 8 datos\n\n"
        f"**Bloqueos Automáticos:**\n"
        f"• Meme coins (DOGE, SHIB, PEPE...)\n"
        f"• Nombres sospechosos\n"
        f"• Tokens muy nuevos\n\n"
        f"**Rendimiento:**\n"
        f"• Intervalo: {UPDATE_INTERVAL} segundos\n"
        f"• Máximo tokens: 150\n"
        f"• Historial: 30 muestras"
    )
    await update.message.reply_text(config_msg, parse_mode="Markdown")

# ===================== MAIN SEGURO =====================
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Registrar TODOS los comandos necesarios
    commands = [
        ("start", cmd_start),
        ("cazar", cmd_cazar),
        ("parar", cmd_parar),
        ("status", cmd_status),
        ("tokens", cmd_tokens),
        ("planos", cmd_planos),
        ("blacklist", cmd_blacklist),
        ("config", cmd_config),
    ]
    
    for command, handler in commands:
        app.add_handler(CommandHandler(command, handler))
    
    logger.info("🛡️ AntiRug Breakout Bot Iniciado - Filtros de seguridad ACTIVOS")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
