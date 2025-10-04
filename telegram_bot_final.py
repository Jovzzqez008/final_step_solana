# ... (todo el código anterior sin cambios hasta la función incubator_checker_task) ...

async def procesar_nuevo_token(token_address, source, chat_id):
    logger.info(f"[CAZADOR] Token detectado: {token_address}. Enviando a procesar...") ### NUEVO ###
    reporte_seguridad, es_seguro, _ = get_security_report(token_address)
    
    if es_seguro:
        if token_address not in incubator and token_address not in watchlist:
            logger.info(f"  - ✅ [OK SEGURIDAD] Pasa filtro de seguridad. Añadiendo a incubadora y DB: {token_address}") ### NUEVO ###
            new_data = {'found_at': time.time(), 'source': source, 'security_report': reporte_seguridad, 'symbol': 'Fetching...'}
            incubator[token_address] = new_data
            await db_add_to_incubator(token_address, new_data)
    else:
        logger.info(f"  - ❌ [RECHAZADO SEGURIDAD] No pasó el filtro de seguridad. Razón: {reporte_seguridad}") ### NUEVO ###

async def incubator_checker_task(chat_id):
    logger.info("Iniciando Vigilante de la Incubadora...")
    while True:
        try:
            await asyncio.sleep(300)
            logger.info("Vigilante de Incubadora despertando...")
            
            promoted_tokens = []
            expired_tokens = []
            current_time = time.time()
            
            for token_address, data in list(incubator.items()):
                if current_time - data.get('found_at', 0) > 7200:
                    expired_tokens.append(token_address)
                    continue

                birdeye_url = f"https://public-api.birdeye.so/defi/token_overview?address={token_address}"; headers_birdeye = {"X-API-KEY": BIRDEYE_API_KEY}
                try:
                    res = requests.get(birdeye_url, headers=headers_birdeye, timeout=10); res.raise_for_status()
                    api_data = res.json()
                    if not api_data.get("success") or not api_data.get("data"): continue
                    
                    token_data = api_data["data"]
                    symbol = token_data.get("symbol", "N/A")
                    liquidity = token_data.get("liquidity", 0)
                    holders = token_data.get("holders", 0)

                    # Actualizamos el símbolo en nuestra data en memoria
                    incubator[token_address]['symbol'] = symbol

                    logger.info(f"  - [INCUBADORA] Chequeando {symbol}: Liquidez=${liquidity:,.2f} (Req: >7500), Holders={holders} (Req: >50)") ### NUEVO ###
                    
                    if liquidity > 7500 and holders > 50:
                        logger.info(f"  - 🔥 ¡PROMOCIÓN! {symbol} ({token_address}) cumple los criterios.")
                        
                        alerta = (
                            f"🕵️‍♂️ *NUEVO CANDIDATO A VIGILAR* (Fuente: {data['source']})\n\n"
                            f"*{symbol}* ({token_address})\n\n"
                            f"Ha madurado en la incubadora y ahora cumple los criterios de mercado. Se añade a la watchlist para seguimiento.\n\n"
                            f"*Reporte de Seguridad Inicial:*\n{data['security_report']}"
                        )
                        enviar_alerta_telegram_sync(alerta, chat_id)
                        
                        watchlist[token_address] = {
                            'found_at': data['found_at'], 'symbol': symbol, 'status': 'new',
                            'initial_liquidity': liquidity, 'initial_holders': holders, 'source': data['source']
                        }
                        promoted_tokens.append(token_address)
                        
                except Exception as e:
                    logger.error(f"  - Error revisando {token_address} en Birdeye: {e}")

            for token in promoted_tokens:
                if token in incubator:
                    await db_add_to_watchlist(token, watchlist[token])
                    await db_remove_from_incubator(token)
                    del incubator[token]
            
            for token in expired_tokens:
                if token in incubator:
                    await db_remove_from_incubator(token)
                    del incubator[token]
                    logger.info(f"  - 🚮 Token expirado y eliminado de la incubadora y DB: {token}")

        except asyncio.CancelledError:
            logger.info("Vigilante de Incubadora detenido."); break
        except Exception as e:
            logger.error(f"Error en Vigilante de Incubadora: {e}")
            
# ... (el resto del código de los cazadores y watcher sin cambios hasta los comandos de telegram) ...

# --- COMANDOS DE TELEGRAM ACTUALIZADOS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Bienvenido al Bot Cazador PRO v5.1 (Diagnóstico)!\n\nUsa /cazar para iniciar la búsqueda.\nUsa /parar para detenerla.\nUsa /status para ver el estado.\nUsa /diagnostico para ver la incubadora.")

### NUEVO COMANDO ###
async def diagnostic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not incubator:
        await update.message.reply_text("🐣 La incubadora está vacía en este momento.")
        return
    
    message = "🐣 *Tokens Actualmente en la Incubadora:*\n\n"
    for addr, data in incubator.items():
        symbol = data.get('symbol', 'N/A')
        age_minutes = (time.time() - data.get('found_at', 0)) / 60
        message += f"- `{symbol}` (`{addr[:4]}...{addr[-4:]}`)\n  - Edad: {age_minutes:.1f} mins\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

def main():
    print("--- 🤖 Iniciando Bot de Telegram... ---")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cazar", hunt_command))
    application.add_handler(CommandHandler("parar", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("diagnostico", diagnostic_command)) ### NUEVO ###
    print("--- 🎧 El bot está escuchando a Telegram... ---")
    application.run_polling()

if __name__ == '__main__':
    main()

# El resto del código que no se muestra aquí permanece igual
