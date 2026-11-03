#!/usr/bin/env python3
"""Check recent samples for temperature/CPU alerts; mail with durable cooldowns."""
__version__ = "0.2.0"

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import re

LOG_DIR = Path(os.environ.get("PRW_LOG_DIR", "/var/log/proxmox-resource-watch"))
STATE = LOG_DIR / "alert-state.json"
INTERVAL = int(os.environ.get("PRW_INTERVAL", "10"))
WARNING_C = float(os.environ.get("PRW_CPU_WARNING_C", "85"))
CRITICAL_C = float(os.environ.get("PRW_CPU_CRITICAL_C", "92"))
WARNING_SECONDS = int(os.environ.get("PRW_WARNING_SECONDS", "30"))
CPU_WARNING_PERCENT = float(os.environ.get("PRW_ALERT_CPU_PERCENT", "80"))
CPU_WARNING_SECONDS = int(os.environ.get("PRW_ALERT_CPU_SECONDS", "300"))
COOLDOWN_SECONDS = int(os.environ.get("PRW_ALERT_COOLDOWN_SECONDS", "1800"))

spec = importlib.util.spec_from_file_location("prw_report", str(Path(__file__).with_name("proxmox-resource-report.py")))
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)

spec = importlib.util.spec_from_file_location("prw_followup", str(Path(__file__).with_name("prw_followup.py")))
followups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(followups)


def tail_rows(path, limit=160 * 1024):
    try:
        with reporter.regular_open(path) as stream:
            size = os.fstat(stream.fileno()).st_size
            start = max(0, size - limit)
            stream.seek(start)
            data = stream.read(limit)
    except OSError:
        return []
    if start:
        data = data.partition(b"\n")[2]
    result = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
            timestamp = reporter.timestamp(row["time"])
            result.append((timestamp, row))
        except (ValueError, KeyError, TypeError, RecursionError):
            continue
    return result


def recent(now):
    all_rows = []
    for day in (now.date() - dt.timedelta(days=1), now.date()):
        all_rows.extend(tail_rows(LOG_DIR / f"{day}.jsonl"))
    cutoff = now - dt.timedelta(seconds=max(CPU_WARNING_SECONDS, WARNING_SECONDS, 60) + 60)
    normalized = reporter.normalize_rows(all_rows)
    return reporter.NormalizedRows([(t, row) for t, row in normalized if cutoff <= t <= now], normalized.clock_uncertain)


def top_cpu_temperature(row):
    values = [(v, name) for name, v in (row.get("temperatures_c") or {}).items()
              if reporter.finite(v) and reporter.is_cpu_sensor(name)]
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
        with reporter.regular_open(STATE) as stream:
            state = json.loads(stream.read(65536))
        if isinstance(state, dict):
            return {k: v for k, v in state.items() if isinstance(v, str) or
                    (k == "temp_sensors" and isinstance(v, list) and all(isinstance(n, str) for n in v))}
    except (OSError, ValueError, RecursionError):
        pass
    return {}


def save_state(state):
    fd, name = tempfile.mkstemp(prefix=".alert-state-", dir=STATE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, separators=(",", ":"))
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, STATE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def candidate(now, rows):
    rows = reporter.normalize_rows(rows)
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
    lines += ["", ("A separate supplemental context message may follow; fuller diagnostics are in the daily report."
                   if followups.enabled() else "Optional process and workload diagnostics are deferred to the daily report.")]
    lines += ["", "Only sampled top processes are available; CPU contributors are estimated.",
              "Review current load, cooling and recent workloads.",
              "This alert does not shut down or modify the host.",
              "Observed processes are correlations, not proven causes.",
              "A warning/critical threshold is not the same as the hardware's published Tjmax.", ""]
    return title, re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", "\n".join(lines))


