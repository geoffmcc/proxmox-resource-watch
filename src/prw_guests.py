"""Read-only local guest snapshots, deliberately independent of critical alerts."""
import datetime as dt
import json
import math
import os
from pathlib import Path
import selectors
import socket
import stat
import subprocess
import time

import prw_discord
import prw_storage

MAX_BYTES = 2 * 1024 * 1024
MAX_GUESTS = 512


def number(value, low=0, high=1e18):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high
    except OverflowError:
        return False


def normalize(data, node):
    if not isinstance(data, list) or len(data) > MAX_GUESTS:
        raise ValueError('Invalid guest response')
    guests = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError('Invalid guest record')
        if item.get('node') != node or item.get('status') != 'running':
            continue
        vmid, kind = item.get('vmid'), item.get('type')
        if type(vmid) is not int or not 1 <= vmid <= 999999999 or kind not in ('qemu', 'lxc'):
            continue
        cpu, cores = item.get('cpu'), item.get('maxcpu')
        mem, maximum = item.get('mem'), item.get('maxmem')
        guests.append(dict(vmid=vmid, type=kind, name=prw_discord.clean_identifier(item.get('name', 'unknown'), 80),
                           state='running', uptime=item.get('uptime') if number(item.get('uptime')) else None,
                           cpu_percent_capacity=cpu * 100 if number(cpu, 0, 1) else None,
                           vcpus=cores if number(cores, 1, 4096) else None,
                           memory_percent=mem / maximum * 100 if number(mem) and number(maximum, 1) and mem <= maximum else None))
    return guests


