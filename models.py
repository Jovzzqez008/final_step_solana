# models.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class TradeSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


@dataclass
class Position:
    mint: str
    symbol: str
    name: str
    entry_price_sol: float        # precio por token en SOL (simulado/real)
    size_sol: float               # cuánto SOL invertimos
    amount_tokens: float          # cantidad de tokens recibidos (simulado/real)
    opened_at: float = field(default_factory=lambda: time.time())
    closed_at: Optional[float] = None

    status: PositionStatus = PositionStatus.OPEN

    # trailing / SL
    max_price_sol: float = 0.0    # máximo precio observado
    trailing_stop_percent: float = 0.0
    stop_loss_percent: float = 0.0

    # P&L realizado
    realized_pnl_sol: float = 0.0
    realized_pnl_percent: float = 0.0

    # último precio visto (para /positions)
    last_price_sol: float = 0.0
