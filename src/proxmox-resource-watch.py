#!/usr/bin/env python3
"""Lightweight, persistent Proxmox/Linux resource sampler."""
__version__ = "0.2.0"

import datetime as dt
import glob
import gzip
import json
import os
from pathlib import Path
import time
import tempfile
import stat
import signal
import threading
import sys

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
import prw_storage

INTERVAL = int(os.environ.get("PRW_INTERVAL", "10"))
RETENTION_DAYS = int(os.environ.get("PRW_RETENTION_DAYS", "30"))
LOG_DIR = Path(os.environ.get("PRW_LOG_DIR", "/var/log/proxmox-resource-watch"))
HZ = os.sysconf("SC_CLK_TCK")
os.umask(0o077)


def read(path):
    return Path(path).read_text().strip()


def cpu_ticks():
    values = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
    # guest/guest_nice are already included in user/nice. Count each tick once.
    return sum(values[:8]), values[3] + values[4]


def process_ticks():
    found = {}
    for stat_path in glob.glob("/proc/[0-9]*/stat"):
        try:
            pid = int(Path(stat_path).parent.name)
            text = read(stat_path)
            name, tail = text.rsplit(") ", 1)
            fields = tail.split()
            # After comm, fields[0] is /proc/PID/stat field 3.
            ticks = int(fields[11]) + int(fields[12])
            starttime = int(fields[19])  # field 22; distinguishes PID reuse
            name = name.split("(", 1)[1]
            try:
                cmd = (Path(f"/proc/{pid}/cmdline").read_bytes()
                       .replace(b"\0", b" ").decode(errors="replace").strip()[:160])
            except OSError:
                cmd = ""
            found[pid] = (ticks, starttime, name, cmd or name)
        except (OSError, ValueError, IndexError):
            continue
    return found


def sensors():
    result = {"gpu_busy_percent": None, "gpu_vram_used_bytes": None,
              "temperatures_c": {}, "fans_rpm": {}}
    for device in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        base = device / "device"
        try:
            busy = int(read(base / "gpu_busy_percent"))
            if result["gpu_busy_percent"] is None:
                result["gpu_busy_percent"] = busy
                try:
                    result["gpu_vram_used_bytes"] = int(read(base / "mem_info_vram_used"))
                except (OSError, ValueError):
                    pass
        except (OSError, ValueError):
            continue
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            name = read(hwmon / "name")
        except OSError:
            continue
        # Device symlink is stable across hwmon numbering changes on most hosts.
        try:
            device_id = hwmon.resolve().parent.parent.name
        except OSError:
            device_id = hwmon.name
        prefix = f"{name}:{device_id}"
        for sensor in sorted(hwmon.glob("temp*_input")):
            stem = sensor.name.removesuffix("_input")
            try:
                label = read(hwmon / f"{stem}_label")
            except OSError:
                label = stem
            try:
                result["temperatures_c"][f"{prefix}:{label}"] = round(int(read(sensor)) / 1000, 1)
            except (OSError, ValueError):
                continue
        for sensor in sorted(hwmon.glob("fan*_input")):
            try:
                result["fans_rpm"][f"{prefix}:{sensor.stem}"] = int(read(sensor))
            except (OSError, ValueError):
                continue
    return result


def maintenance(today):
    with prw_storage.log_lock(LOG_DIR):
        _maintenance(today)


def _maintenance(today):
    cutoff = today - dt.timedelta(days=RETENTION_DAYS - 1)
    # Do not compress yesterday: a late report can still read uncompressed data.
    for path in LOG_DIR.glob("????-??-??.jsonl"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if day < cutoff:
            path.unlink()
        elif day < today - dt.timedelta(days=1):
            dest = path.with_suffix(".jsonl.gz")
            if dest.exists():
                continue
            fd, tmp_name = tempfile.mkstemp(prefix=".compress-", dir=LOG_DIR)
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as raw:
                    source_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    source = os.fdopen(source_fd, "rb")
                    with source:
                        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                            raise OSError("Compression source must be regular")
                        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6) as output:
                            while chunk := source.read(1024 * 1024):
                                output.write(chunk)
                tmp.replace(dest)
            finally:
                tmp.unlink(missing_ok=True)
            path.unlink()
    for path in LOG_DIR.glob("????-??-??.jsonl.gz"):
        try:
            day = dt.date.fromisoformat(path.name[:10])
        except ValueError:
            continue
        if day < cutoff:
            path.unlink()


def append_sample(log_path, row):
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("Monitoring log must be a regular file")
    with os.fdopen(fd, "a", encoding="utf-8") as output:
        output.write(json.dumps(row, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def main():
    if INTERVAL < 1 or not 1 <= RETENTION_DAYS <= 3650:
        raise ValueError("Invalid interval or retention setting")
    if LOG_DIR.is_symlink():
        raise ValueError("Log directory must not be a symlink")
    LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.geteuid() == 0 and LOG_DIR.stat().st_uid != 0:
        raise ValueError("Log directory must be owned by root")
    os.chmod(LOG_DIR, 0o700)
    stopping = threading.Event()
    def request_stop(signum, frame):
        stopping.set()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    previous_total, previous_idle = cpu_ticks()
    previous_proc = process_ticks()
    previous_time = time.monotonic()
    previous_stamp = dt.datetime.now().astimezone()
    last_day = None
    while not stopping.wait(INTERVAL):
        now = time.monotonic()
        total, idle = cpu_ticks()
        elapsed = now - previous_time
        total_delta = total - previous_total
        busy_cpu = round(100 * (1 - (idle - previous_idle) / total_delta), 1) if total_delta > 0 else None
        current = process_ticks()
        processes = []
        for pid, (ticks, start, name, command) in current.items():
            old = previous_proc.get(pid)
            if old and old[1] == start and ticks >= old[0] and elapsed > 0:
                percent = round(100 * (ticks - old[0]) / HZ / elapsed, 1)
                if percent >= 0.5:
                    processes.append({"pid": pid, "name": name, "start_ticks": start,
                                      "cpu_percent_one_core": percent, "command": command})
        processes.sort(key=lambda p: p["cpu_percent_one_core"], reverse=True)
        try:
            load = [float(v) for v in read("/proc/loadavg").split()[:3]]
        except (OSError, ValueError):
            load = None
        stamp = dt.datetime.now().astimezone()
        day = stamp.date()
        if day != last_day:
            maintenance(day)
            last_day = day
        row = {"time": stamp.isoformat(timespec="seconds"), "sample_seconds": round(elapsed, 6),
               "clock_discontinuity": abs((stamp-previous_stamp).total_seconds()-elapsed) > 2,
               "host_cpu_busy_percent": busy_cpu, "load_average_1_5_15": load,
               **sensors(), "top_processes": processes[:5]}
        if stopping.is_set():
            break
        log_path = LOG_DIR / f"{day.isoformat()}.jsonl"
        with prw_storage.log_lock(LOG_DIR):
            if not stopping.is_set():
                append_sample(log_path, row)
        previous_total, previous_idle, previous_proc, previous_time = total, idle, current, now
        previous_stamp = stamp


if __name__ == "__main__":
    main()
