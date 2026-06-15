"""
Caricamento e preparazione configurazione remota (Web App) per il gateway.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from config import (
    fetch_remote_config,
    load_config_from_cache,
    normalize_remote_gateway_config,
    save_config_to_cache,
)
from modules.gateway_config_adapter import apply_progettotesi_device_plumbing
from modules.web_auth import WebAppAuthClient, WebAppAuthError

log = logging.getLogger(__name__)


def prepare_remote_gateway_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    return apply_progettotesi_device_plumbing(normalize_remote_gateway_config(raw))


def config_revision(cfg: Dict[str, Any]) -> str:
    """Chiave per confronto cambi (config_version o generated_at)."""
    v = cfg.get("config_version")
    if v is not None and str(v).strip() != "":
        return f"v:{v}"
    ga = cfg.get("generated_at")
    if ga is not None and str(ga).strip() != "":
        return f"t:{ga}"
    return ""


def merge_local_remote(local_config: Dict[str, Any], remote_conf: Dict[str, Any]) -> Dict[str, Any]:
    return {**remote_conf, **local_config}


async def fetch_prepared_remote_config(
    web_auth: WebAppAuthClient,
    local_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Download + normalize + plumbing. None se auth o HTTP falliscono."""
    try:
        token = web_auth.get_token()
    except WebAppAuthError as e:
        log.warning("Refresh config: autenticazione M2M fallita: %s", e)
        return None
    raw = fetch_remote_config(
        base_url=local_config["remote_config"]["url"],
        condominio_id=local_config["id_condominio"],
        token=token,
    )
    if not raw:
        return None
    return prepare_remote_gateway_config(raw)


def load_remote_gateway_config_at_startup(
    web_auth: WebAppAuthClient,
    local_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    remote_conf: Optional[Dict[str, Any]] = None
    try:
        token = web_auth.get_token()
        raw = fetch_remote_config(
            base_url=local_config["remote_config"]["url"],
            condominio_id=local_config["id_condominio"],
            token=token,
        )
        if raw:
            remote_conf = prepare_remote_gateway_config(raw)
            log.info("Configurazione remota scaricata con successo.")
            save_config_to_cache(remote_conf)
    except WebAppAuthError as e:
        log.error("Autenticazione client_credentials fallita: %s", e)

    if not remote_conf:
        log.warning("Download fallito. Tentativo di caricamento dalla cache...")
        cached = load_config_from_cache()
        if cached:
            remote_conf = prepare_remote_gateway_config(cached)

    return remote_conf
