import asyncio
import concurrent.futures
import logging
import os
import sys
import time

from modules.readings_json import (
    device_telemetry_document,
    expand_readings_for_gateway_export,
    format_telemetry_json,
    normalize_readings,
    protocol_for_device,
)
from config import (
    load_config,
    fetch_remote_config,
    save_config_to_cache,
    load_config_from_cache,
    normalize_remote_gateway_config,
)
from modules.gateway_config_adapter import apply_progettotesi_device_plumbing
from modules.knx_gateway_pool import KnxGatewayPool
from modules.managers import DeviceManager
from modules.data_buffer import DataBuffer
from modules.data_uploader import DataUploader

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
    # pymodbus: messaggi ERROR su connect / I/O (spesso ridondanti con i nostri WARNING).
    logging.getLogger("pymodbus.logging").setLevel(logging.CRITICAL)
    logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

async def main():
    setup_logging()
    logging.info("Avvio del Gateway...")

    try:
        local_config = load_config()
        logging.info("Configurazione locale caricata correttamente.")
    except ValueError as e:
        logging.error(f"Errore critico nella configurazione locale: {e}")
        sys.exit(1)

    remote_conf = fetch_remote_config(
        base_url=local_config["remote_config"]["url"],
        condominio_id=local_config["id_condominio"],
        token=local_config["remote_config"]["token"]
    )

    if remote_conf:
        remote_conf = normalize_remote_gateway_config(remote_conf)
        remote_conf = apply_progettotesi_device_plumbing(remote_conf)
        logging.info("Configurazione remota scaricata con successo.")
        save_config_to_cache(remote_conf)
    else:
        logging.warning("Download fallito. Tentativo di caricamento dalla cache...")
        remote_conf = load_config_from_cache()
        if remote_conf:
            remote_conf = normalize_remote_gateway_config(remote_conf)
            remote_conf = apply_progettotesi_device_plumbing(remote_conf)

    if not remote_conf:
        logging.error("Impossibile ottenere la configurazione, né dal server né dalla cache. L'applicazione non può continuare.")
        sys.exit(1)
    
    logging.info("Configurazione remota pronta per l'uso (da server o cache).")

    full_config = {**local_config, **remote_conf}

    buffer = DataBuffer()
    uploader = DataUploader(config=full_config, buffer=buffer)
    devices = DeviceManager.create_devices(full_config)
    logging.info(f"Dispositivi inizializzati: {len(devices)}")

    # Un worker per dispositivo (o più) così nessun COM/TCP lento blocca le letture parallele.
    io_workers = max(16, len(devices) * 3, 1)
    io_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=io_workers,
        thread_name_prefix="gw_io",
    )
    asyncio.get_running_loop().set_default_executor(io_executor)
    logging.info("Pool thread I/O: %s worker (letture dispositivi in parallelo).", io_workers)

    await KnxGatewayPool.start_all()

    uploader_task = asyncio.create_task(uploader.run())

    try:
        read_interval = float(full_config.get("system_config", {}).get("poll_interval", 60))
    except (TypeError, ValueError):
        read_interval = 60.0
    logging.info("Intervallo di lettura impostato a %s secondi.", read_interval)

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
                "Timeout lettura (%ss) per dispositivo %s; si passa al ciclo successivo.",
                read_timeout,
                device.device_id,
            )
            return []

    async def read_labeled(device):
        return device, await read_with_timeout(device)

    try:
        while True:
            cycle_started = time.monotonic()
            try:
                logging.info("--- Inizio ciclo di lettura ---")

                active_devices = [d for d in devices if d.enabled]
                if active_devices:
                    tasks = [
                        asyncio.create_task(read_labeled(d), name=f"read:{d.device_id}")
                        for d in active_devices
                    ]
                    for done in asyncio.as_completed(tasks):
                        try:
                            device, res = await done
                        except Exception as e:
                            logging.error(
                                "Errore lettura (task): %s",
                                e,
                                exc_info=False,
                            )
                            continue
                        if isinstance(res, Exception):
                            logging.error(
                                "Errore lettura da %s: %s",
                                device.device_id,
                                res,
                                exc_info=False,
                            )
                            continue
                        safe = normalize_readings(res) if res else []
                        export_rows = expand_readings_for_gateway_export(device, safe)
                        # Salva sempre (formato uscita per protocollo: vedi expand_readings_for_gateway_export).
                        buffer.save_readings(device.device_id, export_rows)
                        doc = device_telemetry_document(
                            device.device_id,
                            device.name,
                            protocol_for_device(device),
                            export_rows,
                        )
                        if not device.emits_telemetry_json_from_driver():
                            logging.info(
                                "TELEMETRY_JSON %s", format_telemetry_json(doc)
                            )
                else:
                    logging.info(
                        "Nessun dispositivo attivo: ciclo a vuoto, il loop continua."
                    )
            finally:
                elapsed = time.monotonic() - cycle_started
                pause = max(0.0, read_interval - elapsed)
                logging.info(
                    "--- Ciclo terminato in %.1fs. Dati in attesa: %s. Pausa %.1fs ---",
                    elapsed,
                    buffer.count_pending(),
                    pause,
                )
                await asyncio.sleep(pause)

    except asyncio.CancelledError:
        logging.info("Loop principale in fase di chiusura.")
    except KeyboardInterrupt:
        logging.info("Interruzione da tastiera ricevuta.")
    finally:
        logging.info("Arresto dei servizi...")
        uploader_task.cancel()
        try:
            await uploader_task
        except asyncio.CancelledError:
            pass
        await KnxGatewayPool.stop_all()
        try:
            ex = asyncio.get_running_loop().get_default_executor()
            if isinstance(ex, concurrent.futures.ThreadPoolExecutor):
                ex.shutdown(wait=False, cancel_futures=True)
        except RuntimeError:
            pass
        logging.info("Gateway arrestato.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Applicazione terminata.")
