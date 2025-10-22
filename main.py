#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 MAIN ENTRY POINT - BOT + HEALTH SERVER
==========================================
Este script asegura que tanto el bot como el health server se ejecuten correctamente
"""

import asyncio
import logging
import sys
import os

# Configurar logging básico
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
    """Entry point principal que ejecuta bot + health server en paralelo"""
    
    try:
        # Importar módulos
        logger.info("🔧 Importando módulos...")
        
        from health_server import start_health_server, update_bot_status, bot_status
        from bot_trader_final import main_trading_loop, ml_predictor, config, state
        
        logger.info("✅ Módulos importados correctamente")
        
        # Inicializar estado del health server
        logger.info("🏥 Inicializando health server...")
        
        update_bot_status(
            running=True,
            scans=0,
            positions=0,
            signals=0,
            trades=0,
            wins=0,
            losses=0,
            total_pnl=0.0,
            ml_enabled=ml_predictor.is_trained,
            mode="DRY_RUN" if config.DRY_RUN else ("SIMULATION" if config.SIMULATION_MODE else "REAL")
        )
        
        logger.info("🚀 Iniciando bot + health server en paralelo...")
        
        # Obtener puerto desde variable de entorno (Railway lo proporciona)
        port = int(os.getenv('PORT', '8080'))
        logger.info(f"📡 Health server escuchando en puerto: {port}")
        
        # Ejecutar ambos servicios en paralelo
        await asyncio.gather(
            start_health_server(port=port),  # Health server PRIMERO
            main_trading_loop(),  # Bot trading después
            return_exceptions=True
        )
        
    except ImportError as e:
        logger.error(f"❌ Error importando módulos: {e}")
        logger.error("Verifica que bot_trader_final.py y health_server.py existan")
        sys.exit(1)
        
    except Exception as e:
        logger.error(f"❌ Error crítico: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    try:
        logger.info("=" * 60)
        logger.info("🚀 SOLANA TRADING BOT ML - STARTING")
        logger.info("=" * 60)
        
        asyncio.run(main())
        
    except KeyboardInterrupt:
        logger.info("⏸️ Bot detenido por usuario")
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"❌ Error fatal: {e}", exc_info=True)
        sys.exit(1)
