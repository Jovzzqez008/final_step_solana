# trading_engine.py
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from config import BotConfig
from models import Position, PositionStatus


class TradingEngine:
    """
    Motor principal de trading.
    Ahora mismo:
      - Solo abre posiciones SIMULADAS al recibir mints de Flintr.
      - Guarda estado en memoria.
      - Expone métodos para Telegram.
    Más adelante:
      - Implementamos compras reales en Pump.fun + Jupiter.
      - Monitor de precios para trailing/SL.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._lock = threading.Lock()

        # mint -> Position
        self._positions: Dict[str, Position] = {}

        # stats simples
        self._total_realized_pnl_sol: float = 0.0
        self._total_trades: int = 0
        self._wins: int = 0
        self._losses: int = 0

        # bandera para aceptar nuevas entradas
        self.active: bool = True

    # ------------- Hooks desde Flintr -------------

    def handle_flintr_mint(self, event: Dict[str, Any]) -> None:
        """
        Llamado por FlintrClient cuando llega un MINT de pump.fun.
        Aquí decidimos si entrar y abrimos posición.
        """
        data = event.get("data", {})
        mint = data.get("mint")
        meta = data.get("metaData") or {}
        token_data = data.get("tokenData") or {}

        if not mint:
            return

        symbol = meta.get("symbol") or ""
        name = meta.get("name") or ""

        # latestPrice (si viene) es precio aproximado por token en SOL
        latest_price = token_data.get("latestPrice")
        try:
            entry_price = float(latest_price) if latest_price is not None else 0.0
        except (TypeError, ValueError):
            entry_price = 0.0

        with self._lock:
            if not self.active:
                print(f"[Engine] Ignorando {symbol} (bot desactivado)")
                return

            if mint in self._positions:
                print(f"[Engine] Ya tenemos posición en {mint}, ignorando.")
                return

            if len(self._positions) >= self.config.max_active_trades:
                print("[Engine] Max active trades alcanzado, ignorando nuevo mint.")
                return

            # Por ahora siempre entramos; luego podemos añadir filtros (MC, dev, etc.)
            size_sol = self.config.invest_amount_sol

            if self.config.mode == "simulation":
                self._open_simulated_position(
                    mint=mint,
                    symbol=symbol,
                    name=name,
                    entry_price_sol=entry_price if entry_price > 0 else 0.0,
                    size_sol=size_sol,
                )
            else:
                # TODO: implementar compra real en Pump.fun
                print(
                    f"[Engine] (REAL) Debería comprar {symbol} / {mint} con {size_sol} SOL"
                )
                self._open_simulated_position(
                    mint=mint,
                    symbol=symbol,
                    name=name,
                    entry_price_sol=entry_price if entry_price > 0 else 0.0,
                    size_sol=size_sol,
                )

    # ------------- Apertura de posiciones -------------

    def _open_simulated_position(
        self,
        mint: str,
        symbol: str,
        name: str,
        entry_price_sol: float,
        size_sol: float,
    ) -> None:
        # Si no tenemos precio, asumimos 1 token = (size_sol / 1e6) para no dejarlo vacío.
        if entry_price_sol <= 0:
            entry_price_sol = size_sol / 1_000_000.0

        amount_tokens = size_sol / entry_price_sol if entry_price_sol > 0 else 0.0

        pos = Position(
            mint=mint,
            symbol=symbol or mint[:6],
            name=name or "",
            entry_price_sol=entry_price_sol,
            size_sol=size_sol,
            amount_tokens=amount_tokens,
            trailing_stop_percent=self.config.trailing_stop_percent,
            stop_loss_percent=self.config.stop_loss_percent,
            max_price_sol=entry_price_sol,
            last_price_sol=entry_price_sol,
        )

        self._positions[mint] = pos

        print(
            f"[Engine] (SIM) Nueva posición {symbol} mint={mint} "
            f"size={size_sol} SOL, price={entry_price_sol}"
        )

    # ------------- Actualización de precios (hook futuro) -------------

    def update_price(self, mint: str, price_sol: float) -> Optional[Position]:
        """
        Hook para el monitor de precios. Se llamará cada vez que tengamos un
        nuevo precio para un token. Aquí se actualiza trailing/SL.
        De momento solo actualiza last_price/max_price; la lógica de salida
        la añadimos después.
        """
        with self._lock:
            pos = self._positions.get(mint)
            if not pos or pos.status != PositionStatus.OPEN:
                return None

            pos.last_price_sol = price_sol
            if price_sol > pos.max_price_sol:
                pos.max_price_sol = price_sol

            # TODO: evaluar stop loss / trailing y cerrar posición si se cumplen las condiciones
            return pos

    # ------------- Snapshots para Telegram -------------

    def get_positions_snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for pos in self._positions.values():
                out.append(
                    {
                        "mint": pos.mint,
                        "symbol": pos.symbol,
                        "name": pos.name,
                        "status": pos.status.value,
                        "entry_price": pos.entry_price_sol,
                        "last_price": pos.last_price_sol,
                        "pnl_percent": (
                            (pos.last_price_sol - pos.entry_price_sol)
                            / pos.entry_price_sol * 100.0
                            if pos.entry_price_sol > 0 and pos.last_price_sol > 0
                            else 0.0
                        ),
                        "size_sol": pos.size_sol,
                    }
                )
            return out

    def get_stats_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            win_rate = (
                (self._wins / self._total_trades) * 100.0
                if self._total_trades > 0
                else 0.0
            )
            return {
                "mode": self.config.mode,
                "active": self.active,
                "num_positions": len(self._positions),
                "total_realized_pnl_sol": self._total_realized_pnl_sol,
                "total_trades": self._total_trades,
                "wins": self._wins,
                "losses": self._losses,
                "win_rate": win_rate,
            }

    # ------------- Control desde Telegram -------------

    def set_active(self, value: bool) -> None:
        with self._lock:
            self.active = value

    def is_active(self) -> bool:
        with self._lock:
            return self.active
