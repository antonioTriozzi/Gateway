#!/bin/sh
# Esegui SULLA Raspberry per verificare deploy lab mirror.
#   cd Gateway_IoT-main && sh scripts/check_lab_on_pi.sh

set -e
cd "$(dirname "$0")/.."

echo "=== .env lab ==="
grep -E '^PC_IP=' .env 2>/dev/null || echo "[!] PC_IP mancante in .env"

echo ""
echo "=== Codice remap (PC_IP) ==="
if test -f modules/modbus_lab_resolve.py; then
    echo "OK: modbus_lab_resolve.py"
else
    echo "[!] VECCHIO CODICE: aggiorna gateway dal PC"
fi

echo ""
echo "=== Reachability PC ==="
PC="$(grep -E '^PC_IP=' .env 2>/dev/null | cut -d= -f2- | tr -d ' ')"
PC="${PC:-192.168.8.115}"
for port in 9000 502 9010; do
    if timeout 2 bash -c "echo >/dev/tcp/$PC/$port" 2>/dev/null; then
        echo "OK  TCP $PC:$port"
    else
        echo "[!] TCP $PC:$port non raggiungibile (mirror/sim sul PC?)"
    fi
done
