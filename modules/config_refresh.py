"""
Refresh periodico configurazione remota (Web App) durante la pausa del ciclo gateway.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import save_config_to_cache
from modules.gateway_config_loader import (
    config_revision,
    fetch_prepared_remote_config,
    merge_local_remote,
)
from modules.knx_gateway_pool import KnxGatewayPool
from modules.managers import DeviceManager
from modules.transport_registry import TransportRegistry
from modules.web_auth import WebAppAuthClient

log = logging.getLogger(__name__)


def config_refresh_interval_seconds() -> float:
    """0 = disabilitato. Default 300s."""
    raw = (os.getenv("CONFIG_REFRESH_INTERVAL_SECONDS") or "300").strip().lower()
    if raw in ("0", "off", "false", "no", "disabled"):
        return 0.0
    try:
        return max(60.0, float(raw))
    except (TypeError, ValueError):
        return 300.0


def cycle_timing_from_config(full_config: Dict[str, Any]) -> Tuple[float, float, float]:
    try:
        cycle_total = float(full_config.get("system_config", {}).get("poll_interval", 60))
    except (TypeError, ValueError):
        cycle_total = 60.0
    env_ct = (os.getenv("GATEWAY_CYCLE_TOTAL_SECONDS") or "").strip()
    if env_ct:
        try:
            cycle_total = float(env_ct)
        except (TypeError, ValueError):
            pass
    cycle_total = max(1.0, cycle_total)

    env_rp = (os.getenv("GATEWAY_READ_PHASE_SECONDS") or "").strip()
    if env_rp:
        try:
            read_phase = float(env_rp)
        except (TypeError, ValueError):
            read_phase = cycle_total / 2.0
    else:
        read_phase = cycle_total / 2.0
    read_phase = max(0.0, min(read_phase, cycle_total))
    upload_phase = max(0.0, cycle_total - read_phase)
    return cycle_total, read_phase, upload_phase


@dataclass
class GatewayRuntimeState:
    full_config: Dict[str, Any]
    devices: List[Any]
    revision: str
    cycle_total: float
    read_phase: float
    upload_phase: float
    last_refresh_monotonic: float


async def reload_gateway_runtime(
    remote_conf: Dict[str, Any],
    local_config: Dict[str, Any],
    prev_revision: str,
) -> GatewayRuntimeState:
    save_config_to_cache(remote_conf)
    new_revision = config_revision(remote_conf)
    full_config = merge_local_remote(local_config, remote_conf)
    cycle_total, read_phase, upload_phase = cycle_timing_from_config(full_config)

    await KnxGatewayPool.stop_all()
    TransportRegistry.close_all_modbus()

    devices = DeviceManager.create_devices(full_config)
    await KnxGatewayPool.start_all()

    old_label = prev_revision or "?"
    log.info(
        "Config remota aggiornata (%s -> %s): %d dispositivi, ciclo %.1fs.",
        old_label,
        new_revision or "?",
        len(devices),
        cycle_total,
    )

    return GatewayRuntimeState(
        full_config=full_config,
        devices=devices,
        revision=new_revision,
        cycle_total=cycle_total,
        read_phase=read_phase,
        upload_phase=upload_phase,
        last_refresh_monotonic=time.monotonic(),
    )


async def maybe_refresh_gateway_config(
    state: GatewayRuntimeState,
    web_auth: WebAppAuthClient,
    local_config: Dict[str, Any],
    interval: float,
) -> GatewayRuntimeState:
    """
    Durante la pausa del ciclo: fetch config, reload solo se config_version/generated_at cambia.
    Se il fetch fallisce, mantiene la config operativa attuale.
    """
    if interval <= 0:
        return state

    now = time.monotonic()
    if now - state.last_refresh_monotonic < interval:
        return state

    state.last_refresh_monotonic = now
    log.info("Refresh configurazione remota (intervallo %.0fs)...", interval)

    prepared = await fetch_prepared_remote_config(web_auth, local_config)
    if not prepared:
        log.warning("Refresh config: download fallito; continuo con config attuale.")
        return state

    new_revision = config_revision(prepared)
    if new_revision and new_revision == state.revision:
        log.info("Refresh config: nessun cambiamento (%s).", new_revision)
        return state

    if not new_revision and state.revision:
        log.info(
            "Refresh config: revisione nuova assente nel JSON; applico reload per sicurezza."
        )

    try:
        return await reload_gateway_runtime(prepared, local_config, state.revision)
    except Exception as e:
        log.error("Refresh config: reload fallito (%s); continuo con config attuale.", e)
        return state
