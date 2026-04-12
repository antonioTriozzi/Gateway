import asyncio
import logging
import sys
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

    await KnxGatewayPool.start_all()

    uploader_task = asyncio.create_task(uploader.run())

    read_interval = full_config.get('system_config', {}).get('poll_interval', 60)
    logging.info(f"Intervallo di lettura impostato a {read_interval} secondi.")

    try:
        while True:
            logging.info("--- Inizio ciclo di lettura ---")
            
            active_devices = [device for device in devices if device.enabled]
            read_tasks = [device.read() for device in active_devices]

            if read_tasks:
                results = await asyncio.gather(*read_tasks, return_exceptions=True)

                for device, res in zip(active_devices, results):
                    if isinstance(res, Exception):
                        logging.error(f"Errore lettura da {device.device_id}: {res}", exc_info=False)
                    elif res:
                        buffer.save_readings(device.device_id, res)
            else:
                logging.warning("Nessun dispositivo attivo da leggere.")
            
            logging.info(f"--- Ciclo terminato. Dati in attesa: {buffer.count_pending()} ---")
            await asyncio.sleep(read_interval)

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
        logging.info("Gateway arrestato.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Applicazione terminata.")
