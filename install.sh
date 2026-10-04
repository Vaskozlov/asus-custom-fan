#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_REPO="https://github.com/Keyitdev/asus-fan-control-ec.git"
# Pin the reviewed upstream revision instead of silently changing EC write logic.
UPSTREAM_COMMIT="c3a8ba77bcc27e645946b770a56ee15abbbdfcee"
INSTALL_DIR="/usr/local/libexec/asus-custom-fan"
CONFIG_FILE="/etc/asus-custom-fan.conf"
SERVICE_FILE="/etc/systemd/system/asus-custom-fan.service"
CLI_FILE="/usr/local/bin/asus-custom-fan"
SERVICE="asus-custom-fan.service"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YES=0
NO_START=0

usage() {
    cat <<USAGE
Usage: ./install.sh [--yes] [--no-start]

  --yes       Accept the PM3606CKA 100% fan validation test without prompting.
  --no-start  Install everything but do not enable/start the service.
USAGE
}

while (($#)); do
    case "$1" in
        --yes|-y) YES=1 ;;
        --no-start) NO_START=1 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

log() { printf '\n==> %s\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

command -v sudo >/dev/null 2>&1 || die "sudo is required"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"

install_dependencies() {
    if command -v apt-get >/dev/null 2>&1; then
        log "Installing build/runtime dependencies with apt"
        sudo apt-get update
        sudo apt-get install -y git build-essential python3
    elif command -v dnf >/dev/null 2>&1; then
        log "Installing build/runtime dependencies with dnf"
        sudo dnf install -y git gcc make python3
    elif command -v pacman >/dev/null 2>&1; then
        log "Installing build/runtime dependencies with pacman"
        sudo pacman -S --needed --noconfirm git base-devel python
    else
        die "unsupported package manager; install git, make, a C11 compiler, and Python 3 manually"
    fi
}

extract_rpm() {
    awk '/^fan 0: [0-9]+ rpm$/ {print $3; exit}'
}

release_with() {
    local tool=$1
    sudo "$tool" setp -1 >/dev/null 2>&1 || true
}

pm3606_probe() {
    local tool=$1 baseline after set_output speed_output delta min_delta

    log "PM3606CKA / Healthy-table-v26 compatibility path"
    cat <<'WARN'
Upstream fan-info cannot validate this model because its hard-coded MMIO telemetry
address reads 0xFFFF on the PM3606CKA. On the tested PM3606CKA, however, the
private Healthy-table command channel is live: table v26 reports the real fan RPM
and registers 0x31/0x35 successfully provide manual mode/PWM control.

The next test temporarily requests 100% fan for 6 seconds and then ALWAYS hands
control back to the firmware. You should hear the fan accelerate.
WARN

    if (( ! YES )); then
        read -r -p "Run the 6-second 100% fan validation test? [y/N] " answer
        [[ "$answer" =~ ^[Yy]$ ]] || die "hardware validation declined"
    fi

    baseline=$(sudo "$tool" fan-speed | extract_rpm || true)
    [[ "$baseline" =~ ^[0-9]+$ ]] || die "could not read baseline fan RPM"
    echo "Baseline fan speed: ${baseline} rpm"

    trap 'release_with "$tool"' EXIT INT TERM
    set_output=$(sudo "$tool" setp 100) || {
        echo "$set_output" >&2
        release_with "$tool"
        die "100% fan write failed"
    }
    echo "$set_output"
    sleep 6
    speed_output=$(sudo "$tool" fan-speed)
    echo "$speed_output"
    after=$(printf '%s\n' "$speed_output" | extract_rpm || true)
    release_with "$tool"
    trap - EXIT INT TERM

    [[ "$after" =~ ^[0-9]+$ ]] || die "could not read fan RPM after the test"
    delta=$((after - baseline))
    min_delta=250
    echo "Fan speed change: ${baseline} -> ${after} rpm (${delta:+$delta} rpm)"
    (( delta >= min_delta )) || die "fan did not accelerate enough; refusing to install automatic control"
    echo "PM3606CKA manual fan control validated."
}

hardware_check() {
    local tool=$1 model version_out fan_info_out
    model="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true) $(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
    version_out=$(sudo "$tool" version 2>&1 || true)
    echo "$version_out"

    log "Running upstream read-mostly compatibility check"
    if fan_info_out=$(sudo "$tool" fan-info 2>&1); then
        echo "$fan_info_out"
        echo "Upstream MMIO/EC validation succeeded."
        return 0
    fi
    echo "$fan_info_out"

    if [[ "$model" == *"ASUS EXPERTBOOK PM3606CKA"* ]] && \
       grep -q 'Healthy table version: 26 ' <<<"$version_out"; then
        pm3606_probe "$tool"
        return 0
    fi

    cat >&2 <<EOF2

This machine did not pass upstream validation and is not the PM3606CKA / Healthy
Table v26 combination validated by this repository. No fan writes were enabled.

Detected model: $model

Do not bypass this check by guessing EC registers. Add and validate the model
explicitly first.
EOF2
    return 1
}

install_dependencies

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

log "Fetching pinned asus-fan-control-ec revision"
git -C "$tmpdir" init -q
git -C "$tmpdir" remote add origin "$UPSTREAM_REPO"
git -C "$tmpdir" fetch -q --depth 1 origin "$UPSTREAM_COMMIT"
git -C "$tmpdir" checkout -q FETCH_HEAD

log "Building low-level ASUS EC utility"
make -C "$tmpdir"
TOOL="$tmpdir/asus-fan-control-ec"
[[ -x "$TOOL" ]] || die "upstream build did not produce asus-fan-control-ec"

hardware_check "$TOOL"

log "Installing files"
sudo install -d -m 0755 "$INSTALL_DIR"
sudo install -m 0755 "$TOOL" "$INSTALL_DIR/asus-fan-control-ec"
sudo install -m 0755 "$SCRIPT_DIR/src/fan_curve.py" "$INSTALL_DIR/fan_curve.py"
sudo install -m 0755 "$SCRIPT_DIR/bin/asus-custom-fan" "$CLI_FILE"
sudo install -m 0644 "$SCRIPT_DIR/systemd/asus-custom-fan.service" "$SERVICE_FILE"

if [[ ! -e "$CONFIG_FILE" ]]; then
    sudo install -m 0644 "$SCRIPT_DIR/config/asus-custom-fan.conf" "$CONFIG_FILE"
else
    echo "Keeping existing $CONFIG_FILE"
fi

# An enabled upstream curve service would compete for the same global EC manual
# mode. We do not remove it, but stop/disable it if it is currently enabled.
if systemctl list-unit-files asus-fan-control-ec.service >/dev/null 2>&1; then
    if systemctl is-enabled --quiet asus-fan-control-ec.service 2>/dev/null || \
       systemctl is-active --quiet asus-fan-control-ec.service 2>/dev/null; then
        log "Disabling conflicting upstream asus-fan-control-ec.service"
        sudo systemctl disable --now asus-fan-control-ec.service || true
    fi
fi

log "Validating installed configuration and sensor"
/usr/local/libexec/asus-custom-fan/fan_curve.py --config "$CONFIG_FILE" --check

sudo systemctl daemon-reload
if (( NO_START )); then
    log "Installed without starting the service"
    echo "Start later with: sudo systemctl enable --now $SERVICE"
else
    log "Enabling custom fan curve"
    sudo systemctl enable --now "$SERVICE"
    sleep 1
    sudo systemctl --no-pager --full status "$SERVICE" || true
fi

cat <<'DONE'

Installation complete.

Useful commands:
  asus-custom-fan status
  asus-custom-fan rpm
  asus-custom-fan logs
  asus-custom-fan config
  asus-custom-fan release

Configuration:
  /etc/asus-custom-fan.conf
DONE
