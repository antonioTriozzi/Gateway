"""
Remap Modbus dalla Web App (127.0.0.1, COMx) verso simulatore+mirror sul PC (lab Pi).
Solo PC_IP nel .env — stesso schema di resolve_mbus_port per M-Bus.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

_SOCKET_LOCAL = re.compile(r"^socket://(?:127\.0\.0\.1|localhost):(\d+)$", re.I)


def _pc_ip() -> str:
    return (os.getenv("PC_IP") or "").strip()


def _lab_remap_active() -> bool:
    return os.name != "nt" and bool(_pc_ip())


def resolve_modbus_tcp_host_port(host: str, port: int) -> tuple[str, int]:
    """Su Pi: 127.0.0.1/localhost dalla Web App -> PC_IP + porta mirror (default 502)."""
    h = (host or "127.0.0.1").strip()
    hl = h.lower()
    pc_ip = _pc_ip()
    if _lab_remap_active() and pc_ip and hl in ("127.0.0.1", "localhost", "0.0.0.0"):
        try:
            ext = int((os.getenv("MODBUS_TCP_PORT") or str(port)).strip())
        except (TypeError, ValueError):
            ext = int(port)
        log.info(
            "Modbus TCP: %s:%s su Pi -> %s:%s (PC_IP / mirror).",
            h,
            port,
            pc_ip,
            ext,
        )
        return pc_ip, ext
    return h, int(port)


def resolve_modbus_rtu_port(config_port: str | None) -> str:
    """Su Pi: COM3 o socket://127.0.0.1:… dalla Web App -> socket://PC_IP:9010."""
    port = (config_port or "").strip()
    if not port:
        return port
    pc_ip = _pc_ip()
    if not _lab_remap_active() or not pc_ip:
        return port
    m = _SOCKET_LOCAL.match(port)
    if m:
        tcp_port = m.group(1)
        url = f"socket://{pc_ip}:{tcp_port}"
        log.info("Modbus RTU: %s su Pi -> %s (PC_IP / mirror RTU).", port, url)
        return url
    if re.match(r"^COM\d+$", port, re.I):
        tcp_port = (os.getenv("MODBUS_RTU_TCP_PORT") or "9010").strip()
        url = f"socket://{pc_ip}:{tcp_port}"
        log.info(
            "Modbus RTU: porta Windows %s su Pi -> %s (PC_IP / mirror RTU).",
            port,
            url,
        )
        return url
    return port
