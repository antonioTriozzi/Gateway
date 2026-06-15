import asyncio
import concurrent.futures
import logging
import os
import signal
import sys
import time

from modules.readings_json import (
    device_telemetry_document,
    expand_readings_for_gateway_export,
    format_telemetry_json,
    format_telemetry_log_line,
    normalize_readings,
    protocol_for_device,
    readings_for_buffer_export,
)
from config import load_config
from modules.config_refresh import (
    config_refresh_interval_seconds,
    cycle_timing_from_config,
    GatewayRuntimeState,
    maybe_refresh_gateway_config,
)
from modules.gateway_config_loader import (
    config_revision,
    load_remote_gateway_config_at_startup,
    merge_local_remote,
)
from modules.knx_gateway_pool import KnxGatewayPool
from modules.managers import DeviceManager
from modules.data_buffer import DataBuffer
from modules.data_uploader import DataUploader
from modules.transport_registry import TransportRegistry
from modules.web_auth import WebAppAuthClient


def _quiet_keyboard_interrupt(exc_type, exc_value, exc_tb):
    if exc_type is KeyboardInterrupt:
        logging.getLogger().info("Applicazione terminata.")
        return
    sys.__excepthook__(exc_type, exc_value, exc_tb)


async def _interruptible_sleep(seconds: float, shutdown: asyncio.Event) -> bool:
    """Attende fino a `seconds`; True se è stato richiesto lo shutdown."""
    if seconds <= 0:
        return shutdown.is_set()
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def setup_logging():
    """Configura il logging per l'applicazione."""
    log = logging.getLogger()
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if log.hasHandlers():
        log.handlers.clear()
    log.addHandler(handler)
    logging.getLogger("pymodbus.logging").setLevel(logging.CRITICAL)
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)
    logging.getLogger("xknx").setLevel(logging.ERROR)


