# main.py
# Script optimizado para la detección de nuevos pools en Raydium
# Versión final para despliegue en Railway (CON CORRECCIÓN DE SINTAXIS)

import asyncio
import httpx
import logging
import os
import sys
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Finalized
from solana.rpc.websocket_api import connect

# --- 1. CONFIGURACIÓN DESDE VARIABLES DE ENTORNO ---
# El script leerá estas variables desde la configuración de tu proyecto en Railway.

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    HELIUS_API_KEY = os.environ['HELIUS_API_KEY']
    TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
    TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
    RPC_URL_WEBSOCKET = os.environ['RPC_URL_WEBSOCKET']
except KeyError as e:
    logging.error(f"Error: La variable de entorno {e} no está configurada. El bot no puede iniciar.")
    sys.exit("Deteniendo el bot por falta de configuración.")

# --- Constantes del Programa (NO TOCAR) ---
RAYDIUM_LP_V4_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
TOKENS_CONOCIDOS = {
    "So11111111111111111111111111111111111111112"  # Wrapped SOL
}

# --- 2. ALMACENAMIENTO Y COLAS ASÍNCRONAS ---
# Las colas permiten que los diferentes componentes del bot se comuniquen entre sí.
procesador_queue = asyncio.Queue()
incubadora_queue = asyncio.Queue()
firmas_procesadas = set()
tokens_en_incubadora = set()


# --- 3. COMPONENTES DEL BOT ---

async def enviar_mensaje_telegram(texto):
    """Envía un mensaje a tu chat de Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=10.0)
            if response.status_code != 200:
                logging.error(f"Error al enviar mensaje a Telegram: {response.status_code} - {response.text}")
    except Exception as e:
        logging.error(f"Excepción al conectar con Telegram: {e}")

async def cazador_de_pools():
    """Caza transacciones de Raydium usando un filtro eficiente por Program ID."""
    logging.info("Iniciando el Cazador de Pools...")
    raydium_pubkey = Pubkey.from_string(RAYDIUM_LP_V4_PROGRAM_ID)
    
    while True:
        try:
            async with connect(RPC_URL_WEBSOCKET) as websocket:
                await websocket.logs_subscribe(filter_=raydium_pubkey, commitment=Finalized)
                logging.info(f"Cazador conectado. Escuchando a Raydium: {RAYDIUM_LP_V4_PROGRAM_ID}")
                await enviar_mensaje_telegram("✅ <b>Bot Desplegado</b>\nCazador conectado y buscando nuevos pools...")
                
                async for msg in websocket:
                    for log_message in msg:
                        signature = log_message.value.signature
                        if signature not in firmas_procesadas:
                            firmas_procesadas.add(signature)
                            logging.info(f"[CAZADOR] Nueva firma candidata: {signature}")
                            await procesador_queue.put(str(signature))
        except Exception as e:
            logging.error(f"Error en Cazador (WebSocket): {e}. Reiniciando en 10 segundos...")
            await enviar_mensaje_telegram(f"⚠️ <b>Error Crítico en Cazador</b>\nSe perdió la conexión WebSocket. Reiniciando...\n<i>Error: {e}</i>")
            await asyncio.sleep(10)

async def procesador_de_transacciones():
    """Procesa firmas en lotes con Helius para encontrar el mint del nuevo token."""
    logging.info("Iniciando el Procesador de Transacciones...")
    api_url = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
    
    while True:
        try:
            first_signature = await procesador_queue.get()
            batch = [first_signature]
            
            while not procesador_queue.empty() and len(batch) < 50:
                batch.append(await procesador_queue.get())
            
            logging.info(f"[PROCESADOR] Analizando lote de {len(batch)} firmas con Helius.")
            
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
                            logging.info(f"[PROCESADOR] Nuevo token candidato encontrado: {mint}")
                            await enviar_mensaje_telegram(f"🐣 <b>Nuevo Candidato Detectado</b>\n\n<b>Mint:</b> <code>{mint}</code>\n\nEnviado a la incubadora para análisis profundo.")
            else:
                logging.error(f"[PROCESADOR] Error en API de Helius: {response.status_code} - {response.text}")
                # --- LÍNEA CORREGIDA ---
                # Se cambiaron las comillas simples " por triples """ para permitir múltiples líneas.
                await enviar_mensaje_telegram(f"""<b>Error en el Procesador</b>
La API de Helius devolvió un error {response.status_code}. Revisa los créditos o el estado de la API.""")

        except Exception as e:
            logging.error(f"Error en Procesador: {e}")
            await asyncio.sleep(5)

async def vigilante_incubadora():
    """Toma tokens de la incubadora y realiza un análisis profundo (simulado)."""
    logging.info("Iniciando el Vigilante de la Incubadora...")
    
    while True:
        try:
            token_mint = await incubadora_queue.get()
            logging.info(f"[VIGILANTE] Analizando token: {token_mint}")
            await enviar_mensaje_telegram(f"🔬 <b>Analizando Candidato...</b>\n\n<b>Mint:</b> <code>{token_mint}</code>")

            # --- AQUÍ VA TU LÓGICA DE ANÁLISIS PROFUNDO ---
            # Este es el lugar para llamar a APIs de seguridad (GoPlus),
            # de precios (Birdeye), y para verificar la liquidez.
            
            # --- Simulación de análisis ---
            await asyncio.sleep(2) # Simula una llamada a API externa
            es_seguro = True  # Simulación: supongamos que el token pasa el filtro

            if es_seguro:
                logging.info(f"[VIGILANTE] Token {token_mint} APROBADO. Alerta final enviada.")
                mensaje = (
                    f"✅ <b>¡ALERTA DE TOKEN APROBADO!</b> ✅\n\n"
                    f"<b>Mint:</b> <code>{token_mint}</code>\n\n"
                    f"🔗 <b>Análisis Rápido:</b>\n"
                    f"├ <a href='https://solscan.io/token/{token_mint}'>Solscan</a>\n"
                    f"├ <a href='https://rugcheck.xyz/tokens/{token_mint}'>RugCheck</a>\n"
                    f"└ <a href='https://dexscreener.com/solana/{token_mint}'>DexScreener</a>"
                )
                await enviar_mensaje_telegram(mensaje)
            else:
                logging.info(f"[VIGILANTE] Token {token_mint} RECHAZADO.")
                await enviar_mensaje_telegram(f"❌ <b>Candidato Rechazado</b>\n\n<b>Mint:</b> <code>{token_mint}</code>\n\nNo pasó los filtros de seguridad.")
            
            incubadora_queue.task_done()

        except Exception as e:
            logging.error(f"Error en el Vigilante: {e}")
            await asyncio.sleep(5)


# --- 4. EJECUCIÓN PRINCIPAL ---

async def main():
    logging.info("Iniciando todos los componentes del bot...")
    cazador_task = asyncio.create_task(cazador_de_pools())
    procesador_task = asyncio.create_task(procesador_de_transacciones())
    vigilante_task = asyncio.create_task(vigilante_incubadora())
    
    await asyncio.gather(cazador_task, procesador_task, vigilante_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot detenido manualmente.")
