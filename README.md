# Gateway_IoT
Repository del progetto di Tesi Triennale

In questo progetto è stato sviluppato un gateway IoT per la raccolta e l'invio di dati da contatori che possono essere di energia elettrica, di calore, di acqua, ecc.

Il software è stato principalmente pensato per essere installato su un Raspberry Pi.
Attualmente supporta i protocolli Modbus RTU e MBus, ma è stato testato solo con Modbus RTU e può essere esteso per supportare altri protocolli.

Esempio di configurazione usata nel test:
```json
{
    "system_config": {
        "poll_interval": 5,
        "interfaces": {
            "modbus": {
                "port": "/dev/ttyACM0",
                "baud_rate": 9600,
                "parity": "N",
                "timeout": 1
            }
        }
    },
    "drivers_definitions": {
        "TAC1101": {
            "description": "Analizzatore Energia Elettrica Monofase",
            "protocol": "modbus",
            "word_order": "big",
            "byte_order": "big",
            "registers": {
                "voltage": {
                    "addresses": [
                        0,
                        1
                    ],
                    "unit": "V",
                    "type": "input",
                    "scale": 1.0
                },
                "current": {
                    "addresses": [
                        6,
                        7
                    ],
                    "unit": "A",
                    "type": "input",
                    "scale": 1.0
                },
                "power": {
                    "addresses": [
                        12,
                        13
                    ],
                    "unit": "W",
                    "type": "input",
                    "scale": 1.0
                },
                "active_energy": {
                    "addresses": [
                        48,
                        49
                    ],
                    "unit": "kWh",
                    "type": "input",
                    "scale": 1.0
                }
            }
        }
    },
    "devices_inventory": [
        {
            "device_id": "1",
            "model": "TAC1101",
            "interface": "modbus",
            "slave_id": 1
        }
    ]
}
```