async def main():
    setup_logging()
    logging.info("Avvio del Gateway...")

    try:
        local_config = load_config()
        logging.info("Configurazione locale caricata correttamente.")
        pc_ip = (os.getenv("PC_IP") or "").strip()
        if pc_ip and os.name != "nt":
            logging.info(
                "Lab mirror: PC_IP=%s (M-Bus/Modbus dalla Web App -> simulatore sul PC).",
                pc_ip,
            )
        elif os.name != "nt":
            logging.warning(
                "PC_IP non impostato nel .env: Modbus/M-Bus useranno host/porta dalla Web App "
                "(127.0.0.1 / COMx sulla Pi)."
            )
    except ValueError as e:
        logging.error(f"Errore critico nella configurazione locale: {e}")
        sys.exit(1)

    web_auth = WebAppAuthClient(
        token_url=local_config["web_auth"]["token_url"],
        client_id=local_config["web_auth"]["client_id"],
        client_secret=local_config["web_auth"]["client_secret"],
    )

    remote_conf = load_remote_gateway_config_at_startup(web_auth, local_config)
    if not remote_conf:
        logging.error(
            "Impossibile ottenere la configurazione, né dal server né dalla cache. "
            "L'applicazione non può continuare."
        )
        sys.exit(1)

    logging.info("Configurazione remota pronta per l'uso (da server o cache).")

    full_config = merge_local_remote(local_config, remote_conf)
    cycle_total, read_phase, upload_phase = cycle_timing_from_config(full_config)
    refresh_interval = config_refresh_interval_seconds()
    if refresh_interval > 0:
        logging.info(
            "Refresh config remota ogni %.0fs (CONFIG_REFRESH_INTERVAL_SECONDS).",
            refresh_interval,
        )
    else:
        logging.info(
            "Refresh config remota disabilitato (CONFIG_REFRESH_INTERVAL_SECONDS=0)."
        )

    buffer = DataBuffer()
    uploader = DataUploader(config=full_config, buffer=buffer, auth=web_auth)
    devices = DeviceManager.create_devices(full_config)
    logging.info(f"Dispositivi inizializzati: {len(devices)}")

    runtime = GatewayRuntimeState(
        full_config=full_config,
        devices=devices,
        revision=config_revision(remote_conf),
        cycle_total=cycle_total,
        read_phase=read_phase,
        upload_phase=upload_phase,
        last_refresh_monotonic=time.monotonic(),
    )

    io_workers = max(16, len(devices) * 3, 1)
    io_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=io_workers,
        thread_name_prefix="gw_io",
    )
    loop = asyncio.get_running_loop()
    loop.set_default_executor(io_executor)

    shutdown = asyncio.Event()

    def _request_shutdown() -> None:
        if shutdown.is_set():
            logging.info("Arresto forzato.")
            os._exit(0)
        logging.info("Interruzione ricevuta, arresto in corso…")
        shutdown.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _request_shutdown)
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)
    except (NotImplementedError, RuntimeError, ValueError):
        pass

    logging.info(
        "Pool thread I/O: %s worker (I/O bloccante in thread; letture dispositivi sequenziali per ciclo).",
        io_workers,
    )

    await KnxGatewayPool.start_all()

    logging.info(
        "Ciclo gateway: %.1fs totali (fase lettura fino a %.1fs, poi pausa %.1fs; "
        "upload telemetria a fine lettura di tutti i dispositivi, retry a fine pausa se il buffer non è vuoto).",
        runtime.cycle_total,
        runtime.read_phase,
        runtime.upload_phase,
    )

    try:
        read_timeout = float(os.getenv("DEVICE_READ_TIMEOUT_SECONDS", "60"))
    except (TypeError, ValueError):
        read_timeout = 60.0
    logging.info(
        "Timeout massimo per singola lettura dispositivo: %ss (DEVICE_READ_TIMEOUT_SECONDS).",
        read_timeout,
    )

    async def read_with_timeout(device):
        try:
            return await asyncio.wait_for(device.read(), timeout=read_timeout)
        except asyncio.TimeoutError:
            logging.warning(
                "Timeout lettura (%ss) per dispositivo %s; si passa al dispositivo successivo.",
                read_timeout,
                device.device_id,
            )
            return []

    try:
        while not shutdown.is_set():
            cycle_started = time.monotonic()
            cycle_total = runtime.cycle_total
            read_phase = runtime.read_phase
            devices = runtime.devices

            try:
                logging.info("--- Inizio ciclo di lettura (sequenziale: una lettura per dispositivo) ---")

                active_devices = [d for d in devices if d.enabled]
                if active_devices:
                    for device in active_devices:
                        if shutdown.is_set():
                            break
                        try:
                            res = await read_with_timeout(device)
                        except Exception as e:
                            logging.error(
                                "Errore lettura da %s: %s",
                                device.device_id,
                                e,
                                exc_info=False,
                            )
                            continue
                        safe = normalize_readings(res) if res else []
                        export_rows = expand_readings_for_gateway_export(device, safe)
                        buffer_rows = readings_for_buffer_export(export_rows)
                        if buffer_rows:
                            buffer.save_readings(device.device_id, buffer_rows)
                        doc = device_telemetry_document(
                            device.device_id,
                            device.name,
                            protocol_for_device(device),
                            export_rows,
                        )
                        if not device.emits_telemetry_json_from_driver():
                            logging.debug(
                                "TELEMETRY_JSON %s", format_telemetry_json(doc)
                            )
                            logging.info("TELEMETRY %s", format_telemetry_log_line(doc))

                    if not shutdown.is_set():
                        await uploader.flush_pending()

                else:
                    logging.info(
                        "Nessun dispositivo attivo: ciclo a vuoto, il loop continua."
                    )
            finally:
                if shutdown.is_set():
                    break
                elapsed_after_read = time.monotonic() - cycle_started
                read_padding = max(0.0, read_phase - elapsed_after_read)
                if read_padding > 0:
                    logging.info(
                        "Fase lettura: attesa aggiuntiva %.1fs (finestra lettura %.1fs).",
                        read_padding,
                        read_phase,
                    )
                if await _interruptible_sleep(read_padding, shutdown):
                    break

                idle_started = time.monotonic()
                runtime = await maybe_refresh_gateway_config(
                    runtime,
                    web_auth,
                    local_config,
                    refresh_interval,
                )

                after_read_window = time.monotonic() - cycle_started
                idle_spent = time.monotonic() - idle_started
                pause = max(0.0, cycle_total - after_read_window - idle_spent)
                logging.info(
                    "--- Dopo fase lettura: %.1fs. Dati in attesa: %s. Pausa (fase invio / idle) %.1fs "
                    "(ciclo totale %.1fs) ---",
                    after_read_window,
                    buffer.count_pending(),
                    pause,
                    cycle_total,
                )
                if await _interruptible_sleep(pause, shutdown):
                    break

                if not shutdown.is_set() and buffer.count_pending() > 0:
                    await uploader.flush_pending()

    finally:
        logging.info("Arresto dei servizi...")
        try:
            await KnxGatewayPool.stop_all()
        except Exception as e:
            logging.debug("Stop KNX: %s", e)
        TransportRegistry.close_all_modbus()
        io_executor.shutdown(wait=False, cancel_futures=True)
        logging.info("Gateway arrestato.")


if __name__ == "__main__":
    sys.excepthook = _quiet_keyboard_interrupt
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
