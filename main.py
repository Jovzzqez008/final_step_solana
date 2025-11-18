# main.py
import logging
import threading
import asyncio

from dotenv import load_dotenv

from config import load_config
from flintr_client import FlintrClient
from trading_engine import TradingEngine
from telegram_bot import build_application
from price_monitor import price_monitor_loop
from jupiter_executor import JupiterExecutor
# Si ya tienes PumpFunExecutor para compras reales:
# from pumpfun_executor import PumpFunExecutor


def main() -> None:
    # Localmente lee .env; en Railway usas variables de entorno directas
    load_dotenv()

    config = load_config()

    # Logging global
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("main")

    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no configurado")
    if not config.flintr_api_key:
        raise RuntimeError("FLINTR_API_KEY no configurado")

    # -------------------------------------------------------------------------
    # Crear TradingEngine + executors
    # -------------------------------------------------------------------------

    # Si aún no tienes PumpFunExecutor, puedes dejar executor=None
    # pump_executor = PumpFunExecutor(config=config)
    pump_executor = None

    jupiter_executor = JupiterExecutor(config=config)

    engine = TradingEngine(
        config=config,
        executor=pump_executor,
        jupiter_executor=jupiter_executor,
    )

    # -------------------------------------------------------------------------
    # Flintr WebSocket en un thread aparte (mints + graduations en tiempo real)
    # -------------------------------------------------------------------------
    flintr = FlintrClient(
        api_key=config.flintr_api_key,
        platform_filter="pump.fun",
        on_mint=engine.handle_flintr_mint,
        on_graduation=engine.handle_flintr_graduation,
        debug=True,
    )

    def flintr_thread() -> None:
        logger.info("🚀 Flintr WebSocket thread iniciado...")
        flintr.run_forever()

    t = threading.Thread(target=flintr_thread, daemon=True)
    t.start()

    # -------------------------------------------------------------------------
    # Telegram + PriceMonitor en el event loop principal (asyncio)
    # -------------------------------------------------------------------------

    async def run_telegram_and_price_monitor() -> None:
        # Construimos el bot de Telegram con todos los comandos
        app = await build_application(config, engine)

        # Lanzamos el monitor de precios como tarea en el mismo loop
        loop = asyncio.get_running_loop()
        loop.create_task(price_monitor_loop(engine))

        logger.info("✅ Telegram bot arrancando (polling) + PriceMonitor activo...")
        await app.run_polling(drop_pending_updates=True)

    try:
        asyncio.run(run_telegram_and_price_monitor())
    except KeyboardInterrupt:
        logger.info("⏹️  Bot detenido por el usuario (Ctrl+C).")
    finally:
        # Cerrar el cliente de Jupiter al apagar
        try:
            jupiter_executor.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
