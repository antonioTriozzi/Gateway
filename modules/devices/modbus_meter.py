import asyncio
import struct
import logging
from typing import Any, Dict, List, Optional, Union

from pymodbus.client import ModbusSerialClient, ModbusTcpClient

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
        self.lock = lock or TransportRegistry.lock_for_modbus_client(client)

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

    async def read(self) -> List[Dict[str, Any]]:
        """
        Esegue la lettura di tutti i registri configurati per questo dispositivo.
        """
        results = []
        register_map = self.config.get('registers', {})
        
        async with self.lock:
            try:
                if not self.client.connected:
                    self.client.connect()
                
                for name, reg_conf in register_map.items():
                    addresses = reg_conf.get('addresses')
                    if not addresses:
                        continue

                    reg_type = reg_conf.get('type', 'input')
                    start_address = addresses[0]
                    count = len(addresses)
                    
                    if reg_type == 'input':
                        response = self.client.read_input_registers(address=start_address, count=count, device_id=self.slave_id)
                    else:
                        response = self.client.read_holding_registers(address=start_address, count=count, device_id=self.slave_id)
                    
                    if response.isError():
                        log.debug(f"Errore Modbus leggendo '{name}' da {self.name} (Slave: {self.slave_id}): {response}")
                        continue
                    
                    value = self._decode_value(response.registers, reg_conf)
                    
                    scale = reg_conf.get('scale', 1.0)
                    final_value = value * scale
                    
                    results.append({
                        'name': name,
                        'value': round(final_value, 3),
                        'unit': reg_conf.get('unit', '')
                    })
                    await asyncio.sleep(0.05)
                
                if results:
                    summary = ", ".join([f"{r['name']}: {r['value']} {r['unit']}" for r in results])
                    log.info(f"Lettura da '{self.name}' (Slave: {self.slave_id}): {summary}")

            except Exception as e:
                log.error(f"Eccezione durante la lettura di {self.name}: {e}", exc_info=True)
            
            return results
