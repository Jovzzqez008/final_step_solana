# bot_diagnostico_completo.py
import asyncio, json, os, time, logging, aiohttp
import websockets
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from collections import defaultdict, deque

# ===================== CONFIGURACIÓN =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("diagnostico_completo")

# Variables de diagnóstico
message_count = 0
transaction_count = 0
token_count = 0

async def helius_monitor_diagnostico(context: ContextTypes.DEFAULT_TYPE):
    """Monitor de diagnóstico que muestra TODO lo que recibe"""
    global message_count, transaction_count, token_count
    
    if not HELIUS_WSS_URL:
        logger.error("❌ HELIUS_WSS_URL no configurada")
        return

    logger.info("🎯 INICIANDO DIAGNÓSTICO COMPLETO...")
    
    # Mostrar URL (segura)
    safe_url = HELIUS_WSS_URL.split('?')[0] if '?' in HELIUS_WSS_URL else HELIUS_WSS_URL
    logger.info(f"📡 Conectando a: {safe_url}")
    
    # Suscripción MÁS AMPLIA posible
    subscription_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "transactionSubscribe",
        "params": [
            {
                # Sin filtros para recibir TODO
                "vote": True,      # Incluir transacciones de votación
                "failed": True,    # Incluir transacciones fallidas
                "accountInclude": [],  # Sin filtros
                "accountExclude": []   # Sin exclusiones
            }
        ],
    }

    try:
        async with websockets.connect(HELIUS_WSS_URL, ping_interval=30, ping_timeout=10) as ws:
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text="✅ CONECTADO A HELIUS WEB SOCKET\n\nEnviando suscripción..."
            )
            
            await ws.send(json.dumps(subscription_msg))
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text="📨 SUSCRIPCIÓN ENVIADA\n\nEsperando transacciones..."
            )
            
            logger.info("🟢 Esperando mensajes...")

            async for message in ws:
                message_count += 1
                
                try:
                    data = json.loads(message)
                    
                    # LOG cada mensaje recibido
                    logger.info(f"📨 MENSAJE #{message_count} RECIBIDO")
                    
                    # Verificar el tipo de mensaje
                    if data.get('method') == 'transactionNotification':
                        transaction_count += 1
                        
                        # Obtener la transacción
                        tx = data.get('params', {}).get('result', {})
                        
                        # Información básica de la transacción
                        signatures = tx.get('transaction', {}).get('signatures', [])
                        signature = signatures[0][:16] + "..." if signatures else "Unknown"
                        
                        # Enviar alerta de transacción recibida
                        await context.bot.send_message(
                            chat_id=TARGET_CHAT_ID,
                            text=f"🔔 TRANSACCIÓN #{transaction_count} RECIBIDA\n\nSignature: `{signature}`",
                            parse_mode="Markdown"
                        )
                        
                        # Analizar la transacción en detalle
                        await analyze_transaction(tx, context)
                        
                    elif data.get('method') == 'ping':
                        logger.info("🏓 PING recibido - Conexión activa")
                    else:
                        logger.info(f"📦 Mensaje de tipo: {data.get('method', 'desconocido')}")
                        
                except Exception as e:
                    logger.error(f"❌ Error procesando mensaje: {e}")
                    await context.bot.send_message(
                        chat_id=TARGET_CHAT_ID,
                        text=f"❌ ERROR: {str(e)}"
                    )

    except websockets.exceptions.InvalidURI:
        error_msg = """
❌ URL DE WEBSOCKET INVÁLIDA

Tu variable HELIUS_WSS_URL parece incorrecta.

✅ DEBERÍA SER:
wss://mainnet.helius-rpc.com/?api-key=tu_key

❌ NO DEBERÍA SER:
https://api.helius.xyz/v0/transactions/...
        """
        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=error_msg)
        
    except Exception as e:
        error_msg = f"""
🚨 ERROR DE CONEXIÓN

No se pudo conectar a Helius:

{str(e)}

Verifica:
1. Tu API key de Helius
2. Que la URL sea WebSocket (wss://)
3. Tu conexión a internet
        """
        await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=error_msg)
        logger.error(f"Error de conexión: {e}")

