# -*- coding: utf-8 -*-

import asyncio
import json
import httpx
import time
import logging
import os
import signal
from dotenv import load_dotenv
import asyncpg
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Carga de variables de entorno ---
load_dotenv()

# --- CONFIGURACIÓN GLOBAL ---
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL")
HELIUS_WSS_URL = os.getenv("HELIUS_WSS_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_CHAT_ID = None

# --- CONSTANTES DE LÓGICA ---
MIN_LIQUIDITY_USD = 7500
JUPITER_RADAR_INTERVAL_SECONDS = 120
INCUBATOR_CHECK_INTERVAL_SECONDS = 180
WATCHLIST_MONITOR_INTERVAL_SECONDS = 300
INCUBATOR_DISCARD_AFTER_HOURS = 3
SECONDS_IN_HOUR = 3600

# --- ESTADO GLOBAL ---
DB_POOL = None
watchlist = {}
incubator = {}

# --- Configuración del Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


# --- 💾 FUNCIONES DE LA BASE DE DATOS 💾 ---
# (Esta sección no necesita cambios)
async def setup_database(pool):
    try:
        async with pool.acquire() as conn:
            await conn.execute('''CREATE TABLE IF NOT EXISTS incubator (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
            await conn.execute('''CREATE TABLE IF NOT EXISTS watchlist (token_address TEXT PRIMARY KEY, data JSONB NOT NULL);''')
        logger.info("Tablas de la base de datos verificadas/creadas correctamente.")
    except Exception as e:
        logger.error(f"Error al configurar la base de datos: {e}")

async def db_add_to_incubator(pool, token_address, data):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO incubator (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO NOTHING", token_address, json.dumps(data))

async def db_remove_from_incubator(pool, token_address):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM incubator WHERE token_address = $1", token_address)

async def db_load_all_incubator(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT token_address, data FROM incubator")
    return {row['token_address']: json.loads(row['data']) for row in rows}

async def db_add_to_watchlist(pool, token_address, data):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO watchlist (token_address, data) VALUES ($1, $2) ON CONFLICT (token_address) DO UPDATE SET data = $2", token_address, json.dumps(data))

async def db_load_all_watchlist(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT token_address, data FROM watchlist")
    return {row['token_address']: json.loads(row['data']) for row in rows}


# --- 📞 FUNCIONES DE APIS EXTERNAS ---
# (Esta sección no necesita cambios)
async def get_dexscreener_data(client: httpx.AsyncClient, token_address: str):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        res = await client.get(url, timeout=15)
        res.raise_for_status()
        data = res.json()
        if data and data.get('pairs'):
            return sorted(data['pairs'], key=lambda p: p.get('liquidity', {}).get('usd', 0), reverse=True)[0]
    except httpx.RequestError as e:
        logger.warning(f"Error de red consultando DexScreener para {token_address}: {e}")
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Error procesando respuesta de DexScreener para {token_address}: {e}")
    except Exception as e:
        logger.error(f"Error inesperado en get_dexscreener_data para {token_address}: {e}")
    return None

async def get_jupiter_top_tokens(client: httpx.AsyncClient):
    query = "solana age > 1 hours and age < 24 hours"
    url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
    try:
        res = await client.get(url, timeout=30)
        res.raise_for_status()
        data = res.json()
        if data and data.get('pairs'):
            return [p for p in data['pairs'] if p.get('chainId') == 'solana']
    except httpx.RequestError as e:
        logger.warning(f"Error de red consultando el radar de DexScreener: {e}")
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Error procesando la respuesta del radar de DexScreener: {e}")
    except Exception as e:
        logger.error(f"Error inesperado en get_jupiter_top_tokens: {e}")
    return []


# --- 🔄 TAREAS ASÍNCRONAS ---
# (Esta sección no necesita cambios)
async def jupiter_momentum_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Radar de Momentum (Júpiter/DexScreener)...")
    client = context.bot_data['client']
    db_pool = context.bot_data['db_pool']
    while True:
        try:
            logger.info("[RADAR JÚPITER] Buscando tokens con momentum en la ventana de 1-24 horas...")
            momentum_tokens = await get_jupiter_top_tokens(client)
            if not momentum_tokens:
                logger.info("[RADAR JÚPITER] No se encontraron nuevos tokens que cumplan el criterio de edad.")
            else:
                for pair in momentum_tokens:
                    token_address = pair.get('baseToken', {}).get('address')
                    if token_address and token_address not in incubator and token_address not in watchlist:
                        logger.info(f"  - 📡 [RADAR] Júpiter detectó {pair.get('baseToken', {}).get('symbol','N/A')}. Enviando a la incubadora.")
                        new_data = {'found_at': time.time(), 'source': 'Júpiter Radar'}
                        incubator[token_address] = new_data
                        await db_add_to_incubator(db_pool, token_address, new_data)
            await asyncio.sleep(JUPITER_RADAR_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("Radar de Momentum detenido.")
            break
        except Exception as e:
            logger.error(f"Error crítico en Radar de Momentum: {e}")
            await asyncio.sleep(60)

async def incubator_checker_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Vigilante de la Incubadora...")
    client = context.bot_data['client']
    db_pool = context.bot_data['db_pool']
    while True:
        try:
            await asyncio.sleep(INCUBATOR_CHECK_INTERVAL_SECONDS)
            if not incubator: continue
            logger.info(f"[INCUBADORA] Verificando {len(incubator)} tokens...")
            now_ts = time.time()
            for token_address, data in list(incubator.items()):
                dex_data = await get_dexscreener_data(client, token_address)
                if not dex_data:
                    if now_ts - data['found_at'] > INCUBATOR_DISCARD_AFTER_HOURS * SECONDS_IN_HOUR:
                        logger.info(f"  - 🗑️ [DESCARTADO] {token_address[:10]}... sin datos en DexScreener tras {INCUBATOR_DISCARD_AFTER_HOURS}h.")
                        del incubator[token_address]
                        await db_remove_from_incubator(db_pool, token_address)
                    continue
                liquidity = dex_data.get('liquidity', {}).get('usd', 0)
                if liquidity < MIN_LIQUIDITY_USD:
                    logger.info(f"  - [EN ESPERA] {token_address[:10]}... liquidez baja (${liquidity:,.0f}). Objetivo: >${MIN_LIQUIDITY_USD:,.0f}")
                    continue
                logger.info(f"  - ✅ [APROBADO] ¡{token_address[:10]}... pasa el filtro de liquidez! Movido a Watchlist.")
                new_watchlist_data = {'approved_at': now_ts, 'last_notified': 'initial'}
                watchlist[token_address] = new_watchlist_data
                await db_add_to_watchlist(db_pool, token_address, new_watchlist_data)
                del incubator[token_address]
                await db_remove_from_incubator(db_pool, token_address)
                mensaje = (
                    f"✅ **Alerta de Oportunidad (Fuente: {data.get('source')})**\n\n"
                    f"**Token:** `{dex_data.get('baseToken', {}).get('symbol', 'N/A')}`\n"
                    f"**Mint:** `{token_address}`\n\n"
                    f"**Liquidez:** `${liquidity:,.2f}` USD\n"
                    f"**Precio:** `${dex_data.get('priceUsd', '0.0')}`\n\n"
                    f"🚨 *Realiza tu propio análisis. Verifica en RugCheck, etc.*"
                )
                if TARGET_CHAT_ID:
                    await context.bot.send_message(chat_id=TARGET_CHAT_ID, text=mensaje, parse_mode='Markdown')
        except asyncio.CancelledError:
            logger.info("Vigilante de Incubadora detenido.")
            break
        except Exception as e:
            logger.error(f"Error crítico en Vigilante de Incubadora: {e}")
            await asyncio.sleep(60)

async def watchlist_monitor_task(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Iniciando Monitor de Watchlist...")
    while True:
        try:
            await asyncio.sleep(WATCHLIST_MONITOR_INTERVAL_SECONDS)
            if not watchlist: continue
            logger.info(f"[WATCHLIST] Monitoreando {len(watchlist)} tokens aprobados...")
        except asyncio.CancelledError:
            logger.info("Monitor de Watchlist detenido.")
            break
        except Exception as e:
            logger.error(f"Error en Monitor de Watchlist: {e}")


# --- 🤖 COMANDOS DE TELEGRAM ---
# (Esta sección no necesita cambios)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_CHAT_ID
    TARGET_CHAT_ID = update.message.chat_id
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ **Error Crítico:** No se pudo conectar a la base de datos. El bot no puede funcionar.")
        return
    await update.message.reply_text(
        "👋 **Bot de Caza v16.2 (Modo Despliegue)**\n\n"
        "Este bot monitorea DexScreener en busca de nuevos tokens en Solana (1-24h de antigüedad) que alcancen un umbral de liquidez.\n\n"
        "**Comandos:**\n"
        "/cazar - Inicia todas las tareas de monitoreo.\n"
        "/parar - Detiene la caza.\n"
        "/status - Muestra el estado actual y contadores.\n"
        "/incubadora - Muestra los últimos tokens encontrados, esperando liquidez.\n"
        "/watchlist - Muestra los tokens que ya fueron aprobados y notificados."
    )

async def hunt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 El bot ya está cazando.")
        return
    db_pool = context.bot_data.get('db_pool')
    if not db_pool:
        await update.message.reply_text("❌ No se puede iniciar la caza: no hay conexión a la base de datos.")
        return
    await update.message.reply_text("📡 ¡Iniciando caza! Cargando estado desde la base de datos y arrancando tareas...")
    global watchlist, incubator
    watchlist = await db_load_all_watchlist(db_pool)
    incubator = await db_load_all_incubator(db_pool)
    logger.info(f"Cargados {len(incubator)} tokens en incubadora y {len(watchlist)} en watchlist desde la BD.")
    client = httpx.AsyncClient()
    context.bot_data.update({'client': client, 'tasks': [
        asyncio.create_task(jupiter_momentum_task(context)),
        asyncio.create_task(incubator_checker_task(context)),
        asyncio.create_task(watchlist_monitor_task(context))
    ]})
    await update.message.reply_text("✅ ¡Caza activa! El radar y los vigilantes están en funcionamiento.")

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get('tasks'):
        await update.message.reply_text("🤔 El bot no está cazando actualmente.")
        return
    for task in context.bot_data['tasks']: task.cancel()
    if client := context.bot_data.get('client'): await client.aclose()
    context.bot_data.clear()
    context.bot_data['db_pool'] = DB_POOL 
    await update.message.reply_text("🛑 ¡Caza detenida! Todas las tareas han sido canceladas.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = "🛑 El bot está **Detenido**."
    if context.bot_data.get('tasks'):
        status_msg = (
            f"✅ El bot está **Activo** (Modo Radar Enfocado).\n\n"
            f"🐣 **{len(incubator)}** tokens en incubadora (esperando liquidez).\n"
            f"🏆 **{len(watchlist)}** tokens en watchlist (aprobados y bajo monitoreo)."
        )
    await update.message.reply_text(status_msg, parse_mode='Markdown')

async def incubator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 La incubadora está vacía."); return
    message = f"🐣 **Últimos 10 Tokens en la Incubadora ({len(incubator)} en total):**\n\n"
    token_addresses = list(incubator.keys()); tokens_to_show = token_addresses[-10:]; tokens_to_show.reverse()
    for token_address in tokens_to_show: message += f"- `{token_address}`\n"
    if len(incubator) > 10: message += f"\n... y {len(incubator) - 10} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not watchlist:
        await update.message.reply_text("🏆 La watchlist está vacía."); return
    message = f"🏆 **Últimos 15 Tokens en la Watchlist ({len(watchlist)} en total):**\n\n"
    token_addresses = list(watchlist.keys()); tokens_to_show = token_addresses[-15:]; tokens_to_show.reverse()
    for token_address in tokens_to_show: message += f"- `{token_address}`\n"
    if len(watchlist) > 15: message += f"\n... y {len(watchlist) - 15} más antiguos."
    await update.message.reply_text(message, parse_mode='Markdown')


# --- 🚀 PUNTO DE ENTRADA PRINCIPAL (CORREGIDO PARA DESPLIEGUE) ---

async def main():
    """Función principal que configura e inicia el bot de forma robusta."""
    global DB_POOL
    
    # Evento para manejar el apagado de forma elegante
    stop_event = asyncio.Event()

    # Configurar manejadores de señales para SIGINT (Ctrl+C) y SIGTERM (enviado por Railway)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    try:
        # --- ARRANQUE ---
        logger.info("Iniciando el bot...")
        DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        logger.info("Pool de conexiones a la base de datos creado.")
        
        application.bot_data['db_pool'] = DB_POOL
        await setup_database(DB_POOL)

        # Añadir handlers de comandos
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("cazar", hunt_command))
        application.add_handler(CommandHandler("parar", stop_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(CommandHandler("incubadora", incubator_command))
        application.add_handler(CommandHandler("watchlist", watchlist_command))

        # Iniciar todos los componentes de la aplicación manualmente
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        logger.info("--- El bot está en línea y escuchando ---")
        
        # Esperar hasta que se reciba una señal de apagado
        await stop_event.wait()

    except Exception as e:
        logger.critical(f"Error crítico durante el arranque o ejecución: {e}")
    finally:
        # --- APAGADO ---
        logger.info("Recibida señal de apagado. Cerrando conexiones...")
        if application.updater and application.updater.is_running():
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()
        
        if DB_POOL:
            await DB_POOL.close()
            logger.info("Pool de conexiones a la base de datos cerrado.")
        logger.info("El bot se ha apagado correctamente.")


if __name__ == '__main__':
    asyncio.run(main())
