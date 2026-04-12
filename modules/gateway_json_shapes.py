"""
Formattazione JSON ProgettoTesi: i parametri operativi per protocollo stanno nella
*seconda parte* della riga dispositivo (`serial`, …) e in `drivers_definitions`,
non nei campi di riepilogo (`assets`, campi flat duplicati).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

# --- Riga devices_inventory (prima parte = metadati; connessione = serial) ---
_DEVICE_ROW_ALIASES = {
    "commProtocol": "comm_protocol",
    "modbusTransport": "modbus_transport",
    "driverRef": "driver_ref",
    "knxGatewayId": "knx_gateway_id",
    "assetId": "asset_id",
    "deviceId": "device_id",
    "buildingId": "building_id",
    "assetType": "asset_type",
    "primaryAddress": "primary_address",
    "secondaryAddress": "secondary_address",
    "pollingIntervalSeconds": "polling_interval_seconds",
}

# --- Oggetto serial (seconda parte: host, porte, baud, …) ---
_SERIAL_ALIASES = {
    "baudRate": "baud_rate",
    "stopBits": "stop_bits",
    "tcpPort": "tcp_port",
    "timeoutSeconds": "timeout_seconds",
}

# --- Template driver ---
_DRIVER_DEF_ALIASES = {
    "groupAddresses": "group_addresses",
    "targetMeasures": "target_measures",
}

# Ogni voce sotto group_addresses (seconda parte: address + dpt)
_GROUP_ADDRESS_ENTRY_ALIASES = {
    "groupAddress": "address",
    "GroupAddress": "address",
}


def apply_key_aliases(d: Dict[str, Any], aliases: Dict[str, str]) -> None:
    for old, new in aliases.items():
        if old not in d:
            continue
        if new in d and new != old:
            d.pop(old, None)
        else:
            d[new] = d.pop(old)


def _pick_serial_block(dev: Dict[str, Any]) -> Dict[str, Any]:
    """Preferisci il blocco annidato canonico; accetta sinonimi da export/API."""
    for key in (
        "serial",
        "serial_connection",
        "serialConnection",
        "connection",
        "comm_params",
        "commParams",
    ):
        v = dev.get(key)
        if isinstance(v, dict) and v:
            return copy.deepcopy(v)
    return {}


def _merge_serial_from_root_if_missing(serial: Dict[str, Any], dev: Dict[str, Any]) -> None:
    """
    Solo retrocompat: se `serial` non ha i campi di connessione, copia dalla root
    della riga (vecchi JSON con tutto in prima parte).
    """
    if serial:
        return
    for key in (
        "host",
        "tcp_port",
        "port",
        "baud_rate",
        "parity",
        "stop_bits",
        "timeout_seconds",
    ):
        if key in dev and dev.get(key) is not None:
            serial[key] = copy.deepcopy(dev[key])


def normalize_device_inventory_row(dev: Dict[str, Any]) -> Dict[str, Any]:
    """
    Allinea una riga `devices_inventory` al contratto gateway: metadati + `serial`
    con chiavi snake_case. I dati operativi del bus restano sotto `serial`.
    """
    d = copy.deepcopy(dev)
    apply_key_aliases(d, _DEVICE_ROW_ALIASES)

    serial = _pick_serial_block(d)
    apply_key_aliases(serial, _SERIAL_ALIASES)
    _merge_serial_from_root_if_missing(serial, d)

    # Un solo blocco connessione per il runtime
    d["serial"] = serial
    for k in (
        "serial_connection",
        "serialConnection",
        "connection",
        "comm_params",
        "commParams",
    ):
        d.pop(k, None)

    return d


def normalize_drivers_definitions(drivers: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(drivers, dict):
        return {}
    out: Dict[str, Any] = {}
    for ref, drv in drivers.items():
        if not isinstance(drv, dict):
            out[ref] = drv
            continue
        d = copy.deepcopy(drv)
        apply_key_aliases(d, _DRIVER_DEF_ALIASES)
        ga = d.get("group_addresses")
        if isinstance(ga, dict):
            for _name, spec in list(ga.items()):
                if isinstance(spec, dict):
                    apply_key_aliases(spec, _GROUP_ADDRESS_ENTRY_ALIASES)
        out[ref] = d
    return out


def normalize_devices_inventory_list(inv: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in inv:
        if isinstance(item, dict):
            rows.append(normalize_device_inventory_row(item))
    return rows
