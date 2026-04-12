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

    def telemetry_protocol(self) -> str:
        return "knx"

    async def _read_body(self) -> List[Dict[str, Any]]:
        if self.handle.is_unavailable:
            log.debug(
                "KNX '%s': tunnel non disponibile (%s:%s).",
                self.name,
                self.handle.host,
                self.handle.port,
            )
            return []

        ga_raw = self.config.get("group_addresses")
        group_addresses = ga_raw if isinstance(ga_raw, dict) else {}
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
                    log.warning(
                        "KNX '%s' timeout lettura GA %s (>%ss)",
                        self.name,
                        addr,
                        ga_timeout,
                    )
                except Exception as e:
                    log.warning(
                        "KNX '%s' lettura GA %s fallita: %s",
                        self.name,
                        addr,
                        e,
                    )

        if results:
            log.debug("Lettura KNX da '%s': %s", self.name, results)
        elif group_addresses:
            log.warning(
                "KNX '%s': nessun valore (gateway %s:%s). Verifica tunnel UDP vs TCP (KNX_TUNNEL_TCP), "
                "GA e DPT nel driver.",
                self.name,
                self.handle.host,
                self.handle.port,
            )
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
