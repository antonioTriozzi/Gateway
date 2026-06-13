import asyncio
import logging
import os
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from modules.devices.base_device import BaseDevice
from modules.devices.knx_meter import KnxMeter
from modules.devices.modbus_meter import ModbusMeter
from modules.devices.mbus_meter import MBusMeter
from modules.gateway_config_adapter import parse_knx_host_port, resolve_driver
from modules.gateway_device_config import merge_gateway_device_config
from modules.knx_gateway_pool import KnxGatewayPool
from modules.serial_manager import SerialManager
from modules.transport_registry import TransportRegistry


class DeviceManager:
    """
    Factory dispositivi da configurazione remota (ProgettoTesi o formato legacy README).
    """

    @staticmethod
    def _open_transports(
        interfaces: Dict[str, Any],
    ) -> Dict[str, Tuple[str, Any, asyncio.Lock]]:
        """
        Restituisce nome_interfaccia -> (protocol_kind, client_or_handle, lock).
        protocol_kind: modbus | mbus | knx
        """
        out: Dict[str, Tuple[str, Any, asyncio.Lock]] = {}
        for name, spec in interfaces.items():
            if not isinstance(spec, dict):
                continue
            transport = (spec.get("transport") or "rtu").lower()
            try:
                if transport == "tcp":
                    host = (spec.get("host") or "127.0.0.1").strip()
                    port = int(spec.get("port") or 502)
                    client, lock = TransportRegistry.get_modbus_tcp(host, port)
                    out[name] = ("modbus", client, lock)
                    logging.info("Modbus TCP %s → %s:%s", name, host, port)
                elif transport == "rtu":
                    port = (spec.get("port") or "").strip()
                    if not port:
                        logging.error("Interfaccia %s: porta RTU mancante.", name)
                        continue
                    baud = int(spec.get("baud_rate") or 9600)
                    parity = spec.get("parity") or "N"
                    stopbits = int(spec.get("stop_bits") or 1)
                    raw_timeout = float(spec.get("timeout") or 1.0)
                    try:
                        cap = float(os.getenv("MODBUS_SERIAL_TIMEOUT_CAP_SECONDS", "12"))
                    except (TypeError, ValueError):
                        cap = 12.0
                    if cap > 0:
                        timeout = max(0.3, min(raw_timeout, cap))
                    else:
                        timeout = max(0.3, raw_timeout)
                    if timeout < raw_timeout:
                        logging.info(
                            "Modbus RTU %s: timeout seriale da config %.1fs limitato a %.1fs "
                            "(MODBUS_SERIAL_TIMEOUT_CAP_SECONDS).",
                            name,
                            raw_timeout,
                            timeout,
                        )
                    client, lock = TransportRegistry.get_modbus_rtu(
                        port,
                        baud,
                        parity=parity,
                        stopbits=stopbits,
                        timeout=timeout,
                    )
                    out[name] = ("modbus", client, lock)
                    logging.info("Modbus RTU %s → %s @ %s", name, port, baud)
                elif transport == "mbus":
                    port = (spec.get("port") or "").strip()
                    if not port:
                        logging.error("Interfaccia %s: porta M-Bus mancante.", name)
                        continue
                    baud = int(spec.get("baud_rate") or 2400)
                    lock = SerialManager.get_lock(f"serial:{port}")
                    binding = SimpleNamespace(port=port, baudrate=baud)
                    out[name] = ("mbus", binding, lock)
                    logging.info("M-Bus %s → %s @ %s", name, port, baud)
                elif transport == "knx":
                    host, kport = parse_knx_host_port(spec.get("host"), spec.get("port"))
                    utcp = spec.get("tunnel_tcp")
                    handle = (
                        KnxGatewayPool.instance(host, kport, use_tcp=bool(utcp))
                        if utcp is not None
                        else KnxGatewayPool.instance(host, kport)
                    )
                    out[name] = ("knx", handle, handle.lock)
                    logging.info("KNX %s → %s:%s", name, host, kport)
                else:
                    logging.warning("Transport sconosciuto %r per interfaccia %s", transport, name)
            except Exception as e:
                logging.error("Impossibile aprire interfaccia '%s': %s", name, e)
        return out

    @staticmethod
    def create_devices(config: Dict[str, Any]) -> List[BaseDevice]:
        devices: List[BaseDevice] = []
        inventory = config.get("devices_inventory") or []
        drivers = config.get("drivers_definitions") or {}
        interfaces = (config.get("system_config") or {}).get("interfaces") or {}

        if not inventory:
            logging.error("devices_inventory vuoto.")
            return []
        if not drivers:
            logging.error("drivers_definitions vuoto.")
            return []
        if not interfaces:
            logging.error("system_config.interfaces vuoto (espandi config web app o usa formato legacy).")
            return []

        transports = DeviceManager._open_transports(interfaces)
        if not transports:
            logging.error("Nessun transport inizializzato.")
            return []

        for dev_info in inventory:
            if not isinstance(dev_info, dict):
                continue
            interface_name = dev_info.get("interface")
            driver_def = resolve_driver(dev_info, drivers)

            if not interface_name:
                logging.error(
                    "Dispositivo ignorato (interface mancante): %s",
                    dev_info.get("device_id"),
                )
                continue
            if not driver_def:
                logging.error(
                    "Driver non trovato per device_id=%s (driver_ref=%s model=%s)",
                    dev_info.get("device_id"),
                    dev_info.get("driver_ref"),
                    dev_info.get("model"),
                )
                continue

            tup = transports.get(interface_name)
            if not tup:
                logging.error(
                    "Interfaccia '%s' non disponibile per device_id=%s",
                    interface_name,
                    dev_info.get("device_id"),
                )
                continue

            kind, client_or_handle, lock = tup
            protocol = (driver_def.get("protocol") or "").lower()
            full_device_config = dict(merge_gateway_device_config(driver_def, dev_info))
            # Middleware consumi richiede building_id: se manca in inventario, usa root config o ID_CONDOMINIO.
            if full_device_config.get("building_id") is None:
                bid = config.get("building_id")
                if bid is None and config.get("id_condominio") not in (None, ""):
                    try:
                        bid = int(str(config["id_condominio"]).strip())
                    except (TypeError, ValueError):
                        bid = None
                if bid is not None:
                    full_device_config["building_id"] = bid
            # client_id / client_mail: solo da devices_inventory (relazione Asset → Client), non dalla root.

            if protocol != kind:
                logging.warning(
                    "Mismatch protocollo driver=%s vs transport=%s per %s (proseguo se compatibile).",
                    protocol,
                    kind,
                    dev_info.get("device_id"),
                )

            try:
                if protocol == "modbus" and kind == "modbus":
                    devices.append(
                        ModbusMeter(
                            config=full_device_config,
                            client=client_or_handle,
                            lock=lock,
                        )
                    )
                elif protocol == "mbus" and kind == "mbus":
                    devices.append(
                        MBusMeter(
                            config=full_device_config,
                            client=client_or_handle,
                            lock=lock,
                        )
                    )
                elif protocol == "knx" and kind == "knx":
                    devices.append(
                        KnxMeter(
                            config=full_device_config,
                            gateway_handle=client_or_handle,
                        )
                    )
                else:
                    logging.warning(
                        "Combinazione protocol/transport non supportata: %s / %s per device_id=%s",
                        protocol,
                        kind,
                        dev_info.get("device_id"),
                    )
                    continue
                logging.info(
                    "Creato dispositivo %s (%s) su interfaccia %s",
                    dev_info.get("device_id"),
                    protocol,
                    interface_name,
                )
            except Exception as e:
                logging.error(
                    "Errore creazione dispositivo %s: %s",
                    dev_info.get("device_id"),
                    e,
                )

        return devices
