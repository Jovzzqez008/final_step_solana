# flintr_client.py
import json
import time
from typing import Any, Callable, Dict, Optional

from websocket import WebSocketApp

FlintrCallback = Callable[[Dict[str, Any]], None]


class FlintrClient:
    """
    Cliente WebSocket para Flintr.
    """

    def __init__(
        self,
        api_key: str,
        *,
        platform_filter: str = "pump.fun",
        on_mint: Optional[FlintrCallback] = None,
        on_graduation: Optional[FlintrCallback] = None,
        debug: bool = True,
        reconnect_delay: float = 5.0,
    ) -> None:
        if not api_key:
            raise RuntimeError("FLINTR_API_KEY vacío")

        self.api_key = api_key
        self.ws_url = f"wss://api-v1.flintr.io/sub?token={self.api_key}"

        self.platform_filter = platform_filter
        self.on_mint = on_mint
        self.on_graduation = on_graduation

        self.debug = debug
        self.reconnect_delay = reconnect_delay

    # ----------------- API pública -----------------

    def run_forever(self) -> None:
        """Loop infinito con reconexión automática."""
        while True:
            self._log(f"[Flintr] Conectando a {self.ws_url} ...")

            ws_app = WebSocketApp(
                self.ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )

            ws_app.run_forever(ping_interval=30, ping_timeout=10)

            self._log(
                f"[Flintr] Desconectado. Reintentando en {self.reconnect_delay}s..."
            )
            time.sleep(self.reconnect_delay)

    # ----------------- Callbacks internos -----------------

    def _on_open(self, ws: WebSocketApp) -> None:
        self._log("[Flintr] ✅ Conectado → escuchando señales…")

    def _on_message(self, ws: WebSocketApp, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self._warn("[Flintr] ⚠️ JSON inválido:", message)
            return

        event = data.get("event") or {}
        event_class = event.get("class")

        # Ping keep-alive
        if event_class == "ping":
            if self.debug:
                self._log("[Flintr] 🔁 Ping:", data.get("time"))
            return

        if event_class == "token":
            self._handle_token_event(data)
            return

        if self.debug:
            self._log("[Flintr] Evento ignorado:", data)

    def _on_error(self, ws: WebSocketApp, error: Exception) -> None:
        self._warn("[Flintr] ⚠️ Error WebSocket:", repr(error))

    def _on_close(
        self,
        ws: WebSocketApp,
        close_status_code: int,
        close_msg: Optional[str],
    ) -> None:
        self._warn(
            f"[Flintr] 🔴 Cerrado (code={close_status_code}, msg={close_msg})"
        )

    # ----------------- Lógica de eventos token -----------------

    def _handle_token_event(self, data: Dict[str, Any]) -> None:
        event = data.get("event") or {}
        platform = event.get("platform")
        event_type = event.get("type")

        if self.platform_filter and platform != self.platform_filter:
            return

        if event_type == "mint":
            self._handle_mint(data)
        elif event_type == "graduation":
            self._handle_graduation(data)
        else:
            if self.debug:
                self._log("[Flintr] Token event ignorado:", platform, event_type)

    def _handle_mint(self, data: Dict[str, Any]) -> None:
        mint = data.get("data", {}).get("mint")
        meta = data.get("data", {}).get("metaData") or {}
        symbol = meta.get("symbol") or ""
        name = meta.get("name") or ""

        self._log(f"🟢 [Flintr] MINT pump.fun → {symbol} ({name}) mint={mint}")

        if self.on_mint:
            try:
                self.on_mint(data)
            except Exception as exc:
                self._warn("[Flintr] Error en on_mint:", repr(exc))

    def _handle_graduation(self, data: Dict[str, Any]) -> None:
        mint = data.get("data", {}).get("mint")
        meta = data.get("data", {}).get("metaData") or {}
        symbol = meta.get("symbol") or ""
        name = meta.get("name") or ""

        self._log(
            f"🎓 [Flintr] GRADUATION pump.fun → {symbol} ({name}) mint={mint}"
        )

        if self.on_graduation:
            try:
                self.on_graduation(data)
            except Exception as exc:
                self._warn("[Flintr] Error en on_graduation:", repr(exc))

    # ----------------- Logs -----------------

    def _log(self, *args: Any) -> None:
        if self.debug:
            print(*args)

    def _warn(self, *args: Any) -> None:
        print(*args)
