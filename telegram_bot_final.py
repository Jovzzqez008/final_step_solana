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
MIN_VOLUME_USD = 50000.0          # Volumen mínimo ALTO
MIN_LIQUIDITY = 30000.0           # Liquidez mínima ALTA  
MIN_AGE_HOURS = 8                # Mínimo 24 horas de antigüedad
FLAT_STD_THRESHOLD = 0.2          # Más estricto para "plano"
BREAKOUT_STEP = 15.0              # Breakout al 30%
UPDATE_INTERVAL = 20              # Más rápido con Jupiter v2

# APIs
JUPITER_TOKENS_V1 = "https://api.jup.ag/tokens/v1/all"  # ✅ v1 probada
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("diagnostico_bot")

# ===================== ESTADO =====================
price_histories = defaultdict(lambda: deque(maxlen=20))
flat_tokens = {}
watchlist = []
token_metadata = {}
bot_active = False
monitored_tokens = set()
blacklisted_tokens = set()

# ===================== API CLIENT CON DIAGNÓSTICO =====================
class DiagnosticAPI:
    def __init__(self):
        self.session = None
        self.request_count = 0
        self.last_error = None
        
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def test_jupiter_connection(self):
        """Prueba la conexión a Jupiter API"""
        try:
            session = await self.get_session()
            logger.info("🔍 TESTEANDO CONEXIÓN JUPITER API...")
            
            async with session.get(JUPITER_TOKENS_V1, timeout=15) as response:
                self.request_count += 1
                
                if response.status == 200:
                    tokens = await response.json()
                    logger.info(f"✅ JUPITER API FUNCIONA: {len(tokens)} tokens recibidos")
                    
                    # Mostrar primeros 3 tokens como ejemplo
                    for i, token in enumerate(tokens[:3]):
                        symbol = token.get('symbol', 'N/A')
                        name = token.get('name', 'N/A')
                        logger.info(f"   📝 Token {i+1}: {symbol} - {name}")
                    
                    return tokens
                else:
                    error_msg = f"❌ Jupiter API error: HTTP {response.status}"
                    logger.error(error_msg)
                    self.last_error = error_msg
                    return []
                    
        except Exception as e:
            error_msg = f"❌ Error conexión Jupiter: {e}"
            logger.error(error_msg)
            self.last_error = error_msg
            return []
    
    async def test_dexscreener_connection(self, token_address: str = "So11111111111111111111111111111111111111112"):
        """Prueba la conexión a DexScreener con SOL como ejemplo"""
        try:
            session = await self.get_session()
            logger.info(f"🔍 TESTEANDO DEXSCREENER API con token SOL...")
            
            url = f"{DEXSCREENER_API}/tokens/{token_address}"
            async with session.get(url, timeout=10) as response:
                self.request_count += 1
                
                if response.status == 200:
                    data = await response.json()
                    if data.get('pairs'):
                        pair = data['pairs'][0]
                        price = pair.get('priceUsd', 'N/A')
                        volume = pair.get('volume', {}).get('h24', 'N/A')
                        logger.info(f"✅ DEXSCREENER FUNCIONA: SOL = ${price}, Vol = ${volume}")
                        return True
                    else:
                        logger.error("❌ DexScreener no devolvió pairs")
                        return False
                else:
                    logger.error(f"❌ DexScreener error: HTTP {response.status}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Error DexScreener: {e}")
            return False
    
    async def get_token_data(self, token_address: str):
        """Obtiene datos de token con diagnóstico detallado"""
        try:
            session = await self.get_session()
            url = f"{DEXSCREENER_API}/tokens/{token_address}"
            
            async with session.get(url, timeout=8) as response:
                self.request_count += 1
                
                if response.status == 200:
                    data = await response.json()
                    
                    if not data.get('pairs') or len(data['pairs']) == 0:
                        logger.debug(f"🔍 Token {token_address[:8]}...: Sin pairs en DexScreener")
                        return None
                    
                    pair = data['pairs'][0]
                    price = float(pair.get('priceUsd', 0))
                    volume24h = float(pair.get('volume', {}).get('h24', 0))
                    liquidity = float(pair.get('liquidity', {}).get('usd', 0))
                    
                    # Log detallado del token
                    logger.info(f"🔍 Token {token_address[:8]}...: ${price:.6f}, Vol: ${volume24h:,.0f}, Liq: ${liquidity:,.0f}")
                    
                    # Aplicar filtros (relajados para pruebas)
                    if price <= 0:
                        logger.debug(f"  ❌ Precio inválido: ${price}")
                        return None
                    
                    if volume24h < MIN_VOLUME_USD:
                        logger.debug(f"  ❌ Volumen bajo: ${volume24h:,.0f} < ${MIN_VOLUME_USD:,.0f}")
                        return None
                    
                    if liquidity < MIN_LIQUIDITY:
                        logger.debug(f"  ❌ Liquidez baja: ${liquidity:,.0f} < ${MIN_LIQUIDITY:,.0f}")
                        return None
                    
                    # ✅ TOKEN VÁLIDO
                    logger.info(f"  ✅ TOKEN VÁLIDO: {token_address[:8]}...")
                    return {
                        'price': price,
                        'volume24h': volume24h,
                        'liquidity': liquidity,
                        'valid': True
                    }
                
                else:
                    logger.debug(f"❌ HTTP {response.status} para token {token_address[:8]}...")
                    return None
                    
        except Exception as e:
            logger.debug(f"Error obteniendo token {token_address[:8]}: {e}")
            return None

