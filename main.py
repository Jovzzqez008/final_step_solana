# main.py
import logging
import threading
import asyncio

from dotenv import load_dotenv

from config import load_config
from flintr_client import FlintrClient
from trading_engine import TradingEngine
from telegram_bot import build_application
from price_monitor_dexscreener import DexscreenerPriceMonitor
from pumpfun_executor import PumpFunExecutor


def main() -> None:
    load_dotenv()  # en Railway no hace nada, pero local sí

    config = load_config()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no configurado")
    if not config.flintr_api_key:
        raise RuntimeError("FLINTR_API_KEY no configurado")

    # ---- Executor Pump.fun (por ahora DRY_RUN) ----
    pumpfun_exec = PumpFunExecutor(config)

    # ---- Motor de trading (DRY_RUN o futuro REAL) ----
    engine = TradingEngine(config=config, executor=pumpfun_exec)

    # ---- Monitor de precios REAL (DexScreener) ----
    price_monitor = DexscreenerPriceMonitor(engine, interval_sec=5.0)
    price_monitor.start()

    # ---- Flintr en thread aparte ----
    flintr = FlintrClient(
        api_key=config.flintr_api_key,
        platform_filter="pump.fun",
        on_mint=engine.handle_flintr_mint,
        on_graduation=None,  # luego conectamos graduations para ventas
        debug=True,
    )

    def flintr_thread() -> None:
        flintr.run_forever()

    t = threading.Thread(target=flintr_thread, daemon=True)
    t.start()

    # ---- Telegram en hilo principal ----
    async def run_telegram() -> None:
        app = await build_application(config, engine)
        print("✅ Telegram bot arrancando (polling)...")
        await app.run_polling(drop_pending_updates=True)

    asyncio.run(run_telegram())


if __name__ == "__main__":
    main()
