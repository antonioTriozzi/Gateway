"""
Pool di client Modbus (RTU seriale e TCP) e lock di accesso esclusivo.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Tuple

from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.transport import CommType

from modules.serial_manager import SerialManager


class TransportRegistry:
    _rtu: Dict[str, ModbusSerialClient] = {}
    _tcp: Dict[Tuple[str, int], ModbusTcpClient] = {}

    @classmethod
    def get_modbus_rtu(
        cls,
        port: str,
        baudrate: int,
        *,
        parity: str = "N",
        stopbits: int = 1,
        timeout: float = 1.0,
    ) -> Tuple[ModbusSerialClient, asyncio.Lock]:
        key = f"{port}|{baudrate}|{parity}|{stopbits}|{timeout}"
        if key not in cls._rtu:
            cls._rtu[key] = ModbusSerialClient(
                port=port,
                baudrate=baudrate,
                parity=parity,
                stopbits=stopbits,
                timeout=timeout,
            )
        lock = SerialManager.get_lock(f"serial:{port}")
        return cls._rtu[key], lock

    @classmethod
    def get_modbus_tcp(
        cls, host: str, port: int, *, timeout: float = 3.0
    ) -> Tuple[ModbusTcpClient, asyncio.Lock]:
        key = (host, int(port))
        if key not in cls._tcp:
            cls._tcp[key] = ModbusTcpClient(host, port=int(port), timeout=timeout)
        lock = SerialManager.get_lock(f"modbus_tcp:{host}:{port}")
        return cls._tcp[key], lock

    @classmethod
    def lock_for_modbus_client(cls, client: ModbusSerialClient | ModbusTcpClient) -> asyncio.Lock:
        p = client.comm_params
        if p.comm_type == CommType.TCP:
            return SerialManager.get_lock(f"modbus_tcp:{p.host}:{p.port}")
        port = getattr(p, "port", None) or ""
        return SerialManager.get_lock(f"serial:{port}")
