"""Offline regression checks for the sampler, digest and alert logic."""
import datetime as dt
import gzip
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]


def load(script, name):
    spec = importlib.util.spec_from_file_location(name, PROJECT / "src" / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    def test_unique_sensor_identity_code_path(self):
        # Labels are explicit, not overwritten by common driver names.
        content = (PROJECT / "src" / "proxmox-resource-watch.py").read_text()
        self.assertIn('f"{prefix}:{label}"', content)

    def test_cpu_ticks_proc_stat(self):
        with mock.patch.object(sampler, "read", return_value="cpu 1 2 3 4 5 6 7 8 9 10"):
            self.assertEqual(sampler.cpu_ticks(), (55, 9))

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


if __name__ == "__main__":
    unittest.main()
