#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 MAIN ENTRY POINT - HEALTH SERVER PRIMERO
============================================
✅ Health server inicia INMEDIATAMENTE
✅ Bot trading inicia después (asíncrono)
✅ Railway recibe 200 OK en segundos
"""

import asyncio
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot_trader.log')
    ]
)

logger = logging.getLogger(__name__)

async def main():
    """✅ Entry point con health server PRIMERO"""
    
    try:
        logger.info("=" * 60)
        logger.info("🚀 SOLANA TRADING BOT ML v4.2")
        logger.info("=" * 60)
        
        # ✅ Importar health server PRIMERO
        logger.info("🏥 Importando health server...")
        from health_server import start_health_server, update_bot_status
        
        # ✅ Obtener puerto de Railway
        port = int(os.getenv('PORT', '8080'))
        logger.info(f"📡 Puerto asignado: {port}")
        
        # ✅ Inicializar estado inicial
        update_bot_status(
            running=False,
            scans=0,
            positions=0,
            signals=0,
            trades=0,
            wins=0,
            losses=0,
            total_pnl=0.0,
            ml_enabled=False,
            mode="initializing"
        )
        
        logger.info("✅ Health server configurado - iniciando servidor HTTP...")
        
        # ✅ CRÍTICO: Iniciar health server EN BACKGROUND
        health_task = asyncio.create_task(start_health_server(port=port))
        
        # ✅ Esperar 2 segundos para que el servidor esté listo
        await asyncio.sleep(2)
        logger.info("✅ Health server ONLINE - Railway debería recibir 200 OK")
        
        # ✅ Ahora SÍ importar y ejecutar el bot (puede tardar)
        logger.info("🤖 Importando módulos del bot...")
        try:
            from bot_trader_final import main_trading_loop, ml_predictor, config
            
            logger.info("✅ Módulos del bot importados")
            
            # Actualizar estado con info del bot
            update_bot_status(
                running=True,
                scans=0,
                positions=0,
                ml_enabled=ml_predictor.is_trained,
                mode="DRY_RUN" if config.DRY_RUN else ("SIMULATION" if config.SIMULATION_MODE else "REAL")
            )
            
            logger.info("🚀 Iniciando loop principal del bot...")
            
            # ✅ Ejecutar bot en paralelo con health server
            await asyncio.gather(
                health_task,  # Ya está corriendo
                main_trading_loop(),  # Iniciar bot ahora
                return_exceptions=True
            )
            
        except ImportError as e:
            logger.error(f"❌ Error importando bot: {e}")
            logger.warning("⚠️ Health server sigue corriendo sin bot")
            
            # Actualizar estado
            update_bot_status(
                running=False,
                scans=0,
                positions=0,
                mode="error_import"
            )
            
            # Mantener health server vivo
            await health_task
        
    except KeyboardInterrupt:
        logger.info("⏸️ Detenido por usuario")
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}", exc_info=True)
        
        # Intentar mantener health server vivo aunque falle el bot
        try:
            from health_server import update_bot_status
            update_bot_status(
                running=False,
                scans=0,
                positions=0,
                mode="error_critical"
            )
            logger.info("⚠️ Health server sigue activo a pesar del error")
            await asyncio.Event().wait()  # Mantener vivo indefinidamente
        except:
            sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Adiós")
        sys.exit(0)
