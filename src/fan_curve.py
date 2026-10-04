#!/usr/bin/env python3
"""Temperature-driven manual fan controller for ASUS laptops.

This intentionally does NOT use asus-fan-control-ec's built-in `curve` command:
some newer ASUS ExpertBook models expose a newer Healthy table while the MMIO
telemetry addresses used by upstream remain model-specific. We use standard
Linux hwmon temperature sensors and only use the upstream utility for the
validated manual fan write/release path.
"""

from __future__ import annotations

import argparse
import configparser
import glob
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG = "/etc/asus-custom-fan.conf"
DEFAULT_TOOL = "/usr/local/libexec/asus-custom-fan/asus-fan-control-ec"


class FanError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    sensor: str
    interval: float
    hysteresis: int
    reassert: float
    log_interval: float
    panic_temp: int
    curve: tuple[tuple[int, int], ...]


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", flush=True)


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def hwmon_candidates() -> list[tuple[int, str, str]]:
    """Return (priority, description, temp_input_path) candidates."""
    result: list[tuple[int, str, str]] = []

    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read_text(os.path.join(hw, "name")) or "unknown"
        labels: dict[str, str] = {}
        for label_path in glob.glob(os.path.join(hw, "temp*_label")):
            label = read_text(label_path)
            if label:
                labels[label_path.removesuffix("_label")] = label

        for input_path in glob.glob(os.path.join(hw, "temp*_input")):
            stem = input_path.removesuffix("_input")
            label = labels.get(stem, "")
            desc = f"{name}/{label or Path(stem).name}"

            if name == "k10temp" and label == "Tctl":
                priority = 0
            elif name == "k10temp":
                priority = 10
            elif name == "coretemp" and label == "Package id 0":
                priority = 20
            elif name == "coretemp":
                priority = 30
            elif name in {"asus", "acpitz"}:
                priority = 70
            else:
                priority = 50

            result.append((priority, desc, input_path))

    # Last-resort thermal-zone sensors.
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        temp = os.path.join(zone, "temp")
        if os.path.isfile(temp):
            zone_type = read_text(os.path.join(zone, "type")) or Path(zone).name
            result.append((100, f"thermal/{zone_type}", temp))

    return sorted(result, key=lambda x: (x[0], x[1], x[2]))


def read_temp_c(path: str) -> int:
    raw = read_text(path)
    if raw is None:
        raise FanError(f"temperature sensor disappeared or is unreadable: {path}")
    try:
        value = float(raw)
    except ValueError as exc:
        raise FanError(f"invalid temperature value {raw!r} from {path}") from exc

    # Linux hwmon uses millidegrees Celsius. Some thermal-zone implementations
    # also do. Accept whole-degree sensors for portability.
    temp = value / 1000.0 if abs(value) >= 1000 else value
    if not -30.0 <= temp <= 150.0:
        raise FanError(f"implausible temperature {temp:.1f} C from {path}")
    return int(round(temp))


def choose_sensor(requested: str) -> tuple[str, str]:
    if requested != "auto":
        if not os.path.isfile(requested):
            raise FanError(f"configured sensor does not exist: {requested}")
        temp = read_temp_c(requested)
        return requested, f"configured sensor ({temp} C)"

    candidates = hwmon_candidates()
    for _priority, desc, path in candidates:
        try:
            temp = read_temp_c(path)
        except FanError:
            continue
        return path, f"{desc} ({temp} C)"

    raise FanError("no usable hwmon or thermal-zone temperature sensor found")


def load_settings(path: str) -> Settings:
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise FanError(f"configuration file not found: {path}")
    if "general" not in cp or "curve" not in cp:
        raise FanError("configuration must contain [general] and [curve]")

    g = cp["general"]
    sensor = g.get("sensor", "auto").strip()
    interval = g.getfloat("interval_seconds", 2.0)
    hysteresis = g.getint("hysteresis_c", 2)
    reassert = g.getfloat("reassert_seconds", 20.0)
    log_interval = g.getfloat("log_seconds", 60.0)
    panic_temp = g.getint("panic_temp_c", 90)

    if interval < 0.25:
        raise FanError("interval_seconds must be >= 0.25")
    if hysteresis < 0 or hysteresis > 20:
        raise FanError("hysteresis_c must be between 0 and 20")
    if reassert < 0:
        raise FanError("reassert_seconds must be >= 0")
    if log_interval < 0:
        raise FanError("log_seconds must be >= 0")
    if not 50 <= panic_temp <= 120:
        raise FanError("panic_temp_c must be between 50 and 120")

    points: list[tuple[int, int]] = []
    for temp_s, percent_s in cp["curve"].items():
        try:
            temp = int(temp_s)
            percent = int(percent_s)
        except ValueError as exc:
            raise FanError(f"invalid curve entry {temp_s!r} = {percent_s!r}") from exc
        if not -30 <= temp <= 150:
            raise FanError(f"curve temperature out of range: {temp}")
        if not 0 <= percent <= 100:
            raise FanError(f"fan percentage out of range at {temp} C: {percent}")
        points.append((temp, percent))

    points.sort()
    if not points:
        raise FanError("curve must contain at least one point")
    if len({t for t, _ in points}) != len(points):
        raise FanError("curve contains duplicate temperature thresholds")

    return Settings(
        sensor=sensor,
        interval=interval,
        hysteresis=hysteresis,
        reassert=reassert,
        log_interval=log_interval,
        panic_temp=panic_temp,
        curve=tuple(points),
    )