def discord_alert(event):
    cpu = event['row'].get('host_cpu_busy_percent')
    cpu_text = f"{cpu:.1f}%" if reporter.finite(cpu) else "unavailable"
    threshold = (f"Immediate ≥ {reporter.temp_format(CRITICAL_C)}" if event['key'] == 'temp_critical'
                 else event['reason'])
    description = (f"🚨 ALERT\nHost: {__import__('socket').gethostname()}\nAlert: {event['key']}\n"
                   f"Observed: {event['stamp'].isoformat()}\n{threshold}\n\n"
                   f"🌡️ TEMPERATURE\nSensor: {event['sensor'] or 'unavailable'}\n"
                   f"Temperature: {reporter.temp_format(event['temperature']) if event['temperature'] is not None else 'unavailable'}\n"
                   f"\n💻 CPU\nHost CPU: {cpu_text}")
    return reporter.discord.payload(f"{event['severity']} — Resource Watch", description,
                                   0xE74C3C if event['severity'] == 'CRITICAL' else 0xE67E22)


def update_recovery(now, rows, state):
    """Re-arm only from a continuous observed cool suffix, including skipped checks."""
    responsible = state.get("temp_sensors", [])
    def cool_temp(row):
        values = row["temperatures_c"]
        peak, _ = top_cpu_temperature(row)
        return (bool(responsible) and peak is not None and peak < WARNING_C - 3
                and all(reporter.finite(values.get(name)) and values[name] < WARNING_C - 3
                        for name in responsible))
    def cool_cpu(row):
        value = row.get("host_cpu_busy_percent")
        return reporter.finite(value) and value < CPU_WARNING_PERCENT - 10
    for group, predicate in (("temp", cool_temp), ("cpu", cool_cpu)):
        key = group + "_recovery_since"
        state.pop(key, None)
        if rows.clock_uncertain or not rows or not 0 <= (now-rows[-1][0]).total_seconds() <= INTERVAL*3:
            continue
        first = last = rows[-1][0]
        for stamp, row in reversed(rows):
            if (last-stamp).total_seconds() > INTERVAL*2.5 or not predicate(row):
                break
            first = last = stamp
        if not predicate(rows[-1][1]):
            continue
        state[key] = first.isoformat()
        if (rows[-1][0]-first).total_seconds() >= 60:
            keys = ("temp_warning", "temp_critical") if group == "temp" else ("cpu_warning",)
            for alert_key in keys:
                state.pop(alert_key, None)
                state.pop("discord_" + alert_key, None)
                state.pop("discord_retry_" + alert_key, None)
            followups.rearm(state, group)
            if group == "temp":
                state.pop("temp_sensors", None)


def diagnose_monitoring(now, rows, state):
    """Journal-visible health transitions; never send health mail ahead of critical mail."""
    if not rows:
        health = "unavailable: no usable recent monitoring samples"
    elif not 0 <= (now-rows[-1][0]).total_seconds() <= INTERVAL*3:
        health = "unavailable: monitoring samples are stale"
    elif top_cpu_temperature(rows[-1][1])[0] is None:
        health = "degraded: CPU temperature unavailable"
    elif any(name not in rows[-1][1]["temperatures_c"] for name in state.get("temp_sensors", [])):
        health = "degraded: responsible CPU sensor unavailable; recovery unknown"
    elif not reporter.finite(rows[-1][1].get("host_cpu_busy_percent")):
        health = "degraded: host CPU usage unavailable"
    elif rows.clock_uncertain:
        health = "degraded: clock ordering uncertain"
    else:
        health = "available"
    if state.get("monitoring_health") != health:
        try:
            print("Resource Watch monitoring health: " + health, file=sys.stderr)
        except OSError:
            pass  # Diagnostic output must not suppress critical delivery.
    state["monitoring_health"] = health


