"""
Merge driver + riga inventario per il runtime.

Se l'inventario contiene chiavi annidate a null o dict vuoti (tipico di JSON
persistito / form parziali), senza questa logica il merge {**driver, **inv}
annulla registers / group_addresses / target_measures definiti nel driver.
"""
from __future__ import annotations

from typing import Any, Dict


def merge_gateway_device_config(
    driver_def: Dict[str, Any], dev_info: Dict[str, Any]
) -> Dict[str, Any]:
    out: Dict[str, Any] = {**driver_def, **dev_info}

    # registers: null o {} nell'inventario → mantieni mappa del driver
    drv_regs = driver_def.get("registers")
    inv_regs = dev_info.get("registers")
    if isinstance(drv_regs, dict) and drv_regs:
        if inv_regs is None or (isinstance(inv_regs, dict) and not inv_regs):
            out["registers"] = drv_regs

    # KNX: group_addresses
    drv_ga = driver_def.get("group_addresses")
    inv_ga = dev_info.get("group_addresses")
    if isinstance(drv_ga, dict) and drv_ga:
        if inv_ga is None or (isinstance(inv_ga, dict) and not inv_ga):
            out["group_addresses"] = drv_ga

    # M-Bus: target_measures null → ripristina driver; [] esplicito resta "tutte"
    drv_tm = driver_def.get("target_measures")
    if "target_measures" in dev_info and dev_info.get("target_measures") is None:
        if drv_tm is not None:
            out["target_measures"] = drv_tm

    return out
