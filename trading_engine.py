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

    - Recibe señales de Flintr (mints / graduations).
    - Abre posiciones (por ahora SIMULADAS/DRY_RUN).
    - Actualiza precios en base a un monitor externo (DexScreener, luego Helius/Jupiter).
    - Aplica Stop Loss y Trailing Stop.
    - Calcula P&L y estadísticas.
    """

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._lock = threading.Lock()

        # mint -> Position
        self._positions: Dict[str, Position] = {}

        # estadísticas globales
        self._total_realized_pnl_sol: float = 0.0
        self._total_trades: int = 0
        self._wins: int = 0
        self._losses: int = 0

        # bandera para aceptar nuevas posiciones
        self.active: bool = True

    # -------------------------------------------------------------------------
    # Hooks desde Flintr
    # -------------------------------------------------------------------------

    def handle_flintr_mint(self, event: Dict[str, Any]) -> None:
        """
        Llamado por FlintrClient cuando llega un MINT de pump.fun.
        Aquí decidimos si entrar y abrimos posición.
        """
        data = event.get("data", {})
        mint = data.get("mint")

        if not mint:
            return

        meta = data.get("metaData") or {}
        token_data = data.get("tokenData") or {}

        symbol = meta.get("symbol") or ""
        name = meta.get("name") or ""

        latest_price_raw = token_data.get("latestPrice")
        try:
            entry_price = float(latest_price_raw) if latest_price_raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            entry_price = 0.0

        with self._lock:
            if not self.active:
                print(f"[Engine] Ignorando {symbol} (bot desactivado)")
                return

            if mint in self._positions:
                print(f"[Engine] Ya existe posición para mint {mint}, ignorando.")
                return

            if len(self._positions) >= self.config.max_active_trades:
                print("[Engine] Max active trades alcanzado, ignorando nuevo mint.")
                return

            size_sol = self.config.invest_amount_sol

            if self.config.mode == "simulation":
                # DRY_RUN: simulamos como si compráramos ahora mismo
                self._open_simulated_position(
                    mint=mint,
                    symbol=symbol,
                    name=name,
                    entry_price_sol=entry_price,
                    size_sol=size_sol,
                )
            else:
                # FUTURO: aquí irá la compra REAL en Pump.fun (Helius + Phantom)
                print(
                    f"[Engine] (REAL FUTURO) Debería comprar {symbol} / {mint} "
                    f"con {size_sol} SOL en Pump.fun"
                )
                # Aunque el modo sea REAL, mantenemos un espejo simulado para PnL interno
                self._open_simulated_position(
                    mint=mint,
                    symbol=symbol,
                    name=name,
                    entry_price_sol=entry_price,
                    size_sol=size_sol,
                )

    # -------------------------------------------------------------------------
    # Apertura de posiciones (DRY_RUN)
    # -------------------------------------------------------------------------

    def _open_simulated_position(
        self,
        mint: str,
        symbol: str,
        name: str,
        entry_price_sol: float,
        size_sol: float,
    ) -> None:
        """
        Crea una posición simulada. Si no tenemos precio todavía, lo fijaremos
        en el primer update_price real que llegue (DexScreener/Jupiter).
        """
        has_price = entry_price_sol > 0

        amount_tokens = (
            size_sol / entry_price_sol if entry_price_sol > 0 else 0.0
        )

        pos = Position(
            mint=mint,
            symbol=symbol or mint[:6],
            name=name or "",
            entry_price_sol=entry_price_sol,
            size_sol=size_sol,
            amount_tokens=amount_tokens,
            trailing_stop_percent=self.config.trailing_stop_percent,
            stop_loss_percent=self.config.stop_loss_percent,
            max_price_sol=entry_price_sol if has_price else 0.0,
            last_price_sol=entry_price_sol if has_price else 0.0,
        )

        self._positions[mint] = pos

        print(
            f"[Engine] (SIM) Nueva posición {pos.symbol} mint={mint} "
            f"size={size_sol} SOL, entry_price={entry_price_sol}"
        )

    # -------------------------------------------------------------------------
    # Actualización de precios + SL / Trailing
    # -------------------------------------------------------------------------

    def update_price(self, mint: str, price_sol: float) -> Optional[Position]:
        """
        Llamado por el monitor de precios (DexScreener/Jupiter/Helius).
        Actualiza last_price y evalúa Stop Loss / Trailing Stop.
        """
        with self._lock:
            pos = self._positions.get(mint)
            if not pos or pos.status != PositionStatus.OPEN:
                return None

            # Si la posición no tenía precio de entrada aún (Flintr sin latestPrice),
            # usamos el primer precio real como precio de compra DRY_RUN.
            if pos.entry_price_sol <= 0 and price_sol > 0:
                pos.entry_price_sol = price_sol
                pos.max_price_sol = price_sol
                pos.last_price_sol = price_sol
                print(
                    f"[Engine] Fijando precio de entrada para {pos.symbol}: "
                    f"{price_sol:.10f} SOL (DRY_RUN)"
                )
                return pos

            # Actualizar último precio
            pos.last_price_sol = price_sol

            # Actualizar máximo histórico
            if price_sol > pos.max_price_sol:
                pos.max_price_sol = price_sol

            # % PnL desde precio de entrada
            if pos.entry_price_sol > 0:
                pnl_percent = (
                    (price_sol - pos.entry_price_sol)
                    / pos.entry_price_sol
                    * 100.0
                )
            else:
                pnl_percent = 0.0

            # % caída desde máximo (para trailing)
            if pos.max_price_sol > 0:
                drawdown_percent = (
                    (price_sol - pos.max_price_sol)
                    / pos.max_price_sol
                    * 100.0
                )
            else:
                drawdown_percent = 0.0

            # ----------------- STOP LOSS -----------------
            if pos.stop_loss_percent > 0 and pnl_percent <= -pos.stop_loss_percent:
                print(
                    f"[SL] Stop Loss activado para {pos.symbol}: "
                    f"{pnl_percent:.2f}%"
                )
                self._close_position_simulated(pos, reason="STOP LOSS")
                return pos

            # ----------------- TRAILING STOP -----------------
            if (
                pos.trailing_stop_percent > 0
                and drawdown_percent <= -pos.trailing_stop_percent
            ):
                print(
                    f"[TS] Trailing Stop activado para {pos.symbol}: "
                    f"drawdown {drawdown_percent:.2f}% desde máximo."
                )
                self._close_position_simulated(pos, reason="TRAILING STOP")
                return pos

            return pos

    # -------------------------------------------------------------------------
    # Cierre de posiciones (DRY_RUN)
    # -------------------------------------------------------------------------

    def _close_position_simulated(self, pos: Position, reason: str) -> None:
        """
        Cierra la posición y calcula P&L simulado en SOL.
        (En modo REAL esto será el espejo de las operaciones on-chain.)
        """
        if pos.status != PositionStatus.OPEN:
            return

        pos.status = PositionStatus.CLOSED
        pos.closed_at = time.time()

        exit_price = pos.last_price_sol or pos.entry_price_sol
        entry = pos.entry_price_sol

        if entry > 0:
            pos.realized_pnl_percent = (exit_price - entry) / entry * 100.0
        else:
            pos.realized_pnl_percent = 0.0

        pos.realized_pnl_sol = pos.size_sol * (pos.realized_pnl_percent / 100.0)

        # Actualizar stats globales
        self._register_closed_position(pos)

        print(
            f"💰 (SIM) CERRADO {pos.symbol} — Razón: {reason}\n"
            f"    Entrada: {entry:.10f} SOL\n"
            f"    Salida:  {exit_price:.10f} SOL\n"
            f"    P&L:     {pos.realized_pnl_percent:.2f}% "
            f"({pos.realized_pnl_sol:.6f} SOL)"
        )

    def _register_closed_position(self, pos: Position) -> None:
        self._total_trades += 1
        self._total_realized_pnl_sol += pos.realized_pnl_sol
        if pos.realized_pnl_sol >= 0:
            self._wins += 1
        else:
            self._losses += 1

    # -------------------------------------------------------------------------
    # Snapshots para Telegram / monitoreo
    # -------------------------------------------------------------------------

    def get_positions_snapshot(self) -> List[Dict[str, Any]]:
        """
        Devuelve una lista de dicts para mostrar en Telegram.
        Incluye tanto abiertas como cerradas, pero el monitor de precios
        sólo usa las OPEN.
        """
        with self._lock:
            out: List[Dict[str, Any]] = []
            for pos in self._positions.values():
                # Para PnL instantáneo usamos last_price si existe, si no entry.
                last_price = pos.last_price_sol or pos.entry_price_sol
                if pos.entry_price_sol > 0 and last_price > 0:
                    pnl_percent = (
                        (last_price - pos.entry_price_sol)
                        / pos.entry_price_sol
                        * 100.0
                    )
                else:
                    pnl_percent = 0.0

                out.append(
                    {
                        "mint": pos.mint,
                        "symbol": pos.symbol,
                        "name": pos.name,
                        "status": pos.status.value,
                        "entry_price": pos.entry_price_sol,
                        "last_price": last_price,
                        "pnl_percent": pnl_percent,
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

    # -------------------------------------------------------------------------
    # Control desde Telegram
    # -------------------------------------------------------------------------

    def set_active(self, value: bool) -> None:
        with self._lock:
            self.active = value

    def is_active(self) -> bool:
        with self._lock:
            return self.active