async def analyze_transaction(tx: dict, context: ContextTypes.DEFAULT_TYPE):
    """Analiza una transacción en detalle"""
    try:
        # Información básica
        signatures = tx.get('transaction', {}).get('signatures', [])
        account_keys = tx.get('transaction', {}).get('message', {}).get('accountKeys', [])
        instructions = tx.get('transaction', {}).get('message', {}).get('instructions', [])
        
        # Meta información
        meta = tx.get('meta', {})
        pre_token_balances = meta.get('preTokenBalances', [])
        post_token_balances = meta.get('postTokenBalances', [])
        
        # Construir mensaje de análisis
        analysis_msg = f"""
🔍 ANÁLISIS DE TRANSACCIÓN

📝 Signatures: {len(signatures)}
👤 Accounts: {len(account_keys)}
📋 Instructions: {len(instructions)}
💰 Pre Token Balances: {len(pre_token_balances)}
💰 Post Token Balances: {len(post_token_balances)}

📊 CUENTAS ENCONTRADAS:
"""
        
        # Mostrar primeras cuentas
        for i, account in enumerate(account_keys[:10]):
            analysis_msg += f"{i+1}. `{account[:16]}...`\n"
        
        if len(account_keys) > 10:
            analysis_msg += f"... y {len(account_keys) - 10} más\n"
        
        # Buscar posibles tokens
        tokens_found = set()
        
        # De preTokenBalances
        for balance in pre_token_balances:
            mint = balance.get('mint')
            if mint:
                tokens_found.add(mint)
        
        # De postTokenBalances  
        for balance in post_token_balances:
            mint = balance.get('mint')
            if mint:
                tokens_found.add(mint)
        
        # De account keys que parecen tokens
        for account in account_keys:
            if len(account) == 44:  # Longitud típica de token
                tokens_found.add(account)
        
        analysis_msg += f"\n🎯 POSIBLES TOKENS: {len(tokens_found)}\n"
        
        for i, token in enumerate(list(tokens_found)[:5]):
            analysis_msg += f"{i+1}. `{token}`\n"
        
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=analysis_msg,
            parse_mode="Markdown"
        )
        
        # Procesar tokens encontrados
        for token in list(tokens_found)[:3]:  # Solo primeros 3 para no saturar
            await process_token_simple(token, context)
            
    except Exception as e:
        logger.error(f"Error analizando transacción: {e}")

async def process_token_simple(token_addr: str, context: ContextTypes.DEFAULT_TYPE):
    """Procesa un token de manera simple"""
    global token_count
    try:
        token_count += 1
        
        # Mensaje simple de token detectado
        await context.bot.send_message(
            chat_id=TARGET_CHAT_ID,
            text=f"🎯 TOKEN #{token_count} DETECTADO\n\n`{token_addr}`",
            parse_mode="Markdown"
        )
        
        logger.info(f"✅ Token detectado: {token_addr}")
        
    except Exception as e:
        logger.error(f"Error procesando token simple: {e}")

# ===================== COMANDOS TELEGRAM =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 DIAGNÓSTICO HELIUS COMPLETO\n\n"
        "Este bot mostrará TODAS las transacciones que reciba de Helius.\n\n"
        "Comandos:\n"
        "• /diagnostico - Iniciar diagnóstico\n"
        "• /estado - Ver estado actual\n"
        "• /parar - Detener diagnóstico",
        parse_mode="Markdown"
    )

async def cmd_diagnostico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el diagnóstico completo"""
    await update.message.reply_text(
        "🔍 INICIANDO DIAGNÓSTICO COMPLETO...\n\n"
        "Voy a mostrar:\n"
        "• Cada mensaje recibido de Helius\n"
        "• Cada transacción detectada\n" 
        "• Análisis detallado de transacciones\n"
        "• Tokens encontrados\n\n"
        "⏳ Conectando...",
        parse_mode="Markdown"
    )
    
    # Iniciar el monitor de diagnóstico
    asyncio.create_task(helius_monitor_diagnostico(context))

async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra estado del diagnóstico"""
    estado_msg = f"""
📊 ESTADO DEL DIAGNÓSTICO

📨 Mensajes recibidos: {message_count}
🔔 Transacciones detectadas: {transaction_count}  
🎯 Tokens identificados: {token_count}

💡 Si todos están en 0, hay problemas de conexión.
    """
    await update.message.reply_text(estado_msg, parse_mode="Markdown")

async def cmd_parar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detiene el diagnóstico"""
    # En una implementación real, necesitarías un mecanismo para detener el loop
    await update.message.reply_text(
        "🛑 Para detener el diagnóstico, necesitas reiniciar el bot.\n\n"
        "En Railway, ve a Deployments y haz 'Redeploy'.",
        parse_mode="Markdown"
    )

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN no configurado")
        return
        
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("diagnostico", cmd_diagnostico))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("parar", cmd_parar))
    
    logger.info("🚀 Bot de Diagnóstico Completo Iniciado")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
