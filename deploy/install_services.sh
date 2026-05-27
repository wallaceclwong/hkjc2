#!/bin/bash
# hkjc2 — Safe service file installer. Always runs daemon-reload.
# Usage: ./deploy/install_services.sh
set -euo pipefail
cd "$(dirname "$0")/.."

SERVICES=(cp-odds cp-racecards cp-predict cp-learn cp-fixtures)

echo "=== Installing systemd services ==="

for svc in "${SERVICES[@]}"; do
    if [ -f "deploy/${svc}.service" ]; then
        cp "deploy/${svc}.service" /etc/systemd/system/
        echo "  Installed ${svc}.service"
    fi
    if [ -f "deploy/${svc}.timer" ]; then
        cp "deploy/${svc}.timer" /etc/systemd/system/
        echo "  Installed ${svc}.timer"
    fi
done

# CRITICAL: reload AFTER copying all files, before enable/start
systemctl daemon-reload
echo "  systemctl daemon-reload — DONE"

for svc in "${SERVICES[@]}"; do
    if [ -f "deploy/${svc}.timer" ]; then
        systemctl enable "${svc}.timer" 2>/dev/null || true
    fi
done

echo ""
echo "=== Verify ==="
systemctl list-timers 'cp-*' --no-pager

echo ""
echo "IMPORTANT: If you edit service files directly on the VM,"
echo "always run: systemctl daemon-reload"
echo "Otherwise systemd uses the cached (old) version."
