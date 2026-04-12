import asyncio
import logging
import httpx
from typing import Dict, Any, List
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

    async def run(self):
        """
        Task principale che gira in background per inviare i dati.
        """
        self.is_running = True
        log.info(f"DataUploader avviato. Intervallo di invio: {self.upload_interval} secondi.")

        if not self.api_url or not self.gateway_id:
            log.warning("URL di upload o ID Gateway non configurati. L'upload è disabilitato.")
            self.is_running = False
            return

        while self.is_running:
            try:
                await asyncio.sleep(self.upload_interval)
                
                pending_data = self.buffer.get_pending_readings(limit=100)
                if not pending_data:
                    continue

                log.info(f"Trovati {len(pending_data)} record da inviare.")
                
                payload = self._build_payload(pending_data)
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(self.api_url, json=payload, headers=self.headers)
                    response.raise_for_status()

                # Se l'invio ha successo, cancella i record dal buffer
                successful_ids = [r['id'] for r in pending_data]
                self.buffer.delete_readings_batch(successful_ids)
                log.info(f"Inviati e cancellati {len(successful_ids)} record con successo.")

            except httpx.HTTPStatusError as e:
                log.error(f"Errore HTTP durante l'upload: {e.response.status_code} - {e.response.text}")
            except httpx.RequestError as e:
                log.error(f"Errore di rete durante l'upload: {e}")
            except Exception as e:
                log.error(f"Errore imprevisto in DataUploader: {e}", exc_info=True)

    def _build_payload(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Costruisce il payload JSON strutturato per l'invio.
        """
        
        payload_data = []
        for record in records:
            payload_data.append({
                "device_id": record['device_name'],
                "timestamp": record['timestamp'],
                "readings": record['data']
            })

        return {
            "gateway_id": self.gateway_id,
            "data": payload_data
        }

    def stop(self):
        self.is_running = False