def raw_band(curve: tuple[tuple[int, int], ...], temp: int) -> int:
    idx = 0
    for i, (threshold, _percent) in enumerate(curve):
        if temp >= threshold:
            idx = i
        else:
            break
    return idx


def select_band(
    curve: tuple[tuple[int, int], ...], temp: int, current: int | None, hysteresis: int
) -> int:
    wanted = raw_band(curve, temp)
    if current is None or wanted >= current:
        return wanted

    # Hold the current band until the temperature is H degrees below the
    # threshold that originally caused us to enter it.
    current_threshold = curve[current][0]
    if temp > current_threshold - hysteresis:
        return current
    return wanted


def run_tool(tool: str, *args: str, quiet: bool = False) -> str:
    try:
        completed = subprocess.run(
            [tool, *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FanError(f"{' '.join((tool, *args))} failed: {exc}") from exc
    if not quiet and completed.stdout.strip():
        for line in completed.stdout.rstrip().splitlines():
            log(f"fan-tool: {line}")
    return completed.stdout


def set_percent(tool: str, percent: int, quiet: bool = True) -> None:
    run_tool(tool, "setp", str(percent), quiet=quiet)


def release(tool: str) -> None:
    try:
        run_tool(tool, "setp", "-1", quiet=True)
    except FanError as exc:
        log(f"WARNING: failed to hand fan control back to EC: {exc}")


def describe_curve(curve: tuple[tuple[int, int], ...]) -> str:
    return ", ".join(f">={t}C:{p}%" for t, p in curve)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--tool", default=os.environ.get("ASUS_FAN_TOOL", DEFAULT_TOOL))
    parser.add_argument("--check", action="store_true", help="validate config/sensor and exit")
    args = parser.parse_args()

    if os.geteuid() != 0 and not args.check:
        print("This daemon must run as root.", file=sys.stderr)
        return 2

    try:
        settings = load_settings(args.config)
        sensor_path, sensor_desc = choose_sensor(settings.sensor)
    except FanError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    log(f"sensor={sensor_path} ({sensor_desc})")
    log(f"curve={describe_curve(settings.curve)} hysteresis={settings.hysteresis}C")

    if args.check:
        try:
            temp = read_temp_c(sensor_path)
        except FanError as exc:
            print(f"Sensor error: {exc}", file=sys.stderr)
            return 2
        log(f"configuration OK; current temperature={temp} C")
        return 0

    stop = False

    def on_signal(signum: int, _frame: object) -> None:
        nonlocal stop
        log(f"received signal {signum}; releasing fan control")
        stop = True

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)

    current_band: int | None = None
    current_percent: int | None = None
    last_apply = 0.0
    last_log = 0.0

    try:
        while not stop:
            try:
                temp = read_temp_c(sensor_path)
            except FanError:
                # Sensor failure is not a reason to leave a manual fan value
                # pinned indefinitely. Release first, then let systemd retry.
                release(args.tool)
                raise

            if temp >= settings.panic_temp:
                target_band = len(settings.curve) - 1
                target_percent = 100
                panic = True
            else:
                target_band = select_band(
                    settings.curve, temp, current_band, settings.hysteresis
                )
                target_percent = settings.curve[target_band][1]
                panic = False

            now = time.monotonic()
            need_transition = current_band != target_band or current_percent != target_percent
            need_reassert = (
                settings.reassert > 0
                and current_percent is not None
                and now - last_apply >= settings.reassert
            )

            if current_percent is None or need_transition or need_reassert:
                old_percent = "EC" if current_percent is None else f"{current_percent}%"
                set_percent(args.tool, target_percent, quiet=True)
                current_band = target_band
                current_percent = target_percent
                last_apply = now
                reason = "panic" if panic else ("transition" if need_transition else "reassert")
                log(f"temp={temp}C target={target_percent}% old={old_percent} reason={reason}")

            if settings.log_interval > 0 and now - last_log >= settings.log_interval:
                log(f"temp={temp}C holding={target_percent}%")
                last_log = now

            # Sleep in short pieces so SIGTERM shutdown is responsive.
            deadline = time.monotonic() + settings.interval
            while not stop and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    except FanError as exc:
        log(f"ERROR: {exc}")
        return 1
    finally:
        release(args.tool)
        log("fan control handed back to EC")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
