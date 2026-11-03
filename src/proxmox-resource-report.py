#!/usr/bin/env python3
"""Plain-text daily digest for proxmox-resource-watch.py (also reads v1 logs)."""
__version__ = "0.2.0"

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
import re
import math
import stat
import tempfile
import multiprocessing
import resource
import bisect
import zlib
import signal
import heapq
import importlib.util

_discord_spec = importlib.util.spec_from_file_location("prw_discord", Path(__file__).with_name("prw_discord.py"))
discord = importlib.util.module_from_spec(_discord_spec)
_discord_spec.loader.exec_module(discord)

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


# Explicit ceilings apply to untrusted input, decompressed bytes and output.
MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100000
MAX_LINE = 65536
MAX_EVENTS = 100


def finite(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def timestamp(value):
    value = dt.datetime.fromisoformat(value)
    # Historical naive records use the host's local zone at that date (DST aware).
    return value.astimezone() if value.tzinfo is None else value


def regular_open(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def clean_row(row):
    if not isinstance(row, dict):
        row = {}
    result = {"clock_discontinuity": row.get("clock_discontinuity") is True}
    for field in ("temperatures_c", "fans_rpm"):
        values = row.get(field)
        result[field] = {str(k): v for k, v in values.items()
                         if finite(v)} if isinstance(values, dict) else {}
    for field in ("host_cpu_busy_percent", "gpu_busy_percent", "sample_seconds"):
        result[field] = row.get(field) if finite(row.get(field)) else None
    result["top_processes"] = []
    procs = row.get("top_processes")
    for proc in procs if isinstance(procs, list) else []:
        if isinstance(proc, dict) and finite(proc.get("cpu_percent_one_core")) and proc["cpu_percent_one_core"] >= 0:
            result["top_processes"].append({"name": re.sub(r"[\x00-\x1f\x7f]", "?", str(proc.get("name", "unknown")))[:160],
                "pid": proc.get("pid") if isinstance(proc.get("pid"), int) else str(proc.get("pid", "unknown"))[:32], "start_ticks": str(proc.get("start_ticks", "unknown"))[:32],
                "cpu_percent_one_core": proc["cpu_percent_one_core"]})
    unique = {}
    for proc in result["top_processes"]:
        key = (proc["name"], proc["pid"], proc["start_ticks"])
        if key not in unique or proc["cpu_percent_one_core"] > unique[key]["cpu_percent_one_core"]:
            unique[key] = proc
    result["top_processes"] = list(unique.values())
    return result


class NormalizedRows(list):
    """Internal canonical rows; consumers must treat row dictionaries as read-only."""
    def __init__(self, rows=(), clock_uncertain=False):
        super().__init__(rows)
        self.clock_uncertain = clock_uncertain


def normalize_rows(rows, clock_uncertain=False, *, _already_clean=False):
    if isinstance(rows, NormalizedRows):
        return rows
    merged = {}
    previous = None
    for stamp, raw in rows:
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
        if previous is not None and stamp < previous:
            clock_uncertain = True
        previous = stamp
        row = raw if _already_clean else clean_row(raw)
        clock_uncertain = clock_uncertain or row["clock_discontinuity"]
        if stamp not in merged:
            merged[stamp] = row
            continue
        old = merged[stamp]
        if _already_clean:
            old = dict(old, temperatures_c=dict(old["temperatures_c"]), fans_rpm=dict(old["fans_rpm"]))
            merged[stamp] = old
        for field in ("temperatures_c", "fans_rpm"):
            for key, value in row[field].items():
                old[field][key] = max(value, old[field].get(key, value))
        for field in ("host_cpu_busy_percent", "gpu_busy_percent"):
            if row[field] is not None:
                old[field] = max(row[field], old[field]) if old[field] is not None else row[field]
        # Conflicting cadence cannot justify a longer integration interval.
        periods = [v for v in (old["sample_seconds"], row["sample_seconds"]) if v is not None and v > 0]
        old["sample_seconds"] = min(periods) if periods else None
        procs = {}
        for proc in old["top_processes"] + row["top_processes"]:
            key = (proc["name"], proc["pid"], proc["start_ticks"])
            if key not in procs or proc["cpu_percent_one_core"] > procs[key]["cpu_percent_one_core"]:
                procs[key] = proc
        old["top_processes"] = list(procs.values())
    return NormalizedRows(sorted(merged.items(), key=lambda pair: pair[0]), clock_uncertain)


def combine_rows(*parts):
    """Merge canonical day windows without recleaning or mutating their rows."""
    ordered = sorted((item for part in parts for item in part), key=lambda pair: pair[0])
    return normalize_rows(ordered, any(part.clock_uncertain for part in parts), _already_clean=True)


def rows_for(day):
    sources = [LOG_DIR / f"{day}.jsonl", LOG_DIR / f"{day}.jsonl.gz", LEGACY, LEGACY.with_suffix(".jsonl.1")]
    rows, errors = [], 0
    clock_uncertain = False
    for path in sources:
        previous_stamp = None
        try:
            with regular_open(path) as raw:
                stream = gzip.GzipFile(fileobj=raw) if path.suffix == ".gz" else raw
                consumed = 0
                while consumed < MAX_BYTES and len(rows) < MAX_ROWS:
                    line = stream.readline(MAX_LINE + 1)
                    if not line:
                        break
                    consumed += len(line)
                    if len(line) > MAX_LINE:
                        errors += 1
                        break
                    try:
                        row = json.loads(line)
                        stamp = timestamp(row["time"])
                        if previous_stamp is not None and stamp < previous_stamp:
                            clock_uncertain = True
                        previous_stamp = stamp
                        if stamp.date().isoformat() == day:
                            rows.append((stamp, row))
                    except (ValueError, KeyError, TypeError, RecursionError):
                        errors += 1
                if consumed >= MAX_BYTES or len(rows) >= MAX_ROWS:
                    errors += 1
        except FileNotFoundError:
            continue
        except (OSError, EOFError, zlib.error):
            errors += 1
    return normalize_rows(sorted(rows, key=lambda pair: pair[0]), clock_uncertain), errors


def iter_temperature_events(rows):
    """Independent episodes per sensor; absence is unknown, never recovery."""
    rows = normalize_rows(rows)
    active = {}
    last = None
    for stamp, row in rows:
        gap = last is not None and (stamp-last).total_seconds() > INTERVAL*2.5
        temps = {k: v for k, v in row["temperatures_c"].items() if is_cpu_sensor(k)}
        for label, threshold in (("warning", CPU_WARNING_C), ("critical", CPU_CRITICAL_C)):
            keys = {key for key in active if key[0] == label} | {(label, sensor) for sensor in temps}
            for key in sorted(keys):
                sensor = key[1]
                value = temps.get(sensor)
                event = active.get(key)
                if event and (gap or value is None):
                    event["status"] = "interrupted (recovery unknown)"
                    yield active.pop(key)
                    event = None
                if value is not None and event is None and value >= threshold:
                    event = dict(level=label, start=stamp, end=stamp, last_hot=stamp,
                        peak=value, peak_time=stamp, sensor=sensor, duration=0.0,
                        status="ongoing (unresolved)", rows=NormalizedRows(), recovery_since=None,
                        recovery_time=None, previous_hot=False, hot_windows=[])
                    active[key] = event
                if event:
                    hot = value >= threshold
                    if hot:
                        left = max(last, stamp-dt.timedelta(seconds=INTERVAL)) if event["previous_hot"] else stamp
                        if event["hot_windows"] and event["hot_windows"][-1][1] == left:
                            event["hot_windows"][-1] = (event["hot_windows"][-1][0], stamp)
                        else:
                            event["hot_windows"].append((left, stamp))
                        if event["previous_hot"]:
                            event["duration"] += min(INTERVAL, (stamp-last).total_seconds())
                        event["last_hot"] = stamp
                        event["rows"].append((stamp, row))
                    event["previous_hot"] = hot
                    event["end"] = stamp
                    if value > event["peak"]:
                        event.update(peak=value, peak_time=stamp)
                    if value < threshold-3:
                        if event["recovery_since"] is None:
                            event["recovery_since"] = stamp
                        elif (stamp-event["recovery_since"]).total_seconds() >= 60:
                            event.update(status="completed (recovered)", recovery_time=stamp)
                            if rows.clock_uncertain:
                                event.update(status="interrupted (clock order uncertain; recovery unknown)", recovery_time=None)
                            yield active.pop(key)
                    else:
                        event["recovery_since"] = None
        last = stamp
    for event in active.values():
        if rows.clock_uncertain:
            event["status"] = "ongoing (clock order uncertain; recovery unknown)"
        yield event


def temperature_events(rows):
    return sorted(iter_temperature_events(rows), key=lambda e: e["start"])


def process_history(rows, start=None, end=None, windows=None):
    scores = {}
    previous = None
    ends = [right for _, right in windows] if windows is not None else []
    for stamp, row in normalize_rows(rows):
        cadence = row.get("sample_seconds")
        delta = (stamp - previous).total_seconds() if previous else (cadence if cadence is not None and 0 < cadence <= 3600 else INTERVAL)
        seconds = cadence if cadence is not None and 0 < cadence <= 3600 else min(INTERVAL, delta)
        seconds = min(seconds, delta)
        left = stamp - dt.timedelta(seconds=max(0, seconds))
        seconds = max(0, (min(stamp, end or stamp) - max(left, start or left)).total_seconds())
        if windows is not None:
            sample_left, sample_right = max(left, start or left), min(stamp, end or stamp)
            position = bisect.bisect_left(ends, sample_left)
            seconds = 0.0
            while position < len(windows) and windows[position][0] <= sample_right:
                first, last = windows[position]
                seconds += max(0, (min(last, sample_right)-max(first, sample_left)).total_seconds())
                position += 1
        previous = stamp
        for proc in row["top_processes"]:
            identity = (proc["name"], proc["pid"], proc["start_ticks"])
            score = scores.setdefault(identity, dict(name=proc["name"], pid=proc["pid"], cpu_seconds=0.0, peak=0.0, samples=0))
            score["cpu_seconds"] += proc["cpu_percent_one_core"] / 100 * seconds
            score["peak"] = max(score["peak"], proc["cpu_percent_one_core"])
            score["samples"] += 1
    return sorted(scores.values(), key=lambda s: s["cpu_seconds"], reverse=True)


def log_timestamp(line, timezone):
    match = re.search(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z| ?[+-]\d\d:?\d\d)?", line)
    if not match:
        return None
    try:
        value = dt.datetime.fromisoformat(match.group().replace("Z", "+00:00").replace(" -", "-").replace(" +", "+"))
        return value if value.tzinfo else value.replace(tzinfo=timezone)
    except ValueError:
        return None


def backup_intervals(lines, timezone):
    """Pair only unique UPIDs. PID/VM-only records remain incomplete evidence."""
    active, intervals, ambiguous = {}, [], set()
    for line in lines[:1000]:
        line = line[:MAX_LINE]
        if line.startswith("{"):
            try:
                record = json.loads(line)
                program = record.get("SYSLOG_IDENTIFIER", record.get("_COMM"))
                pid = str(record.get("_PID", ""))
                message = record["MESSAGE"]
                if program not in ("pvedaemon", "vzdump") or not pid.isdecimal() or not isinstance(message, str) or "\n" in message:
                    continue
                instant = dt.datetime.fromtimestamp(int(record["__REALTIME_TIMESTAMP"])/1000000, dt.timezone.utc)
                line = f"{instant.isoformat()} {program}[{pid}]: {message}"
            except (ValueError, KeyError, TypeError, OverflowError, OSError):
                continue
        stamp = log_timestamp(line, timezone)
        prefix = re.fullmatch(r"\d{4}-\d\d-\d\dT\S+ (?:\S+ )?((?:pvedaemon|vzdump)\[\d+\]: .+)", line)
        if stamp is None or prefix is None:
            continue
        line = prefix.group(1)
        job = re.search(r"(?:pvedaemon|vzdump)\[\d+\]: (?:<[^>]{1,128}> )?(starting|end) task (UPID:[^:\s]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:vzdump:[^:\s]*:[^:\s]+:)(?: (.*))?$", line)
        if job:
            action, upid, outcome = job.groups()
            if action == "starting":
                if upid in active:
                    intervals.append(active.pop(upid))
                    ambiguous.add(upid)
                active[upid] = dict(name="Proxmox backup", start=stamp, end=None, status="completion unknown")
            else:
                item = active.pop(upid, None)
                if item and stamp >= item["start"] and upid not in ambiguous:
                    item.update(end=stamp, status="completed" if outcome == "OK" else "failed/interrupted", confidence="confirmed")
                else:
                    if item:
                        intervals.append(item)
                    item = dict(name="Proxmox backup", start=None, end=stamp, status="identity/start unknown")
                intervals.append(item)
            continue
        match = re.search(r"vzdump\[\d+\]: (?:(?:INFO|ERROR): )?(Starting Backup of (?:VM|CT) (\d+)(?: \(.*\))?|Finished Backup of (?:VM|CT) (\d+)(?: \(.*\))?|Backup of (?:VM|CT) (\d+) failed:.*)$", line)
        if match:
            _, started, finished, failed = match.groups()
            vm = started or finished or failed
            intervals.append(dict(name="Proxmox backup VM/LXC " + vm,
                start=stamp if started else None, end=None if started else stamp,
                status="job identity unknown"))
    return intervals + list(active.values())


def task_duration(line):
    match = re.search(r"\bafter\s+(?:(\d+)\s*(?:hours?|hrs?|h)(?:\(s\))?\s*(?:,|and)?\s*)?(?:(\d+)\s*(?:minutes?|mins?|m)(?:\(s\))?\s*(?:,|and)?\s*)?(?:(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)(?:\(s\))?)?\s*[.]?\s*$", line, re.I)
    if not match or not any(value is not None for value in match.groups()):
        return None
    seconds = int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + float(match.group(3) or 0)
    return dt.timedelta(seconds=seconds) if seconds <= 7 * 86400 else None


def jellyfin_intervals(lines, timezone):
    intervals = []
    for line in lines[:10000]:
        stamp = log_timestamp(line[:MAX_LINE], timezone)
        # TaskManager's own completion duration is usable without guessing a start pair.
        match = re.search(r"^\[[^\]\n]{19,40}\] \[(?:INF|WRN|ERR)\] (?:\[\d+\] )?(?P<logger>Emby\.Server\.Implementations\.ScheduledTasks\.TaskManager: )?\"?(Audio Normalization|Scan Media Library|Scanning media library)\"? Completed after (.+)$", line)
        if stamp is None or match is None:
            continue
        duration = task_duration("after " + match.group(3))
        if duration is not None:
            intervals.append(dict(name=match.group(2), start=stamp-duration, end=stamp,
                                  status="completed (duration reported)", confidence="confirmed" if match.group("logger") else "possible"))
    return intervals


def _collect_evidence(start, end):
    evidence, intervals = [], []
    try:
        # File-backed stdout avoids unbounded capture_output allocation.
        with tempfile.TemporaryFile() as output:
            subprocess.run(["journalctl", "--no-pager", "-o", "json", "--since",
                (start-dt.timedelta(hours=24)).isoformat(), "--until", (end+dt.timedelta(hours=24)).isoformat(),
                "-n", "1000", "-g", "vzdump"], stdout=output, stderr=subprocess.DEVNULL, timeout=3, check=True)
            output.seek(0)
            data = output.read(1024*1024)
        intervals.extend(backup_intervals(data.decode(errors="replace").splitlines(), start.tzinfo))
    except (OSError, subprocess.SubprocessError):
        evidence.append("Proxmox journal unavailable (backup activity unknown)")
    path = os.environ.get("PRW_JELLYFIN_LOG", "")
    if path:
        try:
            if not Path(path).is_absolute():
                raise OSError("absolute path required")
            with regular_open(path) as stream:
                info = os.fstat(stream.fileno())
                age_setting = os.environ.get("PRW_JELLYFIN_MAX_AGE_SECONDS", "")
                if age_setting:
                    try:
                        max_age = int(age_setting)
                    except ValueError:
                        raise OSError("Invalid snapshot age limit") from None
                    age = dt.datetime.now(dt.timezone.utc).timestamp() - info.st_mtime
                    if max_age < 1 or age > max_age or age < -300:
                        raise OSError("Jellyfin snapshot stale")
                size = info.st_size
                offset = max(0, size-1024*1024)
                stream.seek(offset)
                data = stream.read(1024*1024)
                if offset:
                    data = data.partition(b"\n")[2]
            intervals.extend(jellyfin_intervals(data.decode(errors="replace").splitlines(), start.tzinfo))
        except OSError:
            evidence.append("Jellyfin log unavailable (task activity unknown)")
    return evidence, intervals[:1000]


def _evidence_worker(connection, start, end):
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
        connection.send(_collect_evidence(start, end))
    except Exception:
        connection.send((["Workload evidence unavailable (activity unknown)"], []))
    finally:
        connection.close()


def _bounded_evidence(start, end):
    # Slow regular storage is also optional: isolate reads behind a wall-clock deadline.
    parent, child = multiprocessing.Pipe(duplex=False)
    try:
        pid = os.fork()
    except OSError:
        parent.close()
        child.close()
        raise
    if pid == 0:
        parent.close()
        try:
            _evidence_worker(child, start, end)
        finally:
            os._exit(0)
    child.close()
    try:
        if parent.poll(5):
            try:
                return parent.recv()
            except EOFError:
                return ["Workload evidence unavailable (activity unknown)"], []
        return ["Workload evidence timed out (activity unknown)"], []
    finally:
        parent.close()
        # No multiprocessing Process finalizer: it would join indefinitely at exit
        # if optional storage left the child in uninterruptible kernel I/O.
        finished, _ = os.waitpid(pid, os.WNOHANG)
        if not finished:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, os.WNOHANG)


def collect_evidence(start, end):
    try:
        return _bounded_evidence(start, end)
    except (OSError, ValueError, EOFError):
        return ["Workload evidence unavailable (activity unknown)"], []


def workload_evidence(start, end, bundle=None, windows=None):
    notices, intervals = bundle if bundle is not None else collect_evidence(start, end)
    evidence = list(notices)
    ends = [right for _, right in windows] if windows else []
    for item in intervals:
        first, last = item["start"], item["end"]
        if first is not None and last is not None and first <= end and last >= start:
            if windows:
                position = bisect.bisect_left(ends, first)
                if position == len(windows) or windows[position][0] > last:
                    continue
            confidence = "Confirmed workload overlap" if item.get("confidence") == "confirmed" else "Possible workload correlation"
            evidence.append(f"{confidence}: {item['name']}; {first.isoformat()} to {last.isoformat()}; {item['status']}")
        elif (first is not None and last is None and first <= end) or (last is not None and first is None and last >= start):
            evidence.append(f"Possible workload correlation: {item['name']}; {item['status']}; interval incomplete")
        if len(evidence) >= 10:
            break
    return evidence[:10]


def event_lines(event, include_workloads=True, evidence=None):
    recovery = event["recovery_time"].isoformat() if event["recovery_time"] else "unknown"
    out = [f"  {event['level'].upper()}: {event['status']}",
           f"    Start: {event['start'].isoformat()}",
           f"    Last hot observation: {event['last_hot'].isoformat()}",
           f"    Recovery confirmed: {recovery}",
           f"    Approximate observed duration: {event['duration']:.0f}s",
           f"    Peak: {temp_format(event['peak'])} at {event['peak_time'].isoformat()} ({event['sensor']})",
           "",
           "    Aggregate sampled CPU contributors (estimated core-seconds; one-core peak):"]
    scores = process_history(event["rows"], event["start"], event["last_hot"], event["hot_windows"])
    out += [f"      {s['name']} (PID {s['pid']}): ~{s['cpu_seconds']:.1f} core-s, peak {s['peak']:.1f}%, {s['samples']} samples" for s in scores[:5]] or ["      Unavailable"]
    if include_workloads:
        out += ["", "    Workload evidence:", ""]
        lines = workload_evidence(event["start"], event["last_hot"], evidence, event["hot_windows"])
        if not lines:
            out.append("      No matching evidence found")
        for index, line in enumerate(lines):
            if index:
                out.append("")
            parts = line.split("; ")
            out.append("      " + parts[0])
            out.extend("        " + detail for detail in parts[1:])
    return out


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


def report(day, rows, errors, context_rows=None, summary=None):
    rows = normalize_rows(rows)
    if summary is not None:
        summary.update(samples=len(rows), errors=errors, clock_uncertain=rows.clock_uncertain)
    title = f"Proxmox Resource Report | {socket.gethostname()} | {day}"
    out = [title, "=" * len(title), "", f"Samples: {len(rows)} (expected ~{86400 // INTERVAL})"]
    if rows.clock_uncertain:
        out.append("Clock ordering uncertain: recovery cannot be confirmed from this history.")
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
    if summary is not None:
        summary.update(cpu=fmt_stats(cpu, "%"), gaps=len(gaps), first=timestamps[0].isoformat(), last=timestamps[-1].isoformat(),
                       cpu_missing=len(rows)-len(cpu))
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
    if summary is not None:
        peaks = [max(v) for k, v in sensor_values.items() if is_cpu_sensor(k)]
        summary["peak"] = temp_format(max(peaks)) if peaks else "unavailable"
        summary["temperature_samples"] = sum(any(is_cpu_sensor(k) for k in r["temperatures_c"]) for _, r in rows)
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
    if summary is not None:
        summary["sustained_cpu"] = sustained_cpu_count(rows)
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
    out += ["", "CPU TEMPERATURE EVENTS", "----------------------"]
    event_count = 0
    thermal_counts = collections.Counter()
    def relevant_events():
        nonlocal event_count
        for event in iter_temperature_events(context_rows if context_rows is not None else rows):
            if event["start"].date().isoformat() <= day <= event["end"].date().isoformat():
                event_count += 1
                thermal_counts[event["level"]] += 1
                yield event
    selected = heapq.nlargest(MAX_EVENTS, relevant_events(), key=lambda e: e["peak"])
    out.append(f"Events: {event_count}; showing at most {MAX_EVENTS} by peak")
    evidence = collect_evidence(min(e["start"] for e in selected), max(e["last_hot"] for e in selected)) if selected else ([], [])
    if summary is not None:
        summary.update(thermal=dict(thermal_counts), context=[])
    for event in selected:
        if summary is not None:
            for line in workload_evidence(event['start'], event['last_hot'], evidence, event['hot_windows']):
                if line.startswith(("Confirmed workload overlap:", "Possible workload correlation:")):
                    label = line.split(';')[0]
                    if label not in summary['context'] and len(summary['context']) < 4:
                        summary['context'].append(label)
        out.append("")
        out.extend(event_lines(event, evidence=evidence))
    out += ["", "NOTES", "-----", "",
            "Events are correlations, not proof that a process caused the heat or fan change.",
            "Only sampled top processes are available; short-lived or lower-ranked processes can be missed.",
            "Durations are estimates from observed sample intervals; gaps and unobserved time are excluded.",
            "Per-process CPU is percent of one logical core; host CPU is across all cores.",
            "Missing sensor data means unavailable, not zero.", ""]
    body = "\n".join(out)
    if len(body) > 240000:
        body = body[:240000] + "\n[Report truncated at output limit]"
    return title, re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", body) + "\n"


def deliver(subject, body, recipient, mailer):
    if any(c in subject for c in "\r\n"):
        raise ValueError("Invalid subject")
    if not recipient or any(c in recipient for c in "\r\n\x00") or recipient.startswith("-"):
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


def sustained_cpu_count(rows):
    """Cheap CPU-only pass; no repeated process, thermal or workload analysis."""
    count = duration = 0
    last = None
    for stamp, row in rows:
        cpu = row.get('host_cpu_busy_percent')
        if last is not None and (stamp-last).total_seconds() > INTERVAL*2.5:
            count += duration >= SUSTAINED_SECONDS
            duration = 0
        if finite(cpu) and cpu >= CPU_THRESHOLD:
            duration += INTERVAL
        else:
            count += duration >= SUSTAINED_SECONDS
            duration = 0
        last = stamp
    return count + (duration >= SUSTAINED_SECONDS)


def summary_message(day, summary):
    parts = [f"Samples: {summary['samples']} / ~{86400 // INTERVAL}; read/parse errors: {summary['errors']}"]
    if not summary['samples']:
        parts.append("Monitoring data unavailable; CPU, temperatures and event counts unknown.")
    else:
        thermal = (f"warning {summary['thermal'].get('warning', 0)}, critical {summary['thermal'].get('critical', 0)}"
                   if summary['temperature_samples'] else "unknown (CPU temperature unavailable)")
        sustained = str(summary['sustained_cpu']) if summary['cpu_missing'] < summary['samples'] else "unknown (CPU unavailable)"
        parts += [f"Coverage: {summary['first']} to {summary['last']}",
                  f"Host CPU: {summary['cpu']}; missing CPU samples: {summary['cpu_missing']}",
                  f"Peak CPU temperature: {summary['peak']}",
                  f"Observed thermal episodes per sensor: {thermal} (overlap possible); missing temperature samples: {summary['samples']-summary['temperature_samples']}",
                  f"Sustained CPU episodes ≥ {CPU_THRESHOLD:g}% for ~{SUSTAINED_SECONDS}s: {sustained} (observed only)",
                  f"Sampling gaps: {summary['gaps']}; clock uncertain: {summary['clock_uncertain']}"]
        if summary['context']:
            parts.append("Workloads in selected thermal-event window (correlation only): " + ", ".join(summary['context']))
    return discord.payload(f"Daily health | {socket.gethostname()} | {day}", "\n".join(parts), 0x3498DB)


def send_report(subject, body, mailer, day, summary):
    mail_error = None
    try:
        deliver(subject, body, os.environ.get("PRW_REPORT_TO", ""), mailer)
    except Exception as exc:
        mail_error = exc
    discord_error = None
    if discord.enabled():
        try:
            discord_error, _ = discord.send(summary_message(day, summary))
        except Exception:
            discord_error = "Discord summary failed"
    if discord_error:
        print(discord_error, file=sys.stderr)
    if mail_error:
        raise mail_error
    # Optional Discord failure does not fail a successfully mailed report.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discord-test", action="store_true", help="Send one identified Discord test only; no email or threshold changes")
    parser.add_argument("--date", help="Calendar day YYYY-MM-DD (default yesterday)")
    parser.add_argument("--send", action="store_true", help="Send via selected mailer; default prints report")
    parser.add_argument("--mailer", choices=["auto", "mutt", "sendmail"],
                        default=os.environ.get("PRW_MAILER", "auto"))
    args = parser.parse_args()
    if args.discord_test:
        if not discord.enabled():
            raise ValueError("Discord is disabled")
        error, _ = discord.send(discord.payload("TEST — Resource Watch",
            f"Explicit delivery test for {socket.gethostname()}; no real alert condition. Observed {dt.datetime.now().astimezone().isoformat()}", 0x3498DB))
        if error:
            raise RuntimeError(error)
        print("Discord test accepted; confirm receipt in the configured channel.")
        return
    if INTERVAL < 1 or SUSTAINED_SECONDS < 1 or not 0 < CPU_WARNING_C < CPU_CRITICAL_C:
        raise ValueError("Invalid interval, duration, or temperature thresholds")
    day = args.date or (dt.datetime.now().astimezone().date() - dt.timedelta(days=1)).isoformat()
    dt.date.fromisoformat(day)
    rows, errors = rows_for(day)
    previous, previous_errors = rows_for((dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat())
    following, following_errors = rows_for((dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat())
    context = combine_rows(previous, rows, following)
    summary = {} if args.send and discord.enabled() else None
    subject, body = report(day, rows, errors + previous_errors + following_errors, context, summary)
    if args.send:
        send_report(subject, body, args.mailer, day, summary)
    else:
        print(body, end="")


if __name__ == "__main__":
    main()
