"""Durable per-destination daily delivery; transport acceptance is not receipt."""
import datetime as dt
import fcntl
import os
from pathlib import Path
import stat
import time

import prw_storage


def settings():
    age = int(os.environ.get('PRW_DAILY_MAX_AGE_HOURS', '24'))
    attempts = int(os.environ.get('PRW_DAILY_RETRY_LIMIT', '3'))
    delay = int(os.environ.get('PRW_DAILY_RETRY_SECONDS', '900'))
    if not 1 <= age <= 48 or not 1 <= attempts <= 10 or not 300 <= delay <= 86400:
        raise ValueError('Invalid daily delivery bounds')
    return age * 3600, attempts, delay


def due(day):
    date = dt.date.fromisoformat(day) + dt.timedelta(days=1)
    return dt.datetime.combine(date, dt.time(7)).astimezone().timestamp()


def validate(state):
    if not isinstance(state, dict) or set(state) != {'email', 'discord'}:
        raise ValueError('Invalid daily ledger')
    for item in state.values():
        if not isinstance(item, dict) or set(item) != {'status', 'attempts', 'next'}:
            raise ValueError('Invalid daily destination')
        if item['status'] not in ('pending', 'inflight', 'success', 'ambiguous') or type(item['attempts']) is not int or not 0 <= item['attempts'] <= 10:
            raise ValueError('Invalid daily status')
        if type(item['next']) not in (int, float) or not 0 <= item['next'] < 1e12:
            raise ValueError('Invalid daily deadline')
    return state


def run(reporter, day, mailer, create=False, now=None, initial=False):
    now = time.time() if now is None else now
    max_age, limit, backoff = settings()
    age = now - due(day)
    if (age < 0 or age > max_age) and not initial:
        return False
    directory = prw_storage.directory(reporter.LOG_DIR / 'daily')
    fd = os.open(directory / 'lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError('Invalid daily lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if initial:
                raise RuntimeError("Initial delivery already running; verification unavailable")
            return False
        path = directory / (dt.date.fromisoformat(day).isoformat() + '.json')
        try:
            state = validate(prw_storage.read(path))
        except FileNotFoundError:
            if not create:
                return False
            state = {key: dict(status='pending', attempts=0, next=now) for key in ('email', 'discord')}
            prw_storage.atomic(path, state)
        destinations = [key for key, value in state.items() if value['status'] == 'pending' and
                        value['attempts'] < limit and value['next'] <= now and
                        (bool(os.environ.get('PRW_REPORT_TO')) if key == 'email' else reporter.discord.enabled())]
        if not destinations:
            if initial and state['email']['status'] != 'success':
                raise RuntimeError('Initial email delivery not confirmed; inspect private ledger')
            return False
        rows, errors = reporter.rows_for(day)
        summary = {}
        date = dt.date.fromisoformat(day)
        previous, prior_errors = reporter.rows_for((date-dt.timedelta(days=1)).isoformat())
        following, next_errors = reporter.rows_for((date+dt.timedelta(days=1)).isoformat())
        context = reporter.combine_rows(previous, rows, following)
        subject, body = reporter.report(day, rows, errors+prior_errors+next_errors, context, summary=summary)
        import prw_history
        body += '\n' + '\n'.join(prw_history.comparison(reporter, day, rows, errors)) + '\n'
        if age >= 300:
            subject = '[DELAYED] ' + subject
            body = f'Delayed report for {day}; original schedule 07:00 host-local.\n\n' + body
        for destination in destinations:
            item = state[destination]
            item.update(status='inflight', attempts=item['attempts'] + 1)
            prw_storage.atomic(path, state)  # Fail closed before network/mail I/O.
            error, delay = None, 0
            try:
                if destination == 'email':
                    reporter.deliver(subject, body, os.environ['PRW_REPORT_TO'], mailer)
                else:
                    message = reporter.summary_message(day, summary)
                    if age >= 300:
                        message['embeds'][0]['title'] = '[DELAYED] ' + message['embeds'][0]['title']
                    error, delay = reporter.discord.send(message, daily=True)
            except Exception:
                error = 'ambiguous'
            if error is None:
                item['status'] = 'success'
            elif destination == 'discord' and error.startswith('Discord HTTP ') and error != 'Discord HTTP 408':
                item.update(status='pending', next=now + max(delay, min(86400, backoff * 2 ** (item['attempts']-1))))
            else:
                item['status'] = 'ambiguous'
            prw_storage.atomic(path, state)
        if initial and state['email']['status'] != 'success':
            raise RuntimeError('Initial email delivery not confirmed; inspect private ledger')
        # Retain duplicate-prevention state for at least the configured retention.
        cutoff = dt.datetime.fromtimestamp(now).date() - dt.timedelta(days=max(7, int(os.environ.get('PRW_RETENTION_DAYS', '30'))))
        for old in directory.glob('????-??-??.json'):
            if dt.date.fromisoformat(old.stem) < cutoff:
                old.unlink()
        return True


def scheduled(reporter, mailer, now=None):
    now = time.time() if now is None else now
    local = dt.datetime.fromtimestamp(now)
    # Select latest due date, including yesterday's due report before today's 07:00.
    day = local.date() - dt.timedelta(days=1 if local.hour >= 7 else 2)
    catchup = os.environ.get('PRW_DAILY_CATCHUP_ENABLED', 'false').lower() == 'true'
    began = time.monotonic()
    for offset in range(3):
        if time.monotonic()-began > 120:
            break
        candidate = (day-dt.timedelta(days=offset)).isoformat()
        run(reporter, candidate, mailer, create=catchup and offset == 0, now=now)
