# config.py
import os
from dataclasses import dataclass


def _get_env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _get_env_float(name: str, default: float) -> float:
    v = _get_env(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _get_env_int(name: str, default: int) -> int:
    v = _get_env(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _get_env_bool(name: str, default: bool = False) -> bool:
    v = _get_env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass
class BotConfig:
    mode: str
    flintr_api_key: str
    helius_rpc_url: str | None
    wallet_private_key: str | None

    telegram_bot_token: str
    telegram_chat_id: int | None

    stop_loss_percent: float
    trailing_stop_percent: float
    invest_amount_sol: float
    max_active_trades: int

    partial_sell_enabled: bool
    partial_sell_percent: float

    jupiter_api_url: str
    slippage_bps: int

    log_level: str


def load_config() -> BotConfig:
    mode = _get_env("MODE", "simulation").lower()
    if mode not in ("simulation", "real"):
        mode = "simulation"

    telegram_chat_id_str = _get_env("TELEGRAM_CHAT_ID")
    telegram_chat_id = int(telegram_chat_id_str) if telegram_chat_id_str else None

    return BotConfig(
        mode=mode,
        flintr_api_key=_get_env("FLINTR_API_KEY", "") or "",
        helius_rpc_url=_get_env("HELIUS_RPC_URL"),
        wallet_private_key=_get_env("WALLET_PRIVATE_KEY"),

        telegram_bot_token=_get_env("TELEGRAM_BOT_TOKEN", "") or "",
        telegram_chat_id=telegram_chat_id,

        stop_loss_percent=_get_env_float("STOP_LOSS_PERCENT", 15.0),
        trailing_stop_percent=_get_env_float("TRAILING_STOP_PERCENT", 20.0),
        invest_amount_sol=_get_env_float("INVEST_AMOUNT_SOL", 0.05),
        max_active_trades=_get_env_int("MAX_ACTIVE_TRADES", 5),

        partial_sell_enabled=_get_env_bool("PARTIAL_SELL_ENABLED", False),
        partial_sell_percent=_get_env_float("PARTIAL_SELL_PERCENT", 50.0),

        jupiter_api_url=_get_env("JUPITER_API_URL", "https://lite-api.jup.ag"),
        slippage_bps=_get_env_int("SLIPPAGE_BPS", 300),

        log_level=_get_env("LOG_LEVEL", "INFO"),
    )
