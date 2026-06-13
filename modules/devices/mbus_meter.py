import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from .base_device import BaseDevice
from modules.readings_json import (
    device_telemetry_document,
    expand_readings_for_gateway_export,
    format_telemetry_json,
    format_telemetry_log_line,
    normalize_readings,
)
from modules.transport_registry import TransportRegistry

try:
    import meterbus
except ImportError:
    meterbus = None

log = logging.getLogger(__name__)


def resolve_mbus_port(config_port: str | None) -> str:
    """
    Porta effettiva M-Bus: MBUS_SOCKET_URL nel .env, oppure su Linux/Pi
    COM1/COM2 dalla Web App -> socket://PC_IP:9000 (sim+mirror sul PC).
    """
    env_url = (os.getenv("MBUS_SOCKET_URL") or "").strip()
    if env_url:
        return env_url
    port = (config_port or "").strip()
    if not port:
        return port
    if os.name != "nt" and re.match(r"^COM\d+$", port, re.I):
        pc_ip = (os.getenv("PC_IP") or "").strip()
        tcp_port = (os.getenv("MBUS_TCP_PORT") or "9000").strip()
        if pc_ip:
            url = f"socket://{pc_ip}:{tcp_port}"
            log.info(
                "M-Bus: porta Windows %s su Pi -> %s (PC_IP / mirror TCP).",
                port,
                url,
            )
            return url
    return port


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


def _mbus_raw_to_list(raw: Any) -> List[int]:
    if raw is None or raw is False:
        return []
    if isinstance(raw, (bytes, bytearray)):
        return list(raw)
    if isinstance(raw, list):
        return raw
    return list(raw)


