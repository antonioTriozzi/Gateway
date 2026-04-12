import asyncio
from typing import Dict
from pymodbus.client import ModbusSerialClient
import serial.tools.list_ports
import sys
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SerialManager:
    """
    Gestisce le connessioni seriali condivise.
    Garantisce che più dispositivi sulla stessa porta usino lo stesso client
    e che l'accesso sia esclusivo (tramite Lock) per evitare collisioni.
    """
    _instances = {}
    _locks = {}

    @classmethod
    def get_client(cls, port: str, baudrate: int = 9600) -> ModbusSerialClient:
        if port not in cls._instances:
            cls._instances[port] = ModbusSerialClient(
                port=port,
                baudrate=baudrate,
                timeout=1
            )
            cls._locks[port] = asyncio.Lock()
        return cls._instances[port]

    @classmethod
    def get_lock(cls, port: str) -> asyncio.Lock:
        if port not in cls._locks:
            cls._locks[port] = asyncio.Lock()
        return cls._locks[port]

    @staticmethod
    def discover_port(baudrate: int, slave_id: int) -> str:
        """
        Scansiona le porte seriali disponibili e tenta di comunicare con un dispositivo Modbus.
        Restituisce la prima porta in cui la comunicazione ha successo.
        """
        logger.info(f"Avvio della scansione dinamica delle porte (Baud: {baudrate}, Slave: {slave_id})...")
        
        candidates = []
        ports = serial.tools.list_ports.comports()
        
        for p in ports:
            if sys.platform.startswith('win'):
                if "COM" in p.device:
                    candidates.append(p.device)
            elif sys.platform.startswith('linux'):
                if "USB" in p.device or "ACM" in p.device:
                    candidates.append(p.device)
                elif "AMA" in p.device:
                     logger.debug(f"Skipping potential console port: {p.device}")
                     continue

        if not candidates:
            logger.error("Nessuna porta trovata.")
            raise Exception("Nessuna porta trovata per la scansione dinamica.")

        logger.info(f"Porte candidate: {candidates}")

        for port in candidates:
            logger.info(f"Probing port: {port}...")
            try:
                client = ModbusSerialClient(
                    port=port,
                    baudrate=baudrate,
                    timeout=0.5,
                    stopbits=1,
                    bytesize=8,
                    parity='N'
                )
                
                if client.connect():
                    rr = client.read_holding_registers(0, 1, device_id=slave_id)
                    client.close()
                    
                    if not rr.isError():
                        logger.info(f"SUCCESSO: Dispositivo trovato sulla porta {port}")
                        return port
                    else:
                        logger.debug(f"Probe fallito sulla porta {port} (Modbus Error): {rr}")
                else:
                    client.close()
                    logger.debug(f"Impossibile aprire la porta {port}")
            
            except Exception as e:
                logger.debug(f"Eccezione durante il probing della porta {port}: {e}")
        
        logger.error("Auto-discovery fallito: Nessun dispositivo Modbus raggiungibile trovato.")
        raise Exception("Auto-discovery fallito: Nessun dispositivo Modbus raggiungibile trovato.")