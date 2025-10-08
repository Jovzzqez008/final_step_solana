# bot_antirug_final.py - FILTROS EXTREMOS CONTRA RUG PULLS
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
FLAT_STD_THRESHOLD = 0.1          # Más estricto para "plano"
BREAKOUT_STEP = 20.0              # Breakout más significativo
UPDATE_INTERVAL = 30              # Más lento para mejor análisis

# APIs
JUPITER_TOKENS_API = "https://api.jup.ag/tokens/v1/all"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"
BIRDEYE_API = "https://public-api.birdeye.so/public"  # Para datos adicionales

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
        return self_session
    
    async def get_token_security_data(self, token_address: str):
        """Obtiene datos de seguridad del token"""
        try:
            # Primero, datos básicos de DexScreener
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
                        
                        # Intentar obtener más datos de seguridad
                        security_info = await self.get_additional_security_info(token_address)
                        if security_info:
                            token_data.update(security_info)
                        
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
            # Convertir timestamp a horas de antigüedad
            created_timestamp = pair_created_at / 1000  # DexScreener usa milliseconds
            current_time = time.time()
            age_hours = (current_time - created_timestamp) / 3600
            return age_hours
        except:
            return None
    
    async def get_additional_security_info(self, token_address: str):
        """Obtiene información adicional de seguridad"""
        try:
            session = await self.get_session()
            
            # Intentar con Birdeye para más datos (opcional)
            if self.birdeye_api_key:
                headers = {"X-API-KEY": self.birdeye_api_key}
                url = f"{BIRDEYE_API}/token/{token_address}?chain=solana"
                
                async with session.get(url, headers=headers, timeout=8) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('data'):
                            return {
                                'holders': data['data'].get('holder', 0),
                                'security_score': self.calculate_security_score(data['data'])
                            }
            
            return {}
        except Exception as e:
            logger.debug(f"Error info adicional {token_address}: {e}")
            return {}
    
    def calculate_security_score(self, token_data):
        """Calcula puntuación de seguridad basada en múltiples factores"""
        score = 50  # Puntuación base
        
        # Factor: Liquidez vs FDV
        liquidity = token_data.get('liquidity', 0)
        fdv = token_data.get('fdv', 1)
        liquidity_ratio = liquidity / fdv if fdv > 0 else 0
        
        if liquidity_ratio > 0.1:
            score += 20
        elif liquidity_ratio > 0.05:
            score += 10
        elif liquidity_ratio < 0.01:
            score -= 20
        
        # Factor: Volumen sostenido
        volume = token_data.get('volume24h', 0)
        if volume > 100000:
            score += 15
        elif volume < 10000:
            score -= 10
            
        return min(100, max(0, score))

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
                for token in tokens:
                    symbol = token.get('symbol', '').upper()
                    name = token.get('name', '').upper()
                    
                    # 🚨 FILTRO DE SÍMBOLOS/NOMBRES SOSPECHOSOS
                    suspicious_indicators = [
                        'TEST', 'FAKE', 'SCAM', 'RUG', 'PULL', 
                        'DUMP', 'SHIT', 'MEME', 'MOON', 'SHIB', 
                        'DOGE', 'ELON', 'TSUKI', 'AKITA', 'HUSKY'
                    ]
                    
                    is_suspicious = any(indicator in symbol or indicator in name 
                                      for indicator in suspicious_indicators)
                    
                    if not is_suspicious:
                        safe_tokens.append(token)
                
                logger.info(f"🛡️ Filtrados {len(tokens) - len(safe_tokens)} tokens sospechosos")
                return safe_tokens
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
        
        # Tomar tokens limitados para análisis profundo
        safe_tokens = []
        for token in tokens[:100]:  # Solo 100 para análisis detallado
            addr = token.get('address')
            if addr and addr not in monitored_tokens and addr not in blacklisted_tokens:
                # Verificar seguridad antes de añadir
                token_data = await security_api.get_token_security_data(addr)
                if token_data and token_data.get('valid'):
                    safe_tokens.append(addr)
                    monitored_tokens.add(addr)
                else:
                    blacklisted_tokens.add(addr)
        
        logger.info(f"🛡️ Inicializados {len(safe_tokens)} tokens SEGUROS")
        logger.info(f"🚫 Blacklisted {len(blacklisted_tokens)} tokens")
        return safe_tokens
        
    except Exception as e:
        logger.error(f"Error inicializando tokens seguros: {e}")
        return []