diagnostic_api = DiagnosticAPI()

# ===================== SISTEMA DE DIAGNÓSTICO =====================
async def run_full_diagnostic(context: ContextTypes.DEFAULT_TYPE):
    """Ejecuta diagnóstico completo del sistema"""
    logger.info("🩺 INICIANDO DIAGNÓSTICO COMPLETO...")
    
    await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text="🩺 *INICIANDO DIAGNÓSTICO COMPLETO*\n\nProbando todas las conexiones...",
        parse_mode="Markdown"
    )
    
    # 1. Probar Jupiter API
    tokens = await diagnostic_api.test_jupiter_connection()
    
    if not tokens:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="❌ *FALLA CRÍTICA: Jupiter API no responde*\n\n" +
                 f"Error: {diagnostic_api.last_error}",
            parse_mode="Markdown"
        )
        return False
    
    # 2. Probar DexScreener API
    dexscreener_ok = await diagnostic_api.test_dexscreener_connection()
    
    if not dexscreener_ok:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="❌ *FALLA CRÍTICA: DexScreener API no responde*",
            parse_mode="Markdown"
        )
        return False
    
    # 3. Probar con tokens reales
    await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text="✅ *APIS FUNCIONANDO*\n\nProbando con tokens reales...",
        parse_mode="Markdown"
    )
    
    # Probar con algunos tokens de ejemplo
    test_tokens = []
    for token in tokens[:10]:  # Primeros 10 tokens
        addr = token.get('address')
        if addr:
            test_tokens.append(addr)
    
    valid_tokens_found = 0
    for i, token_addr in enumerate(test_tokens):
        token_data = await diagnostic_api.get_token_data(token_addr)
        if token_data and token_data.get('valid'):
            valid_tokens_found += 1
            logger.info(f"✅ Token #{i+1} válido: {token_addr[:8]}...")
    
    # Reporte final
    diagnostic_report = (
        f"📊 *DIAGNÓSTICO COMPLETADO*\n\n"
        f"✅ APIs funcionando: Jupiter + DexScreener\n"
        f"📦 Tokens probados: {len(test_tokens)}\n"
        f"🎯 Tokens válidos: {valid_tokens_found}\n"
        f"📞 Requests totales: {diagnostic_api.request_count}\n\n"
    )
    
    if valid_tokens_found > 0:
        diagnostic_report += f"🟢 *SISTEMA FUNCIONAL* - {valid_tokens_found} tokens listos"
    else:
        diagnostic_report += (
            f"🔴 *PROBLEMA* - 0 tokens válidos\n\n"
            f"💡 Posibles causas:\n"
            f"• Filtros muy estrictos\n"
            f"• Tokens sin datos en DexScreener\n"
            f"• Problemas de red"
        )
    
    await context.bot.send_message(
        chat_id=TARGET_CHAT_ID,
        text=diagnostic_report,
        parse_mode="Markdown"
    )
    
    return valid_tokens_found > 0

