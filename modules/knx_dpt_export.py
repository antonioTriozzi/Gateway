"""
Etichette export per letture KNX: measure = parola descrittiva dal tipo DPT (non il codice),
unit = simbolo coerente con il DPT. Allineato alle option in assets_config.html (ProgettoTesi).
"""
from __future__ import annotations

from typing import Dict, Tuple

# (measure_key, unit) — measure in minuscolo, leggibile in API
_KNX_DPT_MEASURE_UNIT: Dict[str, Tuple[str, str]] = {
    "1.001": ("boolean", ""),
    "1.002": ("boolean_controlled", ""),
    "5.001": ("scaling", "%"),
    "7.001": ("relative_humidity", "%"),
    "9.001": ("temperature", "°C"),
    "9.006": ("pressure", "Pa"),
    "14.001": ("illuminance", "lux"),
    "14.019": ("power", "W"),
    "14.027": ("power", "W"),
    "12.001": ("counter", ""),
    "13.001": ("energy_meter", ""),
    "13.010": ("active_energy", ""),
    "13.013": ("active_energy", "kWh"),
}


def measure_and_unit_for_dpt(dpt: str | None) -> Tuple[str | None, str | None]:
    """
    Se dpt è noto nella mappa, ritorna (measure, unit) per l'export.
    Altrimenti (None, None) per lasciare il fallback sulla chiave logica / unit da config.
    """
    if not dpt or not isinstance(dpt, str):
        return None, None
    key = dpt.strip()
    hit = _KNX_DPT_MEASURE_UNIT.get(key)
    if not hit:
        return None, None
    return hit[0], hit[1]
