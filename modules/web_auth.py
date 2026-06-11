"""
Autenticazione M2M verso la Web App (flusso OAuth2 Client Credentials).

Il gateway si autentica con le sue credenziali di macchina (GATEWAY_CLIENT_ID +
GATEWAY_CLIENT_SECRET) su POST /app/api/auth/token e riceve un JWT a vita breve.
Il token vive SOLO nella memoria del processo (mai su file) e viene rinnovato
automaticamente alla scadenza o dopo un 401.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Margine di sicurezza: rinnova il token un po' prima della scadenza dichiarata.
_EXPIRY_MARGIN_SECONDS = 60.0


class WebAppAuthError(RuntimeError):
    """Autenticazione client_credentials verso la web app fallita."""


class WebAppAuthClient:
    """
    Mantiene in RAM il JWT M2M e lo rinnova quando serve.
    Thread-safe: usato sia dal fetch config (startup) sia dall'uploader.
    """

    def __init__(self, token_url: str, client_id: str, client_secret: str, timeout: float = 12.0):
        self._token_url = (token_url or "").strip()
        self._client_id = (client_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._timeout = timeout
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0  # epoch monotonic

    @property
    def configured(self) -> bool:
        return bool(self._token_url and self._client_id and self._client_secret)

    def get_token(self) -> str:
        """Ritorna un JWT valido, rinnovandolo se scaduto o mai richiesto."""
        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            return self._fetch_token_locked()

    def invalidate(self) -> None:
        """Forza il rinnovo alla prossima richiesta (es. dopo un 401)."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _fetch_token_locked(self) -> str:
        if not self.configured:
            raise WebAppAuthError(
                "Credenziali macchina mancanti: impostare GATEWAY_CLIENT_ID e GATEWAY_CLIENT_SECRET nel .env "
                "(da generare dalla web app: POST /app/api/buildings/{id}/gateway-credentials)."
            )
        try:
            r = requests.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            raise WebAppAuthError(f"Token client_credentials non ottenuto ({self._token_url}): {e}") from e
        except ValueError as e:
            raise WebAppAuthError(f"Risposta token non è JSON valido ({self._token_url})") from e

        token = (data.get("access_token") or "").strip() if isinstance(data, dict) else ""
        if not token:
            raise WebAppAuthError("Risposta token senza access_token")

        try:
            expires_in = float(data.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0

        self._token = token
        self._expires_at = time.monotonic() + max(30.0, expires_in - _EXPIRY_MARGIN_SECONDS)
        log.info(
            "JWT M2M ottenuto dalla web app (client_id=%s, validità %ss); il token resta solo in RAM.",
            self._client_id,
            int(expires_in),
        )
        return token
