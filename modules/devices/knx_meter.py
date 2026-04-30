import asyncio
import logging
import os
from typing import Any, Dict, List

from xknx.core.value_reader import ValueReader
from xknx.dpt import DPTBase
from xknx.telegram.address import parse_device_group_address
from xknx.telegram.apci import GroupValueResponse, GroupValueWrite

from modules.devices.base_device import BaseDevice
from modules.knx_dpt_export import measure_and_unit_for_dpt
from modules.knx_gateway_pool import KnxGatewayHandle
from modules.readings_json import (
    device_telemetry_document,
    expand_readings_for_gateway_export,
    format_telemetry_json,
    normalize_readings,
)

log = logging.getLogger(__name__)


def _coerce_knx_ga_spec(measure_key: str, spec: Any) -> Dict[str, Any] | None:
    """Normalizza una voce di group_addresses (dict, stringa legacy, chiave = GA)."""
    if isinstance(spec, str):
        a = spec.strip()
        if not a:
            return None
        return {"address": a}
    if not isinstance(spec, dict):
        return None
    addr = spec.get("address") or spec.get("ga") or spec.get("group_address")
    if not addr:
        mk = str(measure_key).strip()
        if "/" in mk:
            addr = mk
    if not addr:
        return None
    out = dict(spec)
    out["address"] = str(addr).strip()
    return out


def _knx_reading_labels(
    measure_key: str, spec: Dict[str, Any]
) -> tuple[str, str, str]:
    """(nome export, unit export, dpt per decode xknx)."""
    dpt_raw = spec.get("dpt")
    if isinstance(dpt_raw, str):
        dpt_s = dpt_raw.strip()
    elif dpt_raw is not None and dpt_raw != "":
        dpt_s = str(dpt_raw).strip()
    else:
        dpt_s = ""
    dpt_read = dpt_s if dpt_s else "1.001"
    exp_m, exp_u = measure_and_unit_for_dpt(dpt_s) if dpt_s else (None, None)
    cfg_u = spec.get("unit")
    unit_fallback = "" if cfg_u is None else str(cfg_u)
    name_out = exp_m if exp_m is not None else str(measure_key)
    unit_out = exp_u if exp_u is not None else unit_fallback
    return name_out, unit_out, dpt_read


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
            ga_timeout = float(os.getenv("KNX_GROUP_READ_TIMEOUT_SECONDS", "20"))
        except (TypeError, ValueError):
            ga_timeout = 20.0

        results: List[Dict[str, Any]] = []

        async with self.handle.lock:
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
                for measure_name, spec_raw in group_addresses.items():
                    spec = _coerce_knx_ga_spec(str(measure_name), spec_raw)
                    if not spec:
                        continue
                    n_out, u_out, _ = _knx_reading_labels(str(measure_name), spec)
                    results.append({"name": n_out, "value": None, "unit": u_out})
                return results

            for measure_name, spec_raw in group_addresses.items():
                spec = _coerce_knx_ga_spec(str(measure_name), spec_raw)
                if not spec:
                    continue
                addr = spec["address"]
                n_out, u_out, dpt_read = _knx_reading_labels(str(measure_name), spec)
                val: Any = None
                try:
                    val = await asyncio.wait_for(
                        _read_group_address(
                            self.handle.xknx, str(addr), str(dpt_read), ga_timeout
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
                results.append({"name": n_out, "value": val, "unit": u_out})
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
