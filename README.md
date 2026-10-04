# ASUS Custom Fan Curve for Linux

A small installer and systemd service for **real manual fan-speed control** on
compatible ASUS laptops where Linux exposes no normal writable `pwm*` hwmon
interface.

The default curve is:

| Temperature | Fan speed |
|---:|---:|
| below 60 °C | 0% |
| 60–64 °C | 25% |
| 65–69 °C | 35% |
| 70–74 °C | 50% |
| 75–79 °C | 75% |
| 80 °C and above | 100% |

A 2 °C hysteresis is used while stepping down, so the fan does not oscillate
between adjacent bands around a threshold.

## Why this exists

Some ASUS machines do not expose fan PWM through Linux hwmon/ACPI even though
the embedded controller (EC) can control the fan directly. This repository uses
[`Keyitdev/asus-fan-control-ec`](https://github.com/Keyitdev/asus-fan-control-ec)
for the low-level EC write path, but **does not use its built-in temperature
curve**. Instead, temperatures come from normal Linux hwmon sensors such as AMD
`k10temp/Tctl` or Intel `coretemp`.

That distinction matters for newer machines such as the **ASUS ExpertBook
PM3606CKA**: the private ASUS Healthy table works, but the upstream utility's
hard-coded MMIO telemetry layout does not match this model.

## Confirmed PM3606CKA behavior

On an ASUS ExpertBook PM3606CKA with **Healthy table version 26**:

- `0x30` reports one fan;
- `0x31` switches between EC automatic and manual control;
- `0x33/0x34` report live fan RPM;
- writing manual duty through `0x35` works;
- 100% manual control produced roughly 4,800 RPM in testing;
- the upstream MMIO tachometer address returns `0xFFFF`, so upstream
  `fan-info` reports `MISMATCH` even though the table-command control path works;
- the older hard-coded MMIO temperature/EC-version fields are therefore not
  used by this repository's daemon.

This is a hardware-specific interface. **Do not assume that another ASUS model
uses the same register layout just because it is also an ExpertBook.**

## Installation

Clone the repository and run:

```bash
./install.sh
```

For unattended installation on an already-understood PM3606CKA:

```bash
./install.sh --yes
```

The installer:

1. installs the small set of build/runtime dependencies;
2. downloads a **pinned** revision of `asus-fan-control-ec`;
3. builds it from source;
4. validates the hardware before installing automatic control;
5. installs the EC utility under `/usr/local/libexec/asus-custom-fan/`;
6. installs the temperature-driven controller;
7. installs `/etc/asus-custom-fan.conf` without overwriting an existing config;
8. installs and enables `asus-custom-fan.service`.

### Hardware validation rules

The installer deliberately does not treat every ASUS laptop as compatible.

- If upstream `fan-info` validates the EC against its independent MMIO
  tachometer source, installation proceeds.
- If the machine is exactly **ASUS ExpertBook PM3606CKA** and reports **Healthy
  table v26**, the installer uses the model-specific validation path discovered
  on this hardware: it reads baseline RPM, temporarily requests 100% for six
  seconds, verifies a meaningful RPM increase, and releases control back to the
  firmware.
- Other machines that fail upstream validation are rejected. Add explicit model
  validation rather than bypassing the check.

The PM3606CKA write test is intentionally **100%**, not 0% or a low value: if a
register assumption is wrong, a short maximum-cooling test is much safer than a
short fan-stop test.

## Configuration

Edit:

```text
/etc/asus-custom-fan.conf
```

or simply run:

```bash
asus-custom-fan config
```

Default configuration:

```ini
[general]
sensor = auto
interval_seconds = 2.0
hysteresis_c = 2
reassert_seconds = 20
log_seconds = 60
panic_temp_c = 90

[curve]
0 = 0
60 = 25
65 = 35
70 = 50
75 = 75
80 = 100
```

The controller reasserts the current manual speed periodically because some
firmware events/profile transitions may return the EC to automatic control.

### Temperature sensor selection

`sensor = auto` prefers, in order:

1. AMD `k10temp` with the `Tctl` label;
2. another `k10temp` sensor;
3. Intel `coretemp` package temperature;
4. another hwmon temperature sensor;
5. a Linux thermal-zone sensor as a fallback.

For a fixed sensor, set its sysfs path explicitly, for example:

```ini
sensor = /sys/class/hwmon/hwmon7/temp1_input
```

Because `hwmonN` numbering can change across boots, `auto` is preferable unless
you create a stable symlink/udev rule.

## Commands

```bash
asus-custom-fan status       # service status
asus-custom-fan rpm          # EC mode + actual fan RPM
asus-custom-fan logs         # follow service journal
asus-custom-fan check-config # validate config/sensor
asus-custom-fan config       # edit config and restart
asus-custom-fan restart
asus-custom-fan stop
asus-custom-fan start
asus-custom-fan release      # stop service + return control to EC
```

To inspect directly:

```bash
sudo /usr/local/libexec/asus-custom-fan/asus-fan-control-ec fan-speed
```

## Safety behavior

Several safeguards are intentional:

- On normal shutdown, SIGTERM, SIGINT, or SIGHUP, the daemon calls `setp -1` and
  returns fan control to ASUS firmware.
- systemd repeats `setp -1` with `ExecStopPost` as a second safety net.
- If temperature reading fails, the daemon releases the fan to firmware before
  exiting.
- `panic_temp_c` forces 100% independently of the configured curve.
- The installer refuses unknown models that fail the upstream compatibility
  check.
- The low-level dependency is pinned to a reviewed Git commit so an upstream
  change cannot silently alter EC write behavior during installation.

**Important:** `0%` means actual *manual fan off*. It does not mean "automatic".
If you prefer ASUS firmware control below 60 °C instead of a stopped fan, this
repository would need a per-band "release to EC" mode rather than `0%`.

## Service

```bash
sudo systemctl status asus-custom-fan
journalctl -fu asus-custom-fan
```

The service runs as root because the underlying EC program needs raw I/O access
to the ASUS controller ports.

## Uninstall

Keep your configuration:

```bash
./uninstall.sh
```

Remove the configuration as well:

```bash
./uninstall.sh --purge
```

The uninstaller stops the service and explicitly gives fan control back to the
firmware before removing the low-level utility.

## Development

Run local syntax/unit tests:

```bash
make check
```

The tests cover the curve-selection and hysteresis logic. CI performs the same
checks and does **not** perform any hardware/EC writes.

## Upstream and licensing

This repository's installer/daemon code is MIT licensed.

The low-level utility is downloaded at install time from
[`Keyitdev/asus-fan-control-ec`](https://github.com/Keyitdev/asus-fan-control-ec)
and remains subject to its own **GPLv3+** license. It is not copied into this
repository. The exact upstream commit used by `install.sh` is pinned for
reproducibility and safety.
