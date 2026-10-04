#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/usr/local/libexec/asus-custom-fan"
CONFIG_FILE="/etc/asus-custom-fan.conf"
SERVICE_FILE="/etc/systemd/system/asus-custom-fan.service"
CLI_FILE="/usr/local/bin/asus-custom-fan"
PURGE=0

if [[ "${1:-}" == "--purge" ]]; then
    PURGE=1
elif [[ $# -gt 0 ]]; then
    echo "Usage: ./uninstall.sh [--purge]" >&2
    exit 2
fi

sudo systemctl disable --now asus-custom-fan.service 2>/dev/null || true

if [[ -x "$INSTALL_DIR/asus-fan-control-ec" ]]; then
    sudo "$INSTALL_DIR/asus-fan-control-ec" setp -1 >/dev/null 2>&1 || true
fi

sudo rm -f "$SERVICE_FILE" "$CLI_FILE"
sudo rm -rf "$INSTALL_DIR"
if (( PURGE )); then
    sudo rm -f "$CONFIG_FILE"
    echo "Removed configuration $CONFIG_FILE"
else
    echo "Preserved configuration $CONFIG_FILE (use --purge to remove it)"
fi

sudo systemctl daemon-reload
sudo systemctl reset-failed asus-custom-fan.service 2>/dev/null || true

echo "ASUS custom fan controller removed; firmware/EC control restored."
