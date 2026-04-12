import asyncio
import logging
import os
from typing import Any, Dict, List

from xknx.tools.group_communication import read_group_value

from modules.devices.base_device import BaseDevice
from modules.knx_gateway_pool import KnxGatewayHandle

log = logging.getLogger(__name__)


class KnxMeter(BaseDevice):
    """Lettura valori da indirizzi di gruppo KNX (tunneling IP)."""

    def __init__(self, config: Dict[str, Any], gateway_handle: KnxGatewayHandle):
        super().__init__(
            device_id=config.get("device_id"),
            name=config.get("name", f"Device_{config.get('device_id')}"),
        )
        self.config = config
        self.handle = gateway_handle
        self.enabled = config.get("enabled", True)

    async def _read_body(self) -> List[Dict[str, Any]]:
        if self.handle.is_unavailable:
            return []

        group_addresses = self.config.get("group_addresses") or {}
        if not group_addresses:
            log.warning("KNX %s: group_addresses vuoto.", self.name)
            return []

        results: List[Dict[str, Any]] = []
        try:
            ga_timeout = float(os.getenv("KNX_GROUP_READ_TIMEOUT_SECONDS", "8"))
        except (TypeError, ValueError):
            ga_timeout = 8.0

        async with self.handle.lock:
            await self.handle.ensure_started()
            if self.handle.is_unavailable:
                return []
            for measure_name, spec in group_addresses.items():
                if not isinstance(spec, dict):
                    continue
                addr = spec.get("address")
                if not addr:
                    continue
                dpt = spec.get("dpt") or "1.001"
                try:
                    val = await asyncio.wait_for(
                        read_group_value(self.handle.xknx, addr, dpt),
                        timeout=ga_timeout,
                    )
                    results.append(
                        {
                            "name": str(measure_name),
                            "value": val,
                            "unit": spec.get("unit") or "",
                        }
                    )
                    await asyncio.sleep(0.02)
                except asyncio.TimeoutError:
                    log.debug(
                        "KNX timeout lettura %s %s (>%ss)",
                        self.name,
                        addr,
                        ga_timeout,
                    )
                except Exception as e:
                    log.debug(
                        "KNX lettura %s %s fallita: %s", self.name, addr, e, exc_info=False
                    )

        if results:
            log.info("Lettura KNX da '%s': %s", self.name, results)
        return results

    async def read(self) -> List[Dict[str, Any]]:
        try:
            dev_timeout = float(os.getenv("DEVICE_READ_TIMEOUT_SECONDS", "60"))
        except (TypeError, ValueError):
            dev_timeout = 60.0
        try:
            return await asyncio.wait_for(self._read_body(), timeout=dev_timeout)
        except asyncio.TimeoutError:
            log.warning(
                "Timeout KNX (%ss) per dispositivo '%s'; ciclo prosegue.",
                dev_timeout,
                self.name,
            )
            return []
