from abc import ABC, abstractmethod
from typing import Dict, Any, List

class BaseDevice(ABC):
    """
    Classe base astratta per tutti i dispositivi di misurazione.
    """
    def __init__(self, device_id: str, name: str):
        if not device_id:
            raise ValueError("È richiesto un device_id per ogni dispositivo.")
        
        self.device_id = device_id
        self.name = name if name else f"Device_{device_id}"
        self.enabled = True # Per ora, tutti i dispositivi creati sono abilitati

    @abstractmethod
    async def read(self) -> List[Dict[str, Any]]:
        """
        Legge i dati dal dispositivo e li restituisce come lista di dizionari.
        """
        pass