def query(timeout=8):
    # Fixed executable and GET route. Never accept API paths or verbs from config.
    process = subprocess.Popen(['/usr/bin/pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError('Guest query deadline')
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_BYTES:
                    raise ValueError('Guest query output limit')
            if process.wait(timeout=max(.01, deadline-time.monotonic())):
                raise ValueError('Guest query failed')
        return json.loads(output)
    finally:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def collect():
    root = Path(os.environ.get('PRW_LOG_DIR', '/var/log/proxmox-resource-watch'))
    cleanup(root, dt.datetime.now().astimezone().date())
    if os.environ.get('PRW_GUEST_ENABLED', 'false').lower() != 'true':
        return
    directory = prw_storage.directory(Path(os.environ.get('PRW_LOG_DIR', '/var/log/proxmox-resource-watch')) / 'guests')
    now = dt.datetime.now().astimezone()
    interval = int(os.environ.get('PRW_GUEST_INTERVAL_SECONDS', '60'))
    if not 60 <= interval <= 3600:
        raise ValueError('Invalid guest interval')
    checkpoint = directory / 'last.json'
    try:
        previous = prw_storage.read(checkpoint)
        last = previous['time']
        if 0 <= now.timestamp() - last < max(interval, previous.get('interval', interval)):
            return
    except FileNotFoundError:
        pass
    prw_storage.atomic(checkpoint, {'time': now.timestamp()})
    data = normalize(query(), socket.gethostname().split('.')[0])
    row = json.dumps(dict(time=now.isoformat(), interval_seconds=interval, guests=data), allow_nan=False).encode() + b'\n'
    # Adapt large inventories to the fixed disk budget, never increase polling.
    effective = max(interval, math.ceil(len(row) * 90000 / (15 * 1024 * 1024)))
    prw_storage.atomic(checkpoint, {'time': now.timestamp(), 'interval': effective})
    row = json.dumps(dict(time=now.isoformat(), interval_seconds=effective, guests=data), allow_nan=False).encode() + b'\n'
    path = directory / f'{now.date()}.jsonl'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'ab') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size + len(row) > 16 * 1024 * 1024:
            raise ValueError('Guest history limit')
        stream.write(row)


def cleanup(root, today):
    directory = Path(root) / 'guests'
    if not directory.exists():
        return
    directory = prw_storage.directory(directory)
    cutoff = today - dt.timedelta(days=int(os.environ.get('PRW_RETENTION_DAYS', '30'))-1)
    for path in directory.glob('????-??-??.jsonl'):
        try:
            if dt.date.fromisoformat(path.stem) < cutoff:
                path.unlink()
        except ValueError:
            continue


def observations(reporter, day):
    path = reporter.LOG_DIR / 'guests' / f'{day}.jsonl'
    result = []
    try:
        with reporter.regular_open(path) as stream:
            consumed = 0
            while consumed < 16 * 1024 * 1024 and len(result) < 2880:
                line = stream.readline(262145)
                if not line:
                    break
                consumed += len(line)
                if len(line) > 262144:
                    break
                try:
                    row = json.loads(line)
                    stamp = reporter.timestamp(row['time'])
                    if stamp.date().isoformat() != day or not isinstance(row['guests'], list):
                        continue
                    interval = row.get('interval_seconds', 60)
                    interval = interval if number(interval, 60, 86400) else 60
                    guests = [dict(g, interval_seconds=interval) for g in row['guests'][:MAX_GUESTS] if isinstance(g, dict) and
                              type(g.get('vmid')) is int and g.get('type') in ('qemu', 'lxc')]
                    result.append((stamp, guests))
                except (ValueError, TypeError, KeyError, RecursionError):
                    continue
    except OSError:
        pass
    return result


def lines(reporter, day, windows=(), uncertain=False):
    observed = observations(reporter, day)
    out = ['GUEST ACTIVITY (sampled; contribution and cause unknown)', f'Guest snapshots: {len(observed)}; sampling is sparse, gaps are unknown; input capped at 16 MiB/day.']
    scores, previous, segments = {}, {}, {}
    for stamp, guests in observed:
        for guest in guests:
            cpu = guest.get('cpu_percent_capacity')
            if not number(cpu, 0, 100):
                continue
            name = prw_discord.clean_identifier(guest.get('name', 'unknown'), 80)
            key = (guest['type'], guest['vmid'])
            uptime = guest.get('uptime')
            old = previous.get(key)
            # Break continuity across name changes, resets, missing or inconsistent uptime,
            # gaps and backwards clocks. VMID reuse with indistinguishable metadata remains unknown.
            continuous = old and name == old[2] and number(uptime) and number(old[1])
            if continuous:
                elapsed = (stamp-old[0]).total_seconds()
                continuous = 0 < elapsed <= max(150, guest.get('interval_seconds', 60)*2.5) and abs((uptime-old[1])-elapsed) <= 10
            if not continuous:
                segments[key] = segments.get(key, 0) + 1
            previous[key] = (stamp, uptime, name)
            score = scores.setdefault((*key, name, segments[key]), dict(peak=0, count=0, overlaps=[]))
            score['peak'] = max(score['peak'], cpu)
            score['count'] += 1
            if any(start <= stamp <= end for start, end in windows):
                score['overlaps'].append((stamp, cpu))
    for (kind, vmid, name, segment), score in sorted(scores.items(), key=lambda p: (bool(p[1]['overlaps']), p[1]['peak']), reverse=True)[:10]:
        out.append(f"{kind.upper()} {vmid} {name} (observation segment {segment}): daily peak {score['peak']:.1f}% assigned CPU capacity; {score['count']} observations.")
        overlaps = score['overlaps']
        if overlaps:
            stamp, peak = max(overlaps, key=lambda p: p[1])
            if uncertain:
                out.append(f'  Possible correlation: {len(overlaps)} snapshots share event timestamps, but host chronology or data is uncertain; overlap and contribution unknown.')
            else:
                out.append(f'  Confirmed temporal overlap: {len(overlaps)} snapshots; event-window peak {peak:.1f}% at {stamp.isoformat()}; elevated activity ' + ('observed (>=50% capacity).' if peak >= 50 else 'not observed (>=50% capacity).'))
        else:
            out.append('  No guest snapshot inside host event windows; contribution unknown.')
    if not scores:
        out.append('Guest observations unavailable; no activity inferred.')
    out.append('Snapshots do not establish sustained activity between observations. Guest CPU is never added to host CPU. Indistinguishable VMID reuse cannot be ruled out.')
    return out


if __name__ == '__main__':
    try:
        collect()
    except Exception:
        raise SystemExit('Guest collection unavailable; primary alerts unaffected.')
