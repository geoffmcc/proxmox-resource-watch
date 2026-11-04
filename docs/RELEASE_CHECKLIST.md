# v0.2.0 release acceptance

Discord and alert-context follow-ups are merged into main. No live validation or
installation of subsequent acceptance fixes is implied by passing offline tests.

## Acceptance-review corrections

- Verify guest and niced-guest time are counted once, including a guest-dominated
  workload below the CPU alert boundary and an actual threshold crossing.
- Verify event lines show unavailable for missing measurements, preserve observed
  zeros and negative temperatures, and sort episodes with partial data.
- Validate the service memory/swap limits, OOM policy and deadlines with
  `systemd-analyze verify`. Measure report latency and peak RSS using a realistic
  three-day synthetic history with multiple sensors and five process contributors.
- In a separately authorized, disposable systemd environment, verify effective
  cgroup limits and exercise memory/deadline exhaustion. Never perform this on the
  production Proxmox host. Syntax validation does not prove runtime enforcement.
- Deployment of these corrections remains a separate step: compare installed
  hashes and effective unit properties, observe fresh samples, and confirm normal
  scheduled delivery. Previously recorded production evidence remains valid for
  the deployed baseline; it does not establish deployment of the corrections.

## Offline gates

- Run discovery and direct test-file execution on Python 3.10 and the deployment Python version. The CI matrix includes 3.10 and 3.12; CI must actually run before recording a pass.
- Run Python compilation, bash syntax, ShellCheck, and git diff --check.
- Review clock rollback, skipped recovery samples, ownership-command failure, mail failure/retry, monitoring-health transitions, and sanitized workload fixtures.
- Review peak memory and report latency with realistic sensor counts and long histories. Input bounds do not imply a small fixed RSS ceiling.

## Controlled deployment gates

A v0.2.0 candidate was exercised on a Proxmox host with Python 3.13, including sampling, report delivery, and the optional Jellyfin helper. Validate the following in an appropriate environment before broader rollout; the host trial did not cover every item.

1. Snapshot configuration, log contents, alert state and their ownership/modes. Include a legacy state without sensor identities and historical compressed logs.
2. Exercise installation/upgrade in that environment. Verify root:root/0700 directory policy, unchanged configuration and record contents, restricted new state files, and preserved cooldown timestamps.
3. Verify actual installed Python version and systemd service restrictions. Exercise the selected mail transport under the unchanged sandbox and confirm mailbox receipt.
4. Exercise a failed mail transport, restore it, and verify retry without a premature successful-send timestamp.
5. Feed synthetic temperature records: critical delivery, cooldown, escalation, continuous recovery, an intervening missing sensor, clock rollback, and monitoring-data loss/restoration.
6. Verify optional workload failure never delays critical mail. Confirm journal health transitions are visible to the operator.
7. Review the independent audit again and satisfy the session's authorized deployment gates. Keep the original deployment recoverable.

No real host, VM, container, or external service should be changed as part of offline regression tests.

## Discord candidate gates

- Run both regression files and full discovery with mocked HTTP/mail; compile Python,
  check Bash/ShellCheck, verify systemd units and inspect the final diff.
- Independently review transport isolation, legacy state, recovery, critical escalation,
  secret handling, HTTP errors and missing-data summaries. Resolve high/critical findings.
- Record installed baseline hashes, target commit, private backup path and rollback
  commands in a deployment record. Snapshot scripts, configuration, units/drop-ins,
  state and unit enablement before replacing anything. Preserve historical records.
- Check configured webhook presence without printing it. Stop before enabling if absent;
  request secure operator configuration. No credentials in Git or test output.
- Verify sampler advances, unchanged timer calendars, sandboxed email delivery and
  mailbox receipt, one explicitly identified Discord TEST and operator receipt.
- Do not merge or publish a release without explicit approval. Any deployment from
  the feature branch is a pre-release candidate, identified by exact commit.

