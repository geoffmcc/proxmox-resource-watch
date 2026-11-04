# Guest attribution, history and daily delivery

This development candidate explains resource events without managing infrastructure.
It has no new Python dependencies. Python 3.10+ remains required. Proxmox VE 8/9
with `/usr/bin/pvesh` and the cluster resources GET schema is the intended target;
no live version compatibility or systemd runtime enforcement has been verified for
this candidate. Installation and production testing require separate authorization.

## Guest observations

Enable `PRW_GUEST_ENABLED=true` to collect local running QEMU/LXC snapshots in a
separate oneshot. The timer wakes once a minute; `PRW_GUEST_INTERVAL_SECONDS`
(60–3600, default 60) sets the minimum query interval. Large snapshots automatically
lengthen that interval to stay within a 16 MiB daily budget. This sacrifices temporal
resolution rather than increasing memory, retention or polling. At most 512 cluster
entries and 2 MiB response bytes are accepted; larger clusters fail safely. Local
node matching uses the hostname before its first dot. Guest failure cannot suppress
primary alerts. Cleanup runs even when collection is disabled or queries fail.

The only command is a fixed argument vector:

```
/usr/bin/pvesh get /cluster/resources --type vm --output-format json
```

It reads Proxmox's existing status cache without introducing API credentials. The
service runs under the existing local root identity: the GET-only guarantee is in
the fixed implementation, not an OS restriction that removes root's authority.
No guest commands, namespace entry, lifecycle operations or configuration writes
are used. Host process inspection was considered: it cannot reliably identify all
LXC activity or express comparable assigned-capacity percentages. No remote API
client is included. A future credential-based implementation would need VM.Audit
on monitored `/vms/<vmid>` paths, never an administrator token.

