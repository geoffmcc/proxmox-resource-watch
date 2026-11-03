"""Offline report benchmark: synthetic files only, no journal or mail access.

Run with python3 tests/benchmark_report.py. The process has a 512 MiB address-space
limit as a smoke check; this does not verify systemd/cgroup enforcement.
"""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import resource
import tempfile
import time
from unittest import mock


def main():
    budget = 512 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (budget, budget))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    spec = importlib.util.spec_from_file_location(
        "benchmark_report", Path(__file__).resolve().parents[1] / "src/proxmox-resource-report.py")
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    first = dt.datetime(2026, 11, 1, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    with tempfile.TemporaryDirectory(prefix="prw-report-benchmark-") as directory:
        log_dir = Path(directory)
        for day in range(3):
            start = first + dt.timedelta(days=day)
            with (log_dir / f"{start.date()}.jsonl").open("w") as stream:
                for index in range(8640):
                    stamp = start + dt.timedelta(seconds=index * 10)
                    hot = index % 600 < 6
                    row = dict(time=stamp.isoformat(), sample_seconds=10,
                               host_cpu_busy_percent=20 if hot else 3,
                               temperatures_c={"k10temp:cpu:Tctl": 95 if hot else 50,
                                               **{f"device:{i}:temp1": 40 + i for i in range(8)}},
                               fans_rpm={}, top_processes=[
                                   dict(pid=i + 1, start_ticks=i + 100, name=f"worker{i}",
                                        cpu_percent_one_core=20 + i,
                                        command=f"worker{i} --synthetic-input") for i in range(5)])
                    stream.write(json.dumps(row, separators=(",", ":")) + "\n")
        began = time.monotonic()
        with mock.patch.object(report, "LOG_DIR", log_dir), \
                mock.patch.object(report, "LEGACY", log_dir / "absent-legacy"), \
                mock.patch.object(report, "collect_evidence", return_value=([], [])):
            histories = [report.rows_for(str((first + dt.timedelta(days=i)).date())) for i in range(3)]
            assert not any(errors for _, errors in histories), "Synthetic input was truncated or invalid"
            context = report.combine_rows(*(rows for rows, _ in histories))
            summary = {}
            _, body = report.report("2026-11-02", histories[1][0], 0, context, summary)
            report.summary_message("2026-11-02", summary)
        assert len(context) == 25920
        assert summary["samples"] == 8640
        assert summary["thermal"] == {"warning": 15, "critical": 15}
        assert "CPU max 20.0%, temp max 95.0°C" in body
        print(json.dumps(dict(samples=len(context), sensors_per_sample=9, processes_per_sample=5,
                              seconds=round(time.monotonic() - began, 3),
                              peak_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                              address_space_limit_mib=512, output_bytes=len(body.encode()))))


if __name__ == "__main__":
    main()
