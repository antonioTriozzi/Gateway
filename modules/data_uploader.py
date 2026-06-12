import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from .readings_json import middleware_consumo_numeric_value
from .data_buffer import DataBuffer
from .web_auth import WebAppAuthClient, WebAppAuthError

log = logging.getLogger(__name__)


def _prefer_ipv4_localhost(api_url: str) -> str:
    """Su Windows `localhost` può risolvere in ::1 mentre il server è solo su 127.0.0.1."""
    raw = (api_url or "").strip()
    if not raw:
        return raw
    p = urlparse(raw)
    if (p.hostname or "").lower() != "localhost":
        return raw
    port = f":{p.port}" if p.port else ""
    userinfo = ""
    if p.username or p.password:
        userinfo = p.username or ""
        if p.password:
            userinfo += f":{p.password}"
        userinfo += "@"
    new_netloc = f"{userinfo}127.0.0.1{port}"
    return urlunparse((p.scheme, new_netloc, p.path, p.params, p.query, p.fragment))


class DataUploader:
    """
    Gestisce l'invio dei dati bufferizzati a un endpoint API remoto.
    Raggruppa i dati per dispositivo e li invia in batch.
    """

    def __init__(self, config: Dict[str, Any], buffer: DataBuffer, auth: Optional[WebAppAuthClient] = None):
        uploader_config = config.get("data_upload") or {}
        self.api_url = _prefer_ipv4_localhost(
            (uploader_config.get("url") or "").strip()
        ) or None
        self.upload_interval = uploader_config.get("upload_interval_seconds", 60)
        self.upload_format = str(uploader_config.get("format") or "web").strip().lower()

        self.gateway_id = config.get("id_condominio")

        # JWT M2M dinamico (client_credentials): mai statico, mai su file.
        self._auth = auth

        log.info(
            "DataUploader: format=%s auth_m2m=%s url=%s",
            self.upload_format,
            "sì" if (self._auth and self._auth.configured) else "no",
            self.api_url or "(mancante)",
        )

        self.buffer = buffer
        self.is_running = False
        self._flush_lock = asyncio.Lock()

    def _bearer_token(self) -> Optional[str]:
        """JWT M2M corrente (rinnovato automaticamente); None se auth non configurata o irraggiungibile."""
        if not (self._auth and self._auth.configured):
            return None
        try:
            return self._auth.get_token()
        except WebAppAuthError as e:
            log.error("Token M2M non disponibile per l'upload: %s", e)
            return None

    def _post_headers(self) -> Dict[str, str]:
        """
        Middleware e web: Bearer M2M (JWT client_credentials, ROLE_GATEWAY) dalla web app.
        """
        h: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        token = self._bearer_token()
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def flush_pending(self) -> int:
        """
        Invia tutti i record pendenti in un solo batch (thread-safe tra chiamate).
        Chiamare a fine ciclo lettura dispositivi, così non si invia solo il primo dispositivo
        (es. Modbus TCP) mentre gli altri sono ancora in lettura.
        """
        if not self.api_url:
            return 0
        if self.upload_format != "middleware" and not self.gateway_id:
            return 0
        if self.upload_format == "middleware":
            if not (self._auth and self._auth.configured):
                log.error(
                    "Upload middleware: credenziali M2M obbligatorie "
                    "(GATEWAY_CLIENT_ID + GATEWAY_CLIENT_SECRET nel .env)."
                )
                return 0
            if not self._bearer_token():
                return 0

        async with self._flush_lock:
            try:
                pending_data = self.buffer.get_pending_readings(limit=100)
                if not pending_data:
                    return 0

                log.info("Trovati %s record da inviare.", len(pending_data))

                if self.upload_format == "middleware":
                    payload = self._build_middleware_consumi_payload(pending_data)
                    if not payload:
                        log.warning("Formato middleware: nessuna misura nel batch, skip upload.")
                        return 0
                else:
                    payload = self._build_payload(pending_data)

                headers = self._post_headers()
                # trust_env=False: niente proxy/variabili che alterano richieste; follow_redirects=False: no POST che perdono header.
                async with httpx.AsyncClient(
                    timeout=30.0,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(self.api_url, json=payload, headers=headers)
                    if response.status_code == 401 and self._auth and self._auth.configured:
                        # Token M2M probabilmente scaduto: rinnova e riprova una sola volta.
                        log.warning("Upload 401: rinnovo del token M2M e nuovo tentativo.")
                        self._auth.invalidate()
                        headers = self._post_headers()
                        response = await client.post(self.api_url, json=payload, headers=headers)
                    response.raise_for_status()

                readings_saved = None
                warnings_list: List[Any] = []
                if self.upload_format == "middleware":
                    ct = (response.headers.get("content-type") or "").lower()
                    if "application/json" in ct:
                        try:
                            summary = response.json()
                            if isinstance(summary, dict):
                                readings_saved = summary.get("readingsSaved")
                                if readings_saved is None:
                                    readings_saved = summary.get("readings_saved")
                                w = summary.get("warnings")
                                warnings_list = list(w) if isinstance(w, list) else []
                                log.info(
                                    "Middleware ingest OK: readingsSaved=%s warnings=%s",
                                    readings_saved,
                                    warnings_list,
                                )
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass

                    n_items = len(payload) if isinstance(payload, list) else 0
                    rs_int: Optional[int] = None
                    if readings_saved is not None:
                        try:
                            rs_int = int(readings_saved)
                        except (TypeError, ValueError):
                            rs_int = None
                    if n_items > 0 and rs_int is not None and rs_int == 0:
                        log.error(
                            "Middleware ingest: salvate 0 letture su %s righe — buffer non svuotato "
                            "(verificare building_id/device_id/measure nel JSON). warnings=%s",
                            n_items,
                            warnings_list,
                        )
                        return 0

                successful_ids = [r["id"] for r in pending_data]
                self.buffer.delete_readings_batch(successful_ids)
                log.info("Inviati e cancellati %s record buffer con successo.", len(successful_ids))
                return len(successful_ids)

            except httpx.HTTPStatusError as e:
                detail = (e.response.text or "").strip()
                if e.response.status_code == 401 and self.upload_format == "middleware":
                    log.error(
                        "Upload middleware 401 (anche dopo rinnovo token): verificare GATEWAY_CLIENT_ID/"
                        "GATEWAY_CLIENT_SECRET e che app.web-app.jwt.secret sul middleware = jwt.secret web app. "
                        "Dettaglio: %s | headers risposta: %s",
                        detail[:500] if detail else "(vuoto)",
                        dict(e.response.headers),
                    )
                else:
                    log.error(
                        "Errore HTTP durante l'upload: %s - %s",
                        e.response.status_code,
                        detail[:500] if detail else "(vuoto)",
                    )
            except httpx.RequestError as e:
                log.error("Errore di rete durante l'upload: %s", e)
            except Exception as e:
                log.error("Errore imprevisto in DataUploader.flush_pending: %s", e, exc_info=True)
            return 0

    async def run(self) -> None:
        """
        Non usato dal gateway: l'upload è `flush_pending()` da main.py dopo tutte le letture.
        Mantenuto per compatibilità con eventuali script esterni.
        """
        self.is_running = True
        log.info(
            "DataUploader.run() è deprecato: invio batch da main dopo il ciclo dispositivi "
            "(upload_interval_seconds=%s ignorato per il timer).",
            self.upload_interval,
        )

    def _build_payload(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Costruisce il payload JSON strutturato per l'invio.
        """

        payload_data = []
        for record in records:
            raw = record["data"]
            if isinstance(raw, str):
                try:
                    readings = json.loads(raw)
                except json.JSONDecodeError:
                    readings = []
            else:
                readings = raw
            payload_data.append(
                {
                    "device_id": record["device_name"],
                    "timestamp": record["timestamp"],
                    "readings": readings,
                }
            )

        return {
            "gateway_id": self.gateway_id,
            "data": payload_data,
        }

    def _build_middleware_consumi_payload(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Payload verso il Middleware come da specifica architetturale: lista JSON di
        {dispositivo_id, valore_consumo, timestamp, edificio_id, measure, unit}.

        NESSUN dato anagrafico (email/nomi utenti): l'associazione utente↔consumo
        è delegata al Middleware tramite dispositivo_id/edificio_id.
        """
        items: List[Dict[str, Any]] = []
        for record in records:
            raw = record["data"]
            if isinstance(raw, str):
                try:
                    readings = json.loads(raw)
                except json.JSONDecodeError:
                    readings = []
            elif isinstance(raw, list):
                readings = raw
            else:
                readings = []
            timestamp = record.get("timestamp")
            for row in readings:
                if not isinstance(row, dict):
                    continue
                coerced = middleware_consumo_numeric_value(row.get("value"))
                if coerced is None:
                    continue
                items.append(
                    {
                        "dispositivo_id": row.get("device_id"),
                        "valore_consumo": coerced,
                        "timestamp": timestamp,
                        "edificio_id": row.get("building_id"),
                        "measure": row.get("measure"),
                        "unit": row.get("unit"),
                    }
                )
        return items

    def stop(self):
        self.is_running = False
