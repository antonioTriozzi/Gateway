import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from .base_device import BaseDevice
from modules.transport_registry import TransportRegistry

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
            device_id=config.get("device_id"),
            name=config.get("name", f"Device_{config.get('device_id')}"),
        )
        self.config = config
        self.client = client
        self.slave_id = config.get("primary_address")
        if self.slave_id is None:
            self.slave_id = config.get("slave_id")
        self.enabled = self.config.get("enabled", True)

        port = getattr(client, "port", None)
        if hasattr(client, "comm_params"):
            port = client.comm_params.port
        port = port or ""
        self._mbus_port = str(port)
        self._mbus_tlock = (
            TransportRegistry.mbus_thread_lock(self._mbus_port) if self._mbus_port else None
        )

        if self.slave_id is None:
            raise ValueError(
                f"Indirizzo ('slave_id') non specificato per il dispositivo M-Bus {self.name}."
            )

        log.info("Dispositivo M-Bus '%s' pronto all'indirizzo %s.", self.name, self.slave_id)

    def _read_all_sync(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        target_measures = self.config.get("target_measures", [])
        port = getattr(self.client, "port", None)
        baudrate = getattr(self.client, "baudrate", 2400)
        data = self._sync_read(port, baudrate)
        if data:
            for record in data.body.records:
                measure_name = record.description
                if not target_measures or measure_name in target_measures:
                    results.append(
                        {
                            "name": measure_name,
                            "value": record.value,
                            "unit": record.unit,
                        }
                    )
            if results:
                log.info("Lettura M-Bus da '%s': %s misure trovate.", self.name, len(results))
        return results

    def _thread_wrapped_read(self) -> List[Dict[str, Any]]:
        if self._mbus_tlock is not None:
            with self._mbus_tlock:
                return self._read_all_sync()
        return self._read_all_sync()

    async def read(self) -> List[Dict[str, Any]]:
        if not meterbus:
            log.error("Libreria 'pyMeterBus' non installata. Impossibile leggere dispositivo M-Bus.")
            return []

        try:
            timeout = float(os.environ.get("DEVICE_READ_TIMEOUT_SECONDS", "60"))
        except (TypeError, ValueError):
            timeout = 60.0

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._thread_wrapped_read),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "Timeout M-Bus (%ss) per '%s' su %s; il thread può ancora terminare in background.",
                timeout,
                self.name,
                self._mbus_port or "?",
            )
            return []
        except Exception as e:
            log.error("Errore durante la lettura M-Bus di %s: %s", self.name, e)
            return []

    def _sync_read(self, port: str, baudrate: int):
        try:
            meterbus.send_ping(port, self.slave_id)
            frame = meterbus.request_data(port, self.slave_id)
            if frame:
                return meterbus.load(frame)
        except Exception as e:
            log.debug("Lettura seriale M-Bus fallita: %s", e)
        return None
