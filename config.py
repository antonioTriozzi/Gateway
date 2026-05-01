import logging
import os
import copy
import requests
import json
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

REMOTE_CONFIG_CACHE_PATH = "remote_config.cache.json"

# Chiavi alternative (camelCase / export mobile) → schema snake_case del gateway Python
_ROOT_KEY_ALIASES = {
    "configVersion": "config_version",
    "buildingId": "building_id",
    "generatedAt": "generated_at",
    "devicesInventory": "devices_inventory",
    "driversDefinitions": "drivers_definitions",
    "systemConfig": "system_config",
    "assetGatewayPreferences": "asset_gateway_preferences",
    "uiShow": "ui_show",
    "clientId": "client_id",
    "clientMail": "client_mail",
}

_SYSTEM_CONFIG_KEY_ALIASES = {
    "serialBindings": "serial_bindings",
    "pollingIntervalSeconds": "polling_interval_seconds",
}

_KNX_BLOCK_ALIASES = {
    "defaultGateway": "default_gateway",
    "tunnelTcp": "tunnel_tcp",
}

_KNX_GATEWAY_ENTRY_ALIASES = {
    "tunnelTcp": "tunnel_tcp",
}


def coerce_optional_bool(v: Any) -> Optional[bool]:
    """None se assente/ambiguo; altrimenti bool da JSON/scalar/stringa."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(int(v))
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return None


def _apply_key_aliases(d: Dict[str, Any], aliases: Dict[str, str]) -> None:
    """Rinomina chiavi in-place; se esiste già la forma snake_case, scarta il duplicato camelCase."""
    for old, new in aliases.items():
        if old not in d:
            continue
        if new in d and new != old:
            d.pop(old, None)
        else:
            d[new] = d.pop(old)


def coerce_devices_inventory(inv: Any) -> List[Dict[str, Any]]:
    """
    Accetta array JSON o oggetto con chiavi numeriche (0,1,2…) come da alcuni export.
    """
    if inv is None:
        return []
    if isinstance(inv, list):
        return [x for x in inv if isinstance(x, dict)]
    if isinstance(inv, dict):
        out: List[Dict[str, Any]] = []

        def _sort_key(k: Any) -> tuple:
            s = str(k)
            if s.isdigit():
                return (0, int(s))
            return (1, s)

        for k in sorted(inv.keys(), key=_sort_key):
            v = inv[k]
            if isinstance(v, dict):
                out.append(v)
        return out
    return []


def coerce_drivers_definitions(dd: Any) -> Dict[str, Any]:
    if isinstance(dd, dict):
        return dd
    return {}


def _prefer_ipv4_localhost_url(url: str) -> str:
    """Evita ::1 su Windows se il server è solo su 127.0.0.1."""
    raw = (url or "").strip()
    if not raw:
        return raw
    p = urlparse(raw)
    if (p.hostname or "").lower() != "localhost":
        return raw
    port = f":{p.port}" if p.port else ""
    userinfo = ""
    if p.username or p.password:
        userinfo = (p.username or "") + (f":{p.password}" if p.password else "") + "@"
    new_netloc = f"{userinfo}127.0.0.1{port}"
    return urlunparse((p.scheme, new_netloc, p.path, p.params, p.query, p.fragment))


def resolve_web_login_url(remote_config_url: str) -> str:
    """
    Da REMOTE_CONFIG_URL (es. http://host/app/api/config) → http://host/app/api/auth/login
    Accetta anche .../config/{id} nel .env: usa il prefisso prima di {id}.
    """
    raw = (remote_config_url or "").strip().rstrip("/")
    if "{id}" in raw or "{building_id}" in raw:
        raw = raw.split("{")[0].rstrip("/")
    if raw.endswith("/config"):
        base = raw[: -len("/config")]
    elif "/app/api/config" in raw:
        base = raw.split("/app/api/config")[0].rstrip("/") + "/app/api"
    else:
        base = raw.rstrip("/")
    return _prefer_ipv4_localhost_url(f"{base}/auth/login")


def fetch_web_access_token(login_url: str, username: str, password: str, timeout: float = 12.0) -> str:
    """
    POST /app/api/auth/login (ProgettoTesi) → access_token Bearer per GET config e upload verso web/middleware.
    """
    url = _prefer_ipv4_localhost_url(login_url.strip())
    try:
        r = requests.post(
            url,
            json={"username": username.strip(), "password": password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("Risposta login non è un oggetto JSON")
        tok = (data.get("access_token") or data.get("accessToken") or "").strip()
        if not tok:
            raise ValueError("Risposta login senza access_token")
        return tok
    except requests.RequestException as e:
        raise ValueError(f"Login web non riuscito ({url}): {e}") from e


def normalize_remote_gateway_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    - Payload completo web app (assets, uiShow, …) o minimale GET /config: ignora chiavi extra.
    - camelCase (Jackson) → snake_case dove serve al runtime.
    - devices_inventory come dict indicizzato → lista di dispositivi.
    - system_config.serial_bindings → interfaces (se interfaces assente).
    - polling_interval_seconds → poll_interval.
    """
    if not data or not isinstance(data, dict):
        return data
    out = copy.deepcopy(data)
    _apply_key_aliases(out, _ROOT_KEY_ALIASES)

    out["devices_inventory"] = coerce_devices_inventory(out.get("devices_inventory"))
    out["drivers_definitions"] = coerce_drivers_definitions(out.get("drivers_definitions"))

    sc = out.get("system_config")
    if not isinstance(sc, dict):
        return out
    sc = dict(sc)
    _apply_key_aliases(sc, _SYSTEM_CONFIG_KEY_ALIASES)

    knx = sc.get("knx")
    if isinstance(knx, dict):
        knx = dict(knx)
        _apply_key_aliases(knx, _KNX_BLOCK_ALIASES)
        gateways = knx.get("gateways")
        if isinstance(gateways, list):
            normalized_gw: List[Dict[str, Any]] = []
            for item in gateways:
                if not isinstance(item, dict):
                    continue
                g = dict(item)
                _apply_key_aliases(g, _KNX_GATEWAY_ENTRY_ALIASES)
                normalized_gw.append(g)
            knx["gateways"] = normalized_gw
        sc["knx"] = knx

    if "interfaces" not in sc and "serial_bindings" in sc:
        bindings = sc.get("serial_bindings")
        if isinstance(bindings, dict):
            sc["interfaces"] = copy.deepcopy(bindings)
    if "poll_interval" not in sc:
        pis = sc.get("polling_interval_seconds")
        if pis is not None:
            try:
                sc["poll_interval"] = int(pis)
            except (TypeError, ValueError):
                pass
    out["system_config"] = sc
    return out

def load_config() -> Dict[str, Any]:
    """
    Carica la configurazione locale dal file .env.
    """
    _root = Path(__file__).resolve().parent
    load_dotenv(_root / ".env")
    try:
        upload_interval = int(os.getenv("DATA_UPLOAD_INTERVAL_SECONDS", "30"))
    except (ValueError, TypeError):
        print("Attenzione: DATA_UPLOAD_INTERVAL_SECONDS non valido. Uso default (30).")
        upload_interval = 30

    upload_format = (os.getenv("DATA_UPLOAD_FORMAT") or "web").strip().lower()
    upload_token = (os.getenv("DATA_UPLOAD_TOKEN") or "").strip()
    gateway_ingest_secret = (os.getenv("GATEWAY_INGEST_SECRET") or "").strip()
    # Allineato al default dev del middleware (JwtAuthenticationFilter) quando app.gateway-ingest.secret è vuoto.
    _default_local_ingest = "dev-gateway-ingest-secret"

    config = {
        "id_condominio": (os.getenv("ID_CONDOMINIO") or "").strip() or None,
        "remote_config": {
            "url": (os.getenv("REMOTE_CONFIG_URL") or "").strip() or None,
            "token": os.getenv("REMOTE_CONFIG_TOKEN"),
        },
        "data_upload": {
            "url": _prefer_ipv4_localhost_url(
                (os.getenv("DATA_UPLOAD_URL") or "").strip()
            )
            or None,
            # Con middleware: Bearer = REMOTE_CONFIG_TOKEN / DATA_UPLOAD_TOKEN; oppure (meglio in dev) anche GATEWAY_INGEST_SECRET
            # perché il server controlla prima X-Gateway-Ingest-Token (= app.gateway-ingest.secret).
            "token": upload_token or None,
            "upload_interval_seconds": upload_interval,
            # web = batch {gateway_id, data:[...]} verso ProgettoTesi telemetry;
            # middleware = array piatta ConsumoIngestItem verso POST .../api/consumi
            "format": upload_format,
            # Se valorizzato con middleware: header X-Gateway-Ingest-Token (stesso valore di app.gateway-ingest.secret)
            "gateway_ingest_secret": gateway_ingest_secret or None,
        },
    }

    if not config["id_condominio"] or config["id_condominio"] == "INSERIRE_ID_QUI":
        raise ValueError("Errore: ID_CONDOMINIO non è impostato nel file .env.")
    if not config["remote_config"]["url"]:
        raise ValueError("Errore: REMOTE_CONFIG_URL non è impostato nel file .env.")
    config["remote_config"]["url"] = _prefer_ipv4_localhost_url(config["remote_config"]["url"])
    token = (config["remote_config"].get("token") or "").strip()
    if not token:
        # Credenziali admin web (stesso login form / API della ProgettoTesi): il gateway ottiene il JWT all'avvio.
        login_user = (
            (os.getenv("WEB_ADMIN_EMAIL") or "").strip()
            or (os.getenv("GATEWAY_WEB_USERNAME") or "").strip()
        )
        login_pw = (os.getenv("WEB_ADMIN_PASSWORD") or os.getenv("GATEWAY_WEB_PASSWORD") or "").strip()
        login_url = (os.getenv("WEB_AUTH_LOGIN_URL") or "").strip()
        if login_user and login_pw:
            if not login_url:
                login_url = resolve_web_login_url(config["remote_config"]["url"] or "")
            _log.info(
                "REMOTE_CONFIG_TOKEN assente — ottengo JWT con login web (%s, utente=%s).",
                login_url,
                login_user,
            )
            print(f"Login web per token JWT → {login_url} (utente={login_user})")
            token = fetch_web_access_token(login_url, login_user, login_pw)
            _log.info("JWT web ottenuto (lunghezza %s).", len(token))
        else:
            raise ValueError(
                "Errore: nessun token per la config remota. Imposta nel .env uno tra: "
                "(1) REMOTE_CONFIG_TOKEN (JWT da POST /app/api/auth/login), oppure "
                "(2) WEB_ADMIN_EMAIL + WEB_ADMIN_PASSWORD (o GATEWAY_WEB_USERNAME + GATEWAY_WEB_PASSWORD) "
                "per scaricare il JWT all'avvio; opzionale WEB_AUTH_LOGIN_URL se l'URL di login non è deducibile da REMOTE_CONFIG_URL."
            )
    config["remote_config"]["token"] = token
    if not config["data_upload"]["url"]:
        raise ValueError("Errore: DATA_UPLOAD_URL non è impostato nel file .env.")

    if upload_format == "middleware" and not gateway_ingest_secret:
        u = (config["data_upload"]["url"] or "").lower()
        if "127.0.0.1" in u or "localhost" in u:
            config["data_upload"]["gateway_ingest_secret"] = _default_local_ingest
            print(
                f"Attenzione: GATEWAY_INGEST_SECRET assente — per POST verso host locale uso X-Gateway-Ingest-Token="
                f"'{_default_local_ingest}' (stesso default del middleware in dev). Aggiungi GATEWAY_INGEST_SECRET al .env "
                "se il middleware usa un segreto diverso."
            )

    return config

def fetch_remote_config(base_url: str, condominio_id: str, token: str) -> Dict[str, Any]:
    """
    Scarica la configurazione remota per un dato edificio (building id).
    Se l'URL contiene {id} o {building_id}, viene sostituito con ID_CONDOMINIO;
    altrimenti si usa GET {base_url}/{id} (es. .../app/api/config/1).
    """
    raw = (base_url or "").strip()
    if "{id}" in raw or "{building_id}" in raw:
        url = raw.replace("{id}", str(condominio_id)).replace("{building_id}", str(condominio_id))
    else:
        base = raw.rstrip("/")
        url = f"{base}/{condominio_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        print(f"Download configurazione remota da: {url}")
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Attenzione: Impossibile scaricare la configurazione remota. {e}")
        return None
    except json.JSONDecodeError:
        print("Errore: la risposta dal server non è un JSON valido.")
        return None

def save_config_to_cache(config_data: Dict[str, Any]) -> None:
    """
    Salva la configurazione fornita in un file di cache JSON.
    """
    try:
        with open(REMOTE_CONFIG_CACHE_PATH, "w") as f:
            json.dump(config_data, f, indent=4)
        print(f"Configurazione salvata correttamente nel file di cache: {REMOTE_CONFIG_CACHE_PATH}")
    except IOError as e:
        print(f"Errore durante il salvataggio della cache di configurazione: {e}")

def load_config_from_cache() -> Dict[str, Any]:
    """
    Carica la configurazione dal file di cache JSON.
    """
    if not os.path.exists(REMOTE_CONFIG_CACHE_PATH):
        print("Info: File di cache non trovato. È normale al primo avvio.")
        return None
        
    try:
        with open(REMOTE_CONFIG_CACHE_PATH, "r") as f:
            print(f"Caricamento configurazione dal file di cache: {REMOTE_CONFIG_CACHE_PATH}")
            return json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        print(f"Errore durante la lettura o il parsing del file di cache: {e}")
        return None

