"""Offline report analysis with adjacent-day context and 30 retained days.

Excludes dataset creation, pvesh startup/API, transports and concurrent systemd
services. The measured window must exercise rollback, malformed/missing samples
and guest restarts. This is not a full scheduled-service benchmark.
"""
import datetime as dt
import json
import os
from pathlib import Path
import resource
import types
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_guest_history_daily import report
import prw_history
import prw_guests


def io_characters():
    try:
        return int(next(line.split(':')[1] for line in Path('/proc/self/io').read_text().splitlines() if line.startswith('rchar:')))
    except (OSError, StopIteration):
        return 0


def main():
    resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_CPU, (90,)*2)
    first = dt.date(2026, 9, 1)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root/'guests').mkdir()
        written = 0
        for index in range(30):
            day = first+dt.timedelta(days=index)
            start = dt.datetime.combine(day, dt.time()).astimezone()
            with (root/f'{day}.jsonl').open('w') as stream, (root/'guests'/f'{day}.jsonl').open('w') as guest_stream:
                for sample in range(8640):
                    if sample % 901 == 100:  # genuine missing observations
                        continue
                    hot = sample % 600 < 12
                    stamp = start+dt.timedelta(seconds=sample*10)
                    if index == 23 and sample == 300:
                        stamp -= dt.timedelta(seconds=60)
                    row = dict(time=stamp.isoformat(),sample_seconds=10,clock_discontinuity=index==23 and sample==300,
                               host_cpu_busy_percent=80 if hot else 12, temperatures_c={f'cpu:sensor{i}':90 if hot else 50+i for i in range(4)},
                               top_processes=[dict(name=f'worker{i}',pid=i,start_ticks=100+i,cpu_percent_one_core=25+i) for i in range(5)])
                    if index == 26 and sample == 500:
                        row['host_cpu_busy_percent'] = None
                        row['temperatures_c'] = {}
                    if index == 27 and sample == 600:
                        stream.write('{malformed\n')
                    encoded = json.dumps(row,separators=(',',':'))+'\n'
                    stream.write(encoded)
                    written += len(encoded)
                    if sample % 6 == 0:
                        guests = [dict(vmid=100+i,type='qemu' if i%2 else 'lxc',name=f'worker{i}',uptime=sample*10 if sample<4300 else (sample-4300)*10,
                                       cpu_percent_capacity=70 if hot else 10,vcpus=2,memory_percent=40,state='running') for i in range(10)]
                        guest_stream.write(json.dumps(dict(time=stamp.isoformat(),guests=guests))+'\n')
        baseline_source = os.environ.get('PRW_BENCHMARK_BASELINE')
        baseline_seconds = None
        if baseline_source:
            baseline = types.ModuleType('baseline_report')
            baseline.__file__ = str(Path(__file__).resolve().parents[1]/'src/proxmox-resource-report.py')
            exec(compile(Path(baseline_source).read_text(), baseline.__file__, 'exec'), baseline.__dict__)
            began_baseline = time.monotonic()
            with mock.patch.object(baseline,'LOG_DIR',root), mock.patch.object(baseline,'LEGACY',root/'absent'), mock.patch.object(baseline,'collect_evidence',return_value=([],[])):
                baseline_rows, baseline_errors = baseline.rows_for(str(first+dt.timedelta(days=29)))
                baseline_previous, prior_errors = baseline.rows_for(str(first+dt.timedelta(days=28)))
                baseline_following, next_errors = baseline.rows_for(str(first+dt.timedelta(days=30)))
                baseline.report(str(first+dt.timedelta(days=29)),baseline_rows,baseline_errors+prior_errors+next_errors,
                                baseline.combine_rows(baseline_previous,baseline_rows,baseline_following),summary={})
            baseline_seconds = round(time.monotonic()-began_baseline,3)
            del baseline_rows, baseline_previous, baseline_following
        began = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_SELF)
        read_start = io_characters()
        observed_aggregates = {}
        real_aggregate = prw_history.aggregate
        def checked_aggregate(reporter, date, rows, errors=0):
            result = real_aggregate(reporter, date, rows, errors)
            observed_aggregates[date] = dict(clock=rows.clock_uncertain, errors=errors,
                missing=any(row.get('host_cpu_busy_percent') is None for _,row in rows))
            return result
        with mock.patch.object(prw_history,'aggregate',new=checked_aggregate), mock.patch.object(report,'LOG_DIR',root), mock.patch.object(report,'LEGACY',root/'absent'), mock.patch.object(report,'collect_evidence',return_value=([],[])):
            day = str(first+dt.timedelta(days=29))
            rows, errors = report.rows_for(day)
            previous, prior_errors = report.rows_for(str(first+dt.timedelta(days=28)))
            following, next_errors = report.rows_for(str(first+dt.timedelta(days=30)))
            context = report.combine_rows(previous, rows, following)
            _, body = report.report(day,rows,errors+prior_errors+next_errors,context,summary={})
            current_report_seconds = round(time.monotonic()-began,3)
            body += '\n'.join(prw_history.comparison(report,day,rows,errors))
            self_check = prw_history.aggregate(report,day,rows)
            assert self_check['metrics']['cpu']['average'] is not None
            assert 'previous seven-day baseline' in body
            assert observed_aggregates[str(first+dt.timedelta(days=23))]['clock']
            assert observed_aggregates[str(first+dt.timedelta(days=27))]['errors'] > 0
            assert observed_aggregates[str(first+dt.timedelta(days=26))]['missing']
            snapshots = prw_guests.observations(report, day)
            uptimes = [guest['uptime'] for _, values in snapshots for guest in values if guest['vmid']==100]
            assert any(b<a for a,b in zip(uptimes,uptimes[1:])), 'Measured guest window must include restart'
            assert any((b[0]-a[0]).total_seconds()>report.INTERVAL*1.5 for a,b in zip(rows,rows[1:])), 'Measured day must have a sampling gap'
            collection = time.monotonic()
            for _ in range(100):
                prw_guests.normalize([dict(node='test',status='running',vmid=100+i,type='qemu',cpu=.5,maxcpu=2,mem=100,maxmem=200,uptime=100,name='test') for i in range(100)],'test')
            guest_ms = (time.monotonic()-collection)*10
        actual_read_characters = io_characters()-read_start
        after = resource.getrusage(resource.RUSAGE_SELF)
        result = dict(days_on_disk=30,host_samples=30*(8640-10),guest_count=10,sensors=4,baseline_report_with_context_seconds=baseline_seconds,current_report_with_context_seconds=current_report_seconds,
                      measurement="report analysis only; adjacent-day context included; API/transport/concurrency excluded",
                      adverse_cases_verified=["clock rollback", "malformed record", "missing values", "sampling gaps", "guest restart"],
                      latency_seconds=round(time.monotonic()-began,3),cpu_seconds=round(after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,3),
                      peak_rss_mib=round(after.ru_maxrss/1024,1),host_history_bytes=written,
                      read_bytes_logical_bound=15*report.MAX_BYTES*2,read_characters=actual_read_characters,guest_history_bytes=sum(path.stat().st_size for path in (root/'guests').glob('*.jsonl')),guest_normalize_100_ms=round(guest_ms,3),output_bytes=len(body.encode()))
        assert result['latency_seconds'] < 30
        assert result['peak_rss_mib'] < 400
        print(json.dumps(result))


if __name__ == '__main__':
    main()
