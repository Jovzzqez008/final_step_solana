# telegram_bot.py
import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import BotConfig
from trading_engine import TradingEngine


logger = logging.getLogger(__name__)


class TelegramController:
    def __init__(self, config: BotConfig, engine: TradingEngine) -> None:
        self.config = config
        self.engine = engine

    # --------- handlers ---------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return

        txt = (
            "🚀 *Pump.fun Sniper Bot*\n\n"
            f"Modo: `{self.config.mode}`\n"
            f"Activo: `{self.engine.is_active()}`\n\n"
            "Comandos:\n"
            "• /status – estado del bot\n"
            "• /positions – posiciones abiertas\n"
            "• /stats – rendimiento\n"
            "• /activate – activar entradas nuevas\n"
            "• /deactivate – pausar entradas\n"
            "• /mode – mostrar modo (SIM/REAL)\n"
        )
        await update.message.reply_text(txt, parse_mode="Markdown")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return

        stats = self.engine.get_stats_snapshot()
        txt = (
            f"📊 *Status Bot*\n\n"
            f"Modo: `{stats['mode']}`\n"
            f"Activo: `{stats['active']}`\n"
            f"Posiciones abiertas: `{stats['num_positions']}`\n"
            f"Trades totales: `{stats['total_trades']}`\n"
            f"Win rate: `{stats['win_rate']:.1f}%`\n"
            f"P&L realizado: `{stats['total_realized_pnl_sol']:.4f} SOL`\n"
        )
        await update.message.reply_text(txt, parse_mode="Markdown")

    async def positions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._is_authorized(update):
            return

        positions = self.engine.get_positions_snapshot()
        if not positions:
            await update.message.reply_text("No hay posiciones abiertas.")
            return

        lines = ["🏹 *Posiciones abiertas:*", ""]
        for p in positions:
            lines.append(
                f"• `{p['symbol']}` ({p['name']})\n"
                f"  Mint: `{p['mint']}`\n"
                f"  Estado: `{p['status']}`\n"
                f"  Entrada: `{p['entry_price']:.10f} SOL`\n"
                f"  Último: `{p['last_price']:.10f} SOL`\n"
                f"  PnL: `{p['pnl_percent']:.2f}%` sobre precio entrada\n"
                f"  Size: `{p['size_sol']:.4f} SOL`\n"
            )

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.status(update, context)

    async def activate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        self.engine.set_active(True)
        await update.message.reply_text("✅ Bot activado (aceptando nuevas entradas).")

    async def deactivate(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not await self._is_authorized(update):
            return
        self.engine.set_active(False)
        await update.message.reply_text("⏸ Bot pausado (no entra en nuevos tokens).")

    async def mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_authorized(update):
            return
        await update.message.reply_text(
            f"Modo actual: `{self.config.mode}`", parse_mode="Markdown"
        )

    # --------- auth ---------

    async def _is_authorized(self, update: Update) -> bool:
        if self.config.telegram_chat_id is None:
            # primera vez: fijamos chat como dueño
            if update.effective_chat:
                self.config.telegram_chat_id = update.effective_chat.id
                return True
            return False

        if update.effective_chat and update.effective_chat.id == self.config.telegram_chat_id:
            return True

        # ignorar mensajes de otros chats
        logger.warning(
            "Mensaje de chat no autorizado: %s", update.effective_chat.id
        )
        return False


async def build_application(config: BotConfig, engine: TradingEngine) -> Application:
    app = Application.builder().token(config.telegram_bot_token).build()

    ctrl = TelegramController(config, engine)

    app.add_handler(CommandHandler("start", ctrl.start))
    app.add_handler(CommandHandler("status", ctrl.status))
    app.add_handler(CommandHandler("positions", ctrl.positions))
    app.add_handler(CommandHandler("stats", ctrl.stats))
    app.add_handler(CommandHandler("activate", ctrl.activate))
    app.add_handler(CommandHandler("deactivate", ctrl.deactivate))
    app.add_handler(CommandHandler("mode", ctrl.mode))

    return app
