"""Bounded daily aggregates. Missing time never contributes zero-valued samples."""
import datetime as dt
import os


def bounds(day):
    date = dt.date.fromisoformat(day)
    # Convert each local midnight separately so DST days have 23/25 hours.
    start = dt.datetime.combine(date, dt.time()).astimezone().timestamp()
    end = dt.datetime.combine(date + dt.timedelta(days=1), dt.time()).astimezone().timestamp()
    return start, end


def aggregate(reporter, day, rows, errors=0):
    start, end = bounds(day)
    totals, peaks, seconds = {}, {}, {}
    hot_seconds, hot_count, cpu_count = {}, {}, 0
    runs = {}
    uncertain = bool(errors or rows.clock_uncertain or rows.data_uncertain)
    stamps = [stamp.timestamp() for stamp, _ in rows]
    for index, (stamp, row) in enumerate(rows):
        now = stamps[index]
        period = row.get('sample_seconds')
        if not reporter.finite(period) or not 0 < period <= reporter.INTERVAL * 2.5:
            period = reporter.INTERVAL
        next_stamp = stamps[index + 1] if index + 1 < len(stamps) else now
        gap = next_stamp - now
        weight = min(period, gap, end-now) if 0 < gap <= reporter.INTERVAL * 2.5 and not uncertain else 0
        if now < start or now >= end:
            continue
        values = {'cpu': row.get('host_cpu_busy_percent')}
        values.update({'sensor:' + key: value for key, value in row['temperatures_c'].items() if reporter.is_cpu_sensor(key)})
        for key in set(runs) - set(values):
            runs[key] = 0
        for key, value in values.items():
            following = rows[index+1][1] if index+1 < len(rows) else {}
            other = following.get('host_cpu_busy_percent') if key == 'cpu' else following.get('temperatures_c', {}).get(key[7:])
            next_valid = reporter.finite(other) and (0 <= other <= 100 if key == 'cpu' else -50 <= other <= 150)
            support = weight if next_valid else 0
            valid = reporter.finite(value) and (0 <= value <= 100 if key == 'cpu' else -50 <= value <= 150)
            if not valid:
                runs[key] = 0
                continue
            peaks[key] = max(peaks.get(key, value), value)
            totals[key] = totals.get(key, 0) + ((value+other)/2 if next_valid else value) * support
            seconds[key] = seconds.get(key, 0) + support
            threshold = reporter.CPU_THRESHOLD if key == 'cpu' else reporter.TEMP_THRESHOLD
            previous = runs.get(key, 0)
            runs[key] = previous + support if value >= threshold and next_valid and other >= threshold and support else 0
            if previous < reporter.SUSTAINED_SECONDS <= runs[key]:
                if key == 'cpu':
                    cpu_count += 1
                else:
                    hot_count[key] = hot_count.get(key, 0) + 1
            if key != 'cpu' and runs[key] >= reporter.SUSTAINED_SECONDS:
                # Include the run's initial confirmed support once, then extend.
                hot_seconds[key] = hot_seconds.get(key, 0) + (runs[key] if previous < reporter.SUSTAINED_SECONDS else support)
    metrics = {}
    for key in peaks:
        coverage = seconds.get(key, 0) / (end-start)
        metrics[key] = dict(peak=peaks[key], coverage=coverage,
                            average=totals[key]/seconds[key] if coverage >= .8 and not uncertain else None,
                            hot_seconds=None if uncertain else hot_seconds.get(key, 0), events=None if uncertain else (cpu_count if key == 'cpu' else hot_count.get(key, 0)))
    return dict(day=day, metrics=metrics, uncertain=uncertain)


def comparison(reporter, day, rows, errors=0):
    current = aggregate(reporter, day, rows, errors)
    recent = []
    date = dt.date.fromisoformat(day)
    retention = int(os.environ.get('PRW_RETENTION_DAYS', '30'))
    # At most 14 daily bounded reads; no large combined history retained.
    for offset in range(1, min(14, retention-1) + 1):
        prior = (date-dt.timedelta(days=offset)).isoformat()
        history, failures = reporter.rows_for(prior, include_legacy=False)
        recent.append(aggregate(reporter, prior, history, failures))
    out = ['HISTORICAL COMPARISON', 'Averages need >=80% daily coverage; baselines need >=4 comparable complete days per seven-day period.']
    for key, value in sorted(current['metrics'].items()):
        label = 'Host CPU (%)' if key == 'cpu' else reporter.discord.clean(key[7:], 120).replace('\n', ' ') + ' (C)'
        average = f"{value['average']:.1f}" if value['average'] is not None else 'unavailable (insufficient coverage or uncertain clock)'
        line = f"{label}: average {average}; peak {value['peak']:.1f}; coverage {value['coverage']:.1%}; sustained events {value['events']}"
        if key != 'cpu':
            line += '; confirmed elevated time ' + (f"{value['hot_seconds']:.0f}s (per sensor)" if value['hot_seconds'] is not None else 'unknown (clock/input uncertainty)')
        def baseline(days):
            values = [r['metrics'][key]['average'] for r in days if key in r['metrics'] and r['metrics'][key]['average'] is not None]
            return sum(values)/len(values) if len(values) >= 4 else None
        baseline7 = baseline(recent[:7])
        current7, preceding7 = baseline([current, *recent[:6]]), baseline(recent[6:13])
        if baseline7 is not None:
            line += f'; previous seven-day baseline {baseline7:.1f}'
            if value['average'] is not None:
                line += f"; difference {value['average']-baseline7:+.1f}"
        else:
            line += '; insufficient historical data for baseline'
        if current7 is not None and preceding7 is not None:
            line += f'; current seven-day period versus preceding period {current7-preceding7:+.1f}'
        supported_events = [r['metrics'][key]['events'] for r in recent[:7] if key in r['metrics'] and r['metrics'][key]['average'] is not None and r['metrics'][key]['events'] is not None]
        if len(supported_events) >= 4:
            line += f'; previous seven-day sustained events/day {sum(supported_events)/len(supported_events):.1f} ({len(supported_events)} supported days)'
        out.append(line)
    if not current['metrics']:
        out.append('Insufficient historical data; no baseline established.')
    out.append('Missing intervals are unknown. Sensor identities are compared separately; renamed sensors start a new baseline.')
    return out