# ===================== ANÁLISIS TÉCNICO SEGURO =====================
def is_flat_safe(hist):
    """Detección MUY estricta de tokens planos"""
    if len(hist) < 8:  # Más muestras requeridas
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
        
    # 🛡️ CRITERIOS MÁS ESTRICTOS
    sd = pstdev(returns) if len(returns) > 1 else 100
    max_move = max(abs(x) for x in returns) if returns else 100
    avg_move = mean([abs(x) for x in returns]) if returns else 100
    
    return (sd < FLAT_STD_THRESHOLD and 
            max_move < 0.5 and  # Movimiento máximo muy pequeño
            avg_move < 0.2)     # Movimiento promedio muy pequeño

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
                    tokens_to_monitor.remove(token_addr)
                    threats_detected += 1
                    continue
                
                # Procesar token seguro
                result = await process_safe_token(token_addr, token_data, context)
                if result:
                    processed += 1
                
                await asyncio.sleep(0.2)  # Más lento para no saturar
            
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
        
        result = {'processed': True}
        
        # Detectar tokens planos (más estricto)
        if token_addr not in flat_tokens and is_flat_safe(hist):
            flat_tokens[token_addr] = {
                "first_price": current_price,
                "flat_since": time.time(),
                "max_alert": 0,
                "volume": token_data.get('volume24h', 0),
                "liquidity": token_data.get('liquidity', 0),
                "age_hours": token_data.get('age_hours', 0),
                "security_score": token_data.get('security_score', 50)
            }
            logger.info(f"🛡️ TOKEN PLANO SEGURO: {token_addr[:8]}...")
            result['flat_detected'] = True
        
        # Detectar breakout (más estricto)
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
                        result['breakout'] = True
        
        return result
        
    except Exception as e:
        logger.debug(f"Error procesando token seguro {token_addr}: {e}")
        return None

# ===================== ALERTAS SEGURAS =====================
async def send_safe_breakout_alert(context, token_addr, breakout_pct, token_data):
    """Envía alertas de breakout con información de seguridad"""
    try:
        short_addr = token_addr[:8] + "..." + token_addr[-6:]
        age_hours = token_data.get('age_hours', 0)
        security_score = token_data.get('security_score', 50)
        
        # Evaluación de riesgo basada en múltiples factores
        risk_level = "🟢 BAJO"
        if security_score < 40:
            risk_level = "🔴 ALTO"
        elif security_score < 60:
            risk_level = "🟡 MEDIO"
        
        emoji = "🚀" if breakout_pct > 25 else "📈" if breakout_pct > 15 else "🔼"
        
        msg = (
            f"{emoji} *BREAKOUT SEGURO DETECTADO* 🛡️\n\n"
            f"*Token:* `{short_addr}`\n"
            f"*Cambio:* +{breakout_pct:.2f}%\n"
            f"*Precio:* ${token_data['price']:.6f}\n"
            f"*Volumen 24h:* ${token_data['volume24h']:,.0f}\n"
            f"*Liquidez:* ${token_data['liquidity']:,.0f}\n"
            f"*Edad aprox.:* {age_hours:.1f} horas\n"
            f"*Puntuación seguridad:* {security_score}/100\n"
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

# ===================== COMANDOS SEGURIDAD =====================
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
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado con foco en seguridad"""
    avg_security = 0
    if flat_tokens:
        avg_security = sum(t.get('security_score', 50) for t in flat_tokens.values()) / len(flat_tokens)
    
    status_msg = (
        f"🛡️ *ESTADO DE SEGURIDAD*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if bot_active else '🔴 DETENIDO'}\n"
        f"✅ Tokens seguros: {len(monitored_tokens)}\n"
        f"🚫 Tokens bloqueados: {len(blacklisted_tokens)}\n"
        f"📊 Tokens planos seguros: {len(flat_tokens)}\n"
        f"📈 Breakouts detectados: {sum(1 for t in flat_tokens.values() if t['max_alert'] > 0)}\n"
        f"🎯 Puntuación seguridad avg: {avg_security:.1f}/100\n\n"
        f"💡 _Sistema anti-rug pulls ACTIVADO_"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

# ===================== MAIN SEGURO =====================
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
        ("blacklist", cmd_blacklist),
        ("config", cmd_config),
    ]
    
    for command, handler in commands:
        app.add_handler(CommandHandler(command, handler))
    
    logger.info("🛡️ AntiRug Breakout Bot Iniciado - Filtros de seguridad ACTIVOS")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
