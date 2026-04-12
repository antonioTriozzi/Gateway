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


def _mbus_description_matches_targets(label: str, targets: List[Any]) -> bool:
    """Confronto case-insensitive su etichetta misura (tipo/unit dal telegramma)."""
    if not targets:
        return True
    d = (label or "").strip().lower()
    if not d:
        return False
    for t in targets:
        ts = str(t).strip().lower()
        if not ts:
            continue
        if d == ts or ts in d or d in ts:
            return True
    return False


def _mbus_record_display_name(record: Any) -> str:
    """python-meterbus (0.8.x): niente .description; usare `interpreted` o unità."""
    try:
        d = record.interpreted
        if isinstance(d, dict):
            typ = str(d.get("type") or "")
            if "." in typ:
                typ = typ.split(".")[-1]
            typ = typ.replace("_", " ").strip()
            if typ:
                return typ
            u = str(d.get("unit") or "").strip()
            if u:
                return u
    except Exception:
        pass
    return "measure"


def _mbus_data_records(data: Any) -> List[Any]:
    if data is None:
        return []
    rec = getattr(data, "records", None)
    if rec is not None:
        return list(rec)
    body = getattr(data, "body", None)
    if body is not None:
        payload = getattr(body, "bodyPayload", None)
        if payload is not None:
            pr = getattr(payload, "records", None)
            if pr is not None:
                return list(pr)
    return []


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
        raw_addr = config.get("primary_address")
        if raw_addr is None:
            raw_addr = config.get("slave_id")
        if raw_addr is None:
            raise ValueError(
                f"Indirizzo primario non specificato per il dispositivo M-Bus {self.name}."
            )
        try:
            self.slave_id = int(raw_addr)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Indirizzo M-Bus non numerico per {self.name}: {raw_addr!r}"
            ) from e
        self.enabled = self.config.get("enabled", True)

        port = getattr(client, "port", None)
        if hasattr(client, "comm_params"):
            port = client.comm_params.port
        port = port or ""
        self._mbus_port = str(port)
        self._mbus_tlock = (
            TransportRegistry.mbus_thread_lock(self._mbus_port) if self._mbus_port else None
        )

        log.info("Dispositivo M-Bus '%s' pronto all'indirizzo %s.", self.name, self.slave_id)

    def telemetry_protocol(self) -> str:
        return "mbus"

    def _read_all_sync(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        target_measures = self.config.get("target_measures") or []
        port = getattr(self.client, "port", None)
        baudrate = getattr(self.client, "baudrate", 2400)
        data = self._sync_read(port, baudrate)
        if not data:
            return results

        records = _mbus_data_records(data)

        def append_record(record: Any) -> None:
            label = _mbus_record_display_name(record)
            try:
                val = record.value
            except Exception:
                val = None
            try:
                unit = record.unit
            except Exception:
                unit = ""
            results.append(
                {
                    "name": label,
                    "value": val,
                    "unit": "" if unit is None else str(unit),
                }
            )

        for record in records:
            label = _mbus_record_display_name(record)
            if _mbus_description_matches_targets(label, target_measures):
                append_record(record)

        if not results and records and target_measures:
            log.warning(
                "M-Bus '%s': target_measures %s non coincide con le etichette del contatore; "
                "invio tutte le %s misure decodificate (allinea i nomi nel driver o svuota target_measures).",
                self.name,
                target_measures,
                len(records),
            )
            for record in records:
                append_record(record)
        elif results:
            log.debug("Lettura M-Bus da '%s': %s misure trovate.", self.name, len(results))
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
        """
        API pymeterbus / python-meterbus 0.8.x: pyserial + send_ping_frame / send_request_frame / recv_frame.
        (Le vecchie send_ping/request_data non esistono in questa versione.)
        """
        if not meterbus or not port:
            return None
        try:
            import serial
        except ImportError:
            log.error("M-Bus '%s': pyserial non installato.", self.name)
            return None
        try:
            ser = serial.Serial(
                port=port,
                baudrate=int(baudrate),
                parity=serial.PARITY_EVEN,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=2.0,
            )
        except Exception as e:
            log.warning(
                "M-Bus '%s': impossibile aprire la porta seriale %s (%s).",
                self.name,
                port,
                e,
            )
            return None
        try:
            meterbus.send_ping_frame(ser, self.slave_id)
            meterbus.send_request_frame(ser, self.slave_id)
            raw = meterbus.recv_frame(ser)
            if not raw or raw is False:
                return None
            return meterbus.load(raw)
        except Exception as e:
            log.warning(
                "M-Bus '%s' su %s indirizzo %s: lettura fallita (%s). Porta libera, baud e cablaggio?",
                self.name,
                port,
                self.slave_id,
                e,
            )
            return None
        finally:
            try:
                ser.close()
            except Exception:
                pass
