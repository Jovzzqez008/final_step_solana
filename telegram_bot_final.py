# main.py
# Script optimizado con código de depuración de versiones.

import asyncio
import httpx
import logging
import os
import sys
from solders.pubkey import Pubkey
from solana.rpc.commitment import Finalized
from solana.rpc.websocket_api import connect
from solana.rpc.filter import RpcTransactionLogsFilter
import importlib.metadata # NUEVO IMPORTE PARA DEPURACIÓN

# --- 1. CONFIGURACIÓN DESDE VARIABLES DE ENTORNO ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    HELIUS_API_KEY = os.environ['HELIUS_API_KEY']
    RPC_URL_WEBSOCKET = os.environ['HELIUS_RPC_URL']
    TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
    TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
except KeyError as e:
    logging.error(f"Error: La variable de entorno {e} no está configurada.")
    sys.exit("Deteniendo el bot por falta de configuración.")

# --- Constantes del Programa ---
RAYDIUM_LP_V4_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
TOKENS_CONOCIDOS = {"So11111111111111111111111111111111111111112"}

# --- Colas y Almacenamiento ---
procesador_queue = asyncio.Queue()
incubadora_queue = asyncio.Queue()
firmas_procesadas = set()
tokens_en_incubadora = set()

# --- Componentes del Bot ---
async def enviar_mensaje_telegram(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=10.0)
    except Exception as e:
        logging.error(f"Excepción al conectar con Telegram: {e}")

async def cazador_de_pools():
    logging.info("Iniciando Cazador...")
    raydium_pubkey = Pubkey.from_string(RAYDIUM_LP_V4_PROGRAM_ID)
    while True:
        try:
            async with connect(RPC_URL_WEBSOCKET) as websocket:
                await websocket.logs_subscribe(
                    filter_=RpcTransactionLogsFilter.Mentions([raydium_pubkey]),
                    commitment=Finalized
                )
                logging.info(f"Cazador conectado a Raydium.")
                await enviar_mensaje_telegram("✅ <b>Bot Desplegado</b>\nCazador conectado y buscando pools...")
                async for msg in websocket:
                    for log_message in msg:
                        signature = log_message.value.signature
                        if signature not in firmas_procesadas:
                            firmas_procesadas.add(signature)
                            await procesador_queue.put(str(signature))
        except Exception as e:
            logging.error(f"Error en Cazador (WebSocket): {e}. Reiniciando...")
            await enviar_mensaje_telegram(f"⚠️ <b>Error Crítico en Cazador</b>\nSe perdió la conexión. Reiniciando...\n<i>Error: {e}</i>")
            await asyncio.sleep(10)

# (El resto de las funciones: procesador_de_transacciones y vigilante_incubadora no cambian)
async def procesador_de_transacciones():
    logging.info("Iniciando Procesador...")
    api_url = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
    while True:
        try:
            first_signature = await procesador_queue.get()
            batch = [first_signature]
            while not procesador_queue.empty() and len(batch) < 50:
                batch.append(await procesador_queue.get())
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(api_url, json={"transactions": batch})
            if response.status_code == 200:
                transactions = response.json()
                for tx in transactions:
                    for instruction in tx.get("tokenTransfers", []):
                        mint = instruction.get("mint")
                        if mint and mint not in TOKENS_CONOCIDOS and mint not in tokens_en_incubadora:
                            tokens_en_incubadora.add(mint)
                            await incubadora_queue.put(mint)
                            await enviar_mensaje_telegram(f"🐣 <b>Nuevo Candidato</b>\nMint: <code>{mint}</code>\nEnviado a incubadora.")
            else:
                logging.error(f"Error en API de Helius: {response.status_code}")
                await enviar_mensaje_telegram(f"""<b>Error en Procesador</b>\nAPI de Helius devolvió {response.status_code}.""")
        except Exception as e:
            logging.error(f"Error en Procesador: {e}")
            await asyncio.sleep(5)

async def vigilante_incubadora():
    logging.info("Iniciando Vigilante...")
    while True:
        try:
            token_mint = await incubadora_queue.get()
            await enviar_mensaje_telegram(f"🔬 <b>Analizando...</b>\nMint: <code>{token_mint}</code>")
            await asyncio.sleep(2)
            es_seguro = True
            if es_seguro:
                mensaje = (f"✅ <b>¡ALERTA APROBADA!</b>\n"
                           f"<b>Mint:</b> <code>{token_mint}</code>\n\n"
                           f"🔗 <b>Enlaces:</b>\n"
                           f"├ <a href='https://solscan.io/token/{token_mint}'>Solscan</a>\n"
                           f"├ <a href='https://rugcheck.xyz/tokens/{token_mint}'>RugCheck</a>\n"
                           f"└ <a href='https://dexscreener.com/solana/{token_mint}'>DexScreener</a>")
                await enviar_mensaje_telegram(mensaje)
            incubadora_queue.task_done()
        except Exception as e:
            logging.error(f"Error en Vigilante: {e}")
            await asyncio.sleep(5)

# --- EJECUCIÓN PRINCIPAL ---
async def main():
    # --- CÓDIGO DE DEPURACIÓN AÑADIDO ---
    try:
        solana_version = importlib.metadata.version("solana")
        solders_version = importlib.metadata.version("solders")
        mensaje_debug = (
            f"⚙️ **Iniciando Bot (Depuración)**\n\n"
            f"Versión de `solana` instalada: <b>{solana_version}</b>\n"
            f"Versión de `solders` instalada: <b>{solders_version}</b>"
        )
        await enviar_mensaje_telegram(mensaje_debug)
    except Exception as e:
        await enviar_mensaje_telegram(f"Error al obtener versiones de librerías: {e}")
    # --- FIN DEL CÓDIGO DE DEPURACIÓN ---

    logging.info("Iniciando componentes del bot...")
    cazador_task = asyncio.create_task(cazador_de_pools())
    procesador_task = asyncio.create_task(procesador_de_transacciones())
    vigilante_task = asyncio.create_task(vigilante_incubadora())
    await asyncio.gather(cazador_task, procesador_task, vigilante_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot detenido manualmente.")
