# Independent pre-PR review: finding resolution

Reviewed starting point: `9db3b01ec00cd43035e4c564923ee5a69b9b93c7` on
`feature/guest-attribution-historical-trends`. The original Desktop/report.md is
preserved as the historical review. This document records the subsequent fixes.
No production deployment, guest operations or live notifications were performed.

## Resolution of all eight findings

| Finding | Correction | Evidence |
|---|---|---|
| High: optional modules could block critical alerts at import | Import guest/history/daily code only inside its own execution paths | Synthetic critical alert reaches mocked email with each optional module unavailable or raising during import; eight fault combinations |
| Medium: 07:00 lock contention lost delivery intent | Persist private atomic date request before lock acquisition; maintenance resumes existing intent with catch-up disabled; expire stale requests | Held lock, invoked scheduled run, released lock, then maintenance delivered once and recorded success |
| Medium: Retry-After started before expensive work | Set retry deadline from transport completion; refresh time per candidate; recheck report age before sends | Slow mail plus numeric and HTTP-date rate limits; later candidate clock progression |
| Medium: validator accepted invalid runtime settings | Reject zero immediate CPU alert threshold and empty required log directory | Boundary tests retain positive fractional CPU threshold and 100% compatibility |
| Medium: incorrect CPU weighting | Integrate each measured preceding CPU interval separately from temperature interpolation; clip day boundaries/overlap; exclude absent cadence | Analytic 12-second 100% / 10-second 0% fixture now yields 54.545% with correct coverage; midnight, overlap and missing-cadence tests |
| Medium: uncertain guest overlap called confirmed | Propagate clock and duplicate-data confidence through daily reports, follow-up filtering and actual bounded alert reader | Rolled-back/conflicting observations produce possible correlation in both report paths |
| Low: multiline guest names could spoof report lines | Dedicated single-line identifier sanitizer at collection and rendering | Newline, carriage return, Unicode separator, URL and mention fixture |
| Low: benchmark adverse conditions outside measurement | Put adverse records inside the measured window, assert their presence and include adjacent-day report context | Benchmark asserts rollback, malformed records, missing values, sampling gaps and guest restart evidence |

The fresh independent reviewer additionally found two issues during correction:
follow-up filtering still lost the original conflict flag, and whole-second sampler
timestamps split constant 10.1-second CPU observations into multiple sustained
runs. Both were corrected and independently reproduced again. The CPU continuity
check now tolerates whole-second timestamp quantization without expanding the
integrated sample support. The bounded reader preserves confidence metadata;
primary threshold, escalation, transport and cooldown behavior is unchanged.

The independent reviewer passed all 13 new failure-path regressions and reported
all eight original findings resolved, with no additional actionable defects in
the final delta. This is a separate reviewer context, not a self-review claim.

## Local verification

- Python 3.14.8: full offline discovery **185 tests passed** in 5.707 seconds.
- Direct correction suite: **13 tests passed**; existing tests were retained.
- Python compilation, Bash syntax, ShellCheck, configuration-example validation
  and `git diff --check` passed.
- `systemd-analyze verify` passed with the existing sandbox socket-option warnings;
  no units were started or changed on the host.
- CI now also runs the correction suite directly. Python 3.10/3.12 CI execution is
  still required; local results do not establish those matrix results.

## Benchmark interpretation

The updated benchmark measures report analysis with previous/following-day context
and 30 days retained on disk. It includes CPU baselines over the bounded comparison
window and guest attribution. Dataset creation, actual pvesh startup/API work,
notification transport, concurrent services, storage contention and cgroup
resource enforcement are excluded. It is not a complete scheduled-service test.

The synthetic dataset contains 258,900 host samples, four CPU sensors and ten
guests. Approximately 83.4 MB of logical reads and 66.1 MB of guest history are
measured. Instrumentation records only aggregate metadata; it does not retain
historical row arguments and artificially inflate peak memory. No memory or time
limits were raised. Final measured timings are recorded below.

## Deployment and rollback

No new settings, services or timers were introduced by these corrections. The
private daily directory can additionally contain `<date>.requested` files while a
scheduled delivery waits for the worker lock. They contain the date only, use the
existing atomic/private state mechanism and expire at the configured maximum age.
The successful destination ledger remains authoritative for duplicate prevention.

Before deployment, finish the existing authorized disposable-systemd, supported
Proxmox version, installation/rollback and transport-receipt acceptance gates.
Existing ambiguity tradeoffs remain: an in-flight crash or uncertain transport
outcome can require operator investigation rather than automatic retry.

Rollback follows docs/GUEST_HISTORY_DELIVERY.md. Preserve daily ledgers and request
markers alongside monitoring history. Older feature code ignores request markers,
so rolling back may lose a pending scheduled request; do not remove successful
ledger entries to compensate. The original pre-feature version does not honor the
ledger and may send a repeated report. Record that risk when restoring backups.

Final measurements (same local machine, no production activity):

| Measurement | Result |
|---|---|
| Updated 30-day report analysis | 5.013 seconds wall; 5.012 CPU seconds |
| Peak RSS | 138.5 MiB |
| Baseline report with adjacent-day context | 0.492 seconds |
| Feature report with identical context, before history comparison | 0.644 seconds |
| Logical read characters | 83,389,015 |
| Synthetic guest-history bytes | 66,108,000 |
| 100-guest normalization (API excluded) | 0.294 ms |
| Existing three-day benchmark | 0.937 seconds; 129.0 MiB RSS |

The previous 115.4 MiB measurement omitted adjacent-day context and did not place
rollback inside its measured window. The updated result is a broader analysis
measurement, not evidence of an increased configured resource budget.
