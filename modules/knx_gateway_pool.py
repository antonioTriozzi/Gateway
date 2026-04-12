"""
Un XKNX per ogni coppia (host, port) del gateway IP KNX; avvio/arresto centralizzati.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Tuple

from xknx import XKNX
from xknx.exceptions.exception import CommunicationError
from xknx.io import ConnectionConfig, ConnectionType

log = logging.getLogger(__name__)


def _env_use_knx_tcp() -> bool:
    v = (os.getenv("KNX_TUNNEL_TCP") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


class KnxGatewayHandle:
    def __init__(self, host: str, port: int, *, use_tcp: bool | None = None):
        self.host = host
        self.port = int(port)
        self.lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        tcp = _env_use_knx_tcp() if use_tcp is None else use_tcp
        conn_type = ConnectionType.TUNNELING_TCP if tcp else ConnectionType.TUNNELING
        self.xknx = XKNX(
            connection_config=ConnectionConfig(
                connection_type=conn_type,
                gateway_ip=self.host,
                gateway_port=self.port,
            )
        )
        self._started = False
        self._connection_failed = False

    @property
    def is_unavailable(self) -> bool:
        return self._connection_failed

    async def ensure_started(self) -> None:
        if self._connection_failed:
            return
        if self._started:
            return
        async with self._start_lock:
            if self._connection_failed or self._started:
                return
            try:
                await self.xknx.start()
                self._started = True
                log.info("KNX tunnel avviato verso %s:%s", self.host, self.port)
            except CommunicationError as e:
                self._connection_failed = True
                log.warning(
                    "KNX non raggiungibile su %s:%s (%s). "
                    "Dispositivi KNX disabilitati per questa sessione. "
                    "Verifica che il gateway/simulatore sia avviato; se usa tunnel TCP imposta KNX_TUNNEL_TCP=true nel .env.",
                    self.host,
                    self.port,
                    e,
                )
            except Exception as e:
                self._connection_failed = True
                log.warning(
                    "Avvio KNX fallito verso %s:%s: %s",
                    self.host,
                    self.port,
                    e,
                )

    async def stop(self) -> None:
        if not self._started:
            return
        await self.xknx.stop()
        self._started = False


class KnxGatewayPool:
    _handles: Dict[Tuple[str, int], KnxGatewayHandle] = {}

    @classmethod
    def instance(cls, host: str, port: int) -> KnxGatewayHandle:
        key = (host.strip(), int(port))
        if key not in cls._handles:
            cls._handles[key] = KnxGatewayHandle(key[0], key[1])
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
