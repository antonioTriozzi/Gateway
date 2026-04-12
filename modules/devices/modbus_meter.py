import asyncio
import logging
import os
import struct
from typing import Any, Dict, List, Optional, Union

from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException

from .base_device import BaseDevice
from modules.transport_registry import TransportRegistry

log = logging.getLogger(__name__)

ModbusClient = Union[ModbusSerialClient, ModbusTcpClient]


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
        self.slave_id = self.config.get('slave_id')
        self.enabled = self.config.get('enabled', True)
        # lock asyncio ignorato: serializzazione I/O su client condiviso via threading.Lock nel worker
        self._modbus_tlock = TransportRegistry.modbus_thread_lock(client)

        if not self.slave_id:
            raise ValueError(f"ID schiavo ('slave_id') non specificato per il dispositivo {self.name}.")

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

    def _sync_read_registers(self) -> List[Dict[str, Any]]:
        """I/O pymodbus (bloccante): eseguito in thread per non fermare l'event loop."""
        results: List[Dict[str, Any]] = []
        register_map = self.config.get("registers", {})
        try:
            if not self.client.connected:
                if not self.client.connect():
                    log.warning(
                        "Modbus '%s': server non raggiungibile (%s). Nessuna lettura in questo ciclo.",
                        self.name,
                        self.client,
                    )
                    return []

            for name, reg_conf in register_map.items():
                addresses = reg_conf.get("addresses")
                if not addresses:
                    continue

                reg_type = reg_conf.get("type", "input")
                start_address = addresses[0]
                count = len(addresses)

                try:
                    if reg_type == "input":
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
                    continue

                if response.isError():
                    log.debug(
                        "Errore Modbus leggendo '%s' da %s (Slave: %s): %s",
                        name,
                        self.name,
                        self.slave_id,
                        response,
                    )
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
                log.info("Lettura da '%s' (Slave: %s): %s", self.name, self.slave_id, summary)

        except ConnectionException as e:
            log.warning("Modbus '%s': errore di connessione: %s", self.name, e)
            self._safe_close()
        except ModbusException as e:
            log.warning("Modbus '%s': errore bus/IO: %s", self.name, e)
            self._safe_close()
        except Exception as e:
            log.warning("Modbus '%s': errore imprevisto: %s", self.name, e)
            self._safe_close()

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
        L'I/O pymodbus gira in thread; il lock è threading (non asyncio) così un timeout
        sul ciclo principale non lascia il bus bloccato per i giri successivi.
        """
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
                "Timeout Modbus (%ss) per '%s'; il thread può ancora terminare in background.",
                timeout,
                self.name,
            )
            return []
