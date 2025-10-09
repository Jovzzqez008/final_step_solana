# bot_jupiter_v2.py - USANDO JUPITER LITE v2
import asyncio, json, os, time, logging, aiohttp
from statistics import pstdev, mean
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict, deque

# ===================== CONFIGURACIÓN OPTIMIZADA =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 🛡️ FILTROS ANTIRUG PULL
MIN_VOLUME_USD = 75000.0          # Volumen mínimo ALTO
MIN_LIQUIDITY = 50000.0           # Liquidez mínima ALTA  
MIN_AGE_HOURS = 24                # Mínimo 24 horas de antigüedad
FLAT_STD_THRESHOLD = 0.1          # Más estricto para "plano"
BREAKOUT_STEP = 30.0              # Breakout al 30%
UPDATE_INTERVAL = 25              # Más rápido con Jupiter v2

# APIs OPTIMIZADAS
JUPITER_TOKENS_V2 = "https://api.jup.ag/tokens/v2"  # ✅ Jupiter Lite v2
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("jupiter_v2_bot")

# ===================== ESTADO =====================
price_histories = defaultdict(lambda: deque(maxlen=25))
flat_tokens = {}
watchlist = []
token_metadata = {}
bot_active = False
monitored_tokens = set()
blacklisted_tokens = set()

# ===================== JUPITER V2 CLIENT =====================
class JupiterV2API:
    def __init__(self):
        self.session = None
        self.request_count = 0
        self.cache_tokens = []
        self.cache_time = 0
        self.cache_duration = 1800  # 30 minutos cache
        
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def get_all_tokens_v2(self):
        """Obtiene tokens desde Jupiter Lite v2 - MÁS RÁPIDO"""
        try:
            # Cache para reducir requests
            if time.time() - self.cache_time < self.cache_duration and self.cache_tokens:
                return self.cache_tokens
                
            session = await self.get_session()
            async with session.get(JUPITER_TOKENS_V2, timeout=10) as response:
                if response.status == 200:
                    tokens = await response.json()
                    self.cache_tokens = tokens
                    self.cache_time = time.time()
                    logger.info(f"✅ Jupiter v2: {len(tokens)} tokens cargados")
                    return tokens
            return []
        except Exception as e:
            logger.error(f"Error Jupiter v2: {e}")
            return []
    
    async def get_token_price(self, token_address: str):
        """Obtiene precio desde DexScreener"""
        try:
            self.request_count += 1
            session = await self.get_session()
            url = f"{DEXSCREENER_API}/tokens/{token_address}"
            
            async with session.get(url, timeout=6) as response:  # Timeout más corto
                if response.status == 200:
                    data = await response.json()
                    if data.get('pairs') and len(data['pairs']) > 0:
                        pair = data['pairs'][0]
                        
                        price = float(pair.get('priceUsd', 0))
                        volume24h = float(pair.get('volume', {}).get('h24', 0))
                        liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                        fdv = float(pair.get('fdv', 0))
                        pair_created_at = pair.get('pairCreatedAt')
                        age_hours = self.calculate_token_age(pair_created_at)
                        
                        # 🛡️ FILTROS DE SEGURIDAD
                        if (price <= 0 or 
                            volume24h < MIN_VOLUME_USD or 
                            liquidity < MIN_LIQUIDITY or
                            (age_hours is not None and age_hours < MIN_AGE_HOURS)):
                            return None
                        
                        return {
                            'price': price,
                            'volume24h': volume24h,
                            'liquidity': liquidity,
                            'fdv': fdv,
                            'age_hours': age_hours,
                            'dex': pair.get('dexId'),
                            'pair_address': pair.get('pairAddress'),
                            'valid': True
                        }
            return None
        except Exception as e:
            logger.debug(f"Error precio {token_address}: {e}")
            return None
    
    def calculate_token_age(self, pair_created_at):
        """Calcula edad del token en horas"""
        if not pair_created_at:
            return None
        try:
            created_timestamp = pair_created_at / 1000
            current_time = time.time()
            age_hours = (current_time - created_timestamp) / 3600
            return age_hours
        except:
            return None

jupiter_api = JupiterV2API()