def _mbus_load_frame(raw: Any) -> Any:
    if not raw or raw is False:
        return None
    try:
        return meterbus.load(_mbus_raw_to_list(raw))
    except Exception:
        return None


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
        port = resolve_mbus_port(port or "")
        self._mbus_port = str(port)
        self._mbus_tlock = (
            TransportRegistry.mbus_thread_lock(self._mbus_port) if self._mbus_port else None
        )

        log.info(
            "Dispositivo M-Bus '%s' pronto su %s, indirizzo slave %s.",
            self.name,
            self._mbus_port,
            self.slave_id,
        )

    def telemetry_protocol(self) -> str:
        return "mbus"

    def emits_telemetry_json_from_driver(self) -> bool:
        return True

    def _open_mbus_serial(self, port: str, baudrate: int):
        """Porta seriale allineata a config (parity da `serial`) e timeout da env (come Modbus RTU)."""
        import serial

        ser_cfg = self.config.get("serial")
        if not isinstance(ser_cfg, dict):
            ser_cfg = {}
        parity_raw = (ser_cfg.get("parity") or "E").strip().upper()
        if parity_raw in ("EVEN", "E"):
            par = serial.PARITY_EVEN
        elif parity_raw in ("ODD", "O"):
            par = serial.PARITY_ODD
        else:
            par = serial.PARITY_NONE

        try:
            raw_to = float(os.getenv("MBUS_SERIAL_TIMEOUT_SECONDS", "3.0"))
        except (TypeError, ValueError):
            raw_to = 3.0
        try:
            cap = float(os.getenv("MBUS_SERIAL_TIMEOUT_CAP_SECONDS", "12"))
        except (TypeError, ValueError):
            cap = 12.0
        if cap > 0:
            timeout = max(0.3, min(raw_to, cap))
        else:
            timeout = max(0.3, raw_to)
        if timeout < raw_to:
            log.debug(
                "M-Bus '%s': timeout seriale %.1fs limitato a %.1fs (MBUS_SERIAL_TIMEOUT_CAP_SECONDS).",
                self.name,
                raw_to,
                timeout,
            )

        port_s = (port or "").strip()
        if "://" in port_s:
            # TCP bridge (socket://192.168.8.115:9000): serial_for_url, non Serial(COM...)
            net_timeout = max(timeout, 5.0)
            log.debug(
                "M-Bus '%s': apertura %s (timeout %.1fs, baud ignorato su TCP).",
                self.name,
                port_s,
                net_timeout,
            )
            return serial.serial_for_url(port_s, timeout=net_timeout)

        return serial.Serial(
            port=port_s,
            baudrate=int(baudrate),
            parity=par,
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.EIGHTBITS,
            timeout=timeout,
        )

    def _placeholder_results(self) -> List[Dict[str, Any]]:
        targets = self.config.get("target_measures") or []
        if not targets:
            return []
        return [
            {"name": str(m), "value": None, "unit": ""}
            for m in targets
            if str(m).strip()
        ]

    def _read_all_sync(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        target_measures = self.config.get("target_measures") or []
        port = resolve_mbus_port(
            getattr(self.client, "port", None) or self._mbus_port or ""
        )
        baudrate = getattr(self.client, "baudrate", 2400)
        data = self._sync_read(port, baudrate)
        if not data:
            if target_measures:
                log.warning(
                    "M-Bus '%s': nessun telegramma su %s (slave %s). "
                    "Verifica sim+mirror sul PC, firewall TCP 9000, MBUS_SOCKET_URL sulla Pi.",
                    self.name,
                    port,
                    self.slave_id,
                )
            return self._placeholder_results()

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
        result: List[Dict[str, Any]] = []
        if not meterbus:
            log.error("Libreria 'pyMeterBus' non installata. Impossibile leggere dispositivo M-Bus.")
        else:
            try:
                timeout = float(os.environ.get("DEVICE_READ_TIMEOUT_SECONDS", "60"))
            except (TypeError, ValueError):
                timeout = 60.0
            try:
                result = await asyncio.wait_for(
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
            except Exception as e:
                log.error("Errore durante la lettura M-Bus di %s: %s", self.name, e)

        safe = normalize_readings(result) if result else []
        export_rows = expand_readings_for_gateway_export(self, safe)
        doc = device_telemetry_document(
            self.device_id,
            self.name,
            self.telemetry_protocol(),
            export_rows,
        )
        log.debug("TELEMETRY_JSON %s", format_telemetry_json(doc))
        log.info("TELEMETRY %s", format_telemetry_log_line(doc))
        return result

    def _sync_read(self, port: str, baudrate: int):
        """
        API pymeterbus / python-meterbus 0.8.x: pyserial + send_ping_frame / send_request_frame / recv_frame.
        Dopo il ping la risposta è spesso un ACK/corto: si svuota con recv ripetuti, poi REQ_UD2 e si
        attende un TelegramLong (dati variabili).
        """
        if not meterbus or not port:
            return None
        from meterbus.telegram_long import TelegramLong

        try:
            ser = self._open_mbus_serial(port, baudrate)
        except ImportError:
            log.error("M-Bus '%s': pyserial non installato.", self.name)
            return None
        except Exception as e:
            log.warning(
                "M-Bus '%s': impossibile aprire la porta seriale %s (%s).",
                self.name,
                port,
                e,
            )
            return None
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            time.sleep(0.02)

            skip_ping = (os.getenv("MBUS_SKIP_PING") or "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

            if not skip_ping:
                meterbus.send_ping_frame(ser, self.slave_id)
                for _ in range(4):
                    raw = meterbus.recv_frame(ser)
                    if not raw or raw is False:
                        break
                    parsed = _mbus_load_frame(raw)
                    if isinstance(parsed, TelegramLong):
                        return parsed

            meterbus.send_request_frame(ser, self.slave_id)
            for _ in range(20):
                raw = meterbus.recv_frame(ser)
                if not raw or raw is False:
                    continue
                parsed = _mbus_load_frame(raw)
                if isinstance(parsed, TelegramLong):
                    return parsed
            log.warning(
                "M-Bus '%s': REQ_UD senza RSP_UD su %s (slave %s).",
                self.name,
                port,
                self.slave_id,
            )
            return None
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
