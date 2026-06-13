"""
Un XKNX per ogni coppia (host, port) del gateway IP KNX; avvio/arresto centralizzati.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Tuple

from xknx import XKNX
from xknx.exceptions.exception import CommunicationError
from xknx.io import ConnectionConfig, ConnectionType

log = logging.getLogger(__name__)


def _env_use_knx_tcp() -> bool:
    v = (os.getenv("KNX_TUNNEL_TCP") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _knx_route_back(host: str) -> bool:
    """
  Relay/mirror (Pi -> PC:3672): il mirror corregge CONNECT_RESPONSE con route-back;
  xknx può usare route_back=False (KV accetta CONNECT con IP reale della Pi).
  Con route_back=True KV rifiuta spesso il CONNECT. Default OFF; override .env.
    """
    v = (os.getenv("KNX_ROUTE_BACK") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _knx_reconnect_interval_seconds() -> float:
    try:
        return max(5.0, float(os.getenv("KNX_RECONNECT_INTERVAL_SECONDS", "60")))
    except (TypeError, ValueError):
        return 60.0


class KnxGatewayHandle:
    def __init__(self, host: str, port: int, *, use_tcp: bool | None = None):
        self.host = host
        self.port = int(port)
        self.lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        tcp = _env_use_knx_tcp() if use_tcp is None else bool(use_tcp)
        self._use_tcp = tcp
        self._route_back = _knx_route_back(self.host)
        conn_type = ConnectionType.TUNNELING_TCP if tcp else ConnectionType.TUNNELING
        self.xknx = XKNX(
            connection_config=ConnectionConfig(
                connection_type=conn_type,
                gateway_ip=self.host,
                gateway_port=self.port,
                route_back=self._route_back,
            )
        )
        self._started = False
        self._connection_failed = False
        self._next_retry_at: float | None = None

    @property
    def is_unavailable(self) -> bool:
        """True durante il backoff dopo un fallimento (non più blocco permanente)."""
        if self._started:
            return False
        if not self._connection_failed:
            return False
        if self._next_retry_at is None:
            return True
        return time.monotonic() < self._next_retry_at

    @property
    def is_connected(self) -> bool:
        """Tunnel KNX avviato con successo."""
        return self._started

    async def ensure_started(self) -> None:
        if self._started:
            return
        now = time.monotonic()
        if self._connection_failed:
            if self._next_retry_at is not None and now < self._next_retry_at:
                return
            # Backoff scaduto: nuovo tentativo (come riconnessione Modbus dopo guasto)
            self._connection_failed = False
            self._next_retry_at = None
            log.info(
                "KNX nuovo tentativo connessione verso %s:%s (tunnel %s).",
                self.host,
                self.port,
                "TCP" if self._use_tcp else "UDP",
            )

        async with self._start_lock:
            if self._started:
                return
            if self._connection_failed and self._next_retry_at is not None:
                if time.monotonic() < self._next_retry_at:
                    return
            try:
                await self.xknx.start()
                self._started = True
                self._connection_failed = False
                self._next_retry_at = None
                log.info("KNX tunnel avviato verso %s:%s", self.host, self.port)
                if not self._use_tcp and self._route_back:
                    log.info(
                        "KNX UDP route_back attivo (relay/NAT verso %s:%s).",
                        self.host,
                        self.port,
                    )
            except CommunicationError as e:
                self._connection_failed = True
                self._next_retry_at = time.monotonic() + _knx_reconnect_interval_seconds()
                log.warning(
                    "KNX non raggiungibile su %s:%s (%s). Riprovo tra %.0fs "
                    "(KNX_RECONNECT_INTERVAL_SECONDS). Tunnel attuale: %s. Se serve TCP: "
                    "KNX_TUNNEL_TCP=true nel .env oppure system_config.knx.tunnel_tcp (o per gateway).",
                    self.host,
                    self.port,
                    e,
                    _knx_reconnect_interval_seconds(),
                    "TCP" if self._use_tcp else "UDP",
                )
            except Exception as e:
                self._connection_failed = True
                self._next_retry_at = time.monotonic() + _knx_reconnect_interval_seconds()
                log.warning(
                    "Avvio KNX fallito verso %s:%s: %s. Riprovo tra %.0fs.",
                    self.host,
                    self.port,
                    e,
                    _knx_reconnect_interval_seconds(),
                )

    async def stop(self) -> None:
        try:
            if self.xknx.started.is_set():
                await self.xknx.stop()
        except Exception as e:
            log.debug("KNX stop: %s", e)
        self._started = False
        self._connection_failed = False
        self._next_retry_at = None


class KnxGatewayPool:
    _handles: Dict[Tuple[str, int, bool], KnxGatewayHandle] = {}

    @classmethod
    def instance(
        cls, host: str, port: int, *, use_tcp: bool | None = None
    ) -> KnxGatewayHandle:
        tcp = _env_use_knx_tcp() if use_tcp is None else bool(use_tcp)
        key = (host.strip(), int(port), tcp)
        if key not in cls._handles:
            cls._handles[key] = KnxGatewayHandle(key[0], key[1], use_tcp=tcp)
        return cls._handles[key]

    @classmethod
    async def start_all(cls) -> None:
        for h in list(cls._handles.values()):
            try:
                await h.ensure_started()
            except Exception as e:
                log.warning("KNX start_all: errore imprevisto: %s", e)

    @classmethod
    async def stop_all(cls) -> None:
        for h in list(cls._handles.values()):
            try:
                await h.stop()
            except Exception as e:
                log.debug("Errore stop KNX: %s", e)
        cls._handles.clear()
