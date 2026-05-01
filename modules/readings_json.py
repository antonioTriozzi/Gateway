"""
Normalizzazione letture interne (name, value, unit) e formato uscita gateway
(`expand_readings_for_gateway_export`) per Modbus/M-Bus vs KNX.

`protocol` su dispositivo: mbus | modbus_rtu | modbus_tcp | knx
(via telemetry_protocol() su MBusMeter, ModbusMeter, KnxMeter).
"""
from __future__ import annotations

import json
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List


def json_safe_value(val: Any) -> Any:
    """Converte valori KNX/M-Bus/enum in tipi serializzabili in JSON."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val
    if isinstance(val, str):
        return val
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, Enum):
        inner = val.value
        if isinstance(inner, bool):
            return inner
        if isinstance(inner, (int, float, str)) or inner is None:
            return inner
        return str(val)
    if hasattr(val, "value") and not isinstance(val, (bytes, bytearray, memoryview)):
        try:
            inner = val.value
            if isinstance(inner, bool):
                return inner
            if isinstance(inner, (int, float, str)) or inner is None:
                return inner
        except Exception:
            pass
    try:
        return bool(val)
    except Exception:
        return str(val)


def normalize_readings(readings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lista di misure nello stesso formato del Modbus, valori JSON-safe."""
    out: List[Dict[str, Any]] = []
    for r in readings:
        if not isinstance(r, dict):
            continue
        u = r.get("unit", "")
        out.append(
            {
                "name": str(r.get("name", "")),
                "value": json_safe_value(r.get("value")),
                "unit": "" if u is None else str(u),
            }
        )
    return out


def protocol_for_device(device: Any) -> str:
    """mbus | modbus_rtu | modbus_tcp | knx (da telemetry_protocol() su ogni dispositivo)."""
    fn = getattr(device, "telemetry_protocol", None)
    if callable(fn):
        return fn()
    return "unknown"


def device_telemetry_document(
    device_id: str,
    device_name: str,
    protocol: str,
    readings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "device_id": device_id,
        "device_name": device_name,
        "protocol": protocol,
        "readings": readings,
    }


def format_telemetry_json(doc: Dict[str, Any]) -> str:
    return json.dumps(doc, ensure_ascii=False)


def format_telemetry_log_line(doc: Dict[str, Any]) -> str:
    """Riga compatta per log INFO (il JSON completo va in DEBUG)."""
    readings = doc.get("readings")
    if not isinstance(readings, list):
        readings = []
    n = len(readings)
    with_val = sum(
        1 for r in readings if isinstance(r, dict) and r.get("value") is not None
    )
    did = doc.get("device_id") or "?"
    proto = doc.get("protocol") or "?"
    name = doc.get("device_name") or ""
    tail = f" ({name})" if name else ""
    return f"{did}{tail} [{proto}] {with_val}/{n} misure con valore"


def _cfg_ids(device: Any) -> tuple[Any, str, str]:
    """building_id, device_id, asset_name da config inventario + BaseDevice."""
    cfg = getattr(device, "config", None) or {}
    device_id = getattr(device, "device_id", None) or cfg.get("device_id") or ""
    asset_name = getattr(device, "name", None) or cfg.get("name") or ""
    return cfg.get("building_id"), str(device_id), str(asset_name)


def _coerce_building_id(val: Any) -> Any:
    """Intero JSON per `building_id` (middleware: Long). Accetta int, float intero, stringa numerica."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        try:
            i = int(val)
            return i if i == val or abs(val - i) < 1e-9 else None
        except (ValueError, OverflowError):
            return None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        try:
            return int(s, 10)
        except ValueError:
            try:
                return int(float(s.replace(",", ".")))
            except ValueError:
                return None
    return None


def middleware_consumo_numeric_value(val: Any) -> Any:
    """
    Valore JSON numerico per POST /api/consumi (middleware: `value` come Double).
    KNX/M-Bus possono produrre boolean — Jackson non deserializza true/false in Double senza adattatore.
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return 1.0 if val else 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        lower = s.lower()
        if lower in ("true", "on", "yes"):
            return 1.0
        if lower in ("false", "off", "no"):
            return 0.0
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return None
    if isinstance(val, Decimal):
        return float(val)
    return None


def _common_telemetry_context(device: Any) -> Dict[str, Any]:
    building_id, device_id, asset_name = _cfg_ids(device)
    cfg = getattr(device, "config", None) or {}
    return {
        "device_id": device_id,
        "building_id": _coerce_building_id(building_id),
        "asset_name": asset_name,
        "client_mail": cfg.get("client_mail"),
    }


def readings_for_buffer_export(export_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Buffer/upload: solo righe con valore presente (esclude measure con value null)."""
    out: List[Dict[str, Any]] = []
    for r in export_rows:
        if not isinstance(r, dict):
            continue
        if r.get("value") is None:
            continue
        out.append(r)
    return out


def expand_readings_for_gateway_export(
    device: Any, safe_readings: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Formato uscita per protocollo (come da specifica integrazione):
    - Modbus TCP/RTU e M-Bus: measure, value, unit, device_id, building_id,
      asset_name, client_mail
    - KNX: measure, value, unit, device_id, building_id, asset_name, client_mail
    """
    protocol = protocol_for_device(device)
    ctx = _common_telemetry_context(device)

    if protocol == "knx":
        out: List[Dict[str, Any]] = []
        for r in safe_readings:
            if not isinstance(r, dict):
                continue
            measure = str(r.get("name", ""))
            val = middleware_consumo_numeric_value(json_safe_value(r.get("value")))
            unit = "" if r.get("unit") is None else str(r.get("unit", ""))
            row: Dict[str, Any] = {
                "measure": measure,
                "value": val,
                "unit": unit,
            }
            row.update(ctx)
            out.append(row)
        return out

    out_mb: List[Dict[str, Any]] = []
    for r in safe_readings:
        if not isinstance(r, dict):
            continue
        row = {
            "measure": str(r.get("name", "")),
            "value": middleware_consumo_numeric_value(json_safe_value(r.get("value"))),
            "unit": "" if r.get("unit") is None else str(r.get("unit", "")),
        }
        row.update(ctx)
        out_mb.append(row)
    return out_mb
