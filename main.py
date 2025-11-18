# main.py
import logging
import threading
import asyncio

from dotenv import load_dotenv

from config import load_config
from flintr_client import FlintrClient
from trading_engine import TradingEngine
from telegram_bot import build_application
from price_monitor import PriceMonitor


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

    # Motor de trading
    engine = TradingEngine(config=config)

    # ---- Flintr en thread aparte ----
    flintr = FlintrClient(
        api_key=config.flintr_api_key,
        platform_filter="pump.fun",
        on_mint=engine.handle_flintr_mint,
        on_graduation=engine.handle_flintr_graduation,
        debug=True,
    )

    def flintr_thread() -> None:
        flintr.run_forever()

    t_flintr = threading.Thread(target=flintr_thread, daemon=True)
    t_flintr.start()

    # ---- PriceMonitor en otro thread ----
    price_monitor = PriceMonitor(
        engine=engine,
        poll_interval_seconds=5.0,  # puedes ajustar a tu gusto
    )

    def price_thread() -> None:
        asyncio.run(price_monitor.run_forever())

    t_price = threading.Thread(target=price_thread, daemon=True)
    t_price.start()

    # ---- Telegram en hilo principal ----
    async def run_telegram() -> None:
        app = await build_application(config, engine)
        print("✅ Telegram bot arrancando (polling)...")
        await app.run_polling(drop_pending_updates=True)

    asyncio.run(run_telegram())


if __name__ == "__main__":
    main()
