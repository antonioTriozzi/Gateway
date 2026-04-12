import os
import copy
import requests
import json
from dotenv import load_dotenv
from typing import Dict, Any

REMOTE_CONFIG_CACHE_PATH = "remote_config.cache.json"


def normalize_remote_gateway_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adatta il JSON della web app ProgettoTesi (system_config.serial_bindings,
    polling_interval_seconds) allo schema atteso dal gateway (interfaces, poll_interval).
    """
    if not data or not isinstance(data, dict):
        return data
    out = copy.deepcopy(data)
    sc = out.get("system_config")
    if not isinstance(sc, dict):
        return out
    sc = dict(sc)
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
    load_dotenv()
    try:
        upload_interval = int(os.getenv("DATA_UPLOAD_INTERVAL_SECONDS", "30"))
    except (ValueError, TypeError):
        print("Attenzione: DATA_UPLOAD_INTERVAL_SECONDS non valido. Uso default (30).")
        upload_interval = 30

    config = {
        "id_condominio": os.getenv("ID_CONDOMINIO"),
        "remote_config": {
            "url": os.getenv("REMOTE_CONFIG_URL"),
            "token": os.getenv("REMOTE_CONFIG_TOKEN"),
        },
        "data_upload": {
            "url": os.getenv("DATA_UPLOAD_URL"),
            "upload_interval_seconds": upload_interval,
        }
    }

    if not config["id_condominio"] or config["id_condominio"] == "INSERIRE_ID_QUI":
        raise ValueError("Errore: ID_CONDOMINIO non è impostato nel file .env.")
    if not config["remote_config"]["url"]:
        raise ValueError("Errore: REMOTE_CONFIG_URL non è impostato nel file .env.")
    token = (config["remote_config"].get("token") or "").strip()
    if not token:
        raise ValueError(
            "Errore: REMOTE_CONFIG_TOKEN non è impostato. "
            "Ottieni un JWT con POST /app/api/auth/login (utente ADMIN) e incollalo nel .env."
        )
    config["remote_config"]["token"] = token
    if not config["data_upload"]["url"]:
        raise ValueError("Errore: DATA_UPLOAD_URL non è impostato nel file .env.")

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

