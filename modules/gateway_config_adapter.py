"""
Adatta il JSON gateway della web app ProgettoTesi (devices_inventory con
comm_protocol, modbus_transport, serial, driver_ref) al formato atteso dal runtime.
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def _sanitize_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(s))


def _is_web_style_inventory(inventory: List[Any]) -> bool:
    for d in inventory:
        if isinstance(d, dict) and (d.get("driver_ref") or d.get("comm_protocol")):
            return True
    return False


def apply_progettotesi_device_plumbing(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    - Risolve driver_ref → lookup in drivers_definitions.
    - Costruisce system_config.interfaces da ogni riga di devices_inventory
      (Modbus TCP/RTU, M-Bus, KNX), così coesistono più endpoint con lo stesso protocollo.
    """
    out = copy.deepcopy(cfg)
    inv = out.get("devices_inventory") or []
    if not inv or not _is_web_style_inventory(inv):
        return out

    drivers = out.get("drivers_definitions") or {}
    sc = out.setdefault("system_config", {})
    knx_block = sc.get("knx") or {}
    gateways = knx_block.get("gateways") or []
    knx_by_id: Dict[str, Dict[str, Any]] = {}
    for g in gateways:
        if isinstance(g, dict) and g.get("id"):
            knx_by_id[str(g["id"])] = g
    default_gw = knx_block.get("default_gateway")
    if isinstance(default_gw, dict) and default_gw.get("id"):
        knx_by_id.setdefault(str(default_gw["id"]), default_gw)

    new_ifaces: Dict[str, Dict[str, Any]] = {}

    for dev in inv:
        if not isinstance(dev, dict):
            continue
        if dev.get("interface"):
            continue

        proto = (dev.get("comm_protocol") or "").strip().upper()
        serial = dev.get("serial") or {}
        if not isinstance(serial, dict):
            serial = {}

        iface: Optional[str] = None

        if proto == "MODBUS":
            transport = (dev.get("modbus_transport") or "RTU").strip().upper()
            if transport == "TCP":
                host = (serial.get("host") or "127.0.0.1").strip()
                try:
                    tcp_port = int(serial.get("tcp_port") or 502)
                except (TypeError, ValueError):
                    tcp_port = 502
                iface = f"modbus_tcp_{_sanitize_key(host)}_{tcp_port}"
                new_ifaces.setdefault(
                    iface,
                    {"transport": "tcp", "host": host, "port": tcp_port},
                )
            else:
                port = (serial.get("port") or "").strip()
                if not port:
                    log.warning(
                        "Modbus RTU senza serial.port per device_id=%s: dispositivo ignorato.",
                        dev.get("device_id"),
                    )
                    continue
                try:
                    baud = int(serial.get("baud_rate") or 9600)
                except (TypeError, ValueError):
                    baud = 9600
                parity = _map_parity(serial.get("parity") or "N")
                try:
                    stop_bits = int(serial.get("stop_bits") or 1)
                except (TypeError, ValueError):
                    stop_bits = 1
                try:
                    timeout = float(serial.get("timeout_seconds") or 1.0)
                except (TypeError, ValueError):
                    timeout = 1.0
                iface = f"modbus_rtu_{_sanitize_key(port)}"
                new_ifaces.setdefault(
                    iface,
                    {
                        "transport": "rtu",
                        "port": port,
                        "baud_rate": baud,
                        "parity": parity,
                        "stop_bits": stop_bits,
                        "timeout": timeout,
                    },
                )

        elif proto == "MBUS":
            port = (serial.get("port") or "").strip()
            if not port:
                log.warning(
                    "M-Bus senza serial.port per device_id=%s: dispositivo ignorato.",
                    dev.get("device_id"),
                )
                continue
            try:
                baud = int(serial.get("baud_rate") or 2400)
            except (TypeError, ValueError):
                baud = 2400
            iface = f"mbus_{_sanitize_key(port)}"
            new_ifaces.setdefault(
                iface,
                {"transport": "mbus", "port": port, "baud_rate": baud},
            )

        elif proto == "KNX":
            gw_id = dev.get("knx_gateway_id")
            g: Dict[str, Any] = {}
            if gw_id is not None and str(gw_id) in knx_by_id:
                g = knx_by_id[str(gw_id)]
            elif isinstance(default_gw, dict):
                g = default_gw
            host = (g.get("host") or "127.0.0.1").strip()
            try:
                kport = int(g.get("port") or 3671)
            except (TypeError, ValueError):
                kport = 3671
            iface = f"knx_{_sanitize_key(host)}_{kport}"
            new_ifaces.setdefault(
                iface,
                {"transport": "knx", "host": host, "port": kport},
            )
        else:
            log.warning(
                "comm_protocol non supportato %r per device_id=%s",
                proto,
                dev.get("device_id"),
            )
            continue

        dev["interface"] = iface

    sc["interfaces"] = new_ifaces
    return out


def _map_parity(p: str) -> str:
    u = (p or "N").strip().upper()
    if u in ("E", "EVEN"):
        return "E"
    if u in ("O", "ODD"):
        return "O"
    return "N"


def resolve_driver(dev: Dict[str, Any], drivers: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ref = dev.get("driver_ref")
    if ref and ref in drivers:
        return drivers[ref]
    model = dev.get("model")
    if model and model in drivers:
        return drivers[model]
    return None