# ===================== DETECCIÓN OPTIMIZADA =====================
async def get_filtered_tokens_v2():
    """Obtiene y filtra tokens usando Jupiter v2"""
    try:
        tokens = await jupiter_api.get_all_tokens_v2()
        if not tokens:
            return []
        
        # Filtrar tokens sospechosos
        safe_tokens = []
        suspicious_keywords = [
            'TEST', 'FAKE', 'SCAM', 'RUG', 'PULL', 'DUMP', 
            'SHIT', 'MEME', 'MOON', 'SHIB', 'DOGE', 'ELON', 
            'TSUKI', 'AKITA', 'HUSKY', 'FLOKI', 'PEPE', 'WOJAK'
        ]
        
        for token in tokens:
            symbol = token.get('symbol', '').upper()
            name = token.get('name', '').upper()
            
            # 🚨 FILTRO DE SEGURIDAD
            is_suspicious = any(keyword in symbol or keyword in name 
                              for keyword in suspicious_keywords)
            
            if not is_suspicious:
                safe_tokens.append(token)
        
        logger.info(f"🛡️ Jupiter v2: {len(safe_tokens)} tokens seguros de {len(tokens)}")
        return safe_tokens
    except Exception as e:
        logger.error(f"Error filtrando tokens v2: {e}")
        return []

async def initialize_tokens_v2():
    """Inicializa tokens usando Jupiter v2"""
    try:
        tokens = await get_filtered_tokens_v2()
        if not tokens:
            return []
        
        # Procesar tokens en lote más pequeño pero más rápido
        safe_tokens = []
        batch_size = 80  # Menos tokens pero procesados más rápido
        
        for token in tokens[:batch_size]:
            addr = token.get('address')
            if addr and addr not in monitored_tokens and addr not in blacklisted_tokens:
                token_data = await jupiter_api.get_token_price(addr)
                if token_data and token_data.get('valid'):
                    safe_tokens.append(addr)
                    monitored_tokens.add(addr)
                else:
                    blacklisted_tokens.add(addr)
        
        logger.info(f"🚀 Jupiter v2: {len(safe_tokens)} tokens listos para monitoreo")
        return safe_tokens
        
    except Exception as e:
        logger.error(f"Error inicializando tokens v2: {e}")
        return []

# ===================== ANÁLISIS TÉCNICO =====================
def is_flat_optimized(hist):
    """Detección optimizada de tokens planos"""
    if len(hist) < 7:  # Menos muestras necesarias
        return False
        
    prices = [point["price"] for point in hist if point["price"] > 0]
    if len(prices) < 5:
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
    
    return sd < FLAT_STD_THRESHOLD and max_move < 0.6

