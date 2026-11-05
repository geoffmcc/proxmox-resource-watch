"""Offline acceptance regressions: no Proxmox, mail or HTTP required."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SRC))
import prw_daily as daily
import prw_guests as guests
import prw_history as history
import prw_storage as storage


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, SRC / file)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

report = module('new_report', 'proxmox-resource-report.py')
config = module('check_config', 'proxmox-resource-check-config.py')


class Guests(unittest.TestCase):
    def data(self, **kw):
        return dict(node='test', status='running', vmid=200, type='qemu', cpu=.5,
                    maxcpu=4, mem=1024, maxmem=2048, uptime=60, name='@everyone <bad> https://private/token', **kw)

    def test_types_names_denominators(self):
        qemu = self.data()
        lxc = dict(qemu, type='lxc', vmid=102)
        result = guests.normalize([qemu, lxc, dict(qemu, node='other'), dict(qemu, status='stopped')], 'test')
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['cpu_percent_capacity'], 50)
        self.assertEqual(result[0]['vcpus'], 4)
        self.assertEqual(result[0]['memory_percent'], 50)
        self.assertNotIn('@', result[0]['name'])
        self.assertNotIn('https', result[0]['name'])
        self.assertNotIn('host_cpu_busy_percent', result[0])

    def test_invalid(self):
        for value in ({}, None, [None], [self.data()] * 513):
            with self.assertRaises(ValueError):
                guests.normalize(value, 'test')
        for value in (float('nan'), float('inf'), -1, 10**1000, True):
            self.assertIsNone(guests.normalize([dict(self.data(), cpu=value)], 'test')[0]['cpu_percent_capacity'])

    def test_query_failures_safe(self):
        for error in (TimeoutError(), PermissionError(), FileNotFoundError()):
            with mock.patch.object(guests.subprocess, 'Popen', side_effect=error):
                with self.assertRaises(type(error)):
                    guests.query()

    def test_segments_window_and_missing(self):
        start = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
        records = [(start, [dict(vmid=200, type='qemu', name='worker', uptime=100, cpu_percent_capacity=90)]),
                   (start+dt.timedelta(seconds=60), [dict(vmid=200, type='qemu', name='worker', uptime=5, cpu_percent_capacity=20)])]
        with mock.patch.object(guests, 'observations', return_value=records):
            output = '\n'.join(guests.lines(report, '2026-11-01', [(start+dt.timedelta(seconds=50), start+dt.timedelta(seconds=70))]))
        self.assertIn('observation period 2', output)
        self.assertIn('in those checks: 20.0%', output)
        self.assertNotIn('in those checks: 90.0%', output)
        with mock.patch.object(guests, 'observations', return_value=[]):
            self.assertIn('No guest activity readings were available', '\n'.join(guests.lines(report, '2026-11-01')))

    def test_optional_failure_isolated(self):
        with mock.patch.object(guests, 'lines', side_effect=RecursionError()):
            self.assertIn('unknown', '\n'.join(report.guest_context('2026-01-01', [])))


class History(unittest.TestCase):
    def rows(self, values, seconds=10):
        start = dt.datetime.fromtimestamp(history.bounds('2026-11-01')[0], dt.timezone.utc)
        return report.normalize_rows([(start+dt.timedelta(seconds=i*seconds), dict(host_cpu_busy_percent=cpu,
                temperatures_c=temps, sample_seconds=seconds)) for i, (cpu, temps) in enumerate(values)])

    def test_confirmed_hot_endpoints(self):
        rows = self.rows([(90, {'cpu:Tctl': 90})]*3+[(0, {'cpu:Tctl': 50})])
        result = history.aggregate(report, '2026-11-01', rows)['metrics']
        self.assertEqual(result['cpu']['events'], 0)
        self.assertEqual(result['sensor:cpu:Tctl']['hot_seconds'], 0)
        result = history.aggregate(report, '2026-11-01', self.rows([(90, {'cpu:Tctl': 90})]*4))
        self.assertEqual(result['metrics']['sensor:cpu:Tctl']['hot_seconds'], 30)
        self.assertEqual(result['metrics']['cpu']['events'], 1)

    def test_missing_sensors_partial_invalid(self):
        rows = self.rows([(10, {'cpu:A': 50}), (float('nan'), {}), (10, {'cpu:B': 60})])
        result = history.aggregate(report, '2026-11-01', rows)['metrics']
        self.assertIsNone(result['cpu']['average'])
        self.assertEqual(result['sensor:cpu:A']['coverage'], 0)
        self.assertEqual(result['sensor:cpu:B']['coverage'], 0)

    def test_conflicting_values_suppress_average_without_clock_claim(self):
        rows = self.rows([(10, {'cpu:A':50})]*8640)
        conflicting = report.normalize_rows([*rows, (rows[-1][0], dict(host_cpu_busy_percent=90, temperatures_c={'cpu:A':90}, sample_seconds=10))])
        self.assertFalse(conflicting.clock_uncertain)
        self.assertTrue(conflicting.data_uncertain)
        self.assertIsNone(history.aggregate(report,'2026-11-01',conflicting)['metrics']['cpu']['average'])

    def test_duplicates_rollback(self):
        rows = self.rows([(90, {'cpu:A': 90})]*5)
        duplicate = report.normalize_rows([rows[0], rows[0], *rows[1:]])
        self.assertEqual(len(duplicate), 5)
        reversed_rows = report.normalize_rows(list(reversed(rows)))
        stats = history.aggregate(report, '2026-11-01', reversed_rows)['metrics']['cpu']
        self.assertIsNone(stats['average'])
        self.assertIsNone(stats['events'])

    def test_baseline_complete_and_bounded(self):
        rows = self.rows([(20, {'cpu:A': 50})]*8640)
        stats = history.aggregate(report, '2026-11-01', rows)['metrics']
        self.assertAlmostEqual(stats['cpu']['average'], 20)
        self.assertGreater(stats['cpu']['coverage'], .95)
        with mock.patch.object(report, 'rows_for', return_value=(report.normalize_rows([]), 0)) as reader:
            self.assertIn('insufficient historical', '\n'.join(history.comparison(report, '2026-11-01', rows)))
            self.assertEqual(reader.call_count, 14)

    @unittest.skipUnless(hasattr(__import__('time'), 'tzset'), 'tzset required')
    def test_dst_bounds(self):
        import time
        old = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/New_York'
            time.tzset()
            a,b = history.bounds('2026-11-01')
            self.assertEqual(b-a, 25*3600)
            a,b = history.bounds('2026-03-08')
            self.assertEqual(b-a, 23*3600)
        finally:
            if old is None: os.environ.pop('TZ', None)
            else: os.environ['TZ'] = old
            time.tzset()


class Daily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.day = '2026-11-01'
        self.now = daily.due(self.day)+600
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, PRW_REPORT_TO='root', PRW_DAILY_MAX_AGE_HOURS='48', PRW_DAILY_RETRY_LIMIT='3', PRW_DAILY_RETRY_SECONDS='300'))
        self.stack.enter_context(mock.patch.object(report, 'LOG_DIR', Path(self.tmp.name)))
        self.stack.enter_context(mock.patch.object(report, 'rows_for', return_value=(report.normalize_rows([]),0)))
        self.stack.enter_context(mock.patch.object(report, 'report', return_value=('Report','body')))
        self.stack.enter_context(mock.patch.object(report, 'summary_message', return_value=report.discord.payload('Daily', 'Summary', 1)))
        self.stack.enter_context(mock.patch.object(history, 'comparison', return_value=['history']))
        self.mail = self.stack.enter_context(mock.patch.object(report, 'deliver'))
        self.stack.enter_context(mock.patch.object(report.discord, 'enabled', return_value=True))
        self.discord = self.stack.enter_context(mock.patch.object(report.discord, 'send', return_value=(None,0)))

    def state(self):
        return storage.read(Path(self.tmp.name)/'daily'/f'{self.day}.json')

    def test_success_duplicate_delayed(self):
        daily.run(report, self.day, 'auto', create=True, now=self.now)
        daily.run(report, self.day, 'auto', create=True, now=self.now+400)
        self.assertEqual(self.mail.call_count,1)
        self.assertEqual(self.discord.call_count,1)
        self.assertIn('DELAYED',self.mail.call_args.args[0])
        self.assertIn('## NOTES', self.discord.call_args.kwargs['attachment'])
        self.assertLess(self.mail.call_args.args[1].index('history'), self.mail.call_args.args[1].index('NOTES'))
        self.assertEqual(self.state()['email']['status'],'success')

    def test_partial_retry_restart(self):
        self.discord.return_value = ('Discord HTTP 500',60)
        daily.run(report,self.day,'auto',create=True,now=self.now)
        self.assertEqual(self.state()['discord']['status'],'pending')
        daily.run(report,self.day,'auto',now=self.now+100)
        self.assertEqual(self.discord.call_count,1)
        self.discord.return_value = (None,0)
        daily.run(report,self.day,'auto',now=self.now+301)
        self.assertEqual(self.mail.call_count,1)
        self.assertEqual(self.state()['discord']['status'],'success')

    def test_rate_limit_exhaustion(self):
        self.discord.return_value=('Discord HTTP 429',3600)
        for elapsed in (0,300,3601,7202,10803):
            daily.run(report,self.day,'auto',create=True,now=self.now+elapsed)
        self.assertEqual(self.discord.call_count,3)
        self.assertEqual(self.mail.call_count,1)

    def test_ambiguous_no_retry(self):
        self.discord.return_value=('Discord connection or response failure',60)
        self.mail.side_effect=TimeoutError()
        daily.run(report,self.day,'auto',create=True,now=self.now)
        daily.run(report,self.day,'auto',now=self.now+1000)
        self.assertEqual(self.mail.call_count,1)
        self.assertEqual(self.discord.call_count,1)
        self.assertEqual(self.state()['email']['status'],'ambiguous')

    def test_initial_fail_closed(self):
        self.mail.side_effect=RuntimeError('private')
        with self.assertRaises(RuntimeError):
            daily.run(report,self.day,'auto',create=True,initial=True,now=self.now)

    def test_initial_before_schedule(self):
        daily.run(report,self.day,'auto',create=True,initial=True,now=self.now-1000)
        daily.run(report,self.day,'auto',create=True,now=self.now)
        self.assertEqual(self.mail.call_count,1)

    def test_stale_rollback(self):
        for now in (daily.due(self.day)-1,self.now+49*3600):
            self.assertFalse(daily.run(report,self.day,'auto',create=True,now=now))
        self.mail.assert_not_called()

    def test_corrupt_and_crash(self):
        directory=storage.directory(Path(self.tmp.name)/'daily')
        path=directory/f'{self.day}.json'
        path.write_text('{bad')
        with self.assertRaises(ValueError): daily.run(report,self.day,'auto',create=True,now=self.now)
        storage.atomic(path,{key:dict(status='inflight',attempts=1,next=self.now) for key in ('email','discord')})
        daily.run(report,self.day,'auto',now=self.now+600)
        self.mail.assert_not_called()

    def test_initial_contention_and_age(self):
        import fcntl
        directory=storage.directory(Path(self.tmp.name)/'daily')
        with (directory/'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):
                daily.run(report,self.day,'auto',create=True,initial=True,now=self.now)
        # Explicit installation verification is allowed beyond the automatic age limit.
        daily.run(report,self.day,'auto',create=True,initial=True,now=self.now+49*3600)
        self.assertEqual(self.state()['email']['status'],'success')

    def test_state_failure_before_send(self):
        with mock.patch.object(storage,'atomic',side_effect=OSError()):
            with self.assertRaises(OSError): daily.run(report,self.day,'auto',create=True,now=self.now)
        self.mail.assert_not_called()
        self.discord.assert_not_called()

    def test_scheduled_previous_pending(self):
        with mock.patch.object(daily,'run') as run:
            daily.scheduled(report,'auto',now=self.now+86400)
            self.assertIn(self.day,[call.args[1] for call in run.call_args_list])


class Config(unittest.TestCase):
    def test_valid_unknown_secret(self):
        values={'PRW_REPORT_TO':'root','UNKNOWN_SECRET':'https://private/token'}
        errors,warnings=config.validate(values)
        self.assertFalse(errors)
        self.assertTrue(warnings)
        self.assertNotIn('SECRET',' '.join(warnings))
        self.assertNotIn('private',' '.join(warnings))

    def test_errors(self):
        for values in ({'PRW_INTERVAL':'nan'}, {'PRW_RETENTION_DAYS':'0'}, {'PRW_CPU_WARNING_C':'100','PRW_CPU_CRITICAL_C':'90'},
                       {'PRW_MAILER':'bad'}, {'PRW_LOG_DIR':'/tmp'}, {'PRW_DISCORD_ENABLED':'true'}, {'PRW_DAILY_RETRY_LIMIT':'1.5'}):
            self.assertTrue(config.validate(dict(PRW_REPORT_TO='root',**values))[0])

    def test_real_rows_reader_preserves_conflict_uncertainty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'2026-11-01.jsonl'
            path.write_text('\n'.join(json.dumps(dict(time='2026-11-01T12:00:00+00:00',sample_seconds=10,host_cpu_busy_percent=value,temperatures_c={'cpu:A':50})) for value in (10,90))+'\n')
            with mock.patch.object(report,'LOG_DIR',Path(tmp)), mock.patch.object(report,'LEGACY',Path(tmp)/'absent'):
                rows,errors=report.rows_for('2026-11-01')
            self.assertEqual(errors,0)
            self.assertEqual(len(rows),1)
            self.assertTrue(rows.data_uncertain)
            self.assertFalse(rows.clock_uncertain)

    def test_nonmutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config'
            text='PRW_REPORT_TO=root\nPRW_DISCORD_WEBHOOK_URL="https://private/secret"\n'
            path.write_text(text)
            with mock.patch.object(sys,'argv',['check','--config',str(path)]), mock.patch('builtins.print') as output:
                self.assertEqual(config.main(),0)
            self.assertEqual(path.read_text(),text)
            self.assertNotIn('secret',str(output.call_args_list))


class Boundaries(unittest.TestCase):
    def test_real_subprocess_timeout_output_bounds(self):
        original = guests.subprocess.Popen
        for program, expected, timeout in [("import time;time.sleep(2)", TimeoutError, .05),
                                            ("import sys;sys.stdout.write('x'*3000000)", ValueError, 2)]:
            def start(*args, **kwargs):
                return original([sys.executable, '-c', program], **kwargs)
            with mock.patch.object(guests.subprocess, 'Popen', side_effect=start):
                with self.assertRaises(expected):
                    guests.query(timeout=timeout)

    def test_cleanup_disabled_and_symlink_security(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = storage.directory(Path(tmp)/'guests')
            old = directory/'2020-01-01.jsonl'
            old.write_text('old')
            with mock.patch.dict(os.environ, PRW_LOG_DIR=tmp, PRW_GUEST_ENABLED='false'):
                guests.collect()
            self.assertFalse(old.exists())
            target = Path(tmp)/'target'
            target.write_text('{}')
            link = Path(tmp)/'link'
            link.symlink_to(target)
            with self.assertRaises(OSError): storage.read(link)
            with self.assertRaises(OSError): storage.directory(link)

    def test_http_date_retry_after_and_redaction(self):
        import urllib.error
        import email.utils
        import time
        future = email.utils.formatdate(time.time()+100000, usegmt=True)
        error = urllib.error.HTTPError('https://private/token',429,'secret',{'Retry-After':future},None)
        opener = mock.Mock()
        opener.open.side_effect = error
        with mock.patch.dict(os.environ, PRW_DISCORD_ENABLED='true', PRW_DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/123/secret'), mock.patch.object(report.discord.urllib.request,'build_opener',return_value=opener):
            message, delay = report.discord.send({},daily=True)
        self.assertEqual(message,'Discord HTTP 429')
        self.assertGreater(delay,99000)
        self.assertNotIn('secret',message)

    def test_atomic_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state'
            storage.atomic(path,{'a':1})
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(storage.read(path),{'a':1})

    def test_installer_and_alert_isolation(self):
        root=SRC.parent
        installer=(root/'install.sh').read_text()
        self.assertNotIn('enable both',installer)
        self.assertIn('PRW_FOLLOWUP_ENABLED=true',installer)
        for name in ('proxmox-resource-alert.py','proxmox-resource-watch.py'):
            content=(SRC/name).read_text()
            self.assertNotIn('prw_guests',content)
            self.assertNotIn('prw_daily',content)
            self.assertNotIn('prw_history',content)


if __name__ == '__main__':
    unittest.main()
