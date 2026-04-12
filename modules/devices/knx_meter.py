import asyncio
import logging
import os
from typing import Any, Dict, List

from xknx.core.value_reader import ValueReader
from xknx.dpt import DPTBase
from xknx.telegram.address import parse_device_group_address
from xknx.telegram.apci import GroupValueResponse, GroupValueWrite

from modules.devices.base_device import BaseDevice
from modules.knx_gateway_pool import KnxGatewayHandle
from modules.readings_json import (
    device_telemetry_document,
    expand_readings_for_gateway_export,
    format_telemetry_json,
    normalize_readings,
)

log = logging.getLogger(__name__)


async def _read_group_address(
    xknx: Any, group_address_str: str, dpt: str, timeout_sec: float
) -> Any:
    """
    Legge un GA con timeout coerente con KNX_GROUP_READ_TIMEOUT_SECONDS.
    (read_group_value di xknx usa ValueReader con timeout fisso 2s, troppo stretto.)
    """
    transcoder = DPTBase.get_dpt(dpt) if dpt else None
    ga = parse_device_group_address(group_address_str)
    t = max(1.0, min(float(timeout_sec), 120.0))
    reader = ValueReader(xknx, ga, timeout_in_seconds=t)
    response = await reader.read()
    if response is None:
        return None
    if not isinstance(response.payload, GroupValueWrite | GroupValueResponse):
        return None
    if transcoder is not None:
        return transcoder.from_knx(response.payload.value)
    return response.payload.value.value


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

    def emits_telemetry_json_from_driver(self) -> bool:
        return True

    async def _read_body(self) -> List[Dict[str, Any]]:
        ga_raw = self.config.get("group_addresses")
        group_addresses = ga_raw if isinstance(ga_raw, dict) else {}
        if not group_addresses:
            log.warning("KNX %s: group_addresses vuoto.", self.name)
            return []

        try:
            ga_timeout = float(os.getenv("KNX_GROUP_READ_TIMEOUT_SECONDS", "10"))
        except (TypeError, ValueError):
            ga_timeout = 10.0

        results: List[Dict[str, Any]] = []

        async with self.handle.lock:
            # Sempre ensure_started prima: altrimenti durante il backoff non si riprova mai
            # (il vecchio check is_unavailable in testa saltava del tutto la riconnessione).
            await self.handle.ensure_started()
            if not self.handle.is_connected:
                log.warning(
                    "KNX '%s': tunnel non connesso (%s:%s). "
                    "Se il gateway usa tunnel TCP: KNX_TUNNEL_TCP=true nel .env oppure "
                    "system_config.knx.tunnel_tcp / tunnelTcp (anche per singolo gateway). "
                    "Verifica firewall e simulatore.",
                    self.name,
                    self.handle.host,
                    self.handle.port,
                )
                for measure_name, spec in group_addresses.items():
                    if not isinstance(spec, dict) or not spec.get("address"):
                        continue
                    results.append(
                        {
                            "name": str(measure_name),
                            "value": None,
                            "unit": "" if spec.get("unit") is None else str(spec.get("unit", "")),
                        }
                    )
                return results

            for measure_name, spec in group_addresses.items():
                if not isinstance(spec, dict):
                    continue
                addr = spec.get("address")
                if not addr:
                    continue
                dpt = spec.get("dpt") or "1.001"
                unit = spec.get("unit") or ""
                val: Any = None
                try:
                    val = await asyncio.wait_for(
                        _read_group_address(
                            self.handle.xknx, str(addr), str(dpt), ga_timeout
                        ),
                        timeout=ga_timeout + 2.0,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "KNX '%s' timeout lettura GA %s (>%ss, timeout ValueReader allineato a env).",
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
                results.append(
                    {
                        "name": str(measure_name),
                        "value": val,
                        "unit": "" if unit is None else str(unit),
                    }
                )
                await asyncio.sleep(0.02)

        if any(r.get("value") is not None for r in results):
            log.debug("Lettura KNX da '%s': %s", self.name, results)
        elif group_addresses:
            log.warning(
                "KNX '%s': tutte le GA senza risposta (%s:%s). Controlla DPT, indirizzo e dispositivi sul bus.",
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
            result = await asyncio.wait_for(self._read_body(), timeout=dev_timeout)
        except asyncio.TimeoutError:
            log.warning(
                "Timeout KNX (%ss) per dispositivo '%s'; ciclo prosegue.",
                dev_timeout,
                self.name,
            )
            result = []
        safe = normalize_readings(result) if result else []
        export_rows = expand_readings_for_gateway_export(self, safe)
        doc = device_telemetry_document(
            self.device_id,
            self.name,
            self.telemetry_protocol(),
            export_rows,
        )
        log.info("TELEMETRY_JSON %s", format_telemetry_json(doc))
        return result
