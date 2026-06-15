# Copia il gateway aggiornato sulla Raspberry (lab: PC -> Pi via SSH).
# Uso: .\scripts\sync_to_pi.ps1
#      .\scripts\sync_to_pi.ps1 -PiHost 192.168.8.154 -PiUser pi

param(
    [string]$PiHost = "192.168.8.154",
    [string]$PiUser = "pi",
    [string]$PiPath = "/home/pi/Gateway_IoT-main"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

Write-Host "Sorgente: $Root"
Write-Host "Destinazione: ${PiUser}@${PiHost}:$PiPath"
Write-Host ""

$items = @(
    "main.py",
    "config.py",
    "modules",
    ".env.example"
)

foreach ($item in $items) {
    $src = Join-Path $Root $item
    if (-not (Test-Path $src)) {
        Write-Warning "Salto (non trovato): $item"
        continue
    }
    Write-Host "scp $item ..."
    scp -r $src "${PiUser}@${PiHost}:$PiPath/"
}

Write-Host ""
Write-Host "Fatto. Sulla Pi:"
Write-Host "  1) .env sulla Pi con PC_IP=192.168.8.115 (nessun MBUS/MODBUS socket URL)"
Write-Host "  2) grep resolve_modbus $PiPath/modules/devices/modbus_meter.py"
Write-Host "  3) Riavvia il gateway"
