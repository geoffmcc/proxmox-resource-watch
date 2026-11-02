#!/usr/bin/env python3
"""Plain-text daily digest for proxmox-resource-watch.py (also reads v1 logs)."""
__version__ = "0.1.0"

import argparse
import collections
import datetime as dt
from email.message import EmailMessage
from email import policy
import gzip
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import socket

LOG_DIR = Path(os.environ.get("PRW_LOG_DIR", "/var/log/proxmox-resource-watch"))
LEGACY = Path("/var/log/proxmox-resource-watch.jsonl")
CPU_THRESHOLD = float(os.environ.get("PRW_CPU_THRESHOLD", "8"))
TEMP_THRESHOLD = float(os.environ.get("PRW_TEMP_THRESHOLD", "75"))
INTERVAL = int(os.environ.get("PRW_INTERVAL", "10"))
SUSTAINED_SECONDS = int(os.environ.get("PRW_SUSTAINED_SECONDS", "30"))
CPU_WARNING_C = float(os.environ.get("PRW_CPU_WARNING_C", "85"))
CPU_CRITICAL_C = float(os.environ.get("PRW_CPU_CRITICAL_C", "92"))


def temp_format(celsius, decimals=1):
    return f"{celsius:.{decimals}f}°C [{celsius * 9 / 5 + 32:.1f}°F]"


def is_cpu_sensor(label):
    lower = label.lower()
    return ("k10temp" in lower or "cpu_" in lower or lower.startswith("cpu:")
            or "coretemp" in lower or "tctl" in lower or "tdie" in lower)


def rows_for(day):
    sources = [LOG_DIR / f"{day}.jsonl", LOG_DIR / f"{day}.jsonl.gz",
               LEGACY, LEGACY.with_suffix(".jsonl.1")]
    rows, seen, errors = [], set(), 0
    for path in sources:
        if not path.is_file():
            continue
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        stamp = dt.datetime.fromisoformat(row["time"])
                        if stamp.date().isoformat() != day:
                            continue
                        key = (row["time"], json.dumps(row, sort_keys=True))
                        if key not in seen:
                            seen.add(key)
                            rows.append((stamp, row))
                    except (ValueError, KeyError, TypeError):
                        errors += 1
        except OSError as exc:
            print(f"Warning: cannot read {path}: {exc}", file=sys.stderr)
            errors += 1
    rows.sort(key=lambda pair: pair[0])
    return rows, errors


def fmt_stats(values, unit="", decimals=1):
    if not values:
        return "unavailable"
    return (f"avg {statistics.mean(values):.{decimals}f}{unit}, "
            f"min {min(values):.{decimals}f}{unit}, max {max(values):.{decimals}f}{unit}")


