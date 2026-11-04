#!/usr/bin/env python3
"""Offline, non-mutating EnvironmentFile validation. Never echo configuration values."""
import argparse
from email.utils import getaddresses
import math
import os
from pathlib import Path
import re
import shlex
import stat

NUMBERS = {
    'PRW_GUEST_INTERVAL_SECONDS': (60, 3600), 'PRW_INTERVAL': (1, 3600), 'PRW_RETENTION_DAYS': (1, 3650),
    'PRW_CPU_THRESHOLD': (0, 100), 'PRW_TEMP_THRESHOLD': (-50, 150),
    'PRW_SUSTAINED_SECONDS': (1, 86400), 'PRW_CPU_WARNING_C': (1, 150),
    'PRW_CPU_CRITICAL_C': (1, 150), 'PRW_WARNING_SECONDS': (1, 86400),
    'PRW_ALERT_CPU_PERCENT': (0, 100), 'PRW_ALERT_CPU_SECONDS': (1, 86400),
    'PRW_ALERT_COOLDOWN_SECONDS': (1, 604800), 'PRW_JELLYFIN_CONTAINER_ID': (1, 999999999),
    'PRW_JELLYFIN_MAX_AGE_SECONDS': (1, 86400), 'PRW_DAILY_MAX_AGE_HOURS': (1, 48),
    'PRW_DAILY_RETRY_LIMIT': (1, 10), 'PRW_DAILY_RETRY_SECONDS': (300, 86400)}
BOOLS = {'PRW_DISCORD_ENABLED', 'PRW_FOLLOWUP_ENABLED', 'PRW_GUEST_ENABLED', 'PRW_DAILY_CATCHUP_ENABLED'}
PATHS = {'PRW_LOG_DIR', 'PRW_JELLYFIN_LOG', 'PRW_JELLYFIN_SNAPSHOT'}
KNOWN = set(NUMBERS) | BOOLS | PATHS | {'PRW_REPORT_TO', 'PRW_MAILER', 'PRW_DISCORD_WEBHOOK_URL'}
DEFAULTS = dict(PRW_CPU_WARNING_C='85', PRW_CPU_CRITICAL_C='92', PRW_MAILER='auto', PRW_LOG_DIR='/var/log/proxmox-resource-watch')
INTEGER = set(NUMBERS) - {'PRW_CPU_THRESHOLD', 'PRW_TEMP_THRESHOLD', 'PRW_CPU_WARNING_C', 'PRW_CPU_CRITICAL_C', 'PRW_ALERT_CPU_PERCENT'}


def validate(values):
    errors, warnings = [], []
    effective = dict(DEFAULTS, **values)
    numeric = {}
    for key, value in values.items():
        label = key if key in KNOWN else 'Unknown configuration key'  # Unknown key could itself contain a secret.
        if key not in KNOWN:
            warnings.append(label + ' (name and value omitted)')
        elif key in NUMBERS:
            try:
                number = float(value)
                low, high = NUMBERS[key]
                if not math.isfinite(number) or not low <= number <= high or (key in INTEGER and not re.fullmatch(r'[0-9]+', value)):
                    raise ValueError()
                numeric[key] = number
            except (ValueError, OverflowError):
                errors.append(label + ': invalid numeric value or range')
        elif key in BOOLS and value.lower() not in ('true', 'false'):
            errors.append(label + ': expected true or false')
        elif key in PATHS and value:
            path = Path(value)
            if not path.is_absolute() or '..' in path.parts or any(ord(c) < 32 for c in value) or any(parent.is_symlink() for parent in (path, *path.parents)):
                errors.append(label + ': unsafe path')
            elif key == 'PRW_LOG_DIR':
                if path in (Path('/'), Path('/tmp'), Path('/var'), Path('/var/log'), Path('/etc'), Path('/usr')):
                    errors.append(label + ': dedicated private directory required')
                elif path.exists() and (not path.is_dir() or path.stat().st_mode & 0o077):
                    errors.append(label + ': directory must be private')
    try:
        if float(effective['PRW_CPU_WARNING_C']) >= float(effective['PRW_CPU_CRITICAL_C']):
            errors.append('PRW_CPU_WARNING_C must be below PRW_CPU_CRITICAL_C')
    except ValueError:
        pass
    if effective['PRW_MAILER'] not in ('auto', 'mutt', 'sendmail'):
        errors.append('PRW_MAILER: unsupported option')
    recipient = effective.get('PRW_REPORT_TO', '')
    if not recipient or recipient.startswith('-') or any(ord(c) < 32 for c in recipient) or not getaddresses([recipient]) or any(not address or address.startswith('-') for _, address in getaddresses([recipient])):
        errors.append('PRW_REPORT_TO: provide a local alias or valid email recipient')
    if effective.get('PRW_DISCORD_ENABLED', 'false').lower() == 'true' and not re.fullmatch(r'https://discord\.com/api(?:/v10)?/webhooks/[0-9]+/[A-Za-z0-9_-]+', effective.get('PRW_DISCORD_WEBHOOK_URL', '')):
        errors.append('PRW_DISCORD_WEBHOOK_URL: missing or invalid for enabled integration')
    for key in ('PRW_JELLYFIN_CONTAINER_ID', 'PRW_JELLYFIN_SNAPSHOT'):
        if key in values and not effective.get('PRW_JELLYFIN_LOG'):
            warnings.append(key + ': Jellyfin report log is not configured')
    return errors, warnings


def load(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    values, warnings = {}, []
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError('Configuration must be a regular file under 64 KiB')
        for line in stream:
            if not line.strip() or line.lstrip().startswith(('#', ';')):
                continue
            key, sep, value = line.strip().partition('=')
            if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValueError('Unsupported configuration syntax (values omitted)')
            # Single-line quoted values only; no expansion or shell execution.
            if value[:1] in ('"', "'"):
                parsed = shlex.split(value, comments=False)
                if len(parsed) != 1:
                    raise ValueError('Unsupported quoted value (value omitted)')
                value = parsed[0]
            if key in values:
                warnings.append('Duplicate setting (name and values omitted)')
            values[key] = value
    return values, warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/default/proxmox-resource-watch')
    args = parser.parse_args()
    try:
        values, warnings = load(args.config)
        errors, more = validate(values)
        warnings += more
    except Exception:
        print('ERROR: configuration unreadable or unsupported syntax (details omitted)')
        return 2
    for warning in warnings:
        print('WARNING: ' + warning)
    for error in errors:
        print('ERROR: ' + error)
    if not errors:
        print('Configuration valid' + (' with warnings' if warnings else ''))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
