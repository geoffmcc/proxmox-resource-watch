"""Bounded supplemental context jobs, separate from primary alert state/delivery."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import uuid

DELAY = 60
MAX_AGE = 15 * 60
JOB_NAME = re.compile(r'[0-9a-f]{32}\.(email|discord)\.json')


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def enabled():
    return os.environ.get('PRW_FOLLOWUP_ENABLED', 'false').lower() == 'true'


def prepare(event, state, delivered=True):
    """Record episode identity before publishing jobs, in the primary atomic state."""
    if not enabled():
        return
    group = 'temp' if event['key'].startswith('temp_') else 'cpu'
    prefix = 'followup_' + group + '_'
    if not any(state.get(k) for k in (event['key'], 'discord_'+event['key'])):
        return
    token = state.get(prefix + 'episode')
    if not token or not re.fullmatch('[0-9a-f]{32}', token):
        if not delivered:
            return
        token = uuid.uuid4().hex
        state[prefix + 'episode'] = token
        state[prefix + 'observed'] = event['stamp'].isoformat()
    if state.get(prefix+'key') != 'temp_critical':
        state[prefix+'key'] = event['key']


def queue(now, event, state, log_dir):
    """Publish only already-persisted episode IDs; no diagnostics or transport calls."""
    if not enabled():
        return
    group = 'temp' if event['key'].startswith('temp_') else 'cpu'
    prefix = 'followup_' + group + '_'
    token = state.get(prefix+'episode')
    if not token:
        return
    directory = Path(log_dir) / 'followups'
    # Follow the existing private, administrator-controlled log-directory policy.
    if directory.is_symlink():
        raise OSError('Unsafe follow-up directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    for destination in ('email', 'discord'):
        delivered_key = event['key'] if destination == 'email' else 'discord_' + event['key']
        if not state.get(delivered_key):
            continue
        if destination == 'email' and not os.environ.get('PRW_REPORT_TO'):
            continue
        if destination == 'discord' and not os.environ.get('PRW_DISCORD_ENABLED', 'false').lower() == 'true':
            continue
        target = directory / f'{token}.{destination}.json'
        # Exclusive attempt markers prevent resending after a worker crash or restart.
        if not target.exists() and not target.with_suffix('.attempted').exists():
            job = dict(episode=token, destination=destination, key=event['key'],
                       observed=state[prefix + 'observed'], due=(now+dt.timedelta(seconds=DELAY)).isoformat())
            fd, name = tempfile.mkstemp(prefix='.pending-', dir=directory)
            try:
                with os.fdopen(fd, 'w') as out:
                    json.dump(job, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(name, target)
                sync_directory(directory)
            finally:
                if os.path.exists(name):
                    os.unlink(name)


def rearm(state, group):
    for key in tuple(state):
        if key.startswith('followup_' + group + '_'):
            state.pop(key, None)


def context(reporter, job, rows):
    """Use bounded recent samples and existing bounded workload collection once."""
    end = reporter.timestamp(job['due'])
    observed = reporter.timestamp(job['observed'])
    seconds = int(os.environ.get('PRW_ALERT_CPU_SECONDS', '300')) if job['key'] == 'cpu_warning' else int(os.environ.get('PRW_WARNING_SECONDS', '30'))
    start = observed - dt.timedelta(seconds=seconds)
    selected = reporter.normalize_rows([(stamp, row) for stamp, row in rows if start <= stamp <= end])
    parts = ['Cause: unknown. Observed overlap and possible correlation do not prove causation.',
             f'Window: {start.isoformat()} to {end.isoformat()}']
    cpu = [row['host_cpu_busy_percent'] for _, row in selected if reporter.finite(row.get('host_cpu_busy_percent'))]
    temps = [value for _, row in selected for name, value in row['temperatures_c'].items() if reporter.is_cpu_sensor(name)]
    parts += [f'Samples: {len(selected)}; host CPU: {reporter.fmt_stats(cpu, "%")}',
              'Peak CPU temperature: ' + (reporter.temp_format(max(temps)) if temps else 'unavailable')]
    gaps = sum((b[0]-a[0]).total_seconds() > reporter.INTERVAL*2.5 for a, b in zip(selected, selected[1:]))
    if (not selected or gaps or selected.clock_uncertain or
            selected[0][0] > start+dt.timedelta(seconds=reporter.INTERVAL*2.5) or
            selected[-1][0] < end-dt.timedelta(seconds=reporter.INTERVAL*2.5)):
        parts.append('Sampling incomplete or clock uncertain; missing time is unknown.')
    processes = reporter.process_history(selected, start, end)
    if processes:
        parts.append('Observed CPU contributors (sampled only): ' + ', '.join(
            f"{reporter.discord.clean(p['name'], 40)} ({p['peak']:.1f}% one-core peak)" for p in processes[:3]))
    else:
        parts.append('Observed CPU contributors: unavailable.')
    if hasattr(reporter, 'guest_context'):
        parts.extend(reporter.guest_context(observed.date().isoformat(), [(start, end)],
                                            uncertain=selected.clock_uncertain or selected.data_uncertain or
                                            getattr(rows, 'clock_uncertain', False) or getattr(rows, 'data_uncertain', False)))
    evidence = reporter.workload_evidence(start, end)
    parts.append('Workload evidence:')
    parts.extend(reporter.discord.clean(line, 240) for line in evidence[:3])
    if not evidence:
        parts.append('Unknown: no matching workload evidence available yet.')
    parts.append('Later task-completion evidence may appear in the daily report.')
    severity = 'CRITICAL' if job['key'] == 'temp_critical' or (temps and max(temps) >= reporter.CPU_CRITICAL_C) else 'WARNING'
    return severity, '\n'.join(parts)


def discord_context(body):
    """Organize the existing context for Discord without changing email text."""
    sections = {'🔎 OBSERVATION': [], '📊 COVERAGE': [], '💻 CPU': [],
                '🌡️ TEMPERATURE': [], '📋 WORKLOAD CONTEXT': []}
    section = '🔎 OBSERVATION'
    for line in body.splitlines():
        if line.startswith(('Window:', 'Samples:', 'Sampling incomplete')):
            section = '📊 COVERAGE'
        elif line.startswith('Peak CPU temperature:'):
            section = '🌡️ TEMPERATURE'
        elif line.startswith('Observed CPU contributors'):
            section = '💻 CPU'
        elif line == 'Workload evidence:':
            section = '📋 WORKLOAD CONTEXT'
            continue
        sections[section].append(line)
    return '\n\n'.join(title + '\n' + '\n'.join(lines)
                       for title, lines in sections.items() if lines)


def run(reporter, directory, rows_loader, now=None):
    """One attempt per destination/episode. Never retry ambiguous supplemental sends."""
    if not enabled():
        return
    now = now or dt.datetime.now().astimezone()
    directory = Path(directory)
    if not directory.exists() or directory.is_symlink():
        return
    processed = 0
    cached = {}
    rows = None
    # At most two transport attempts per invocation; a separate unit owns this work.
    for path in sorted(directory.iterdir()):
        if processed >= 2:
            break
        if not JOB_NAME.fullmatch(path.name):
            continue
        claimed = False
        try:
            with reporter.regular_open(path) as stream:
                job = json.loads(stream.read(4096))
            if (job['destination'] not in ('email', 'discord') or job['key'] not in ('temp_warning', 'temp_critical', 'cpu_warning') or
                    path.name != f"{job['episode']}.{job['destination']}.json"):
                continue
            due = reporter.timestamp(job['due'])
            if now < due:
                continue
            marker = path.with_suffix('.attempted')
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, 'w') as out:
                out.write(now.isoformat()+'\n')
                out.flush()
                os.fsync(out.fileno())
            sync_directory(directory)
            claimed = True
            processed += 1
            if (now-due).total_seconds() > MAX_AGE:
                print('Follow-up expired; primary alerts unaffected.', flush=True)
                continue
            destination = job['destination']
            if destination == 'email' and not os.environ.get('PRW_REPORT_TO'):
                continue
            if destination == 'discord' and not reporter.discord.enabled():
                continue
            # Read primary state only; this worker never updates immediate cooldowns.
            try:
                with reporter.regular_open(directory.parent/'alert-state.json') as stream:
                    primary_state = json.loads(stream.read(65536))
                if (job['key'].startswith('temp_') and
                        primary_state.get('followup_temp_episode') == job['episode'] and
                        primary_state.get('followup_temp_key') == 'temp_critical'):
                    job['key'] = 'temp_critical'
                if destination == 'discord' and primary_state.get('discord_retry_after'):
                    if now < reporter.timestamp(primary_state['discord_retry_after']):
                        print('Follow-up skipped during Discord rate limit.', flush=True)
                        continue
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            cache_key = (job['episode'], job['due'])
            if cache_key not in cached:
                try:
                    if rows is None:
                        rows = rows_loader()
                    cached[cache_key] = context(reporter, job, rows)
                except Exception:
                    cached[cache_key] = ('CRITICAL' if job['key'] == 'temp_critical' else 'WARNING',
                                         'Cause: unknown. Follow-up analysis unavailable; primary alerts unaffected.')
            severity, body = cached[cache_key]
            title = f'{severity} follow-up | {socket.gethostname()}'
            body = reporter.discord.clean(body, len(body))
            try:
                if destination == 'email':
                    reporter.deliver('[PVE context] '+title, body+'\n', os.environ['PRW_REPORT_TO'], os.environ.get('PRW_MAILER', 'auto'))
                else:
                    formatted = discord_context(body)
                    attachment = formatted if len(formatted) > 1800 else None
                    if attachment:
                        formatted = formatted[:1700].rstrip() + '\n\nFull follow-up attached as resource-report.md.'
                    error, _ = reporter.discord.send(reporter.discord.payload(title, formatted, 0xE74C3C if severity == 'CRITICAL' else 0xE67E22), attachment=attachment)
                    if error:
                        print(error+' (follow-up; no retry)', flush=True)
            except Exception:
                print('Follow-up transport failed; no retry; primary alerts unaffected.', flush=True)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            print('Follow-up job unavailable or invalid; primary alerts unaffected.', flush=True)
        finally:
            if claimed:
                path.unlink(missing_ok=True)
    # Preserve active episode records, including very long episodes, to prevent requeue.
    try:
        with reporter.regular_open(directory.parent/'alert-state.json') as stream:
            primary_state = json.loads(stream.read(65536))
        active = {primary_state.get('followup_temp_episode'), primary_state.get('followup_cpu_episode')}
    except (OSError, ValueError, AttributeError):
        return  # Cannot prove a record is inactive: retain it conservatively.
    # Bound supplemental queue retention without touching monitoring logs/state.
    for path in directory.iterdir():
        if (JOB_NAME.fullmatch(path.name) or re.fullmatch(r'[0-9a-f]{32}\.(email|discord)\.attempted', path.name)):
            if path.name.split('.')[0] not in active and now.timestamp()-path.stat().st_mtime > 30*86400:
                path.unlink()