# ===================== MONITOREO CON DIAGNÓSTICO =====================
async def monitor_with_diagnostic(context: ContextTypes.DEFAULT_TYPE):
    """Monitoreo con diagnóstico integrado"""
    logger.info("🎯 INICIANDO MONITOREO CON DIAGNÓSTICO...")
    
    # Primero ejecutar diagnóstico
    system_ok = await run_full_diagnostic(context)
    
    if not system_ok:
        logger.error("❌ Sistema no funciona - Deteniendo monitoreo")
        return
    
    # Obtener tokens de Jupiter
    tokens = await diagnostic_api.test_jupiter_connection()
    if not tokens:
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text="❌ No se pudieron obtener tokens de Jupiter",
            parse_mode="Markdown"
        )
        return
    
    # Filtrar tokens (lista reducida para pruebas)
    tokens_to_monitor = []
    for token in tokens[:30]:  # Solo 30 tokens para empezar
        addr = token.get('address')
        if addr and addr not in monitored_tokens:
            tokens_to_monitor.append(addr)
            monitored_tokens.add(addr)
    
    logger.info(f"🎯 Monitoreando {len(tokens_to_monitor)} tokens")
    
    iteration = 0
    while bot_active:
        try:
            iteration += 1
            processed = 0
            valid_tokens = 0
            
            for token_addr in tokens_to_monitor.copy():
                if not bot_active:
                    break
                
                # Obtener datos del token
                token_data = await diagnostic_api.get_token_data(token_addr)
                
                if token_data and token_data.get('valid'):
                    valid_tokens += 1
                    await process_token_diagnostic(token_addr, token_data, context)
                    processed += 1
            
            # Reporte de progreso
            if iteration % 3 == 0:
                progress_msg = (
                    f"🔍 *MONITOREO - Iteración #{iteration}*\n\n"
                    f"✅ Tokens válidos: {valid_tokens}/{len(tokens_to_monitor)}\n"
                    f"📊 Tokens con historial: {len(price_histories)}\n"
                    f"📈 Tokens planos: {len(flat_tokens)}\n"
                    f"📞 Requests API: {diagnostic_api.request_count}\n\n"
                    f"💡 _Sistema diagnosticado y funcionando_"
                )
                await context.bot.send_message(
                    chat_id=TARGET_CHAT_ID,
                    text=progress_msg,
                    parse_mode="Markdown"
                )
            
            await asyncio.sleep(UPDATE_INTERVAL)
            
        except Exception as e:
            logger.error(f"Error en monitoreo: {e}")
            await asyncio.sleep(20)

async def process_token_diagnostic(token_addr: str, token_data: dict, context: ContextTypes.DEFAULT_TYPE):
    """Procesa token con logging detallado"""
    try:
        current_price = token_data['price']
        
        # Actualizar historial
        hist = price_histories[token_addr]
        hist.append({
            "ts": time.time(), 
            "price": current_price,
            "volume": token_data.get('volume24h', 0)
        })
        
        logger.info(f"📊 Token {token_addr[:8]}...: {len(hist)} datos en historial")
        
        # Detectar patrón plano (simplificado para pruebas)
        if len(hist) >= 5:
            prices = [p["price"] for p in hist]
            returns = [(prices[i] - prices[i-1]) / prices[i-1] * 100 for i in range(1, len(prices))]
            
            if returns:
                volatility = pstdev(returns) if len(returns) > 1 else 0
                
                if volatility < 0.5:  # Muy estable
                    if token_addr not in flat_tokens:
                        flat_tokens[token_addr] = {
                            "first_price": current_price,
                            "flat_since": time.time(),
                            "max_alert": 0
                        }
                        logger.info(f"📈 TOKEN PLANO DETECTADO: {token_addr[:8]}...")
        
        # Verificar breakout
        if token_addr in flat_tokens:
            base_price = flat_tokens[token_addr]["first_price"]
            current_pct = (current_price - base_price) / base_price * 100
            
            if current_pct >= BREAKOUT_STEP:
                logger.info(f"🚀 BREAKOUT {current_pct:.1f}%: {token_addr[:8]}...")
                await send_diagnostic_alert(context, token_addr, current_pct, token_data)
                flat_tokens[token_addr]["max_alert"] = current_pct
        
    except Exception as e:
        logger.debug(f"Error procesando {token_addr[:8]}: {e}")

async def send_diagnostic_alert(context, token_addr, breakout_pct, token_data):
    """Envía alerta de diagnóstico"""
    try:
        short_addr = token_addr[:8] + "..." + token_addr[-6:]
        
        alert_msg = (
            f"🎯 *BREAKOUT {breakout_pct:.1f}% DETECTADO*\n\n"
            f"*Token:* `{short_addr}`\n"
            f"*Cambio:* +{breakout_pct:.2f}%\n"
            f"*Precio:* ${token_data['price']:.6f}\n"
            f"*Volumen:* ${token_data['volume24h']:,.0f}\n\n"
            f"🔍 *Verificación:*\n"
            f"- [DexScreener](https://dexscreener.com/solana/{token_addr})\n"
            f"- [Jupiter](https://jup.ag/swap/SOL-{token_addr})"
        )
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=alert_msg,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        
    except Exception as e:
        logger.error(f"Error enviando alerta: {e}")

