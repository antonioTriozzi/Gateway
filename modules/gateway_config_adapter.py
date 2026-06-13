"""
Adatta il JSON gateway della web app ProgettoTesi (devices_inventory con
comm_protocol, modbus_transport, serial, driver_ref) al formato atteso dal runtime.
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any, Dict, List, Optional

from config import (
    coerce_devices_inventory,
    coerce_drivers_definitions,
    coerce_optional_bool,
)
from modules.gateway_json_shapes import (
    normalize_devices_inventory_list,
    normalize_drivers_definitions,
)

log = logging.getLogger(__name__)


def _sanitize_key(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(s))


def parse_knx_host_port(host_raw: Any, port_raw: Any) -> tuple[str, int]:
    """
    Host e porta KNX separati (come Modbus TCP).
    Se host è ``192.168.8.115:3672``, la porta nel host prevale sul campo port.
    """
    host = str(host_raw or "127.0.0.1").strip()
    try:
        default_port = int(port_raw or 3671)
    except (TypeError, ValueError):
        default_port = 3671
    if not host:
        host = "127.0.0.1"
    colon = host.rfind(":")
    if colon > 0 and colon < len(host) - 1:
        maybe_port = host[colon + 1:].strip()
        if maybe_port.isdigit():
            p = int(maybe_port)
            if 0 < p <= 65535:
                base = host[:colon].strip()
                return (base or "127.0.0.1", p)
    return host, default_port


def _is_web_style_inventory(inventory: List[Any]) -> bool:
    for d in inventory:
        if isinstance(d, dict) and (d.get("driver_ref") or d.get("comm_protocol")):
            return True
    return False


def apply_progettotesi_device_plumbing(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    - Risolve driver_ref → lookup in drivers_definitions.
    - Parametri di bus: solo dalla *seconda parte* del JSON (devices_inventory[].serial
      e driver in drivers_definitions), non da `assets` o campi flat duplicati.
    - Costruisce system_config.interfaces da ogni riga di devices_inventory.
    """
    out = copy.deepcopy(cfg)
    out["devices_inventory"] = normalize_devices_inventory_list(
        coerce_devices_inventory(out.get("devices_inventory"))
    )
    out["drivers_definitions"] = normalize_drivers_definitions(
        coerce_drivers_definitions(out.get("drivers_definitions"))
    )
    inv = out.get("devices_inventory") or []
    if not inv or not _is_web_style_inventory(inv):
        return out

    drivers = out.get("drivers_definitions") or {}
    sc = out.setdefault("system_config", {})
    knx_block = sc.get("knx") or {}
    default_knx_tunnel_tcp = coerce_optional_bool(knx_block.get("tunnel_tcp"))
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
            elif isinstance(default_gw, dict) and (default_gw.get("host") or default_gw.get("port")):
                g = default_gw
            elif serial.get("host"):
                # Seconda parte JSON: tunnel IP sul dispositivo se knx.gateways assente/incompleto
                try:
                    sp = int(serial.get("port") or 3671)
                except (TypeError, ValueError):
                    sp = 3671
                g = {"host": str(serial.get("host")).strip(), "port": sp}
            host, kport = parse_knx_host_port(g.get("host"), g.get("port"))
            tc_g = coerce_optional_bool(g.get("tunnel_tcp"))
            tunnel_tcp = tc_g if tc_g is not None else default_knx_tunnel_tcp
            iface = f"knx_{_sanitize_key(host)}_{kport}"
            knx_iface: Dict[str, Any] = {"transport": "knx", "host": host, "port": kport}
            if tunnel_tcp is not None:
                knx_iface["tunnel_tcp"] = tunnel_tcp
            if iface not in new_ifaces:
                new_ifaces[iface] = knx_iface
            elif tunnel_tcp is not None:
                prev = new_ifaces[iface]
                prev_tcp = coerce_optional_bool(prev.get("tunnel_tcp"))
                if prev_tcp is not None and prev_tcp != tunnel_tcp:
                    log.warning(
                        "KNX %s: tunnel_tcp conflitto %s vs %s (mantengo ultimo).",
                        iface,
                        prev_tcp,
                        tunnel_tcp,
                    )
                prev["tunnel_tcp"] = tunnel_tcp
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