The [upstream resources implementation](https://github.com/proxmox/pve-manager/blob/master/PVE/API2/Cluster.pm)
exposes fractional CPU utilization and gates guest visibility with VM.Audit.
CPU is the provided fraction times 100, expressed as percent of assigned guest CPU
capacity: 50% with four vCPUs represents approximately two busy cores, not 50% of
the host. Proxmox's cached averaging window and guest scheduling affect precision.
QEMU overhead and container accounting differ. Memory is reported usage divided by
maximum configured bytes; ballooning/cache semantics mean this is not application
working-set size. Missing/invalid values remain unavailable. No guest counter
deltas are computed, so counter resets cannot produce negative CPU or large spikes.

Snapshots include time, VMID, name, type, running state, uptime, assigned vCPUs,
CPU and memory utilization. Names are scrubbed of URLs, controls and mention/markup
characters. Daily summaries split observation segments on uptime/name changes,
clock reversal and gaps. Identical metadata can conceal VMID reuse; no immutable
guest identity is claimed. Segments are evidence groupings, not guest lifetimes.

Event attribution uses only snapshot timestamps inside host CPU/temperature event
windows, with a separate event-window peak and timestamp. It never carries activity
across a missing interval. The >=50% assigned-capacity label denotes an observed
high-activity snapshot, not a universal workload threshold. Reports show the top ten
segments, prioritizing observed overlap and then daily peak; guests outside that selection may also have contributed.
Neither observed activity nor temporal overlap establishes heat generation or cause.

Illustrative output (values are examples, not promises):

```
QEMU 200 batch-worker (observation segment 1): daily peak 76.0% assigned CPU capacity; 900 observations.
  Confirmed temporal overlap: 3 snapshots; event-window peak 72.0% at 2026-11-03T13:22:00-05:00; elevated activity observed (>=50% capacity).
LXC 102 media-worker (observation segment 2): daily peak 35.0% assigned CPU capacity; 120 observations.
  No guest snapshot inside host event windows; contribution unknown.
```

Daily guest JSONL files live in the private `guests/` directory under PRW_LOG_DIR.
Retention follows PRW_RETENTION_DAYS. Nothing increases host retention automatically.
Malformed files, input caps, cache staleness and sparse observations limit confidence.

## Historical analysis

The daily report includes average and peak host CPU, separate CPU-sensor averages
and peaks, sustained load/temperature counts and confirmed elevated-temperature
seconds. Integration requires two adjacent valid endpoints; high time requires
both endpoints high. No trailing interval after the last reading is assumed.
Irregular intervals are capped by recorded cadence and the existing gap threshold.
Duplicate timestamps are normalized once; conflicting readings mark uncertainty.
Clock rollback or parse/input-limit errors suppress averages and confirmed durations.
Missing sensors and missing values never become zeros. Readings outside -50..150 C
or CPU outside 0..100% are excluded from historical statistics.

Averages use trapezoidal time weighting, with at least 80% valid daily support.
Host-local midnight boundaries are converted separately to handle 23/25-hour DST
days. Baselines require four supported complete calendar dates in each seven-day
period and compare identical sensor labels only. The report compares its date with
the previous seven days, then the seven-day period ending on that date with the
preceding period. Event baselines state how many supported dates contributed.
Insufficient coverage produces an explanation rather than a numeric baseline.
A replacement sensor with an unchanged label cannot be distinguished automatically.
Reboots without explicit discontinuity evidence cannot be identified reliably.

```
Host CPU (%): average 12.5; peak 86.0; coverage 98.2%; sustained events 3; previous seven-day baseline 10.1; difference +2.4
cpu:Tctl (C): average 54.0; peak 87.0; coverage 98.2%; sustained events 3; confirmed elevated time 120s (per sensor); previous seven-day baseline 51.0; difference +3.0
```

One normalized day is aggregated at a time, at most fourteen prior daily files;
the existing per-file decompression/row/line limits remain. Legacy monolithic logs
remain readable for event reporting but are excluded from multi-day baselines to
avoid reparsing the same large file fourteen times. Events spanning midnight keep
the existing previous/following-day report context. Discord includes compact current
coverage/average and guest overlap context; full comparisons remain in email/text.

## Delivery and catch-up

The daily timer remains at 07:00 host local with Persistent=false. It targets a new
scheduled oneshot using `--tracked`. A separate five-minute maintenance timer uses
`--scheduled` for pending retries and optional catch-up; no daemon is added.
`PRW_DAILY_CATCHUP_ENABLED=false` defaults to preserving the no-catch-up policy.
When true, maintenance creates only the most recent due report, never a backlog.
Before 07:00 that is the report due at yesterday's 07:00, not a premature report
for the day that has just ended. Delayed reports identify the data date and delay.

`PRW_DAILY_MAX_AGE_HOURS=24` (1–48) bounds age from the original 07:00 deadline.
`PRW_DAILY_RETRY_LIMIT=3` (1–10) bounds total attempts per destination.
`PRW_DAILY_RETRY_SECONDS=900` (300–86400) gives exponential minimum backoff,
capped at 24 hours except a larger Discord Retry-After is always respected.
Both numeric and HTTP-date Retry-After values are supported on daily sends.
Maintenance considers up to three eligible dates, preserving pending jobs through
midnight and the next 07:00. Wall-clock rollback cannot create a future-date report;
a future retry deadline waits rather than sending early. DST uses local civil dates.

A private `daily/<date>.json` ledger records email and Discord independently.
Atomic replace, file/directory fsync and an exclusive flock serialize updates and
sends. An in-flight marker is durable before any transport call. Success means mail
transport/Discord acceptance, not verified recipient receipt. Successful email is
never retried because Discord failed. Explicit Discord HTTP rejection can retry;
network failures, malformed success responses, mail exceptions and crash-left
in-flight markers are ambiguous and stop automatically. There is no exactly-once
guarantee across remote acceptance and local persistence. Corrupt state fails closed.
Pending state can therefore require operator investigation rather than silent retry.
Destination changes do not reset success: dates are keyed by channel, not credentials.

The installer invokes `--initial`, which uses the same ledger even before 07:00.
Explicit initial verification is allowed outside the automatic maximum age. It fails verification unless email success is durably recorded. A date already
accepted is skipped, so reinstalling is not a fresh transport test. The existing
manual `--send --date YYYY-MM-DD` remains an explicit untracked send; use it only
when a repeat is intended. Printing reports does not alter delivery state. Do not
delete ledgers to fix errors without considering possible duplicate delivery.
Transport diagnostics never include webhook credentials or response bodies.

## Validation and security

Run without network access or service changes:

```
python3 src/proxmox-resource-check-config.py --config config/proxmox-resource-watch.conf.example
# Installed:
/usr/local/sbin/proxmox-resource-check-config.py --config /etc/default/proxmox-resource-watch
```

Exit 0 means valid (possibly warnings), 1 means invalid settings, 2 means unreadable
file or unsupported syntax. Unknown settings and duplicates warn without echoing
unknown names/values; known errors identify keys only. No settings are rewritten.
Supports single-line EnvironmentFile assignments and quoted values, without shell
expansion. Multiline EnvironmentFile forms need manual review; no options are
currently deprecated. Numeric ranges, booleans, thresholds, recipients, enabled
Discord prerequisites and dedicated non-symlink paths are checked. This validates
configuration shape, not mail deliverability or installed API compatibility.

Immediate alert code, cooldown handling and its unit are unchanged. New oneshots
retain ProtectSystem=strict, NoNewPrivileges, PrivateTmp, private umask, swap denial,
OOM policy and deadlines. Only the existing private log directory and required mail
spool are writable. New services use TasksMax=32; guests have 64 MiB/15 seconds,
reports 512 MiB/300 seconds. Guest subprocess reads are capped at 2 MiB and eight
seconds, kill its process group on timeout, and suppress stderr. Existing report
subprocess/evidence limits remain. The private directory owner must match the service
user and no group/other access is allowed; state reads reject symlinks and nonregular
files. Administrator-owned parent directories remain a trust boundary.

## Upgrade, troubleshooting and rollback

Do not deploy this candidate on production until independent re-review, CI Python
matrix and disposable-systemd acceptance are complete. Back up installed scripts,
config, all units/drop-ins, timer enablement, primary alert/cooldown states and log
ownership first. Review the preserved config offline and add optional settings
explicitly; installer preserves existing config. New timers wake even with features
disabled; guest collection itself remains disabled. No live notification is
necessary for offline verification. The installer still performs its documented
notification verification when an operator explicitly deploys.

Check service results without printing config or credentials. Guest failure means
missing evidence, not a monitoring outage. For daily issues, inspect private ledger
status/attempt counts without sharing sensitive configuration. `inflight` or
`ambiguous` means delivery may have happened; verify recipient acceptance before
choosing a manual repeat. A corrupt ledger must be retained for investigation and
repaired deliberately. Never reset primary alert state for a daily retry problem.

Rollback in an authorized deployment window: stop report, daily-maintenance and
guest timers; let their oneshots finish; restore backed-up scripts/config/units and
the original report timer target; remove only newly introduced units/modules; run
daemon-reload and restore recorded timer states. Primary alert and follow-up units
need no restart unless the restored installation requires it. Preserve host history,
guest history and delivery ledgers. An old version ignores daily ledgers and can
resend a report; record that risk. Do not touch guests, backups, storage or networking.
