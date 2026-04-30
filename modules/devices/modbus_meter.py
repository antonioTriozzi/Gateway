import asyncio
import logging
import os
import struct
import time
from typing import Any, Dict, List, Optional, Union

from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException
from pymodbus.transport import CommType

from .base_device import BaseDevice
from modules.readings_json import (
    device_telemetry_document,
    expand_readings_for_gateway_export,
    format_telemetry_json,
    format_telemetry_log_line,
    normalize_readings,
)
from modules.transport_registry import TransportRegistry

log = logging.getLogger(__name__)

ModbusClient = Union[ModbusSerialClient, ModbusTcpClient]


def _modbus_register_read_kind(reg_type_raw: Any) -> str:
    """input → read_input_registers; tutto il resto (holding, hr, …) → holding."""
    u = str(reg_type_raw or "holding").strip().lower().replace("-", "_")
    if u in ("input", "input_register", "ir"):
        return "input"
    return "holding"


class ModbusMeter(BaseDevice):
    """
    Rappresenta un dispositivo che comunica tramite protocollo Modbus.
    La configurazione specifica (registri, ecc.) è fornita tramite un dizionario.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        client: ModbusClient,
        lock: Optional[asyncio.Lock] = None,
    ):
        super().__init__(
            device_id=config.get('device_id'), 
            name=config.get('name', f"Device_{config.get('device_id')}")
        )
        self.config = config
        self.client = client
        raw_sid = self.config.get("slave_id")
        if raw_sid is None:
            raise ValueError(
                f"ID schiavo ('slave_id') non specificato per il dispositivo {self.name}."
            )
        try:
            self.slave_id = int(raw_sid)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"slave_id non numerico per {self.name}: {raw_sid!r}"
            ) from e
        self.enabled = self.config.get('enabled', True)
        # lock asyncio ignorato: serializzazione I/O su client condiviso via threading.Lock nel worker
        self._modbus_tlock = TransportRegistry.modbus_thread_lock(client)

    def telemetry_protocol(self) -> str:
        if self.client.comm_params.comm_type == CommType.TCP:
            return "modbus_tcp"
        return "modbus_rtu"

    def emits_telemetry_json_from_driver(self) -> bool:
        return True

    def _decode_value(self, registers: List[int], reg_conf: Dict[str, Any]) -> float:
        """Decodifica i registri letti in un valore numerico."""
        data_type = self.config.get('data_type', 'float')
        
        if data_type == 'float':
            if not registers or len(registers) != 2:
                log.warning(f"Attesi 2 registri per float, ricevuti {len(registers)} per {self.name}")
                return 0.0

            byte_order = self.config.get('byte_order', 'big')
            word_order = self.config.get('word_order', 'big')
            
            if word_order == 'big': 
                r0, r1 = registers[0], registers[1]
            else: 
                r0, r1 = registers[1], registers[0]

            fmt = '>H' if byte_order == 'big' else '<H'
            packed_bytes = struct.pack(fmt, r0) + struct.pack(fmt, r1)

            return struct.unpack('>f', packed_bytes)[0]
        
        elif data_type == 'uint16':
            return registers[0] if registers else 0
            
        else:
            log.warning(f"Tipo di dato '{data_type}' non gestito per {self.name}.")
            return 0.0

    def _safe_close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def _is_modbus_tcp(self) -> bool:
        return self.client.comm_params.comm_type == CommType.TCP

    @staticmethod
    def _tcp_keep_session_open() -> bool:
        """Se true: non chiudere TCP dopo la lettura (PLC che preferiscono sessione lunga). Default: chiudi."""
        v = (os.getenv("MODBUS_TCP_KEEP_SESSION_OPEN") or "").strip().lower()
        return v in ("1", "true", "yes")

    def _tcp_host_port(self) -> tuple[str, int]:
        p = self.client.comm_params
        return ((p.host or "127.0.0.1").strip(), int(p.port))

    def _rebind_tcp_client_from_registry(self) -> None:
        """Usa sempre l'istanza attuale nel registry (riciclata dopo l'ultima lettura sulla stessa interfaccia)."""
        host, port = self._tcp_host_port()
        self.client, _ = TransportRegistry.get_modbus_tcp(host, port)
        self._modbus_tlock = TransportRegistry.modbus_thread_lock(self.client)

    def _is_serial_rtu(self) -> bool:
        return self.client.comm_params.comm_type == CommType.SERIAL

    def _flush_serial_rx(self) -> None:
        """Pulisce RX pyserial: riduce frame sporchi tra un ciclo e l'altro o tra letture multiple."""
        if not self._is_serial_rtu():
            return
        try:
            sock = getattr(self.client, "socket", None)
            if sock is not None and hasattr(sock, "reset_input_buffer"):
                sock.reset_input_buffer()
        except Exception:
            pass

    def _rtu_inter_register_delay_s(self) -> float:
        try:
            v = float(os.getenv("MODBUS_RTU_INTER_REGISTER_DELAY_SECONDS", "0.02"))
        except (TypeError, ValueError):
            v = 0.02
        return max(0.0, v)

    def _sync_read_registers(self) -> List[Dict[str, Any]]:
        """I/O pymodbus (bloccante): eseguito in thread per non fermare l'event loop."""
        results: List[Dict[str, Any]] = []
        register_map = self.config.get("registers")
        if not isinstance(register_map, dict):
            register_map = {}
        if not register_map:
            log.warning(
                "Modbus '%s': 'registers' vuoto o assente nella config (driver + inventory); "
                "nessun registro da leggere.",
                self.name,
            )
            return []

        tcp = self._is_modbus_tcp()
        close_tcp_after = tcp and not self._tcp_keep_session_open()

        try:
            if tcp:
                self._rebind_tcp_client_from_registry()
            if not self.client.connected:
                if not self.client.connect():
                    log.warning(
                        "Modbus '%s': server non raggiungibile (%s). Nessuna lettura in questo ciclo.",
                        self.name,
                        self.client,
                    )
                    return []

            if self._is_serial_rtu():
                self._flush_serial_rx()

            delay_s = self._rtu_inter_register_delay_s()
            for reg_index, (name, reg_conf) in enumerate(register_map.items()):
                if reg_index > 0 and delay_s > 0 and self._is_serial_rtu():
                    time.sleep(delay_s)
                addresses = reg_conf.get("addresses")
                if not addresses:
                    continue

                reg_kind = _modbus_register_read_kind(reg_conf.get("type", "holding"))
                try:
                    start_address = int(addresses[0])
                except (TypeError, ValueError):
                    log.warning(
                        "Modbus '%s' misura '%s': indirizzo non numerico %r",
                        self.name,
                        name,
                        addresses[0],
                    )
                    continue
                count = len(addresses)

                try:
                    if reg_kind == "input":
                        response = self.client.read_input_registers(
                            address=start_address, count=count, device_id=self.slave_id
                        )
                    else:
                        response = self.client.read_holding_registers(
                            address=start_address, count=count, device_id=self.slave_id
                        )
                except ConnectionException as e:
                    log.warning(
                        "Modbus '%s': connessione fallita durante '%s': %s",
                        self.name,
                        name,
                        e,
                    )
                    self._safe_close()
                    break
                except ModbusException as e:
                    # Es. ModbusIOException: nessuna risposta / timeout — niente stack trace
                    log.warning(
                        "Modbus '%s' misura '%s': %s",
                        self.name,
                        name,
                        e,
                    )
                    self._flush_serial_rx()
                    continue

                if response.isError():
                    log.warning(
                        "Modbus '%s' slave=%s misura '%s': risposta errore Modbus %s",
                        self.name,
                        self.slave_id,
                        name,
                        response,
                    )
                    self._flush_serial_rx()
                    continue

                value = self._decode_value(response.registers, reg_conf)
                scale = reg_conf.get("scale", 1.0)
                final_value = value * scale
                results.append(
                    {
                        "name": name,
                        "value": round(final_value, 3),
                        "unit": reg_conf.get("unit", ""),
                    }
                )

            if results:
                summary = ", ".join([f"{r['name']}: {r['value']} {r['unit']}" for r in results])
                log.debug("Lettura da '%s' (Slave: %s): %s", self.name, self.slave_id, summary)
            elif register_map:
                log.warning(
                    "Modbus '%s' slave=%s: nessuna misura letta (connessione, indirizzi 0-based, "
                    "tipo input vs holding, o slave_id errato).",
                    self.name,
                    self.slave_id,
                )

        except ConnectionException as e:
            log.warning("Modbus '%s': errore di connessione: %s", self.name, e)
            self._safe_close()
        except ModbusException as e:
            log.warning("Modbus '%s': errore bus/IO: %s", self.name, e)
            self._safe_close()
        except OSError as e:
            # Windows: 10053 connessione abortita dal peer/host; 10054 reset dal peer
            log.warning("Modbus '%s': errore socket OS: %s", self.name, e)
            self._safe_close()
        except Exception as e:
            log.warning("Modbus '%s': errore imprevisto: %s", self.name, e)
            self._safe_close()
        finally:
            # TCP: nuova istanza pymodbus dopo ogni lettura (connect() sullo stesso oggetto può fallire al 2° giro).
            if close_tcp_after:
                host, port = self._tcp_host_port()
                self.client = TransportRegistry.recycle_modbus_tcp(host, port)
                self._modbus_tlock = TransportRegistry.modbus_thread_lock(self.client)

        return results

    def _thread_wrapped_read(self) -> List[Dict[str, Any]]:
        with self._modbus_tlock:
            try:
                return self._sync_read_registers()
            except Exception as e:
                # Evita traceback su stderr da worker thread / futures
                log.warning("Modbus '%s' (worker): %s", self.name, e)
                return []

    async def read(self) -> List[Dict[str, Any]]:
        """
        Esegue la lettura di tutti i registri configurati per questo dispositivo.
        L'I/O pymodbus gira in thread; il lock è threading (non asyncio).
        Il timeout è applicato nel ciclo principale (main.read_with_timeout), non qui,
        per evitare doppio wait_for e sovrapposizioni tra dispositivi sullo stesso bus.
        """
        try:
            result = await asyncio.to_thread(self._thread_wrapped_read)
        except Exception as e:
            log.warning("Modbus '%s' (async read): %s", self.name, e)
            return []
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
