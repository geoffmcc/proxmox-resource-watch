#!/usr/bin/env python3
"""Check recent samples for temperature/CPU alerts; mail with durable cooldowns."""
__version__ = "0.1.0"

import collections
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys

LOG_DIR = Path(os.environ.get("PRW_LOG_DIR", "/var/log/proxmox-resource-watch"))
STATE = LOG_DIR / "alert-state.json"
INTERVAL = int(os.environ.get("PRW_INTERVAL", "10"))
WARNING_C = float(os.environ.get("PRW_CPU_WARNING_C", "85"))
CRITICAL_C = float(os.environ.get("PRW_CPU_CRITICAL_C", "92"))
WARNING_SECONDS = int(os.environ.get("PRW_WARNING_SECONDS", "30"))
CPU_WARNING_PERCENT = float(os.environ.get("PRW_ALERT_CPU_PERCENT", "80"))
CPU_WARNING_SECONDS = int(os.environ.get("PRW_ALERT_CPU_SECONDS", "300"))
COOLDOWN_SECONDS = int(os.environ.get("PRW_ALERT_COOLDOWN_SECONDS", "1800"))

spec = importlib.util.spec_from_file_location("prw_report", "/usr/local/sbin/proxmox-resource-report.py")
if not Path(spec.origin).is_file():
    spec = importlib.util.spec_from_file_location("prw_report", str(Path(__file__).with_name("proxmox-resource-report.py")))
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def tail_rows(path, limit=160 * 1024):
    if not path.exists():
        return []
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        start = max(0, size - limit)
        stream.seek(start)
        data = stream.read()
    if start:
        data = data.partition(b"\n")[2]
    result = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
            timestamp = dt.datetime.fromisoformat(row["time"])
            if timestamp.tzinfo is not None:
                result.append((timestamp, row))
        except (ValueError, KeyError, TypeError):
            continue
    return result


def recent(now):
    all_rows = []
    for day in (now.date() - dt.timedelta(days=1), now.date()):
        all_rows.extend(tail_rows(LOG_DIR / f"{day}.jsonl"))
    cutoff = now - dt.timedelta(seconds=max(CPU_WARNING_SECONDS, WARNING_SECONDS, 60) + 60)
    return [(t, row) for t, row in sorted(all_rows) if cutoff <= t <= now]


def top_cpu_temperature(row):
    values = [(v, name) for name, v in (row.get("temperatures_c") or {}).items()
              if isinstance(v, (float, int)) and reporter.is_cpu_sensor(name)]
    return max(values, default=(None, None))


def consecutive_duration(rows, predicate):
    """Return seconds of matching observations ending at the freshest sample."""
    if not rows or not predicate(rows[-1][1]):
        return 0
    first = rows[-1][0]
    last = first
    for stamp, row in reversed(rows[:-1]):
        if (last - stamp).total_seconds() > INTERVAL * 2.5 or not predicate(row):
            break
        first, last = stamp, stamp
    return (rows[-1][0] - first).total_seconds() + INTERVAL


def load_state():
    try:
        state = json.loads(STATE.read_text())
        if isinstance(state, dict):
            return state
    except (OSError, ValueError):
        pass
    return {}


def save_state(state):
    temp = STATE.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as out:
        json.dump(state, out, separators=(",", ":"))
        out.write("\n")
    os.chmod(temp, 0o600)
    temp.replace(STATE)


def candidate(now, rows):
    if not rows or (now - rows[-1][0]).total_seconds() > INTERVAL * 3:
        return None
    last_stamp, last = rows[-1]
    high, sensor = top_cpu_temperature(last)
    warning_duration = consecutive_duration(rows, lambda r:
        (lambda p: p[0] is not None and p[0] >= WARNING_C)(top_cpu_temperature(r)))
    cpu_duration = consecutive_duration(rows, lambda r:
        isinstance(r.get("host_cpu_busy_percent"), (float, int))
        and r["host_cpu_busy_percent"] >= CPU_WARNING_PERCENT)
    if high is not None and high >= CRITICAL_C:
        key, severity, reason = "temp_critical", "CRITICAL", f"CPU temperature reached {reporter.temp_format(high)}"
    elif high is not None and warning_duration >= WARNING_SECONDS:
        key, severity, reason = "temp_warning", "WARNING", (
            f"CPU temperature remained at or above {reporter.temp_format(WARNING_C)} "
            f"for approximately {warning_duration:.0f}s")
    elif cpu_duration >= CPU_WARNING_SECONDS:
        key, severity, reason = "cpu_warning", "WARNING", (
            f"Host CPU remained at or above {CPU_WARNING_PERCENT:g}% "
            f"for approximately {cpu_duration:.0f}s")
    else:
        return None
    return {"key": key, "severity": severity, "reason": reason, "sensor": sensor,
            "temperature": high, "stamp": last_stamp, "row": last, "rows": rows}


