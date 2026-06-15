"""
Remap Modbus dalla Web App (127.0.0.1, COMx) verso simulatore+mirror sul PC (lab Pi).
Stesso schema di MBUS_SOCKET_URL / resolve_mbus_port.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)


def resolve_modbus_tcp_host_port(host: str, port: int) -> tuple[str, int]:
    """
    Su Pi/Linux: host 127.0.0.1/localhost dalla Web App -> PC_IP + porta mirror TCP.
    Override esplicito: MODBUS_TCP_URL=192.168.8.115:502
    """
    override = (os.getenv("MODBUS_TCP_URL") or "").strip()
    if override:
        raw = override.replace("socket://", "").strip()
        if ":" in raw:
            h, _, p = raw.rpartition(":")
            try:
                resolved = h.strip(), int(p.strip())
                log.info("Modbus TCP: override MODBUS_TCP_URL -> %s:%s", *resolved)
                return resolved
            except ValueError:
                pass
    h = (host or "127.0.0.1").strip()
    hl = h.lower()
    pc_ip = (os.getenv("PC_IP") or "").strip()
    if os.name != "nt" and pc_ip and hl in ("127.0.0.1", "localhost", "0.0.0.0"):
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
    """
    Su Pi/Linux: COM3 dalla Web App -> socket://PC_IP:9010 (bridge RTU sul PC).
    Override: MODBUS_RTU_SOCKET_URL=socket://192.168.8.115:9010
    """
    env_url = (os.getenv("MODBUS_RTU_SOCKET_URL") or "").strip()
    if env_url:
        if config_port and env_url != (config_port or "").strip():
            log.info(
                "Modbus RTU: %s (Web App) -> %s (MODBUS_RTU_SOCKET_URL).",
                config_port,
                env_url,
            )
        return env_url
    port = (config_port or "").strip()
    if not port:
        return port
    if os.name != "nt" and re.match(r"^COM\d+$", port, re.I):
        pc_ip = (os.getenv("PC_IP") or "").strip()
        tcp_port = (os.getenv("MODBUS_RTU_TCP_PORT") or "9010").strip()
        if pc_ip:
            url = f"socket://{pc_ip}:{tcp_port}"
            log.info(
                "Modbus RTU: porta Windows %s su Pi -> %s (PC_IP / mirror RTU).",
                port,
                url,
            )
            return url
    return port
