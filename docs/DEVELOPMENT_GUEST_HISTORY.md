# Guest/history development record

Baseline: c480406508fc6f7f3f0ea126029e3e3a9222deed, fetched public remote main
2026-11-04; remote HEAD main; clean checkout; no local tags. CI Python 3.10/3.12.
Baseline local Python 3.14.8: 141 regressions pass (5.433s). Existing 3-day
benchmark: 25,920 samples, 0.859s, 129.2 MiB peak RSS, 512 MiB address ceiling.
Release acceptance still requires disposable systemd enforcement and authorized
installation/transport verification; none is authorized in this session.

Design before implementation: add optional separate bounded guest oneshot, using
only local pvesh GET /cluster/resources --type vm. No new credentials; root's
existing local API privilege is a boundary, not an API token recommendation.
Filter local node. Retain sparse observations in separate daily files under the
existing retention limit. Never derive counter deltas or add guest CPU to host CPU.
Attribution uses observation timestamps only, never interpolates guest activity.
Uptime/name changes prevent assuming VMID identity continuity.

History: stream one normalized day at a time, reuse report day, per-sensor weighted
aggregates, cap cadence support, refuse averages below 80% coverage or with clock
uncertainty. Compare only complete dates and matching sensor identities. No new DB.

Delivery: separate atomic, locked ledger per report date and destination; persist
in-flight marker before I/O. A crash/ambiguous failure stops automatic retry for
that destination; exactly-once is impossible. Explicit HTTP rejection can retry
with bounded backoff. Email failure never re-sends successful Discord or vice versa.
Keep daily calendar/Persistent=false; separate small timer retries recorded jobs
and optionally catches up the most recent eligible report only. Installer/manual
sends do not populate automatic catch-up candidates implicitly. All expensive work
is outside immediate alert service, whose code and units remain unchanged.

Security review boundaries: root pvesh subprocess, untrusted guest names, private
append-only observation files, decompression/input bounds, atomic private ledger,
wall-clock rollback, notification ambiguity and destination changes. No shell
construction; subprocess deadline/output ceiling and systemd memory/task limits.
Unknown/corrupt state fails closed. No production operations.

## Implementation and verification evidence

Remote default main and baseline HEAD were verified with HTTPS ls-remote/fetch.
SSH client rejected its existing system configuration; no auth/config changes made.
Public `gh release list` returned no releases; local tag list was empty. Working
tree was clean before creation of feature/guest-attribution-historical-trends.
The supplied writable checkout resolves to the shared Projects checkout; no second
independent editing task ran in it. The reviewing agent performed read-only review.

Independent reviewer identified and implementation resolved: retry abandonment at
07:00; installer/catch-up duplication; guest restart identity/window mixing;
unsupported trailing elevated intervals; numeric overflow in guest input; failed
installer verification, including lock contention and age exits; and a final
reader-call regression exposed while adding separate duplicate-data uncertainty.
Medium findings (adaptive guest cadence/retention, HTTP-date Retry-After and recipient
compatibility) were addressed. Review used a separate agent/context; no claims of
live API/systemd verification. Follow-up review evidence is recorded in final results.

Offline tests use synthetic JSONL, mocked transports and temporary private state.
Actual subprocess tests exercise timeout/process termination and response size caps
using a disposable Python child, never pvesh. The new reader regression exercises
real JSONL parsing instead of mocking the reader. Existing tests were preserved.

Performance preliminary: existing 3-day benchmark after feature 0.830s/129.1 MiB
versus baseline 0.859s/129.2 MiB (normal run variability). Thirty retained days with
258,900 host records, four CPU sensors and ten guests: 4.530s wall, 4.529 CPU seconds,
115.4 MiB peak RSS. Same-dataset single-day report: baseline0.246s, feature0.414s.
Guest normalization for100 records:0.285ms; this excludes pvesh process/API latency.
Synthetic host history148,090,799 bytes; final benchmark prints guest disk footprint
and logical read characters separately. No existing memory limit was increased.

Local Python3.14.8 only; Python3.10/3.12 unavailable, CI not executed here.
Bash syntax, ShellCheck and systemd-analyze verify are required final checks.
Systemd syntax emits sandbox socket-option warnings; syntax acceptance does not
prove cgroup limits. Production installation, actual pvesh collection, real mail/
Discord receipt and disposable systemd resource-exhaustion tests remain unperformed.


## Final local results

- Full discovery:172 tests passed in5.631s; all141 original regressions retained.
- New direct regressions:31 passed, also independently executed by the reviewer.
- Python compile, Bash syntax, ShellCheck, config-example validation and systemd
  unit syntax checks passed. Sandbox socket-option warnings accompanied systemd
  verification; no live units were started.
- Final existing3-day benchmark:0.898s,129.1MiB peak RSS (baseline0.859s,129.2MiB).
- Final30-day benchmark:4.587s wall,4.586 CPU seconds,115.4MiB peak RSS;
  same-dataset baseline single-day report0.254s, feature0.418s.
- Thirty-day synthetic history:148,090,799 host bytes plus66,108,000 guest bytes;
  feature processing read76,249,116 logical characters (Linux rchar, includes process
  reads, not physical disk traffic). Guest normalization100 records0.288ms.
- Actual local pvesh startup/API cost is unmeasured; the implementation deadline is
  eight seconds and guest unit has64MiB/15s. Runtime enforcement needs disposable tests.
- Independent final review: no unresolved Critical/High findings identified; all
  previously reported findings resolved, including an independently reproduced
  reader-call regression and its real-file test. Review does not establish deployment.
- Nothing was pushed, merged, installed in production or released. No live test
  notifications or guest/backup/network/storage changes were performed.

Development files: five new Python modules/commands, report and follow-up context
integration, daily-only Discord Retry-After extension, installer/config changes,
five new systemd units (three services,two timers), new regression/benchmark files,
CI workflow additions and documentation. Existing immediate alert/sampler code and
units remain unchanged. See git diff baseline for exact file inventory.


## Changed file inventory

- `.github/workflows/tests.yml`
- `CHANGELOG.md`
- `README.md`
- `config/proxmox-resource-watch.conf.example`
- `docs/DEVELOPMENT_GUEST_HISTORY.md`
- `docs/GUEST_HISTORY_DELIVERY.md`
- `docs/RELEASE_CHECKLIST.md`
- `install.sh`
- `src/proxmox-resource-check-config.py`
- `src/proxmox-resource-report.py`
- `src/prw_daily.py`
- `src/prw_discord.py`
- `src/prw_followup.py`
- `src/prw_guests.py`
- `src/prw_history.py`
- `src/prw_storage.py`
- `systemd/proxmox-resource-daily.service`
- `systemd/proxmox-resource-daily.timer`
- `systemd/proxmox-resource-guests.service`
- `systemd/proxmox-resource-guests.timer`
- `systemd/proxmox-resource-report.service`
- `systemd/proxmox-resource-report.timer`
- `systemd/proxmox-resource-scheduled.service`
- `tests/benchmark_history.py`
- `tests/test_guest_history_daily.py`

Field Notes: prepared and published a1,109-word post (6 minute read); blog check,
build and four validation tests passed; local HTTP returned200. Shared handoff saved
outside the project. No project code or production service was installed.
