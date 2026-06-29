"""
SignalForge — HTTP Health Check Endpoint

Provides a simple asyncio-based HTTP server exposing health-check endpoints
so external monitoring tools (UptimeRobot, Grafana Cloud, Prometheus, etc.)
can verify SignalForge is alive and operating correctly.

Usage (integrated into main.py's event loop)::

    server = HealthServer(port=8080)
    await server.start()
    # ... later, when shutting down ...
    await server.stop()

Other modules call ``set_health()`` to update component status in real-time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_VERSION = "1.0"
_BUFFER_SIZE = 65536
_CRLF = b"\r\n"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_DOWN = "down"

_VALID_STATUSES = {STATUS_OK, STATUS_DEGRADED, STATUS_DOWN}


@dataclass
class ComponentHealth:
    """Tracks health for a single component."""

    status: str = STATUS_OK
    details: str = ""


@dataclass
class PipelineMetrics:
    """Aggregate metrics for the signal analysis pipeline."""

    cycles_run: int = 0
    avg_time_ms: float = 0.0
    last_cycle_ms: float = 0.0
    last_cycle_ts: float = 0.0  # unix epoch seconds


@dataclass
class WsPerSymbol:
    """WebSocket connection status per symbol."""

    connected: bool = False
    last_tick_ts: float = 0.0
    tick_count: int = 0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_response(
    status_code: int,
    status_text: str,
    body: str,
    *,
    cors: bool = True,
    content_type: str = "application/json",
) -> bytes:
    """Build a minimal HTTP/1.1 response byte-string."""
    encoded = body.encode("utf-8")
    headers = [
        f"HTTP/1.1 {status_code} {status_text}",
        f"Content-Type: {content_type}; charset=utf-8",
        f"Content-Length: {len(encoded)}",
        "Connection: close",
    ]
    if cors:
        headers.extend([
            "Access-Control-Allow-Origin: *",
            "Access-Control-Allow-Methods: GET, OPTIONS",
            "Access-Control-Allow-Headers: Content-Type",
        ])
    return _CRLF.join(h.encode("ascii") for h in headers) + _CRLF + _CRLF + encoded


def _json_response(data: dict, status: int = 200) -> bytes:
    text = json.dumps(data, default=str, indent=None, separators=(",", ":"))
    status_text = "OK" if status == 200 else ("Not Found" if status == 404 else "Internal Server Error")
    return _http_response(status, status_text, text)


def _parse_request(data: bytes) -> tuple[str, str, dict]:
    """Parse a raw HTTP request, returning (method, path, headers)."""
    head, _, _ = data.partition(_CRLF + _CRLF)
    lines = head.split(_CRLF)
    if not lines:
        return "GET", "/", {}
    method, raw_path, _ = lines[0].decode("utf-8", errors="replace").split(" ", 2)
    # Strip query params for path matching
    path = raw_path.split("?")[0].rstrip("/") or "/"
    headers: dict[str, str] = {}
    for line in lines[1:]:
        decoded = line.decode("utf-8", errors="replace")
        if ":" in decoded:
            k, _, v = decoded.partition(":")
            headers[k.strip().lower()] = v.strip()
    return method, path, headers


# ---------------------------------------------------------------------------
# HealthServer
# ---------------------------------------------------------------------------


class HealthServer:
    """Simple async HTTP health-check server with no external dependencies.

    Call ``start()`` from the main asyncio event loop (it creates a task).
    Call ``stop()`` during graceful shutdown.

    External modules update component health via ``set_health()``.
    """

    def __init__(
        self,
        port: int = 8080,
        data_dir: str = "/home/ssm-user/signalforge",
    ) -> None:
        """
        Args:
            port:  TCP port to bind the HTTP server on.
            data_dir:  Project root directory (used for resolving paths).
        """
        self.port = port
        self.data_dir = Path(data_dir)

        # ---- Component tracking ----
        self._components: dict[str, ComponentHealth] = {
            "pipeline":   ComponentHealth(STATUS_OK),
            "llm":        ComponentHealth(STATUS_OK),
            "database":   ComponentHealth(STATUS_OK),
        }
        self._ws_symbols: dict[str, WsPerSymbol] = {}

        # ---- Pipeline metrics ----
        self._pipeline_metrics = PipelineMetrics()

        # ---- Internal state ----
        self._start_time: float = time.time()
        self._server: Optional[asyncio.AbstractServer] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_health(
        self,
        component: str,
        status: str,
        details: str = "",
    ) -> None:
        """Update the health status of a tracked component.

        Args:
            component:  Component name (e.g. ``"pipeline"``, ``"websocket"``,
                        ``"llm"``, ``"database"``).
            status:     One of ``"ok"``, ``"degraded"``, ``"down"``.
            details:    Optional human-readable detail string.
        """
        if status not in _VALID_STATUSES:
            logger.warning(
                "[HealthServer] Invalid status '%s' for component '%s' — ignoring",
                status, component,
            )
            return

        if component == "websocket":
            # websocket status is tracked per-symbol; this call expects
            # details like "BTC/USDT:ok" or details to contain the symbol.
            # For simplicity we set the overall WS health in components.
            self._components.setdefault("websocket", ComponentHealth()).status = status
            if details:
                self._components["websocket"].details = details
            logger.info("[HealthServer] component=websocket status=%s details=%s", status, details)
            return

        self._components.setdefault(component, ComponentHealth()).status = status
        if details:
            self._components[component].details = details
        logger.info("[HealthServer] component=%s status=%s details=%s", component, status, details)

    def set_ws_symbol(self, symbol: str, connected: bool) -> None:
        """Update WebSocket connection status for a specific symbol."""
        ws = self._ws_symbols.setdefault(symbol, WsPerSymbol())
        ws.connected = connected
        if connected:
            ws.last_tick_ts = time.time()
            ws.tick_count += 1

    def set_pipeline_metrics(
        self,
        cycles_run: int,
        avg_time_ms: float,
        last_cycle_ms: float,
    ) -> None:
        """Update pipeline performance metrics.

        Called by the signal pipeline after each analysis cycle.
        """
        self._pipeline_metrics.cycles_run = cycles_run
        self._pipeline_metrics.avg_time_ms = avg_time_ms
        self._pipeline_metrics.last_cycle_ms = last_cycle_ms
        self._pipeline_metrics.last_cycle_ts = time.time()

    async def start(self) -> None:
        """Start the HTTP health-check server on the configured port.

        Registers itself as a background asyncio task.  Safe to call
        multiple times — subsequent calls are no-ops.
        """
        if self._running:
            logger.debug("[HealthServer] Already running, ignoring start()")
            return

        self._start_time = time.time()
        self._running = True

        self._server = await asyncio.start_server(
            self._handle_client,
            host="0.0.0.0",
            port=self.port,
            reuse_address=True,
            reuse_port=False,
            backlog=128,
            limit=_BUFFER_SIZE,
        )

        # Keep the server accept loop running as a background task
        self._task = asyncio.create_task(self._serve_forever())

        logger.info("[HealthServer] Listening on http://0.0.0.0:%d", self.port)

    async def stop(self) -> None:
        """Gracefully stop the HTTP health-check server."""
        if not self._running:
            return
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[HealthServer] Stopped.")

    # ------------------------------------------------------------------
    # Internal: server loop
    # ------------------------------------------------------------------

    async def _serve_forever(self) -> None:
        """Accept connections in a loop until the server is stopped."""
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    # ------------------------------------------------------------------
    # Internal: per-connection handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single HTTP connection."""
        peer = writer.get_extra_info("peername", "unknown")
        try:
            raw = await reader.read(_BUFFER_SIZE)
            if not raw:
                return
            method, path, headers = _parse_request(raw)

            # CORS preflight
            if method == "OPTIONS":
                response = _http_response(204, "No Content", "", cors=True)
                writer.write(response)
                await writer.drain()
                return

            # Route
            response = self._route(method, path)
            writer.write(response)
            await writer.drain()

            # Log
            user_agent = headers.get("user-agent", "-")
            logger.info(
                '[HealthServer] %s "%s %s" %s',
                peer, method, path, user_agent,
            )
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logger.exception("[HealthServer] Error handling request from %s", peer)
            try:
                writer.write(_json_response({"error": "internal error"}, 500))
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal: routing
    # ------------------------------------------------------------------

    def _route(self, method: str, path: str) -> bytes:
        """Dispatch an already-parsed request to the appropriate handler."""
        if method != "GET":
            return _http_response(405, "Method Not Allowed", json.dumps({"error": "method not allowed"}))

        if path == "/health":
            return self._handle_root_health()
        if path == "/health/pipeline":
            return self._handle_pipeline_health()
        if path == "/health/ws":
            return self._handle_ws_health()
        return _http_response(404, "Not Found", json.dumps({"error": "not found"}))

    # ------------------------------------------------------------------
    # Internal: endpoint handlers
    # ------------------------------------------------------------------

    def _handle_root_health(self) -> bytes:
        """GET /health — overall system health."""
        uptime = time.time() - self._start_time

        # Determine aggregate status from components
        components = {
            name: {"status": ch.status, "details": ch.details}
            for name, ch in self._components.items()
        }

        # Aggregate: "down" if any down, "degraded" if any degraded, else "ok"
        overall = STATUS_OK
        for ch in self._components.values():
            if ch.status == STATUS_DOWN:
                overall = STATUS_DOWN
                break
            if ch.status == STATUS_DEGRADED:
                overall = STATUS_DEGRADED

        # Add WebSocket symbols
        ws_status = STATUS_OK
        ws_detail = ""
        if self._ws_symbols:
            disconnected = [s for s, w in self._ws_symbols.items() if not w.connected]
            if disconnected:
                ws_status = STATUS_DEGRADED
                ws_detail = f"Disconnected: {', '.join(disconnected)}"
            components["websocket"] = {"status": ws_status, "details": ws_detail}
        else:
            # No symbols registered yet — report as ok (not yet connected)
            components["websocket"] = {"status": STATUS_OK, "details": "no symbols registered"}

        payload = {
            "status": overall,
            "uptime_seconds": round(uptime, 2),
            "components": components,
            "version": _VERSION,
        }
        return _json_response(payload)

    def _handle_pipeline_health(self) -> bytes:
        """GET /health/pipeline — pipeline-specific metrics."""
        m = self._pipeline_metrics
        payload = {
            "cycles_run": m.cycles_run,
            "avg_time_ms": round(m.avg_time_ms, 2),
            "last_cycle_ms": round(m.last_cycle_ms, 2),
            "last_cycle_ts": m.last_cycle_ts,
            "last_cycle_human": (
                time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(m.last_cycle_ts))
                if m.last_cycle_ts > 0 else "never"
            ),
        }
        return _json_response(payload)

    def _handle_ws_health(self) -> bytes:
        """GET /health/ws — WebSocket connection status per symbol."""
        symbols = {}
        for symbol, ws in self._ws_symbols.items():
            age = time.time() - ws.last_tick_ts if ws.last_tick_ts > 0 else -1
            symbols[symbol] = {
                "connected": ws.connected,
                "last_tick_age_s": round(age, 2) if age >= 0 else None,
                "total_ticks": ws.tick_count,
            }
        payload = {
            "symbols": symbols,
            "total_symbols": len(symbols),
        }
        return _json_response(payload)


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Run the health server standalone for testing/debugging.

    Usage::

        python monitoring/health_endpoint.py [PORT]

    Then visit http://localhost:8080/health in a browser or with curl.
    """
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    async def _demo():
        server = HealthServer(port=port)
        await server.start()

        # Simulate some component updates for demo purposes
        server.set_health("database", STATUS_OK)
        server.set_health("llm", STATUS_OK, "GPT-4o responding")
        server.set_ws_symbol("BTC/USDT", True)
        server.set_ws_symbol("ETH/USDT", True)
        server.set_pipeline_metrics(cycles_run=142, avg_time_ms=320.5, last_cycle_ms=287.1)

        logger.info("Demo server running on http://0.0.0.0:%d", port)
        logger.info("Try: curl http://localhost:%d/health", port)
        logger.info("     curl http://localhost:%d/health/pipeline", port)
        logger.info("     curl http://localhost:%d/health/ws", port)

        # Let it run until Ctrl+C
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await server.stop()

    try:
        asyncio.run(_demo())
    except KeyboardInterrupt:
        logger.info("Shutdown by user.")
