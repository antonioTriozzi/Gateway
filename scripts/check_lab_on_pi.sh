#!/bin/sh
# Esegui SULLA Raspberry per verificare deploy lab mirror.
#   cd Gateway_IoT-main && sh scripts/check_lab_on_pi.sh

set -e
cd "$(dirname "$0")/.."

echo "=== .env lab (PC mirror) ==="
grep -E '^(PC_IP|MBUS_SOCKET_URL|MODBUS_RTU_SOCKET_URL|MODBUS_TCP_URL)=' .env 2>/dev/null || echo "[!] .env mancante o variabili assenti"

echo ""
echo "=== Codice remap Modbus ==="
if grep -q resolve_modbus_rtu_port modules/modbus_lab_resolve.py 2>/dev/null; then
    echo "OK: modbus_lab_resolve.py presente"
elif grep -q resolve_modbus_rtu_port modules/devices/modbus_meter.py; then
    echo "OK: remap in modbus_meter.py (vecchio layout)"
else
    echo "[!] VECCHIO CODICE: aggiorna gateway dal PC (scripts/sync_to_pi.ps1)"
fi

echo ""
echo "=== Reachability PC (192.168.8.115) ==="
PC="${PC_IP:-192.168.8.115}"
for port in 9000 502 9010; do
    if timeout 2 bash -c "echo >/dev/tcp/$PC/$port" 2>/dev/null; then
        echo "OK  TCP $PC:$port"
    else
        echo "[!] TCP $PC:$port non raggiungibile (mirror/sim spenti sul PC?)"
    fi
done