# ===================== MONITOREO CON JUPITER V2 =====================
async def monitor_tokens_v2(context: ContextTypes.DEFAULT_TYPE):
    """Monitoreo optimizado con Jupiter v2"""
    logger.info("🚀 Iniciando monitoreo con Jupiter v2...")
    
    tokens_to_monitor = await initialize_tokens_v2()
    
    if not tokens_to_monitor:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="❌ No se encontraron tokens seguros con Jupiter v2"
        )
        return
    
    iteration = 0
    while bot_active:
        try:
            iteration += 1
            start_time = time.time()
            
            processed = 0
            breakouts_detected = 0
            
            # Procesamiento más rápido
            for token_addr in tokens_to_monitor.copy():
                if not bot_active:
                    break
                    
                # Verificación rápida de seguridad
                token_data = await jupiter_api.get_token_price(token_addr)
                if not token_data or not token_data.get('valid'):
                    blacklisted_tokens.add(token_addr)
                    if token_addr in tokens_to_monitor:
                        tokens_to_monitor.remove(token_addr)
                    continue
                
                # Procesar token
                result = await process_token_v2(token_addr, token_data, context)
                if result:
                    processed += 1
                    if result.get('breakout'):
                        breakouts_detected += 1
            
            # Reporte de rendimiento
            elapsed = time.time() - start_time
            logger.info(f"⚡ Iteración #{iteration}: {processed} tokens en {elapsed:.1f}s")
            
            if iteration % 4 == 0:  # Reportes menos frecuentes
                status_msg = (
                    f"🚀 **Jupiter v2 - Iteración #{iteration}**\n\n"
                    f"✅ Tokens activos: {len(tokens_to_monitor)}\n"
                    f"🔍 Procesados: {processed}\n"
                    f"📈 Breakouts 30%+: {breakouts_detected}\n"
                    f"📊 Tokens planos: {len(flat_tokens)}\n"
                    f"⏱️ Tiempo: {elapsed:.1f}s\n\n"
                    f"💡 _Usando Jupiter Lite v2 - Más rápido y eficiente_"
                )
                await context.bot.send_message(
                    chat_id=TARGET_CHAT_ID,
                    text=status_msg,
                    parse_mode="Markdown"
                )
            
            await asyncio.sleep(UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error en monitoreo v2: {e}")
            await asyncio.sleep(20)

async def process_token_v2(token_addr: str, token_data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Procesamiento optimizado de token"""
    try:
        current_price = token_data['price']
        
        # Actualizar historial
        hist = price_histories[token_addr]
        hist.append({
            "ts": time.time(), 
            "price": current_price,
            "volume": token_data.get('volume24h', 0)
        })
        
        # Watchlist optimizada
        if token_addr not in watchlist:
            watchlist.append(token_addr)
            if len(watchlist) > 60:
                watchlist.pop(0)
        
        # Detectar plano
        if token_addr not in flat_tokens and is_flat_optimized(hist):
            flat_tokens[token_addr] = {
                "first_price": current_price,
                "flat_since": time.time(),
                "max_alert": 0,
                "volume": token_data.get('volume24h', 0),
                "liquidity": token_data.get('liquidity', 0),
                "age_hours": token_data.get('age_hours', 0)
            }
        
        # Detectar breakout 30%
        if token_addr in flat_tokens:
            base_price = flat_tokens[token_addr]["first_price"]
            if base_price > 0:
                current_pct = (current_price - base_price) / base_price * 100
                last_alert = flat_tokens[token_addr]["max_alert"]
                
                if current_pct >= last_alert + BREAKOUT_STEP:
                    # Verificación rápida de seguridad
                    current_data = await jupiter_api.get_token_price(token_addr)
                    if current_data and current_data.get('valid'):
                        flat_tokens[token_addr]["max_alert"] = current_pct
                        await send_breakout_alert_v2(context, token_addr, current_pct, token_data)
                        return {'breakout': True}
        
        return {'processed': True}
        
    except Exception as e:
        logger.debug(f"Error procesando {token_addr}: {e}")
        return None

# ===================== ALERTAS OPTIMIZADAS =====================
async def send_breakout_alert_v2(context, token_addr, breakout_pct, token_data):
    """Alertas optimizadas"""
    try:
        short_addr = token_addr[:8] + "..." + token_addr[-6:]
        age_hours = token_data.get('age_hours', 0)
        
        # Emojis según magnitud
        if breakout_pct > 50:
            emoji = "🚀🚀"
        elif breakout_pct > 30:
            emoji = "🚀"
        else:
            emoji = "📈"
        
        msg = (
            f"{emoji} *BREAKOUT {breakout_pct:.1f}% DETECTADO* 🎯\n\n"
            f"*Token:* `{short_addr}`\n"
            f"*Cambio:* +{breakout_pct:.2f}%\n"
            f"*Precio:* ${token_data['price']:.6f}\n"
            f"*Volumen 24h:* ${token_data['volume24h']:,.0f}\n"
            f"*Liquidez:* ${token_data['liquidity']:,.0f}\n"
            f"*Edad:* {age_hours:.1f}h\n\n"
            f"🔍 *Verificación:*\n"
            f"- [DexScreener](https://dexscreener.com/solana/{token_addr})\n"
            f"- [Jupiter Swap](https://jup.ag/swap/SOL-{token_addr})"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        
        logger.info(f"🎯 Breakout {breakout_pct:.1f}%: {short_addr}")
        
    except Exception as e:
        logger.error(f"Error alerta: {e}")

# ===================== COMANDOS TELEGRAM =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.effective_chat.id
    
    welcome_msg = (
        "🤖 *Jupiter v2 Breakout Bot* 🚀\n\n"
        "✅ *Optimizado con Jupiter Lite v2:*\n"
        "• Más rápido y eficiente\n"
        "• Menos consumo de API\n"
        "• Mejor rendimiento\n\n"
        f"🎯 *Configuración:*\n"
        f"• Breakout mínimo: +{BREAKOUT_STEP}%\n"
        f"• Volumen mínimo: ${MIN_VOLUME_USD:,.0f}\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Antigüedad: {MIN_AGE_HOURS}+ horas\n\n"
        "📊 *Comandos:*\n"
        "• /cazar - Iniciar monitoreo\n"
        "• /parar - Detener\n"
        "• /status - Estado\n"
        "• /tokens - Tokens activos\n"
        "• /planos - Tokens planos\n"
        "• /rendimiento - Stats"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia monitoreo con Jupiter v2"""
    global bot_active
    if bot_active:
        await update.message.reply_text("⚙️ Monitoreo ya activo con Jupiter v2")
        return
    
    bot_active = True
    await update.message.reply_text(
        "🚀 *ACTIVANDO JUPITER V2*\n\n"
        "✅ Cargando tokens desde Jupiter Lite v2...\n"
        "⚡ Inicializando monitoreo optimizado...\n"
        f"🎯 Buscando breakouts > {BREAKOUT_STEP}%\n"
        "📊 Filtros de seguridad activos\n\n"
        "_Sistema más rápido y eficiente..._",
        parse_mode="Markdown"
    )
    
    asyncio.create_task(monitor_tokens_v2(context))

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el monitoreo"""
    global bot_active
    bot_active = False
    await update.message.reply_text("🛑 Monitoreo detenido")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado del sistema"""
    status_msg = (
        f"🚀 *JUPITER V2 - ESTADO*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if bot_active else '🔴 DETENIDO'}\n"
        f"✅ Tokens activos: {len(monitored_tokens)}\n"
        f"📊 Tokens planos: {len(flat_tokens)}\n"
        f"📈 Breakouts {BREAKOUT_STEP}%+: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}\n"
        f"📞 Requests: {jupiter_api.request_count}\n\n"
        f"💡 _Jupiter Lite v2 - Optimizado_"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def cmd_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens activos"""
    if not monitored_tokens:
        await update.message.reply_text("📭 No hay tokens activos")
        return
        
    msg = "👁️ *Tokens Activos:*\n\n"
    for i, addr in enumerate(list(monitored_tokens)[:12], 1):
        short_addr = addr[:8] + "..." + addr[-6:]
        status = "📊" if addr in flat_tokens else "🔍"
        msg += f"{i}. `{short_addr}` {status}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_planos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra tokens planos"""
    if not flat_tokens:
        await update.message.reply_text("📊 No hay tokens planos")
        return
        
    msg = "📊 *Tokens Planos:*\n\n"
    for i, (addr, info) in enumerate(list(flat_tokens.items())[:8], 1):
        short_addr = addr[:8] + "..." + addr[-6:]
        since = datetime.fromtimestamp(info["flat_since"]).strftime("%H:%M")
        alert_pct = info.get("max_alert", 0)
        status = f"🚀 +{alert_pct:.1f}%" if alert_pct > 0 else "⏳ Plano"
        msg += f"{i}. `{short_addr}` | {since} | {status}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_rendimiento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estadísticas de rendimiento"""
    avg_hist_len = sum(len(h) for h in price_histories.values()) / max(1, len(price_histories))
    
    perf_msg = (
        f"⚡ *RENDIMIENTO JUPITER V2*\n\n"
        f"📊 Tokens con historial: {len(price_histories)}\n"
        f"📈 Promedio muestras: {avg_hist_len:.1f}\n"
        f"🔄 Requests API: {jupiter_api.request_count}\n"
        f"🚫 Tokens bloqueados: {len(blacklisted_tokens)}\n"
        f"⏱️ Intervalo: {UPDATE_INTERVAL}s\n\n"
        f"💡 _Sistema optimizado con Jupiter Lite v2_"
    )
    await update.message.reply_text(perf_msg, parse_mode="Markdown")

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
        ("rendimiento", cmd_rendimiento),
    ]
    
    for command, handler in commands:
        app.add_handler(CommandHandler(command, handler))
    
    logger.info("🚀 Jupiter v2 Breakout Bot Iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
