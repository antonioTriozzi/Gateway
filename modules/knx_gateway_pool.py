"""
Un XKNX per ogni coppia (host, port) del gateway IP KNX; avvio/arresto centralizzati.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Tuple

from xknx import XKNX
from xknx.io import ConnectionConfig, ConnectionType

log = logging.getLogger(__name__)


class KnxGatewayHandle:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)
        self.lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.xknx = XKNX(
            connection_config=ConnectionConfig(
                connection_type=ConnectionType.TUNNELING,
                gateway_ip=self.host,
                gateway_port=self.port,
            )
        )
        self._started = False

    async def ensure_started(self) -> None:
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            await self.xknx.start()
            self._started = True
            log.info("KNX tunnel avviato verso %s:%s", self.host, self.port)

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
            await h.ensure_started()

    @classmethod
    async def stop_all(cls) -> None:
        for h in list(cls._handles.values()):
            try:
                await h.stop()
            except Exception as e:
                log.debug("Errore stop KNX: %s", e)
        cls._handles.clear()