def main():
    if not os.environ.get("PRW_REPORT_TO") and not reporter.discord.enabled():
        raise ValueError("PRW_REPORT_TO is required for immediate alerts")
    if not 0 < WARNING_C < CRITICAL_C or INTERVAL < 1 or COOLDOWN_SECONDS < 1 or WARNING_SECONDS < 1 or CPU_WARNING_SECONDS < 1 or not 0 < CPU_WARNING_PERCENT <= 100:
        raise ValueError("Invalid alert thresholds or interval")
    now = dt.datetime.now().astimezone()
    rows = reporter.normalize_rows(recent(now))
    state = load_state()
    previous_check = state.get("last_check")
    try:
        elapsed = (now - reporter.timestamp(previous_check)).total_seconds()
        if elapsed < 0:
            rows.clock_uncertain = True
        if not 0 <= elapsed <= INTERVAL * 3:
            state.pop("temp_recovery_since", None)
            state.pop("cpu_recovery_since", None)
    except (ValueError, TypeError):
        state.pop("temp_recovery_since", None)
        state.pop("cpu_recovery_since", None)
    state["last_check"] = now.isoformat()
    if rows:
        hot = {k for k, v in rows[-1][1]["temperatures_c"].items() if reporter.is_cpu_sensor(k) and v >= WARNING_C}
        state["temp_sensors"] = sorted(set(state.get("temp_sensors", [])) | hot)
    update_recovery(now, rows, state)
    event = candidate(now, rows)
    if not event:
        diagnose_monitoring(now, rows, state)
        save_state(state)
        return
    mail_error = None
    discord_error = None
    sent = False
    # Preserve legacy keys for email; Discord has independent durable timestamps.
    for destination in ("email", "discord"):
        if destination == "discord" and not reporter.discord.enabled():
            continue
        if destination == "email" and not os.environ.get("PRW_REPORT_TO"):
            continue
        key = event["key"] if destination == "email" else "discord_" + event["key"]
        try:
            elapsed = (now - reporter.timestamp(state.get(key))).total_seconds()
            if 0 <= elapsed < COOLDOWN_SECONDS:
                continue
        except (ValueError, TypeError):
            pass
        if destination == "discord":
            try:
                if any(now < reporter.timestamp(state.get(k)) for k in
                       ("discord_retry_after", "discord_retry_" + event["key"]) if state.get(k)):
                    continue
            except (ValueError, TypeError):
                pass
            try:
                discord_error, delay = reporter.discord.send(discord_alert(event))
            except Exception:
                discord_error, delay = "Discord alert failed", 60
            if discord_error:
                retry_key = "discord_retry_after" if discord_error == "Discord HTTP 429" else "discord_retry_" + event["key"]
                state[retry_key] = (now + dt.timedelta(seconds=delay)).isoformat()
                continue
            state.pop("discord_retry_after", None)
            state.pop("discord_retry_" + event["key"], None)
        else:
            try:
                title, body = format_alert(event)
                reporter.deliver(title, body, os.environ["PRW_REPORT_TO"], os.environ.get("PRW_MAILER", "auto"))
            except Exception as exc:
                mail_error = exc
                continue
        state[key] = now.isoformat()
        sent = True
    if event["key"].startswith("temp_"):
        sensors = {k for k, v in event["row"]["temperatures_c"].items()
                   if reporter.is_cpu_sensor(k) and v >= WARNING_C}
        state["temp_sensors"] = sorted(set(state.get("temp_sensors", [])) | sensors)
    try:
        followups.prepare(event, state, delivered=sent)
    except Exception:
        pass  # Supplemental scheduling cannot prevent saving primary delivery state.
    diagnose_monitoring(now, rows, state)
    # Keep email-only failure behavior, while persisting partial success/backoff.
    if sent or reporter.discord.enabled() or mail_error is None:
        save_state(state)
        # Persist identity first; a queue failure must never undo primary delivery state.
        try:
            followups.queue(dt.datetime.now().astimezone(), event, state, LOG_DIR)
        except Exception:
            try:
                print("Follow-up queue unavailable; primary alerts unaffected.", file=sys.stderr)
            except OSError:
                pass
    if discord_error:
        try:
            print(discord_error, file=sys.stderr)
        except OSError:
            pass
    if mail_error:
        raise mail_error


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Alert check failed: {exc}", file=sys.stderr)
        raise