def events(rows):
    groups = []
    active = None
    preceding = collections.deque(maxlen=max(1, 30 // max(INTERVAL, 1)))
    for stamp, row in rows:
        cpu = row.get("host_cpu_busy_percent")
        temps = {k: v for k, v in (row.get("temperatures_c") or {}).items()
                 if isinstance(v, (int, float))}
        hot = {k: v for k, v in temps.items() if v >= TEMP_THRESHOLD}
        triggered = ((isinstance(cpu, (int, float)) and cpu >= CPU_THRESHOLD) or bool(hot))
        if not triggered:
            if active:
                groups.append(active)
                active = None
            preceding.append((stamp, row))
            continue
        if active and (stamp - active["last"]).total_seconds() > INTERVAL * 2.5:
            groups.append(active)
            active = None
        if active is None:
            before = collections.defaultdict(float)
            for earlier_time, earlier_row in preceding:
                if 0 < (stamp - earlier_time).total_seconds() <= 30:
                    for proc in earlier_row.get("top_processes") or []:
                        before[proc.get("name", "unknown")] = max(
                            before[proc.get("name", "unknown")], proc.get("cpu_percent_one_core", 0))
            active = {"first": stamp, "last": stamp, "samples": 0, "cpu_peak": 0.0,
                      "temp_peak": 0.0, "hot_sensor": "", "processes": collections.defaultdict(float),
                      "before": before}
        active["last"] = stamp
        active["samples"] += 1
        if isinstance(cpu, (int, float)):
            active["cpu_peak"] = max(active["cpu_peak"], cpu)
        for sensor, value in temps.items():
            if value > active["temp_peak"]:
                active["temp_peak"] = value
                active["hot_sensor"] = sensor
        for proc in row.get("top_processes") or []:
            name = proc.get("name", "unknown")
            active["processes"][name] = max(active["processes"][name],
                                             proc.get("cpu_percent_one_core", 0))
        preceding.append((stamp, row))
    if active:
        groups.append(active)
    return sorted(groups, key=lambda e: (e["cpu_peak"], e["temp_peak"]), reverse=True)


def report(day, rows, errors):
    title = f"Proxmox Resource Report | {socket.gethostname()} | {day}"
    out = [title, "=" * len(title), "", f"Samples: {len(rows)} (expected ~{86400 // INTERVAL})"]
    if errors:
        out.append(f"Log read/parse errors: {errors}")
    if not rows:
        out.extend(["", "No samples found for this date. Check the watcher service, log retention and filesystem."])
        return title, "\n".join(out) + "\n"
    timestamps = [stamp for stamp, _ in rows]
    gaps = [(a, b, (b - a).total_seconds()) for a, b in zip(timestamps, timestamps[1:])
            if (b - a).total_seconds() > INTERVAL * 2.5]
    out += [f"Coverage: {timestamps[0]:%H:%M:%S} to {timestamps[-1]:%H:%M:%S} (record timestamps)",
            f"Sampling gaps > {INTERVAL * 2.5:g}s: {len(gaps)}", ""]
    cpu = [r["host_cpu_busy_percent"] for _, r in rows if isinstance(r.get("host_cpu_busy_percent"), (int, float))]
    gpu = [r["gpu_busy_percent"] for _, r in rows if isinstance(r.get("gpu_busy_percent"), (int, float))]
    out += ["RESOURCE SUMMARY", "----------------", "", f"Host CPU: {fmt_stats(cpu, '%')}",
            f"GPU busy: {fmt_stats(gpu, '%')}"]
    sensor_values = collections.defaultdict(list)
    fan_values = collections.defaultdict(list)
    process_scores = collections.defaultdict(lambda: {"peak": 0.0, "samples": 0})
    for _, row in rows:
        for label, value in (row.get("temperatures_c") or {}).items():
            if isinstance(value, (float, int)):
                sensor_values[label].append(value)
        for label, value in (row.get("fans_rpm") or {}).items():
            if isinstance(value, (float, int)):
                fan_values[label].append(value)
        for proc in row.get("top_processes") or []:
            name = proc.get("name", "unknown")
            score = process_scores[name]
            score["peak"] = max(score["peak"], proc.get("cpu_percent_one_core", 0))
            score["samples"] += 1
    out += ["", "TEMPERATURES", ""]
    out += [f"  {name}: avg {temp_format(statistics.mean(values))}, "
            f"min {temp_format(min(values))}, max {temp_format(max(values))}"
            for name, values in sorted(sensor_values.items())] or ["  Unavailable"]
    all_peaks = [(max(values), name) for name, values in sensor_values.items()]
    out += ["", "PEAK TEMPERATURE AND ASSESSMENT", "-------------------------------", ""]
    if all_peaks:
        value, label = max(all_peaks)
        out.append(f"Highest recorded sensor: {temp_format(value)} ({label})")
        cpu_peaks = [(max(values), name) for name, values in sensor_values.items() if is_cpu_sensor(name)]
        if cpu_peaks:
            cpu_peak, cpu_label = max(cpu_peaks)
            out.append(f"Highest CPU sensor: {temp_format(cpu_peak)} ({cpu_label})")
            if cpu_peak >= CPU_CRITICAL_C:
                out.append("Status: CRITICAL — configured CPU maximum reached or exceeded; investigate cooling/load promptly.")
            elif cpu_peak >= CPU_WARNING_C:
                out.append("Status: WARNING — CPU temperature entered the configured high-temperature range.")
            elif cpu_peak >= TEMP_THRESHOLD:
                out.append("Status: ELEVATED — brief or sustained warmth observed; below configured warning level.")
            else:
                out.append("Status: OK — no CPU readings exceeded the configured temperature event threshold.")
            out.append(f"CPU thresholds: warning {temp_format(CPU_WARNING_C)}, critical {temp_format(CPU_CRITICAL_C)}.")
            out.append("These thresholds must be appropriate for your processor; they are not universal hardware limits.")
        else:
            out.append("Status: UNKNOWN — no recognizable CPU-temperature sensor in the log.")
        out.append("Other sensor types (GPU, NVMe, memory, etc.) require their own device-specific limits.")
    else:
        out.append("Status: UNKNOWN — no temperature sensor readings available.")
    out += ["", "FAN SPEEDS", ""]
    out += [f"  {name}: {fmt_stats(values, ' RPM', 0)}" for name, values in sorted(fan_values.items())] or ["  Unavailable"]
    out += ["", "TOP PROCESSES (observed one-core CPU peaks)", "-------------------------------------------", ""]
    top = sorted(process_scores.items(), key=lambda kv: kv[1]["peak"], reverse=True)[:10]
    out += [f"  {name[:27]:27} peak {stats['peak']:6.1f}% | seen in {stats['samples']} samples"
            for name, stats in top] or ["  No processes above sampling threshold"]
    out += ["", f"THRESHOLD EVENTS (CPU >= {CPU_THRESHOLD:g}% or sensor >= {temp_format(TEMP_THRESHOLD)})",
            "----------------------------------------------------------", ""]
    found = events(rows)
    sustained = [e for e in found if e["samples"] * INTERVAL >= SUSTAINED_SECONDS]
    brief = [e for e in found if e["samples"] * INTERVAL < SUSTAINED_SECONDS]
    def append_events(items, limit):
        for index, event in enumerate(items[:limit]):
            if index:
                out.append("")
            during = sorted(event["processes"].items(), key=lambda item: item[1], reverse=True)[:3]
            before = sorted(event["before"].items(), key=lambda item: item[1], reverse=True)[:3]
            duration = max(INTERVAL, (event["last"] - event["first"]).total_seconds() + INTERVAL)
            out.append(f"  {event['first']:%H:%M:%S}-{event['last']:%H:%M:%S} (~{duration:.0f}s), "
                       f"CPU max {event['cpu_peak']:.1f}%, temp max {temp_format(event['temp_peak'])}")
            out.append("    Before (up to 30s): " + (
                ", ".join(f"{n} {p:.1f}%" for n, p in before) if before else "unavailable"))
            out.append("    During: " + (
                ", ".join(f"{n} {p:.1f}%" for n, p in during) if during else "unavailable"))
            if event["hot_sensor"]:
                out.append("    Hottest sensor: " + event["hot_sensor"])
    out += ["", f"SUSTAINED EVENTS (>= {SUSTAINED_SECONDS}s): {len(sustained)}; showing top 10", ""]
    append_events(sustained, 10)
    if not sustained:
        out.append("  None recorded")
    out += ["", f"BRIEF SPIKES (< {SUSTAINED_SECONDS}s): {len(brief)}; showing top 10", ""]
    append_events(brief, 10)
    if not brief:
        out.append("  None recorded")
    if gaps:
        out += ["", "LARGEST SAMPLING GAPS", ""]
        for start, end, seconds in sorted(gaps, key=lambda item: item[2], reverse=True)[:5]:
            out.append(f"  {start:%H:%M:%S} to {end:%H:%M:%S}: {seconds:.0f}s")
    out += ["", "NOTES", "-----", "",
            "Events are correlations, not proof that a process caused the heat or fan change.",
            "Processes are sampled; very short-lived processes can be missed.",
            "Per-process CPU is percent of one logical core; host CPU is across all cores.",
            "Missing sensor data means unavailable, not zero.", ""]
    return title, "\n".join(out)


def deliver(subject, body, recipient, mailer):
    if not recipient or any(c in recipient for c in "\r\n") or recipient.startswith("-"):
        raise ValueError("Set a valid PRW_REPORT_TO email address")
    if mailer == "auto":
        mailer = "mutt" if shutil.which("mutt") else "sendmail"
    if mailer == "mutt":
        cmd = ["mutt", "-e", "set copy=no", "-s", subject, "--", recipient]
        message = body
    elif mailer == "sendmail":
        cmd = ["/usr/sbin/sendmail", "-t", "-oi"]
        mail = EmailMessage(policy=policy.SMTP)
        mail["To"] = recipient
        mail["Subject"] = subject
        mail.set_content(body, subtype="plain", charset="utf-8")
        message = mail.as_bytes()
    else:
        raise ValueError("Mailer must be auto, mutt, or sendmail")
    if isinstance(message, bytes):
        subprocess.run(cmd, input=message, check=True, timeout=90)
    else:
        subprocess.run(cmd, input=message, text=True, check=True, timeout=90)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Calendar day YYYY-MM-DD (default yesterday)")
    parser.add_argument("--send", action="store_true", help="Send via selected mailer; default prints report")
    parser.add_argument("--mailer", choices=["auto", "mutt", "sendmail"],
                        default=os.environ.get("PRW_MAILER", "auto"))
    args = parser.parse_args()
    day = args.date or (dt.datetime.now().astimezone().date() - dt.timedelta(days=1)).isoformat()
    dt.date.fromisoformat(day)
    subject, body = report(day, *rows_for(day))
    if args.send:
        deliver(subject, body, os.environ.get("PRW_REPORT_TO", ""), args.mailer)
    else:
        print(body, end="")


if __name__ == "__main__":
    main()
