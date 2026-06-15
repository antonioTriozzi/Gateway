"""
Pool di client Modbus (RTU seriale e TCP) e lock di accesso esclusivo.
"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import Dict, Tuple

from pymodbus.client import ModbusSerialClient, ModbusTcpClient
from pymodbus.transport import CommType

from modules.serial_manager import SerialManager
from modules.modbus_lab_resolve import resolve_modbus_rtu_port, resolve_modbus_tcp_host_port


def _modbus_timeout() -> float:
    try:
        return float(os.getenv("MODBUS_TIMEOUT_SECONDS", "5"))
    except (TypeError, ValueError):
        return 5.0


def _modbus_retries() -> int:
    try:
        r = int(os.getenv("MODBUS_RETRIES", "0"))
        return max(0, r)
    except (TypeError, ValueError):
        return 0


class TransportRegistry:
    _rtu: Dict[str, ModbusSerialClient] = {}
    _tcp: Dict[Tuple[str, int], ModbusTcpClient] = {}
    # Lock in thread (non asyncio): evita stalli se wait_for scade mentre pymodbus è ancora in esecuzione.
    _modbus_thread_locks: Dict[int, threading.Lock] = {}
    _mbus_thread_locks: Dict[str, threading.Lock] = {}

    @classmethod
    def modbus_thread_lock(cls, client: ModbusSerialClient | ModbusTcpClient) -> threading.Lock:
        k = id(client)
        if k not in cls._modbus_thread_locks:
            cls._modbus_thread_locks[k] = threading.Lock()
        return cls._modbus_thread_locks[k]

    @classmethod
    def mbus_thread_lock(cls, port: str) -> threading.Lock:
        if port not in cls._mbus_thread_locks:
            cls._mbus_thread_locks[port] = threading.Lock()
        return cls._mbus_thread_locks[port]

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
        port = resolve_modbus_rtu_port(port)
        # Stessa porta fisica → un solo client (il timeout non fa parte della chiave).
        key = f"{port}|{baudrate}|{parity}|{stopbits}"
        if key not in cls._rtu:
            cls._rtu[key] = ModbusSerialClient(
                port=port,
                baudrate=baudrate,
                parity=parity,
                stopbits=stopbits,
                timeout=timeout,
                retries=_modbus_retries(),
            )
        lock = SerialManager.get_lock(f"serial:{port}")
        return cls._rtu[key], lock

    @classmethod
    def get_modbus_tcp(
        cls, host: str, port: int, *, timeout: float | None = None
    ) -> Tuple[ModbusTcpClient, asyncio.Lock]:
        host, port = resolve_modbus_tcp_host_port(host, int(port))
        key = (host, int(port))
        if key not in cls._tcp:
            t = timeout if timeout is not None else _modbus_timeout()
            cls._tcp[key] = ModbusTcpClient(
                host,
                port=int(port),
                timeout=t,
                retries=_modbus_retries(),
            )
        lock = SerialManager.get_lock(f"modbus_tcp:{host}:{port}")
        return cls._tcp[key], lock

    @classmethod
    def recycle_modbus_tcp(cls, host: str, port: int) -> ModbusTcpClient:
        """
        Chiude e sostituisce il client TCP in cache con una nuova istanza pymodbus.
        Alcuni stack (Windows + simulatori) lasciano l'istanza vecchia non riconnettibile
        dopo close(); una nuova istanza evita letture vuote al ciclo successivo.
        """
        key = (host.strip(), int(port))
        host, port = resolve_modbus_tcp_host_port(key[0], key[1])
        key = (host.strip(), int(port))
        old = cls._tcp.pop(key, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        t = _modbus_timeout()
        nc = ModbusTcpClient(
            host.strip(),
            port=int(port),
            timeout=t,
            retries=_modbus_retries(),
        )
        cls._tcp[key] = nc
        return nc

    @classmethod
    def close_all_modbus(cls) -> None:
        """Chiude tutti i client Modbus in cache (reload config / shutdown)."""
        for client in list(cls._rtu.values()):
            try:
                client.close()
            except Exception:
                pass
        cls._rtu.clear()
        for client in list(cls._tcp.values()):
            try:
                client.close()
            except Exception:
                pass
        cls._tcp.clear()

    @classmethod
    def lock_for_modbus_client(cls, client: ModbusSerialClient | ModbusTcpClient) -> asyncio.Lock:
        p = client.comm_params
        if p.comm_type == CommType.TCP:
            return SerialManager.get_lock(f"modbus_tcp:{p.host}:{p.port}")
        port = getattr(p, "port", None) or ""
        return SerialManager.get_lock(f"serial:{port}")
