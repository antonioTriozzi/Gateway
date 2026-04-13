import asyncio
import json
import logging
from typing import Any, Dict, List

import httpx
from .data_buffer import DataBuffer

log = logging.getLogger(__name__)

class DataUploader:
    """
    Gestisce l'invio dei dati bufferizzati a un endpoint API remoto.
    Raggruppa i dati per dispositivo e li invia in batch.
    """
    def __init__(self, config: Dict[str, Any], buffer: DataBuffer):
        uploader_config = config.get('data_upload', {})
        self.api_url = uploader_config.get('url')
        self.upload_interval = uploader_config.get('upload_interval_seconds', 60)
        
        self.gateway_id = config.get('id_condominio')
        
        token = uploader_config.get('token')
        if not token:
            token = config.get('remote_config', {}).get('token')

        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        self.buffer = buffer
        self.is_running = False
        self._flush_lock = asyncio.Lock()

    async def flush_pending(self) -> int:
        """
        Invia tutti i record pendenti in un solo batch (thread-safe tra chiamate).
        Chiamare a fine ciclo lettura dispositivi, così non si invia solo il primo dispositivo
        (es. Modbus TCP) mentre gli altri sono ancora in lettura.
        """
        if not self.api_url or not self.gateway_id:
            return 0

        async with self._flush_lock:
            try:
                pending_data = self.buffer.get_pending_readings(limit=100)
                if not pending_data:
                    return 0

                log.info("Trovati %s record da inviare.", len(pending_data))

                payload = self._build_payload(pending_data)

                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(self.api_url, json=payload, headers=self.headers)
                    response.raise_for_status()

                successful_ids = [r["id"] for r in pending_data]
                self.buffer.delete_readings_batch(successful_ids)
                log.info("Inviati e cancellati %s record con successo.", len(successful_ids))
                return len(successful_ids)

            except httpx.HTTPStatusError as e:
                log.error(
                    "Errore HTTP durante l'upload: %s - %s",
                    e.response.status_code,
                    e.response.text,
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
            "data": payload_data
        }

    def stop(self):
        self.is_running = False
