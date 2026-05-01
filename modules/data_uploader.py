import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from .readings_json import middleware_consumo_numeric_value
from .data_buffer import DataBuffer

log = logging.getLogger(__name__)

# Stesso nome header del middleware (GatewayIngestTokenFilter).
INGEST_HEADER = "X-Gateway-Ingest-Token"


def _normalize_ingest_secret(raw: Optional[str]) -> str:
    """Allinea BOM / trattini Unicode a quanto fa {@code normalizeIngestToken} sul middleware."""
    if not raw:
        return ""
    t = raw.strip().replace("\ufeff", "")
    for ch in ("\u2011", "\u2010", "\u2212"):
        t = t.replace(ch, "-")
    t = t.replace("\u00ad", "")
    return t


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

    def __init__(self, config: Dict[str, Any], buffer: DataBuffer):
        uploader_config = config.get("data_upload") or {}
        self.api_url = _prefer_ipv4_localhost(
            (uploader_config.get("url") or "").strip()
        ) or None
        self.upload_interval = uploader_config.get("upload_interval_seconds", 60)
        self.upload_format = str(uploader_config.get("format") or "web").strip().lower()

        secret = _normalize_ingest_secret((uploader_config.get("gateway_ingest_secret") or "").strip())
        if not secret:
            secret = _normalize_ingest_secret((os.getenv("GATEWAY_INGEST_SECRET") or "").strip())
        self.gateway_ingest_secret = secret or None

        self.gateway_id = config.get("id_condominio")

        upload_tok = (uploader_config.get("token") or "").strip() or None
        remote_tok = (config.get("remote_config") or {}).get("token")
        remote_tok = (remote_tok or "").strip() if remote_tok else None
        web_bearer = upload_tok or remote_tok
        # JWT verso middleware: DATA_UPLOAD_TOKEN oppure stesso token admin web (REMOTE_CONFIG_TOKEN / .env).
        self._middleware_jwt = web_bearer

        log.info(
            "DataUploader: format=%s ingest_secret=%s middleware_jwt=%s url=%s",
            self.upload_format,
            "sì" if self.gateway_ingest_secret else "no",
            "sì" if self._middleware_jwt else "no",
            self.api_url or "(mancante)",
        )
        if self.gateway_ingest_secret:
            log.info(
                "DataUploader: lunghezza GATEWAY_INGEST_SECRET=%s (deve coincidere col middleware)",
                len(self.gateway_ingest_secret),
            )

        token = web_bearer
        self._web_headers: Dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        if self.upload_format == "middleware" and self._middleware_jwt and self.gateway_ingest_secret:
            log.info(
                "DataUploader: middleware con Bearer JWT + %s (il middleware accetta prima il segreto condiviso: "
                "nessun 401 se GATEWAY_INGEST_SECRET = app.gateway-ingest.secret).",
                INGEST_HEADER,
            )
        elif self.upload_format == "middleware" and self._middleware_jwt:
            log.info(
                "DataUploader: upload verso middleware con JWT Bearer (REMOTE_CONFIG_TOKEN o DATA_UPLOAD_TOKEN; "
                "sul middleware serve app.web-app.jwt.secret = jwt.secret della web, oppure JWT da .../auth/login :8081). "
                "In dev imposta anche GATEWAY_INGEST_SECRET=dev-gateway-ingest-secret per evitare 401 se il JWT non coincide.",
            )
        elif self.upload_format == "middleware" and self.gateway_ingest_secret:
            log.info(
                "DataUploader: upload verso middleware solo con header %s (nessun JWT: assente REMOTE_CONFIG_TOKEN/DATA_UPLOAD_TOKEN).",
                INGEST_HEADER,
            )
        elif self.upload_format == "middleware":
            log.warning(
                "DATA_UPLOAD_FORMAT=middleware: servono REMOTE_CONFIG_TOKEN (JWT web admin) o DATA_UPLOAD_TOKEN / GATEWAY_INGEST_SECRET."
            )

        self.buffer = buffer
        self.is_running = False
        self._flush_lock = asyncio.Lock()

    def _post_headers(self) -> Dict[str, str]:
        """
        Middleware: il server controlla prima `X-Gateway-Ingest-Token` (= GATEWAY_INGEST_SECRET), poi Bearer.
        Se entrambi sono impostati, inviamo entrambi: così l’ingest non dipende da JWT web/middleware allineati.
        Bearer: REMOTE_CONFIG_TOKEN o DATA_UPLOAD_TOKEN (JWT web admin se app.web-app.jwt.secret = jwt.secret web,
        oppure JWT :8081/auth/login). Senza Bearer, solo header ingest.
        Web: Bearer = REMOTE_CONFIG_TOKEN o DATA_UPLOAD_TOKEN come prima.
        """
        if self.upload_format == "middleware":
            h: Dict[str, str] = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if self.gateway_ingest_secret:
                h[INGEST_HEADER] = self.gateway_ingest_secret
            if self._middleware_jwt:
                h["Authorization"] = f"Bearer {self._middleware_jwt}"
            return h
        return dict(self._web_headers)

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
        if self.upload_format == "middleware" and not (self.gateway_ingest_secret or self._middleware_jwt):
            log.error(
                "Upload middleware: impostare REMOTE_CONFIG_TOKEN (JWT web), oppure DATA_UPLOAD_TOKEN, oppure GATEWAY_INGEST_SECRET."
            )
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
                        "Upload middleware 401: verificare GATEWAY_INGEST_SECRET = app.gateway-ingest.secret; "
                        "oppure Bearer (REMOTE_CONFIG_TOKEN admin web) con app.web-app.jwt.secret sul middleware = jwt.secret web; "
                        "oppure JWT da POST http://<middleware>:8081/auth/login. Dettaglio: %s | headers risposta: %s",
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
        Body atteso da testMiddleware: lista JSON di oggetti tipo ConsumoIngestItem
        (measure, value, unit, device_id, building_id, asset_name, client_mail, …).
        Il buffer salva già righe in questo formato (expand_readings_for_gateway_export).
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
            for row in readings:
                if isinstance(row, dict):
                    r = dict(row)
                    coerced = middleware_consumo_numeric_value(r.get("value"))
                    if coerced is None:
                        continue
                    r["value"] = coerced
                    items.append(r)
        return items

    def stop(self):
        self.is_running = False
