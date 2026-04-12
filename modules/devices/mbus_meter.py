import asyncio
import logging
from typing import Any, Dict, List, Optional

from .base_device import BaseDevice
from modules.serial_manager import SerialManager

try:
    import meterbus
except ImportError:
    meterbus = None

log = logging.getLogger(__name__)

class MBusMeter(BaseDevice):
    """
    Rappresenta un dispositivo che comunica tramite protocollo M-Bus.
    La logica di lettura si basa sul protocollo EN13757-3.
    """

    def __init__(self, config: Dict[str, Any], client: Any, lock: Optional[asyncio.Lock] = None):
        super().__init__(
            device_id=config.get('device_id'), 
            name=config.get('name', f"Device_{config.get('device_id')}")
        )
        self.config = config
        self.client = client
        self.slave_id = config.get("primary_address")
        if self.slave_id is None:
            self.slave_id = config.get("slave_id")
        self.enabled = self.config.get('enabled', True)

        port = getattr(client, "port", None)
        if hasattr(client, "comm_params"):
            port = client.comm_params.port

        self.lock = lock or (SerialManager.get_lock(f"serial:{port}") if port else asyncio.Lock())

        if self.slave_id is None:
            raise ValueError(f"Indirizzo ('slave_id') non specificato per il dispositivo M-Bus {self.name}.")

        log.info(f"Dispositivo M-Bus '{self.name}' pronto all'indirizzo {self.slave_id}.")

    async def read(self) -> List[Dict[str, Any]]:
        if not meterbus:
            log.error("Libreria 'pyMeterBus' non installata. Impossibile leggere dispositivo M-Bus.")
            return []

        results = []
        target_measures = self.config.get('target_measures', [])

        async with self.lock:
            try:
                port = getattr(self.client, 'port', None)
                baudrate = getattr(self.client, 'baudrate', 2400)
                
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, self._sync_read, port, baudrate)

                if data:
                    for record in data.body.records:
                        measure_name = record.description
                        if not target_measures or measure_name in target_measures:
                            results.append({
                                'name': measure_name,
                                'value': record.value,
                                'unit': record.unit
                            })
                    
                    if results:
                        log.info(f"Lettura M-Bus da '{self.name}': {len(results)} misure trovate.")
                
            except Exception as e:
                log.error(f"Errore durante la lettura M-Bus di {self.name}: {e}")

        return results

    def _sync_read(self, port: str, baudrate: int):
        try:
            meterbus.send_ping(port, self.slave_id)
            frame = meterbus.request_data(port, self.slave_id)
            if frame:
                return meterbus.load(frame)
        except Exception as e:
            log.debug(f"Lettura seriale M-Bus fallita: {e}")
        return None
