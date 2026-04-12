import asyncio
import logging
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

    async def read(self) -> List[Dict[str, Any]]:
        group_addresses = self.config.get("group_addresses") or {}
        if not group_addresses:
            log.warning("KNX %s: group_addresses vuoto.", self.name)
            return []

        results: List[Dict[str, Any]] = []
        async with self.handle.lock:
            await self.handle.ensure_started()
            for measure_name, spec in group_addresses.items():
                if not isinstance(spec, dict):
                    continue
                addr = spec.get("address")
                if not addr:
                    continue
                dpt = spec.get("dpt") or "1.001"
                try:
                    val = await read_group_value(self.handle.xknx, addr, dpt)
                    results.append(
                        {
                            "name": str(measure_name),
                            "value": val,
                            "unit": spec.get("unit") or "",
                        }
                    )
                    await asyncio.sleep(0.02)
                except Exception as e:
                    log.debug(
                        "KNX lettura %s %s fallita: %s", self.name, addr, e, exc_info=False
                    )

        if results:
            log.info("Lettura KNX da '%s': %s", self.name, results)
        return results