# ===================== COMANDOS TELEGRAM =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.effective_chat.id
    
    welcome_msg = (
        "🤖 *Bot de Diagnóstico Completo* 🩺\n\n"
        "Este bot incluye:\n"
        "• Diagnóstico automático de APIs\n"
        "• Pruebas de conexión en tiempo real\n"
        "• Logs detallados de cada paso\n"
        "• Filtros relajados para pruebas\n\n"
        "📊 *Comandos:*\n"
        "• /diagnostico - Ejecutar diagnóstico\n"
        "• /cazar - Iniciar monitoreo con diagnóstico\n"
        "• /parar - Detener\n"
        "• /status - Estado detallado\n"
        "• /logs - Ver últimos logs"
    )
    
    await update.message.reply_text(welcome_msg, parse_mode="Markdown")

async def cmd_diagnostico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ejecuta diagnóstico completo"""
    await update.message.reply_text(
        "🩺 *EJECUTANDO DIAGNÓSTICO COMPLETO*\n\n"
        "Probando:\n"
        "• Conexión a Jupiter API\n"
        "• Conexión a DexScreener\n"
        "• Tokens de ejemplo\n"
        "• Filtros del sistema\n\n"
        "⏳ Esto tomará unos segundos...",
        parse_mode="Markdown"
    )
    
    await run_full_diagnostic(context)

async def cmd_cazar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia monitoreo con diagnóstico"""
    global bot_active
    if bot_active:
        await update.message.reply_text("⚙️ Monitoreo ya activo")
        return
    
    bot_active = True
    await update.message.reply_text(
        "🎯 *INICIANDO MONITOREO CON DIAGNÓSTICO*\n\n"
        "El bot primero ejecutará diagnóstico automático\n"
        "y luego comenzará el monitoreo con logs detallados.",
        parse_mode="Markdown"
    )
    
    asyncio.create_task(monitor_with_diagnostic(context))

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el monitoreo"""
    global bot_active
    bot_active = False
    await update.message.reply_text("🛑 Monitoreo detenido")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Estado detallado del sistema"""
    status_msg = (
        f"🩺 *ESTADO DEL SISTEMA - DIAGNÓSTICO*\n\n"
        f"🔧 Monitoreo: {'🟢 ACTIVO' if bot_active else '🔴 DETENIDO'}\n"
        f"📊 Tokens monitoreados: {len(monitored_tokens)}\n"
        f"📈 Tokens con historial: {len(price_histories)}\n"
        f"📉 Tokens planos: {len(flat_tokens)}\n"
        f"📞 Requests API: {diagnostic_api.request_count}\n"
        f"🚫 Último error: {diagnostic_api.last_error or 'Ninguno'}\n\n"
        f"⚙️ *Configuración actual:*\n"
        f"• Volumen mínimo: ${MIN_VOLUME_USD:,.0f}\n"
        f"• Liquidez mínima: ${MIN_LIQUIDITY:,.0f}\n"
        f"• Breakout: {BREAKOUT_STEP}%\n"
        f"• Intervalo: {UPDATE_INTERVAL}s"
    )
    await update.message.reply_text(status_msg, parse_mode="Markdown")

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra resumen de logs importantes"""
    logs_msg = (
        f"📋 *LOGS RECIENTES - RESUMEN*\n\n"
        f"Para ver logs completos:\n"
        f"Revisa la consola de Railway\n\n"
        f"🔍 *Estadísticas:*\n"
        f"• Requests API: {diagnostic_api.request_count}\n"
        f"• Tokens procesados: {len(monitored_tokens)}\n"
        f"• Errores recientes: {diagnostic_api.last_error or 'Ninguno'}\n\n"
        f"💡 _Los logs detallados están en la consola de Railway_"
    )
    await update.message.reply_text(logs_msg, parse_mode="Markdown")

# ===================== MAIN =====================
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    commands = [
        ("start", cmd_start),
        ("diagnostico", cmd_diagnostico),
        ("cazar", cmd_cazar),
        ("parar", cmd_parar),
        ("status", cmd_status),
        ("logs", cmd_logs),
    ]
    
    for command, handler in commands:
        app.add_handler(CommandHandler(command, handler))
    
    logger.info("🩺 Bot de Diagnóstico Completo Iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
