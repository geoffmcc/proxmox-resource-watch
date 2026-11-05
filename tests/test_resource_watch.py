"""Offline regression checks for the sampler, digest and alert logic."""
import datetime as dt
import gzip
import io
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import time
import stat
import signal
import types
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]


def load(script, name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / "src" / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = load("proxmox-jellyfin-export.py", "exporter_under_test")
sampler = load("proxmox-resource-watch.py", "sampler_under_test")
report = load("proxmox-resource-report.py", "report_under_test")
alert = load("proxmox-resource-alert.py", "alert_under_test")

TZ = dt.timezone(dt.timedelta(hours=-5))


def sample(moment, cpu=1, temp=45, process="kvm", per_core=2):
    return {
        "time": moment.isoformat(timespec="seconds"),
        "host_cpu_busy_percent": cpu,
        "gpu_busy_percent": 0,
        "gpu_vram_used_bytes": None,
        "temperatures_c": {"cpu_k10temp": temp, "gpu_amdgpu": 42},
        "fans_rpm": {},
        "top_processes": [{"pid": 42, "name": process,
                           "cpu_percent_one_core": per_core, "command": process}],
    }


class SamplingAndStorageTests(unittest.TestCase):
    def test_completed_day_repair_keeps_original_and_rejects_other_damage(self):
        date = dt.datetime.now().astimezone().date() - dt.timedelta(days=1)
        day = date.isoformat()
        stamp = dt.datetime.combine(date, dt.time(12)).astimezone()
        valid = json.dumps(sample(stamp)).encode() + b'\n'
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(report, 'LOG_DIR', Path(directory)):
            path = Path(directory) / (day + '.jsonl')
            original = b'\0' * 3681 + valid
            path.write_bytes(original)
            notice = report.repair_completed_days([day])
            self.assertEqual(path.read_bytes(), valid)
            backup = Path(str(path) + '.prw-original')
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertIn('repaired 1 sample', notice[0])
            rows, errors = report.rows_for(day, include_legacy=False)
            self.assertEqual((len(rows), errors), (1, 0))
            self.assertIn('earlier repair', report.repair_completed_days([day])[0])
            path.write_bytes(original)
            self.assertIn('could not be completed', report.repair_completed_days([day])[0])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(backup.read_bytes(), original)
            path.write_bytes(original + b'{bad json}\n')
            self.assertTrue(report.repair_completed_days([day]))
            self.assertEqual(path.read_bytes(), original + b'{bad json}\n')
            self.assertEqual(backup.read_bytes(), original)

    def test_repair_closed_gzip_but_not_today(self):
        today = dt.datetime.now().astimezone().date()
        old = today - dt.timedelta(days=2)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(report, 'LOG_DIR', Path(directory)):
            for date in (old, today):
                line = json.dumps(sample(dt.datetime.combine(date, dt.time(12)).astimezone())).encode() + b'\n'
                path = Path(directory) / (date.isoformat() + '.jsonl.gz')
                path.write_bytes(gzip.compress(b'\0' * 10 + line))
            messages = report.repair_completed_days([old.isoformat(), today.isoformat()])
            self.assertEqual(len(messages), 1)
            self.assertFalse(gzip.decompress((Path(directory) / (old.isoformat() + '.jsonl.gz')).read_bytes()).startswith(b'\0'))
            self.assertTrue(gzip.decompress((Path(directory) / (today.isoformat() + '.jsonl.gz')).read_bytes()).startswith(b'\0'))

    def test_report_recovers_only_complete_null_prefixed_samples_without_modifying_log(self):
        day = '2026-11-04'
        stamp = dt.datetime(2026, 11, 4, 12, tzinfo=TZ)
        valid = json.dumps(sample(stamp)).encode()
        contents = b'\0' * 3681 + valid + b'\n' + b'\0{bad json}\n' + b'\0' + valid
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / (day + '.jsonl')
            path.write_bytes(contents)
            with mock.patch.object(report, 'LOG_DIR', Path(directory)):
                rows, errors = report.rows_for(day, include_legacy=False)
            self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(len(rows), 1)
            self.assertEqual(errors, 3)
            self.assertEqual(rows.recovered, 1)
            self.assertTrue(rows.data_uncertain)
            with mock.patch.object(report, 'collect_evidence', return_value=([], [])):
                _, body = report.report(day, rows, errors)
            self.assertIn('Recovered 1 sample(s)', body)
            self.assertIn('other data may have been lost', body)
            import prw_history
            self.assertTrue(prw_history.aggregate(report, day, rows, errors)['uncertain'])

    def test_sample_is_flushed_and_synced(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(sampler.os, 'fsync', wraps=os.fsync) as sync:
            path = Path(directory) / 'samples.jsonl'
            sampler.append_sample(path, {'time': '2026-11-05T12:00:00+00:00'})
            self.assertEqual(json.loads(path.read_text())['time'], '2026-11-05T12:00:00+00:00')
            sync.assert_called_once()

    def test_sampler_stops_cleanly_on_sigterm(self):
        with tempfile.TemporaryDirectory() as directory:
            program = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("sampler", sys.argv[1])
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
s.LOG_DIR = pathlib.Path(sys.argv[2]); s.INTERVAL = 1
s.cpu_ticks = lambda: (100, 50)
s.process_ticks = lambda: {}
s.sensors = lambda: {}
s.maintenance = lambda day: None
s.main()
'''
            process = subprocess.Popen(['python3', '-c', program, str(PROJECT / 'src/proxmox-resource-watch.py'), directory])
            try:
                deadline = time.monotonic() + 8
                while not list(Path(directory).glob('*.jsonl')) and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(list(Path(directory).glob('*.jsonl')))
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                for line in next(Path(directory).glob('*.jsonl')).read_text().splitlines():
                    self.assertIn('time', json.loads(line))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_signal_during_write_finishes_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            program = '''
import importlib.util, os, pathlib, signal, sys
spec = importlib.util.spec_from_file_location("sampler", sys.argv[1])
s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
s.LOG_DIR = pathlib.Path(sys.argv[2]); s.INTERVAL = 1
s.cpu_ticks = lambda: (100, 50)
s.process_ticks = lambda: {}
s.sensors = lambda: {}
s.maintenance = lambda day: None
original = s.os.fsync
def stop_during_sync(fd):
    os.kill(os.getpid(), signal.SIGTERM)
    original(fd)
s.os.fsync = stop_during_sync
s.main()
'''
            result = subprocess.run(['python3', '-c', program, str(PROJECT / 'src/proxmox-resource-watch.py'), directory], timeout=8)
            self.assertEqual(result.returncode, 0)
            lines = next(Path(directory).glob('*.jsonl')).read_text().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn('time', json.loads(lines[0]))

    def test_unique_sensor_identity_code_path(self):
        # Labels are explicit, not overwritten by common driver names.
        content = (PROJECT / "src" / "proxmox-resource-watch.py").read_text()
        self.assertIn('f"{prefix}:{label}"', content)

    def test_cpu_ticks_proc_stat(self):
        with mock.patch.object(sampler, "read", return_value="cpu 1 2 3 4 5 6 7 8 9 10"):
            self.assertEqual(sampler.cpu_ticks(), (36, 9))

    def test_guest_and_niced_guest_ticks_are_counted_once(self):
        for line in ("cpu 600 0 0 400 0 0 0 0 600 0",
                     "cpu 0 600 0 400 0 0 0 0 0 600",
                     "cpu 300 300 0 400 0 0 0 0 300 300"):
            with self.subTest(line=line), mock.patch.object(sampler, "read", return_value=line):
                total, idle = sampler.cpu_ticks()
                self.assertEqual((total, idle), (1000, 400))
                self.assertAlmostEqual(100 * (1 - idle / total), 60)

    def test_guest_workload_cpu_warning_uses_actual_host_utilization(self):
        start = dt.datetime(2026, 11, 3, 12, tzinfo=TZ)
        # A guest-dominated 70% workload previously appeared as ~82%, above
        # the default sustained warning. An actual 80% workload must still alert.
        for user, idle, expected_key in ((700, 300, None), (800, 200, "cpu_warning")):
            with self.subTest(user=user), mock.patch.object(sampler, "read", side_effect=[
                    "cpu 0 0 0 1000 0 0 0 0 0 0",
                    f"cpu {user} 0 0 {1000 + idle} 0 0 0 0 {user} 0"]):
                first_total, first_idle = sampler.cpu_ticks()
                total, current_idle = sampler.cpu_ticks()
                cpu = 100 * (1 - (current_idle - first_idle) / (total - first_total))
                rows = [(start + dt.timedelta(seconds=i * 10), sample(start, cpu=cpu)) for i in range(30)]
                event = alert.candidate(rows[-1][0], rows)
                self.assertEqual(event["key"] if event else None, expected_key)

    def test_compress_old_dated_log_without_touching_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            old = directory / "2026-10-30.jsonl"
            old.write_text('{"time":"2026-10-30T12:00:00-05:00"}\n')
            yesterday = directory / "2026-11-01.jsonl"
            yesterday.write_text("yesterday\n")
            legacy = directory / "proxmox-resource-watch.jsonl"
            legacy.write_text("preserve this\n")
            with mock.patch.object(sampler, "LOG_DIR", directory), mock.patch.object(sampler, "RETENTION_DAYS", 30):
                sampler.maintenance(dt.date(2026, 11, 2))
            self.assertFalse(old.exists())
            self.assertTrue(yesterday.exists())
            self.assertEqual(legacy.read_text(), "preserve this\n")
            with gzip.open(directory / "2026-10-30.jsonl.gz", "rt") as src:
                self.assertIn("2026-10-30", src.read())

    def test_retention_does_not_delete_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            expired = directory / "2026-09-01.jsonl"
            unrelated = directory / "private-notes.txt"
            expired.write_text("old")
            unrelated.write_text("keep")
            with mock.patch.object(sampler, "LOG_DIR", directory), mock.patch.object(sampler, "RETENTION_DAYS", 30):
                sampler.maintenance(dt.date(2026, 11, 2))
            self.assertFalse(expired.exists())
            self.assertEqual(unrelated.read_text(), "keep")


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.t = dt.datetime(2026, 11, 2, 12, 0, tzinfo=TZ)
        patch = mock.patch.object(report, "collect_evidence", return_value=([], []))
        patch.start()
        self.addCleanup(patch.stop)

    def test_temperature_celsius_and_fahrenheit(self):
        self.assertEqual(report.temp_format(83), "83.0°C [181.4°F]")
        self.assertEqual(report.temp_format(92), "92.0°C [197.6°F]")

    def test_cpu_warning_and_critical_classification(self):
        for temperature, phrase in ((50, "Status: OK"), (83, "Status: ELEVATED"),
                                    (86, "Status: WARNING"), (94, "Status: CRITICAL")):
            _, body = report.report("2026-11-02", [(self.t, sample(self.t, temp=temperature))], 0)
            self.assertIn(phrase, body)

    def test_unknown_cpu_sensor_not_called_ok(self):
        item = sample(self.t)
        item["temperatures_c"] = {"nvme": 75}
        _, body = report.report("2026-11-02", [(self.t, item)], 0)
        self.assertIn("Status: UNKNOWN", body)

    def test_event_lines_distinguish_missing_measurements_from_observed_zero(self):
        for cpu, temp, expected in (
                (20, None, "CPU max 20.0%, temp max unavailable"),
                (None, 90, "CPU max unavailable, temp max 90.0°C [194.0°F]"),
                (float("nan"), 90, "CPU max unavailable, temp max 90.0°C [194.0°F]"),
                (20, float("nan"), "CPU max 20.0%, temp max unavailable"),
                (0, 90, "CPU max 0.0%, temp max 90.0°C [194.0°F]"),
                (20, 0, "CPU max 20.0%, temp max 0.0°C [32.0°F]"),
                (20, -5, "CPU max 20.0%, temp max -5.0°C [23.0°F]")):
            with self.subTest(cpu=cpu, temp=temp):
                row = sample(self.t, cpu=cpu, temp=temp)
                row["temperatures_c"] = {} if temp is None else {"cpu_k10temp": temp}
                _, body = report.report("2026-11-02", [(self.t, row)], 0)
                self.assertIn(expected, body)

    def test_event_peaks_use_available_readings_across_partial_data(self):
        rows = []
        for i, (cpu, temp) in enumerate(((None, 90), (20, None), (0, 0), (None, 91))):
            stamp = self.t + dt.timedelta(seconds=i * 10)
            row = sample(stamp, cpu=cpu)
            row["temperatures_c"] = {} if temp is None else {"cpu_k10temp": temp}
            rows.append((stamp, row))
        _, body = report.report("2026-11-02", rows, 0)
        self.assertIn("CPU max 20.0%, temp max 90.0°C [194.0°F]", body)
        self.assertIn("CPU max unavailable, temp max 91.0°C [195.8°F]", body)

    def test_events_with_missing_peaks_still_sort_by_observed_values(self):
        rows = []
        for i, (cpu, temp) in enumerate(((None, 90), (0, 0), (20, None), (0, 0), (20, 40))):
            stamp = self.t + dt.timedelta(seconds=i * 10)
            row = sample(stamp, cpu=cpu)
            row["temperatures_c"] = {} if temp is None else {"cpu_k10temp": temp}
            rows.append((stamp, row))
        found = report.events(rows)
        self.assertEqual([(e["cpu_peak"], e["temp_peak"]) for e in found],
                         [(20, 40), (20, None), (None, 90)])

    def test_starts_and_stops_brief_and_sustained_events(self):
        samples = [(self.t + dt.timedelta(seconds=i * 10),
                    sample(self.t + dt.timedelta(seconds=i * 10),
                           temp=(87 if i in (1, 2, 3) or i == 6 else 45)))
                   for i in range(8)]
        detected = report.events(samples)
        self.assertEqual(len(detected), 2)
        lengths = sorted(e["samples"] for e in detected)
        self.assertEqual(lengths, [1, 3])
        _, body = report.report("2026-11-02", samples, 0)
        self.assertIn("SUSTAINED EVENTS (>= 30s): 1", body)
        self.assertIn("BRIEF SPIKES (< 30s): 1", body)

    def test_pre_event_process_context(self):
        before = sample(self.t, process="vzdump", temp=45, per_core=60)
        after_time = self.t + dt.timedelta(seconds=10)
        event = sample(after_time, process="jellyfin", temp=90)
        found = report.events([(self.t, before), (after_time, event)])
        self.assertEqual(found[0]["before"]["vzdump"], 60)
        self.assertIn("jellyfin", found[0]["processes"])

    def test_old_and_new_logs_deduplicate_exact_records(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            row = sample(self.t)
            line = json.dumps(row) + "\n"
            (directory / "2026-11-02.jsonl").write_text(line)
            legacy = directory / "legacy.jsonl"
            legacy.write_text(line)
            with mock.patch.object(report, "LOG_DIR", directory), mock.patch.object(report, "LEGACY", legacy):
                records, errors = report.rows_for("2026-11-02")
            self.assertEqual(errors, 0)
            self.assertEqual(len(records), 1)

    def test_corrupt_lines_do_not_crash_report(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "2026-11-02.jsonl").write_text("not-json\n" + json.dumps(sample(self.t)) + "\n")
            with mock.patch.object(report, "LOG_DIR", directory), mock.patch.object(report, "LEGACY", directory / "missing"):
                records, errors = report.rows_for("2026-11-02")
            self.assertEqual((len(records), errors), (1, 1))

    def test_sendmail_header_is_utf8_safe_and_no_real_mail(self):
        with mock.patch.object(report.subprocess, "run") as run:
            report.deliver("CPU 92°C [197.6°F]", "body", "root", "sendmail")
        payload = run.call_args.kwargs["input"]
        self.assertIsInstance(payload, bytes)
        self.assertIn(b"=?utf-8?", payload.lower())
        self.assertIn(b"To: root", payload)

    def test_bad_recipient_rejected(self):
        with self.assertRaises(ValueError):
            report.deliver("test", "body", "-arg", "mutt")


class ImmediateAlertTests(unittest.TestCase):
    def setUp(self):
        self.t = dt.datetime(2026, 11, 2, 12, 0, tzinfo=TZ)

    def series(self, count, interval, temp=45, cpu=1):
        return [(self.t + dt.timedelta(seconds=i * interval),
                 sample(self.t + dt.timedelta(seconds=i * interval), temp=temp, cpu=cpu))
                for i in range(count)]

    def test_critical_is_immediate(self):
        row = self.series(1, 10, temp=92)[0]
        self.assertEqual(alert.candidate(row[0], [row])["key"], "temp_critical")

    def test_warning_requires_three_samples(self):
        rows = self.series(3, 10, temp=86)
        self.assertIsNone(alert.candidate(rows[1][0], rows[:2]))
        self.assertEqual(alert.candidate(rows[-1][0], rows)["key"], "temp_warning")

    def test_short_cpu_spike_does_not_email(self):
        rows = self.series(29, 10, cpu=90)
        self.assertIsNone(alert.candidate(rows[-1][0], rows))
        rows = self.series(30, 10, cpu=90)
        self.assertEqual(alert.candidate(rows[-1][0], rows)["key"], "cpu_warning")

    def test_disconnected_samples_do_not_count_as_sustained(self):
        rows = self.series(3, 30, temp=86)
        self.assertIsNone(alert.candidate(rows[-1][0], rows))

    def test_stale_log_cannot_trigger_alert(self):
        rows = self.series(1, 10, temp=94)
        self.assertIsNone(alert.candidate(rows[0][0] + dt.timedelta(minutes=1), rows))

    def test_alert_state_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "alert-state.json"
            with mock.patch.object(alert, "STATE", target):
                alert.save_state({"temp_critical": self.t.isoformat()})
                self.assertEqual(alert.load_state()["temp_critical"], self.t.isoformat())
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_cooldown_suppresses_repeat(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "alert-state.json"
            rows = self.series(1, 10, temp=94)
            with mock.patch.object(alert, "STATE", target), mock.patch.object(alert, "recent", return_value=rows), \
                    mock.patch.object(alert.dt, "datetime", wraps=dt.datetime) as date_mock, \
                    mock.patch.object(alert.reporter, "deliver") as deliver, \
                    mock.patch.dict(os.environ, {"PRW_REPORT_TO": "root", "PRW_MAILER": "mutt"}):
                date_mock.now.return_value = self.t
                alert.main()
                alert.main()
            self.assertEqual(deliver.call_count, 1)



class HistoricalEventTests(unittest.TestCase):
    def setUp(self):
        self.t = dt.datetime(2026, 11, 2, 23, 59, 50, tzinfo=TZ)

    def rows(self, temps, step=10):
        return [(self.t + dt.timedelta(seconds=i * step),
                 sample(self.t + dt.timedelta(seconds=i * step), temp=temp))
                for i, temp in enumerate(temps)]

    def test_warning_critical_peak_and_midnight(self):
        events = report.temperature_events(self.rows([86, 94, 90]))
        warning, critical = events
        self.assertEqual(warning['level'], 'warning')
        self.assertEqual(warning['duration'], 20)
        self.assertEqual(warning['peak'], 94)
        self.assertEqual(warning['peak_time'].date(), dt.date(2026, 11, 3))
        self.assertEqual(critical['level'], 'critical')

    def test_recovery_and_hysteresis(self):
        events = report.temperature_events(self.rows([86, 83, 81, 81, 81, 81, 81, 81, 81]))
        self.assertEqual(events[0]['status'], 'completed (recovered)')
        self.assertEqual(events[0]['duration'], 0)

    def test_gap_and_missing_sensor_interrupt(self):
        rows = self.rows([90, 90], step=30)
        self.assertIn('interrupted', report.temperature_events(rows)[0]['status'])
        rows = self.rows([90, 90])
        rows[1][1]['temperatures_c'] = {}
        self.assertIn('interrupted', report.temperature_events(rows)[0]['status'])

    def test_process_ranking_duplicates_and_ended_process(self):
        rows = self.rows([90, 90])
        rows[0][1]['top_processes'] = [dict(pid=1, name='job', cpu_percent_one_core=200),
                                       dict(pid=2, name='job', cpu_percent_one_core=50)]
        rows[1][1]['top_processes'] = [dict(pid=2, name='job', cpu_percent_one_core=50)]
        scores = report.process_history([rows[0], rows[0], rows[1]])
        self.assertEqual([s['pid'] for s in scores], [1, 2])
        self.assertEqual(scores[0]['samples'], 1)
        self.assertEqual(scores[1]['samples'], 2)


class WorkloadIntervalTests(unittest.TestCase):
    def setUp(self):
        self.t = dt.datetime(2026, 11, 3, tzinfo=TZ)

    def test_overlapping_backups_specific_format(self):
        lines = ["2026-11-03T00:00:00-05:00 node vzdump[123]: INFO: Starting Backup of VM 101 (qemu)",
                 "2026-11-03T00:00:01-05:00 node vzdump[456]: INFO: Starting Backup of VM 102 (qemu)",
                 "2026-11-03T00:01:00-05:00 node vzdump[123]: INFO: Finished Backup of VM 101 (00:01:00)",
                 "2026-11-03T00:02:00-05:00 node vzdump[456]: ERROR: Backup of VM 102 failed: interrupted"]
        # Start messages have a format suffix; support the actual vzdump format.
        items = report.backup_intervals(lines, TZ)
        self.assertEqual(len(items), 4)
        self.assertTrue(all(x['start'] is None or x['end'] is None for x in items))

    def test_upid_pairs_are_confirmed_and_job_specific(self):
        one = 'UPID:node:00000001:00000002:00000003:vzdump:101:root@pam:'
        two = 'UPID:node:00000001:00000002:00000004:vzdump:101:root@pam:'
        lines = [f'2026-11-03T00:00:00-05:00 node pvedaemon[1]: <root@pam> starting task {one}',
                 f'2026-11-03T00:00:01-05:00 node pvedaemon[1]: <root@pam> starting task {two}',
                 f'2026-11-03T00:01:00-05:00 node pvedaemon[1]: <root@pam> end task {one} OK']
        items = report.backup_intervals(lines, TZ)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['confidence'], 'confirmed')
        self.assertIsNone(items[1]['end'])

    def test_keywords_cannot_create_work(self):
        for text in ('vzdump start VM 101', 'backup job finished', 'Audio Normalization started',
                     'TaskManager: advice: Audio Normalization Completed after 5 minutes',
                     'Scanning media library completed after 3 minutes'):
            line = '2026-11-03T00:00:00-05:00 ' + text
            self.assertEqual(report.backup_intervals([line], TZ), [])
            self.assertEqual(report.jellyfin_intervals([line], TZ), [])

    def test_pid_vm_mismatch_never_pairs(self):
        lines = ['2026-11-03T00:00:00-05:00 vzdump[1]: Starting Backup of VM 101',
                 '2026-11-03T00:01:00-05:00 vzdump[2]: Finished Backup of VM 101']
        self.assertTrue(all(x['start'] is None or x['end'] is None for x in report.backup_intervals(lines, TZ)))

    def test_repeated_start_is_ambiguous(self):
        lines = ['2026-11-03T00:00:00-05:00 vzdump[1]: Starting Backup of VM 101',
                 '2026-11-03T00:00:01-05:00 vzdump[1]: Starting Backup of VM 101',
                 '2026-11-03T00:01:00-05:00 vzdump[1]: Finished Backup of VM 101']
        self.assertTrue(all(x['start'] is None or x['end'] is None for x in report.backup_intervals(lines, TZ)))

    def test_real_jellyfin_duration_crosses_midnight(self):
        items = report.jellyfin_intervals(['[2026-11-03 00:43:22.000 -05:00] [INF] [4] Emby.Server.Implementations.ScheduledTasks.TaskManager: Audio Normalization Completed after 43 minute(s) and 6 seconds'], TZ)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['start'].strftime('%H:%M:%S'), '00:00:16')
        self.assertEqual(items[0]['confidence'], 'confirmed')

    def test_duration_limits(self):
        for text in ('after nonsense', 'after 999999999999999999999 hours', 'after -1 seconds'):
            self.assertIsNone(report.task_duration(text))

    def test_optional_special_files(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / 'fifo'
            os.mkfifo(fifo)
            link = Path(directory) / 'link'
            link.symlink_to(fifo)
            for path in (fifo, link, Path('/dev/null'), Path(directory), Path(directory)/'absent'):
                with mock.patch.dict(os.environ, {'PRW_JELLYFIN_LOG': str(path)}), mock.patch.object(report.subprocess, 'run', side_effect=PermissionError):
                    began = time.monotonic()
                    notices, items = report._collect_evidence(self.t, self.t)
                    self.assertLess(time.monotonic() - began, 0.5)
                    self.assertEqual(len(notices), 2)
                    self.assertEqual(items, [])

    def test_optional_worker_success_and_start_failure(self):
        with mock.patch.object(report, '_collect_evidence', return_value=(['unknown'], [])):
            self.assertEqual(report.collect_evidence(self.t, self.t), (['unknown'], []))
        with mock.patch.object(report.os, 'fork', side_effect=OSError('process limit')):
            self.assertIn('unavailable', report.collect_evidence(self.t, self.t)[0][0])

    def test_misleading_logger_name_is_not_a_record(self):
        lines = ['2026-11-03T00:00:00-05:00 unrelated TaskManager: Audio Normalization Completed after 5 minutes',
                 '2026-11-03T00:00:00-05:00 app: mentioned vzdump[1]: Starting Backup of VM 101']
        self.assertEqual(report.jellyfin_intervals(lines, TZ), [])
        self.assertEqual(report.backup_intervals(lines, TZ), [])

    def test_disabled_and_failed_journal(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(report.subprocess, 'run', side_effect=PermissionError):
            notices, items = report._collect_evidence(self.t, self.t)
        self.assertEqual(len(notices), 1)
        self.assertEqual(items, [])

    def test_journal_bounded_output_and_command(self):
        def write(*args, **kwargs):
            kwargs['stdout'].write(b'2026-11-03T00:00:00-05:00 vzdump[1]: Starting Backup of VM 101\n')
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(report.subprocess, 'run', side_effect=write) as run:
            notices, items = report._collect_evidence(self.t, self.t)
        self.assertEqual(len(items), 1)
        self.assertEqual(run.call_args.kwargs['timeout'], 3)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_recovery_only_workload_excluded(self):
        item = dict(name='job', start=self.t+dt.timedelta(seconds=10), end=self.t+dt.timedelta(seconds=20), status='completed', confidence='confirmed')
        self.assertEqual(report.workload_evidence(self.t, self.t, ([], [item])), [])


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.t = dt.datetime(2026, 11, 3, tzinfo=TZ)

    def rows(self, values):
        return [(self.t+dt.timedelta(seconds=i*10), sample(self.t, temp=v)) for i, v in enumerate(values)]

    def test_conflicting_temperature_duplicate_keeps_99(self):
        rows = self.rows([45]) + [(self.t, sample(self.t, temp=99))]
        for ordered in (rows, rows[::-1]):
            events = report.temperature_events(ordered)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]['peak'], 99)
            self.assertEqual(events[0]['duration'], 0)

    def test_identical_duplicates_no_duration_inflation(self):
        rows = self.rows([95, 95])
        self.assertEqual(report.temperature_events(rows*3)[0]['duration'], 10)

    def test_conflicting_process_duplicates_merge_max(self):
        a, b = sample(self.t, per_core=100), sample(self.t, per_core=200)
        b['top_processes'].append(dict(name='other', pid=43, cpu_percent_one_core=50))
        scores = report.process_history([(self.t, a), (self.t, b)])
        self.assertEqual([x['cpu_seconds'] for x in scores], [20, 5])

    def test_disappearing_responsible_sensor_is_unknown(self):
        rows = self.rows([95]+[45]*8)
        for _, row in rows[1:]:
            row['temperatures_c'] = {'cpu_other': 45}
        self.assertTrue(all('unknown' in e['status'] for e in report.temperature_events(rows)))

    def test_other_hot_sensor_gets_own_episode(self):
        rows = self.rows([95, 45])
        rows[1][1]['temperatures_c']['cpu_other'] = 96
        events = report.temperature_events(rows)
        self.assertEqual({e['sensor'] for e in events}, {'cpu_k10temp', 'cpu_other'})

    def test_hot_end_and_recovery_confirmation_separate(self):
        event = report.temperature_events(self.rows([86]+[45]*7))[0]
        self.assertEqual(event['last_hot'], self.t)
        self.assertEqual(event['recovery_time'], self.t+dt.timedelta(seconds=70))
        self.assertEqual(len(event['rows']), 1)
        self.assertEqual(report.process_history(event['rows'], event['start'], event['last_hot'])[0]['cpu_seconds'], 0)

    def test_numeric_preceding_interval_clipping(self):
        rows = self.rows([90, 90, 90])
        for _, row in rows:
            row['sample_seconds'] = 10
            row['top_processes'][0]['cpu_percent_one_core'] = 200
        score = report.process_history(rows, self.t+dt.timedelta(seconds=5), self.t+dt.timedelta(seconds=15))[0]
        self.assertEqual(score['cpu_seconds'], 20)

    def test_irregular_sampling_missing_intervals(self):
        rows = [(self.t, sample(self.t, per_core=100)), (self.t+dt.timedelta(seconds=50), sample(self.t, per_core=100))]
        self.assertEqual(report.process_history(rows)[0]['cpu_seconds'], 20)
        rows[1][1]['sample_seconds'] = 50
        self.assertEqual(report.process_history(rows)[0]['cpu_seconds'], 60)

    def test_out_of_order_same_result(self):
        rows = self.rows([90, 90, 45])
        forward, backward = report.temperature_events(rows)[0], report.temperature_events(rows[::-1])[0]
        self.assertEqual(forward['peak'], backward['peak'])
        self.assertEqual(forward['duration'], backward['duration'])
        self.assertIn('unknown', backward['status'])

    def test_legacy_naive_and_mixed_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'2026-11-03.jsonl'
            path.write_text(json.dumps(sample(self.t.replace(tzinfo=None)))+'\n'+json.dumps(sample(self.t))+'\n')
            with mock.patch.object(report, 'LOG_DIR', Path(directory)), mock.patch.object(report, 'LEGACY', Path(directory)/'absent'):
                rows, errors = report.rows_for('2026-11-03')
            self.assertEqual(errors, 0)
            self.assertTrue(rows)
            self.assertTrue(all(t.tzinfo is not None for t, _ in rows))

    def test_truncated_gzip_keeps_prior_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'2026-11-03.jsonl.gz'
            path.write_bytes(gzip.compress((json.dumps(sample(self.t))+'\n').encode())[:-5])
            with mock.patch.object(report, 'LOG_DIR', Path(directory)), mock.patch.object(report, 'LEGACY', Path(directory)/'absent'):
                rows, errors = report.rows_for('2026-11-03')
            self.assertEqual(len(rows), 1)
            self.assertGreater(errors, 0)

    def test_malformed_shapes_safe(self):
        rows = [(self.t, {'temperatures_c': [], 'top_processes': [None, {'name': [], 'cpu_percent_one_core': float('nan')}]})]
        self.assertEqual(report.temperature_events(rows), [])
        self.assertIsNone(alert.candidate(self.t, rows))

    def test_critical_delivery_independent_of_diagnostics_and_state_write(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(alert, 'STATE', Path(directory)/'state'), mock.patch.object(alert, 'recent', return_value=self.rows([99])), mock.patch.object(alert.dt, 'datetime', wraps=dt.datetime) as date, mock.patch.object(alert.reporter, 'workload_evidence', side_effect=RuntimeError), mock.patch.object(alert.reporter, 'deliver') as deliver, mock.patch.dict(os.environ, {'PRW_REPORT_TO': 'root', 'PRW_JELLYFIN_LOG': 'invalid-relative'}):
                date.now.return_value = self.t
                alert.main()
                self.assertEqual(deliver.call_count, 1)

    def test_partial_disappearance_does_not_rearm(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(alert, 'STATE', Path(directory)/'state'), mock.patch.object(alert, 'recent') as recent, mock.patch.object(alert.dt, 'datetime', wraps=dt.datetime) as date, mock.patch.object(alert.reporter, 'deliver') as deliver, mock.patch.dict(os.environ, {'PRW_REPORT_TO': 'root'}):
                recent.return_value = self.rows([99]); date.now.return_value = self.t
                alert.main()
                for i in range(1, 9):
                    now = self.t+dt.timedelta(seconds=i*10)
                    row = sample(now); row['temperatures_c'] = {'cpu_other': 40}
                    recent.return_value = [(now, row)]; date.now.return_value = now
                    alert.main()
                self.assertIn('temp_critical', alert.load_state())
                self.assertNotIn('temp_recovery_since', alert.load_state())

    def test_atomic_state_replaces_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'other'; target.write_text('keep')
            state = Path(directory)/'state'; state.symlink_to(target)
            with mock.patch.object(alert, 'STATE', state):
                alert.save_state({'a': 'b'})
            self.assertEqual(target.read_text(), 'keep')
            self.assertFalse(state.is_symlink())

    def test_email_subject_injection_rejected(self):
        with self.assertRaises(ValueError):
            report.deliver('subject\nBcc: attacker', 'body', 'root', 'mutt')

    def test_full_sensor_recovery_rearms(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(alert, 'STATE', Path(directory)/'state'), mock.patch.object(alert, 'recent') as recent, mock.patch.object(alert.dt, 'datetime', wraps=dt.datetime) as date, mock.patch.object(alert.reporter, 'deliver') as deliver, mock.patch.dict(os.environ, {'PRW_REPORT_TO': 'root'}):
                recent.return_value = self.rows([99]); date.now.return_value = self.t
                alert.main()
                for i in range(1, 8):
                    now = self.t+dt.timedelta(seconds=i*10)
                    recent.return_value = [(self.t+dt.timedelta(seconds=j*10), sample(now)) for j in range(1, i+1)]; date.now.return_value = now
                    alert.main()
                self.assertNotIn('temp_critical', alert.load_state())
                now += dt.timedelta(seconds=10)
                recent.return_value = [(now, sample(now, temp=99))]; date.now.return_value = now
                alert.main()
                self.assertEqual(deliver.call_count, 2)

    def test_clock_reversal_does_not_confirm_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(alert, 'STATE', Path(directory)/'state'), mock.patch.object(alert, 'recent', return_value=self.rows([45])), mock.patch.object(alert.dt, 'datetime', wraps=dt.datetime) as date, mock.patch.dict(os.environ, {'PRW_REPORT_TO': 'root'}):
                alert.save_state(dict(temp_sensors=['cpu_k10temp'], temp_critical=self.t.isoformat(), last_check=(self.t+dt.timedelta(hours=1)).isoformat(), temp_recovery_since=(self.t-dt.timedelta(hours=1)).isoformat()))
                date.now.return_value = self.t
                alert.main()
                self.assertIn('temp_critical', alert.load_state())

    def test_critical_delivery_precedes_state_failure(self):
        with mock.patch.object(alert, 'recent', return_value=self.rows([99])), mock.patch.object(alert, 'load_state', return_value={}), mock.patch.object(alert, 'save_state', side_effect=OSError('disk full')), mock.patch.object(alert.dt, 'datetime', wraps=dt.datetime) as date, mock.patch.object(alert.reporter, 'deliver') as deliver, mock.patch.dict(os.environ, {'PRW_REPORT_TO': 'root'}):
            date.now.return_value = self.t
            with self.assertRaises(OSError):
                alert.main()
            deliver.assert_called_once()

    def test_optional_worker_deadline(self):
        def slow(*args):
            time.sleep(10)
        with mock.patch.object(report, '_collect_evidence', side_effect=slow):
            began = time.monotonic()
            notices, items = report.collect_evidence(self.t, self.t)
            self.assertLess(time.monotonic()-began, 6)
            self.assertIn('timed out', notices[0])
            self.assertEqual(items, [])

    def test_required_tail_rejects_fifo_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'fifo'; os.mkfifo(path)
            link = Path(directory)/'link'; link.symlink_to(path)
            for item in (path, link):
                self.assertEqual(alert.tail_rows(item), [])

    def test_oversize_input_reports_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'2026-11-03.jsonl'
            path.write_bytes(b'x'*(report.MAX_LINE+10))
            with mock.patch.object(report, 'LOG_DIR', Path(directory)), mock.patch.object(report, 'LEGACY', Path(directory)/'absent'):
                rows, errors = report.rows_for('2026-11-03')
            self.assertFalse(rows)
            self.assertEqual(errors, 1)

    def test_work_entirely_within_recovery_dip_is_excluded(self):
        event = report.temperature_events(self.rows([90, 45, 45, 90]))[0]
        task = dict(name='task', start=self.t+dt.timedelta(seconds=12), end=self.t+dt.timedelta(seconds=18), status='completed', confidence='confirmed')
        self.assertEqual(report.workload_evidence(event['start'], event['last_hot'], ([], [task]), event['hot_windows']), [])

    def test_single_sample_process_duplicates_do_not_double_count(self):
        row = sample(self.t, per_core=100)
        row['top_processes'] *= 2
        self.assertEqual(report.process_history([(self.t, row)])[0]['cpu_seconds'], 10)

    def test_large_sensor_set_cannot_hide_critical(self):
        row = sample(self.t)
        row['temperatures_c'] = {f'cpu_{i}': 40 for i in range(200)}
        row['temperatures_c']['cpu_last'] = 99
        self.assertEqual(alert.candidate(self.t, [(self.t, row)])['temperature'], 99)

    def test_process_estimate_excludes_cooling_dip(self):
        event = report.temperature_events(self.rows([90, 45, 90, 90]))[0]
        scores = report.process_history(event['rows'], event['start'], event['last_hot'], event['hot_windows'])
        self.assertAlmostEqual(scores[0]['cpu_seconds'], 0.2)

    def test_versions_match(self):
        self.assertEqual({sampler.__version__, report.__version__, alert.__version__}, {'0.2.0'})
        self.assertIn('version = "0.2.0"', (PROJECT/'pyproject.toml').read_text())
        self.assertIn('v0.2.0', (PROJECT/'README.md').read_text())

    def test_large_report_cost_bounded(self):
        rows = [(self.t+dt.timedelta(seconds=i*10), sample(self.t, temp=95)) for i in range(20000)]
        for i, (_, row) in enumerate(rows):
            if i%2:
                row['temperatures_c'] = {}
        began = time.monotonic()
        with mock.patch.object(report, 'collect_evidence', return_value=([], [])) as collect:
            _, body = report.report('2026-11-03', rows, 0)
        elapsed = time.monotonic()-began
        print(f'Performance: 20000 samples, {elapsed:.3f}s, {len(body.encode())} output bytes')
        self.assertLess(elapsed, 10)
        self.assertLessEqual(collect.call_count, 1)
        self.assertLess(len(body), 241000)


class InstallerPreservationTests(unittest.TestCase):
    def test_configuration_upgrade_function_preserves_bytes_and_mode(self):
        source = (PROJECT/'install.sh').read_text()
        function = source[source.index('preserve_configuration() {'):source.index('preserve_configuration "$PROJECT_DIR')]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config'; config.write_bytes(b'custom recipient and thresholds')
            config.chmod(0o640)
            subprocess.run(['bash', '-eu', '-c', function+'\npreserve_configuration "$1" "$2"', 'test', str(PROJECT/'config/proxmox-resource-watch.conf.example'), str(config)], check=True)
            self.assertEqual(config.read_bytes(), b'custom recipient and thresholds')
            self.assertEqual(config.stat().st_mode & 0o777, 0o640)

    def test_upgrade_permission_function_in_temporary_directory(self):
        source = (PROJECT/'install.sh').read_text()
        function = source[source.index('secure_log_directory() {'):source.index('secure_log_directory "$LOG_DST"')]
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)/'logs'; logs.mkdir(mode=0o755)
            record = logs/'history.jsonl'; record.write_bytes(b'preserve existing logs')
            config = Path(directory)/'config'; config.write_bytes(b'preserve config')
            # Exercise policy without requiring privileged ownership changes in CI.
            ownership = 'chown() { [[ "$1" == root:root && "$2" == -- && "$3" == "$PRW_TEST_LOG" ]]; }\n'
            command = ownership+function+'\nsecure_log_directory "$1"'
            command = 'PRW_TEST_LOG=$1\n'+command
            subprocess.run(['bash', '-eu', '-c', command, 'test', str(logs)], check=True)
            self.assertEqual(logs.stat().st_mode & 0o777, 0o700)
            self.assertEqual(record.read_bytes(), b'preserve existing logs')
            self.assertEqual(config.read_bytes(), b'preserve config')
            link = Path(directory)/'link'; link.symlink_to(logs)
            result = subprocess.run(['bash', '-eu', '-c', 'fail() { return 1; }\n'+command, 'test', str(link)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)


class FollowupReviewTests(unittest.TestCase):
    def setUp(self):
        self.t = dt.datetime(2026, 11, 3, tzinfo=TZ)

    def rows(self, values):
        return [(self.t+dt.timedelta(seconds=i*10), sample(self.t, temp=v)) for i,v in enumerate(values)]

    def test_historical_rollback_never_confirms_recovery(self):
        rows = self.rows([95]+[70]*7)
        rows.append((self.t+dt.timedelta(seconds=5), sample(self.t, temp=99)))
        events = report.temperature_events(rows)
        self.assertTrue(all(e['peak'] == 99 for e in events))
        self.assertTrue(all(e['recovery_time'] is None and 'unknown' in e['status'] for e in events))

    def test_clock_marker_survives_normalization(self):
        rows = self.rows([95]+[70]*7)
        rows[1][1]['clock_discontinuity'] = True
        normalized = report.normalize_rows(rows)
        self.assertIs(report.normalize_rows(normalized), normalized)
        self.assertTrue(all(e['recovery_time'] is None for e in report.temperature_events(normalized)))

    def test_context_merge_deduplicates_without_mutating_days(self):
        first=report.normalize_rows(self.rows([70]))
        second=report.normalize_rows(self.rows([99]))
        combined=report.combine_rows(first,second)
        self.assertEqual(len(combined),1)
        self.assertEqual(combined[0][1]['temperatures_c']['cpu_k10temp'],99)
        self.assertEqual(first[0][1]['temperatures_c']['cpu_k10temp'],70)
        self.assertFalse(combined.clock_uncertain)

    def test_file_rollback_is_detected_before_sorting(self):
        with tempfile.TemporaryDirectory() as directory:
            rows = self.rows([95]+[70]*7)+[(self.t+dt.timedelta(seconds=5), sample(self.t,temp=99))]
            for stamp, row in rows:
                row['time'] = stamp.isoformat()
            path = Path(directory)/'2026-11-03.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for _,r in rows))
            with mock.patch.object(report,'LOG_DIR',Path(directory)), mock.patch.object(report,'LEGACY',Path(directory)/'missing'):
                found, errors = report.rows_for('2026-11-03')
            self.assertTrue(found.clock_uncertain)
            self.assertEqual(errors, 0)

    def test_overlapping_sources_are_not_clock_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'2026-11-03.jsonl'
            legacy = Path(directory)/'legacy'
            lines = ''.join(json.dumps(dict(r,time=t.isoformat()))+'\n' for t,r in self.rows([95,70,70]))
            path.write_text(lines); legacy.write_text(lines)
            with mock.patch.object(report,'LOG_DIR',Path(directory)), mock.patch.object(report,'LEGACY',legacy):
                found,_ = report.rows_for('2026-11-03')
            self.assertFalse(found.clock_uncertain)
            self.assertEqual(len(found),3)

    def test_skipped_missing_or_hot_sample_prevents_rearm(self):
        for value in (None, 95):
            rows = self.rows([70]*8)
            rows[-2][1]['temperatures_c'] = {} if value is None else {'cpu_k10temp':value}
            state = dict(temp_sensors=['cpu_k10temp'],temp_critical=self.t.isoformat(),temp_recovery_since=self.t.isoformat())
            alert.update_recovery(rows[-1][0],alert.reporter.normalize_rows(rows),state)
            self.assertIn('temp_critical',state)

    def test_insufficient_retained_history_cannot_rearm(self):
        rows = self.rows([70])
        state = dict(temp_sensors=['cpu_k10temp'],temp_critical=self.t.isoformat(),temp_recovery_since=(self.t-dt.timedelta(minutes=5)).isoformat())
        alert.update_recovery(self.t,alert.reporter.normalize_rows(rows),state)
        self.assertIn('temp_critical',state)

    def test_failed_mail_is_retried_without_recording_success(self):
        with mock.patch.object(alert,'recent',return_value=self.rows([99])), mock.patch.object(alert,'load_state',return_value={}), mock.patch.object(alert,'save_state') as save, mock.patch.object(alert.dt,'datetime',wraps=dt.datetime) as date, mock.patch.object(alert.reporter,'deliver',side_effect=[subprocess.TimeoutExpired('mailer',90),None]) as deliver, mock.patch.dict(os.environ,{'PRW_REPORT_TO':'root'}):
            date.now.return_value=self.t
            with self.assertRaises(subprocess.TimeoutExpired):
                alert.main()
            save.assert_not_called()
            alert.main()
            self.assertEqual(deliver.call_count,2)
            self.assertIn('temp_critical',save.call_args.args[0])

    def test_health_changes_are_visible_and_do_not_spam(self):
        state={}
        with mock.patch('sys.stderr') as stderr:
            alert.diagnose_monitoring(self.t,alert.reporter.NormalizedRows(),state)
            calls=stderr.write.call_count
            alert.diagnose_monitoring(self.t,alert.reporter.NormalizedRows(),state)
            self.assertEqual(stderr.write.call_count,calls)
            alert.diagnose_monitoring(self.t,alert.reporter.normalize_rows(self.rows([45])),state)
            self.assertGreater(stderr.write.call_count,calls)
        self.assertEqual(state['monitoring_health'],'available')

    def test_health_output_failure_cannot_prevent_delivery(self):
        with mock.patch('sys.stderr.write',side_effect=OSError), mock.patch.object(alert,'recent',return_value=self.rows([99])), mock.patch.object(alert,'load_state',return_value={}), mock.patch.object(alert,'save_state'), mock.patch.object(alert.dt,'datetime',wraps=dt.datetime) as date, mock.patch.object(alert.reporter,'deliver') as deliver, mock.patch.dict(os.environ,{'PRW_REPORT_TO':'root'}):
            date.now.return_value=self.t
            alert.main()
            deliver.assert_called_once()

    def test_critical_delivery_precedes_health_logging(self):
        calls=[]
        with mock.patch.object(alert,'recent',return_value=self.rows([99])), mock.patch.object(alert,'load_state',return_value={}), mock.patch.object(alert,'save_state'), mock.patch.object(alert.dt,'datetime',wraps=dt.datetime) as date, mock.patch.object(alert.reporter,'deliver',side_effect=lambda *args:calls.append('mail')), mock.patch.object(alert,'diagnose_monitoring',side_effect=lambda *args:calls.append('health')), mock.patch.dict(os.environ,{'PRW_REPORT_TO':'root'}):
            date.now.return_value=self.t
            alert.main()
        self.assertEqual(calls,['mail','health'])

    def test_sanitized_workload_variants(self):
        fixture=json.loads((PROJECT/'tests/fixtures/workloads.json').read_text())
        backups=report.backup_intervals(fixture['proxmox_json'],TZ)
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0]['confidence'],'confirmed')
        self.assertEqual((backups[0]['end']-backups[0]['start']).total_seconds(),60)
        tasks=report.jellyfin_intervals(fixture['jellyfin'],TZ)
        self.assertEqual([(x['end']-x['start']).total_seconds() for x in tasks],[2586,130])

    def test_unrelated_structured_journal_cannot_claim_backup(self):
        fixture=json.loads((PROJECT/'tests/fixtures/workloads.json').read_text())
        lines=[]
        for raw in fixture['proxmox_json']:
            row=json.loads(raw);row['SYSLOG_IDENTIFIER']='unrelated'
            lines.append(json.dumps(row))
        self.assertEqual(report.backup_intervals(lines,TZ),[])

    def test_ownership_failure_aborts_before_permissions_change(self):
        source=(PROJECT/'install.sh').read_text()
        function=source[source.index('secure_log_directory() {'):source.index('secure_log_directory "$LOG_DST"')]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'logs';path.mkdir();path.chmod(0o755)
            result=subprocess.run(['bash','-eu','-c','chown() { return 1; }\n'+function+'\nsecure_log_directory "$1"','test',str(path)],capture_output=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(path.stat().st_mode & 0o777,0o755)


class JellyfinExportTests(unittest.TestCase):
    def test_pid_must_be_container_init_in_expected_cgroup(self):
        def files(path, **kwargs):
            if path.endswith('/status'):
                return io.StringIO('Name:\tsystemd\nNSpid:\t123\t1\n')
            return io.StringIO('0::/lxc/102/ns/init.scope\n')
        with mock.patch.object(Path,'open',return_value=io.StringIO('123\n')), mock.patch('builtins.open',side_effect=files):
            self.assertEqual(exporter.container_init_pid('102'),'123')
        with mock.patch.object(Path,'open',return_value=io.StringIO('123\n')), mock.patch('builtins.open',side_effect=lambda path,**kwargs: io.StringIO('NSpid:\t123\t1\n' if path.endswith('/status') else '0::/lxc/101/ns/init.scope\n')):
            with self.assertRaises(OSError):
                exporter.container_init_pid('102')

    def test_snapshot_reads_rotated_and_current_logs_and_is_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'root'; logs=root/'var/log/jellyfin'; logs.mkdir(parents=True)
            (logs/'jellyfin20300101.log.1').write_bytes(b'old completion\n')
            (logs/'jellyfin20300101.log').write_bytes(b'')
            (logs/'jellyfin20300102.log').write_bytes(b'current task\n')
            dest=Path(temp)/'snapshot'
            original_open=os.open; original_stat=Path.stat
            def fake_open(path, flags, *args, **kwargs):
                return original_open(root if path == '/proc/123/root' else path, flags, *args, **kwargs)
            def fake_stat(path, *args, **kwargs):
                result=original_stat(path, *args, **kwargs)
                return types.SimpleNamespace(st_mode=result.st_mode,st_uid=0) if path == dest.parent else result
            with mock.patch.object(exporter,'container_init_pid',return_value='123'), mock.patch.object(exporter.os,'open',side_effect=fake_open), mock.patch.object(Path,'stat',fake_stat):
                exporter.export('102',dest,dt.date(2030,1,2))
            self.assertEqual(dest.read_bytes(),b'old completion\ncurrent task\n')
            self.assertEqual(dest.stat().st_mode & 0o777,0o400)

    def test_rejects_symlink_source_and_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'root'; logs=root/'var/log/jellyfin'; logs.mkdir(parents=True)
            (logs/'jellyfin20300102.log').symlink_to('/etc/passwd')
            dest=Path(temp)/'snapshot'; dest.write_text('keep')
            original_open=os.open
            def fake_open(path, flags, *args, **kwargs):
                return original_open(root if path == '/proc/123/root' else path, flags, *args, **kwargs)
            with mock.patch.object(exporter,'container_init_pid',return_value='123'), mock.patch.object(exporter.os,'open',side_effect=fake_open):
                with self.assertRaises(OSError):
                    exporter.export('102',dest,dt.date(2030,1,2))
            self.assertEqual(dest.read_text(),'keep')

    def test_missing_container_does_not_replace_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            dest=Path(temp)/'snapshot';dest.write_text('keep')
            with mock.patch.object(exporter,'container_init_pid',side_effect=OSError('container unavailable')):
                with self.assertRaises(OSError):
                    exporter.export('102',dest)
            self.assertEqual(dest.read_text(),'keep')

    def test_actual_loggerless_completion_is_only_possible_correlation(self):
        line='[2030-01-02 00:43:22.402 -05:00] [INF] "Audio Normalization" Completed after 43 minute(s) and 6 seconds'
        items=report.jellyfin_intervals([line],TZ)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['confidence'],'possible')
        self.assertEqual((items[0]['end']-items[0]['start']).total_seconds(),2586)
        misleading='[2030-01-02 00:43:22.402 -05:00] [INF] explanation: Audio Normalization Completed after 43 minutes'
        self.assertEqual(report.jellyfin_intervals([misleading],TZ),[])

    def test_stale_snapshot_is_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'snapshot';path.write_text('old')
            os.utime(path,(1,1))
            t=dt.datetime(2030,1,2,tzinfo=TZ)
            with mock.patch.dict(os.environ,{'PRW_JELLYFIN_LOG':str(path),'PRW_JELLYFIN_MAX_AGE_SECONDS':'1800'}), mock.patch.object(report.subprocess,'run',side_effect=PermissionError):
                notices, intervals=report._collect_evidence(t,t)
            self.assertTrue(any('Jellyfin log unavailable' in n for n in notices))
            self.assertEqual(intervals,[])


if __name__ == '__main__':
    unittest.main()
