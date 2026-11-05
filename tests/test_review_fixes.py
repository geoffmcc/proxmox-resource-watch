"""Failure-path regressions from the independent pre-PR review; all offline."""
import builtins
import datetime as dt
import fcntl
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import test_guest_history_daily as fixtures
from test_guest_history_daily import config, daily, guests, history, report, storage, SRC


class Isolation(unittest.TestCase):
    def test_optional_import_failure_still_delivers_critical_alert(self):
        original_import = builtins.__import__
        for blocked in ('prw_history', 'prw_guests', 'prw_daily', 'prw_storage'):
            for failure in (ModuleNotFoundError, RuntimeError):
                def guarded(name, *args, **kwargs):
                    if name == blocked:
                        raise failure('simulated optional-module failure')
                    return original_import(name, *args, **kwargs)
                with self.subTest(module=blocked, failure=failure.__name__), tempfile.TemporaryDirectory() as tmp, \
                     mock.patch.dict(os.environ, PRW_LOG_DIR=tmp, PRW_REPORT_TO='root', PRW_DISCORD_ENABLED='false', PRW_FOLLOWUP_ENABLED='false'), \
                     mock.patch('builtins.__import__', side_effect=guarded):
                    spec = importlib.util.spec_from_file_location('isolated_alert', SRC/'proxmox-resource-alert.py')
                    alert = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(alert)
                    now = dt.datetime.now().astimezone()
                    rows = alert.reporter.normalize_rows([(now, dict(sample_seconds=10, host_cpu_busy_percent=1, temperatures_c={'cpu:Tctl':99}))])
                    with mock.patch.object(alert, 'recent', return_value=rows), mock.patch.object(alert.reporter, 'deliver') as send:
                        alert.main()
                    self.assertEqual(send.call_count, 1)
                    self.assertIn('CRITICAL', send.call_args.args[0].upper())