Rollback: stop only the resource alert/report timers and wait for their running
oneshot services to finish. Restore backed-up scripts, configuration, units/drop-ins
and alert state with ownership/modes intact, remove the new `prw_discord.py` if absent
from the baseline, run `systemctl daemon-reload`, restart only the resource sampler,
and restore captured timer enablement/activity. Keep all monitoring JSONL/gzip logs.
Restoring old state can resend alerts delivered since the backup; record this risk.
Verify sampling, calendars and service results. Never alter guest or backup services.

## Alert-context follow-up gates

- Test warning/critical escalation as one thermal episode, CPU warning isolation,
  delivery independence, disabled configuration, due time, recovery/re-arming,
  restart persistence, failed analysis, missing evidence, 429 backoff, expiry and
  exclusive attempt markers. Mock all transports; do not create production alerts.
- Verify context distinguishes observed contributors, overlap, correlation and unknown
  cause, sanitizes names/URLs/mentions, and never includes raw process commands.
- Verify identity is persisted before jobs are published, and all diagnostics run in
  the separate oneshot rather than the immediate checker. Follow-up delivery never
  writes primary-alert cooldowns. No automatic retry after a supplemental attempt.
- Before deployment, back up the alert script, config, state, units and any queue.
  Record exact baseline/target, preserve history and verify rollback. The new timer
  must not alter the existing sampling, report, alert or backup schedules.
- Rollback: stop/disable only proxmox-resource-followup.timer; stop its oneshot;
  restore backed-up alert script/config/state and units, remove the new follow-up
  scripts/unit/timer, daemon-reload, restore original timer states. Preserve monitoring
  history. Keep the private follow-up queue for investigation; the old checker ignores it.

## Guest/history/daily-delivery candidate gates

- Run all regressions plus direct new-test execution; CI must run Python 3.10/3.12.
  Local Python 3.14 results do not establish those matrix passes.
- Run `tests/benchmark_report.py` and `tests/benchmark_history.py`. Compare original
  report cost on the same dataset using PRW_BENCHMARK_BASELINE if supplied. Review
  guest input/write limits and sparse evidence rather than expanding memory limits.
- Re-review separate guest/daily oneshots, fixed GET operation, private atomic ledger,
  lock contention, in-flight ambiguity, duplicate conflicts and retention when disabled.
- In a disposable authorized Proxmox/systemd environment, verify pvesh GET cache
  semantics on supported versions, local-node naming, guest memory limits and timeout
  enforcement; test read-only access and guest absence without touching guest lifecycle.
- Exercise install/upgrade with retained config/state, before/after 07:00, failed
  initial email, an already-delivered date and concurrent workers. Verify unit target
  change retains the 07:00 calendar and Persistent=false.
- With mocked transports test partial delivery, restart, state corruption, missing
  observations, long Retry-After and expired jobs. Separately authorize real recipient
  receipt checks; no live notification is implied by offline results.
- Document exact deployment commit, back up all installed assets/state/timer activity,
  and rehearse rollback in a disposable environment. Do not merge/release automatically.

See [feature/security/rollback guide](GUEST_HISTORY_DELIVERY.md) and
[development evidence](DEVELOPMENT_GUEST_HISTORY.md).


## Pre-PR finding resolution gates

- Run `python3 tests/test_review_fixes.py -v`: critical delivery with unavailable or
  failing optional imports; durable scheduled intent under lock contention; slow
  mail before numeric/HTTP-date Retry-After; fresh time across retry dates; invalid
  zero CPU alert threshold and empty log directory; analytically known irregular
  CPU intervals; midnight/overlap/missing cadence; timestamp precision; uncertain
  daily/follow-up correlation through the actual loader; single-line guest names.
- The history benchmark must assert that rollback, malformed/missing records,
  sampling gaps and guest restarts occur in its measured window. It includes
  adjacent-day report context but excludes API startup, transport, concurrency
  and cgroup enforcement. Do not describe it as a full scheduled-service benchmark.
- Preserve the original pre-PR review as historical evidence. Record corrections
  and current checks separately in `docs/PRE_PR_REVIEW_FIXES.md`.