def format_alert(event):
    row = event["row"]
    host = __import__("socket").gethostname()
    title = f"[PVE {event['severity']}] {event['reason']} | {host}"
    lines = [f"Proxmox Resource Alert — {event['severity']}", "=" * 42,
             f"Host: {host}", f"Observed: {event['stamp'].isoformat()}",
             f"Reason: {event['reason']}",
             f"Host CPU usage: {row.get('host_cpu_busy_percent', 'unavailable')}%"]
    if event["temperature"] is not None:
        lines.append(f"CPU sensor: {event['sensor']} — {reporter.temp_format(event['temperature'])}")
    current = row.get("top_processes") or []
    earlier = [(t, r) for t, r in event["rows"] if 0 < (event["stamp"] - t).total_seconds() <= 30]
    before = collections.defaultdict(float)
    for _, older in earlier:
        for proc in older.get("top_processes") or []:
            name = proc.get("name", "unknown")
            before[name] = max(before[name], proc.get("cpu_percent_one_core", 0))
    lines += ["", "Processes active immediately before (last 30s):"]
    lines.extend(f"  {name}: {pct:.1f}% of one core" for name, pct in
                 sorted(before.items(), key=lambda pair: pair[1], reverse=True)[:5])
    if not before:
        lines.append("  Unavailable")
    lines += ["", "Processes observed during latest sample:"]
    lines.extend(f"  {p.get('name', 'unknown')}: {p.get('cpu_percent_one_core', 0):.1f}% of one core"
                 for p in current[:5])
    if not current:
        lines.append("  Unavailable")
    lines += ["", "Review current load, cooling and recent workloads.",
              "This alert does not shut down or modify the host.",
              "Observed processes are correlations, not proven causes.",
              "A warning/critical threshold is not the same as the hardware's published Tjmax.", ""]
    return title, "\n".join(lines)


def main():
    if not os.environ.get("PRW_REPORT_TO"):
        raise ValueError("PRW_REPORT_TO is required for immediate alerts")
    if not 0 < WARNING_C < CRITICAL_C or INTERVAL < 1 or COOLDOWN_SECONDS < 1:
        raise ValueError("Invalid alert thresholds or interval")
    now = dt.datetime.now().astimezone()
    rows = recent(now)
    state = load_state()
    if rows and (now - rows[-1][0]).total_seconds() <= INTERVAL * 3:
        latest = rows[-1][1]
        peak, _ = top_cpu_temperature(latest)
        cpu = latest.get("host_cpu_busy_percent")
        # Require one full minute below a hysteresis margin before re-arming.
        # A genuinely new episode may then alert even within the cooldown.
        for group, normal in (("temp", peak is not None and peak < WARNING_C - 3),
                              ("cpu", isinstance(cpu, (int, float)) and cpu < CPU_WARNING_PERCENT - 10)):
            key = f"{group}_recovery_since"
            if normal:
                if key not in state:
                    state[key] = now.isoformat()
                else:
                    try:
                        if (now - dt.datetime.fromisoformat(state[key])).total_seconds() >= 60:
                            keys = ("temp_warning", "temp_critical") if group == "temp" else ("cpu_warning",)
                            for alert_key in keys:
                                state.pop(alert_key, None)
                    except (ValueError, TypeError):
                        state[key] = now.isoformat()
            else:
                state.pop(key, None)
    save_state(state)
    event = candidate(now, rows)
    if not event:
        return
    last_sent = state.get(event["key"])
    if last_sent:
        try:
            if (now - dt.datetime.fromisoformat(last_sent)).total_seconds() < COOLDOWN_SECONDS:
                return
        except (ValueError, TypeError):
            pass
    title, body = format_alert(event)
    reporter.deliver(title, body, os.environ["PRW_REPORT_TO"], os.environ.get("PRW_MAILER", "auto"))
    state[event["key"]] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Alert check failed: {exc}", file=sys.stderr)
        raise