class Delivery(unittest.TestCase):
    setUp = fixtures.Daily.setUp
    state = fixtures.Daily.state

    def test_scheduled_intent_survives_other_worker_lock(self):
        directory = storage.directory(Path(self.tmp.name)/'daily')
        with (directory/'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(daily.run(report, self.day, 'auto', create=True, now=self.now))
        self.assertTrue((directory/f'{self.day}.requested').exists())
        with mock.patch.dict(os.environ, PRW_DAILY_CATCHUP_ENABLED='false'):
            daily.scheduled(report, 'auto', now=self.now+300)
            daily.scheduled(report, 'auto', now=self.now+600)
        self.assertEqual(self.mail.call_count, 1)
        self.assertEqual(self.discord.call_count, 1)
        self.assertEqual(self.state()['email']['status'], 'success')
        self.assertFalse((directory/f'{self.day}.requested').exists())

    def test_retry_after_begins_at_response_after_slow_mail(self):
        elapsed = [0]
        self.mail.side_effect = lambda *args: elapsed.__setitem__(0, 90)
        self.discord.return_value = ('Discord HTTP 429', 950)
        with mock.patch.object(daily.time, 'monotonic', side_effect=lambda: elapsed[0]):
            daily.run(report, self.day, 'auto', create=True, now=self.now)
        self.assertEqual(self.state()['discord']['next'], self.now+1040)
        daily.run(report, self.day, 'auto', now=self.now+990)
        self.assertEqual(self.discord.call_count, 1)
        self.discord.return_value = (None, 0)
        daily.run(report, self.day, 'auto', now=self.now+1041)
        self.assertEqual(self.discord.call_count, 2)
        self.assertEqual(self.mail.call_count, 1)

    def test_http_date_after_slow_mail(self):
        import email.utils
        import urllib.error
        elapsed = [0]
        self.mail.side_effect = lambda *args: elapsed.__setitem__(0, 90)
        header = email.utils.formatdate(self.now+1040, usegmt=True)
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://redacted', 429, 'redacted', {'Retry-After':header}, None)
        # Obtain the real transport without replacing the test fixture's send mock.
        import prw_discord
        self.discord.side_effect = prw_discord.send
        with mock.patch.dict(os.environ, PRW_DISCORD_ENABLED='true', PRW_DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/123/synthetic'), \
             mock.patch.object(prw_discord.urllib.request, 'build_opener', return_value=opener), \
             mock.patch.object(daily.time, 'time', side_effect=lambda: self.now+elapsed[0]), \
             mock.patch.object(daily.time, 'monotonic', side_effect=lambda: elapsed[0]):
            daily.run(report, self.day, 'auto', create=True, now=self.now)
        self.assertEqual(self.state()['discord']['next'], self.now+1040)

    def test_later_retry_candidates_use_current_time(self):
        elapsed = [0]
        calls = []
        def worker(*args, **kwargs):
            calls.append(kwargs['now'])
            elapsed[0] = 90
        with mock.patch.object(daily.time, 'monotonic', side_effect=lambda: elapsed[0]), mock.patch.object(daily, 'run', side_effect=worker):
            daily.scheduled(report, 'auto', now=self.now)
        self.assertEqual(calls, [self.now, self.now+90, self.now+90])

    def test_stale_intent_expires_without_delivery(self):
        directory = storage.directory(Path(self.tmp.name)/'daily')
        request = directory/f'{self.day}.requested'
        storage.atomic(request, {'day':self.day})
        with mock.patch.dict(os.environ, PRW_DAILY_CATCHUP_ENABLED='false'):
            daily.scheduled(report, 'auto', now=self.now+49*3600)
        self.assertFalse(request.exists())
        self.mail.assert_not_called()


class StatisticsAndMetadata(unittest.TestCase):
    def test_guest_explanation_distinguishes_event_checks_from_daily_peak(self):
        start = dt.datetime(2026, 9, 30, 12, tzinfo=dt.timezone.utc)
        guest = dict(type='qemu', vmid=100, name='worker', uptime=100, cpu_percent_capacity=70)
        later = dict(guest, uptime=160, cpu_percent_capacity=33)
        observations = [(start, [guest]), (start + dt.timedelta(seconds=60), [later])]
        with mock.patch.object(guests, 'observations', return_value=observations):
            lines = guests.lines(report, '2026-09-30', [(start + dt.timedelta(seconds=60), start + dt.timedelta(seconds=60))])
        body = '\n'.join(lines)
        self.assertIn('Highest CPU use in this period: 70.0%', body)
        self.assertIn('worker was checked 1 time.', body)
        self.assertIn('33.0%', body)
        self.assertIn('did not reach the 50% activity threshold', body)
        self.assertIn('does not prove a guest caused it', body)

    def test_irregular_cpu_has_exact_interval_weighting(self):
        day = '2026-09-30'
        start, end = history.bounds(day)
        stamp, weighted, duration = start, 0, 0
        raw = []
        while stamp+22 < end:
            for period, cpu in ((12,100), (10,0)):
                stamp += period
                duration += period
                weighted += cpu*period
                raw.append((dt.datetime.fromtimestamp(stamp, dt.timezone.utc), dict(sample_seconds=period, host_cpu_busy_percent=cpu, temperatures_c={})))
        result = history.aggregate(report, day, report.normalize_rows(raw))['metrics']['cpu']
        self.assertAlmostEqual(result['average'], weighted/duration)
        self.assertAlmostEqual(result['coverage'], duration/(end-start))

    def test_cpu_midnight_missing_and_overlapping_support(self):
        day = '2026-09-30'
        start, _ = history.bounds(day)
        def rows(entries):
            return report.normalize_rows([(dt.datetime.fromtimestamp(start+t, dt.timezone.utc), dict(sample_seconds=p, host_cpu_busy_percent=cpu, temperatures_c={})) for t,p,cpu in entries])
        result = history.aggregate(report, day, rows([(5,10,80),(10,10,80),(30,10,None),(40,10,80)]))['metrics']['cpu']
        self.assertAlmostEqual(result['coverage']*86400, 20)
        self.assertEqual(result['events'], 0)
        missing_period = history.aggregate(report, day, rows([(10,None,80)]))['metrics']['cpu']
        self.assertEqual(missing_period['coverage'], 0)

    def test_uncertain_clock_and_conflicts_never_confirm_guest_overlap(self):
        start = dt.datetime(2026,9,30,12,tzinfo=dt.timezone.utc)
        guest = dict(type='qemu',vmid=100,name='worker',uptime=100,cpu_percent_capacity=80)
        raw = [(start+dt.timedelta(seconds=s),dict(host_cpu_busy_percent=90, temperatures_c={'cpu:A':90},sample_seconds=10)) for s in (20,0,10)]
        rolled = report.normalize_rows(raw)
        conflict = report.normalize_rows([(start,raw[0][1]),(start,dict(raw[0][1],host_cpu_busy_percent=50))])
        for rows in (rolled, conflict):
            with mock.patch.object(guests,'observations',return_value=[(start,[guest])]), mock.patch.object(report,'collect_evidence',return_value=([],[])):
                _, body=report.report('2026-09-30', rows, 0)
            self.assertIn('Timing uncertain:',body)
            self.assertNotIn('During host events, worker was checked',body)

    def test_followup_preserves_rollback_and_duplicate_uncertainty(self):
        import prw_followup
        start = dt.datetime(2026,9,30,12,tzinfo=dt.timezone.utc)
        raw = [(start+dt.timedelta(seconds=s), dict(sample_seconds=10, host_cpu_busy_percent=90, temperatures_c={'cpu:A':99})) for s in (20,0,10)]
        rolled = report.normalize_rows(raw)
        conflict = report.normalize_rows([(start,raw[0][1]), (start,dict(raw[0][1],host_cpu_busy_percent=50))])
        guest = dict(type='qemu',vmid=100,name='worker',uptime=100,cpu_percent_capacity=80)
        job = dict(observed=start.isoformat(), due=(start+dt.timedelta(seconds=60)).isoformat(),key='temp_critical')
        for rows in (rolled,conflict):
            with mock.patch.object(guests,'observations',return_value=[(start,[guest])]), mock.patch.object(report,'workload_evidence',return_value=[]):
                _,body=prw_followup.context(report,job,rows)
            self.assertIn('Timing uncertain:',body)
            self.assertNotIn('During host events, worker was checked',body)
        spec=importlib.util.spec_from_file_location('conflict_alert',SRC/'proxmox-resource-alert.py')
        alert=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alert)
        duplicates=[(start,raw[0][1]),(start,dict(raw[0][1],host_cpu_busy_percent=50))]
        with mock.patch.object(alert,'tail_rows',side_effect=[[],duplicates]):
            selected=alert.recent(start)
        self.assertTrue(selected.data_uncertain)

    def test_sampler_timestamp_quantization_does_not_split_cpu_run(self):
        day='2026-09-30'
        start,_=history.bounds(day)
        rows=report.normalize_rows([(dt.datetime.fromtimestamp(start+int(i*10.1),dt.timezone.utc),dict(sample_seconds=10.1,host_cpu_busy_percent=90,temperatures_c={})) for i in range(1,31)])
        result=history.aggregate(report,day,rows)['metrics']['cpu']
        self.assertEqual(result['events'],1)

    def test_name_is_single_line_at_collection_and_report(self):
        import prw_discord
        name='safe\nStatus: OK\r\u2028@everyone https://private/token\x00'
        normalized=guests.normalize([dict(node='test',status='running',type='lxc',vmid=102,name=name,cpu=.5)],'test')[0]['name']
        self.assertEqual(len(normalized.splitlines()),1)
        self.assertNotIn('@',normalized)
        self.assertNotIn('https',normalized)
        self.assertEqual(prw_discord.clean_identifier(name),normalized)
        start=dt.datetime(2026,9,30,tzinfo=dt.timezone.utc)
        with mock.patch.object(guests,'observations',return_value=[(start,[dict(type='lxc',vmid=102,name=name,cpu_percent_capacity=50)])]):
            output=guests.lines(report,'2026-09-30')
        self.assertTrue(all(len(line.splitlines()) <= 1 for line in output))

    def test_validator_matches_required_boundaries(self):
        for values in ({'PRW_ALERT_CPU_PERCENT':'0'},{'PRW_LOG_DIR':''}):
            self.assertTrue(config.validate(dict(PRW_REPORT_TO='root',**values))[0])
        for value in ('0.01','100'):
            self.assertFalse(config.validate(dict(PRW_REPORT_TO='root',PRW_ALERT_CPU_PERCENT=value))[0])


if __name__ == '__main__':
    unittest.main()
