# Operations and technical reference

Detailed installation, configuration, behavior, and troubleshooting information for Proxmox Resource Watch. Start with the [README](../README.md) for an overview and quick start.

## Requirements

- Debian/Ubuntu-family Linux host running systemd (including Proxmox VE).
- Python **3.10+**, `bash`, and standard system tools (`install`, `cp`, `systemctl`). No Python packages required for production.
- Root permissions for installation and to collect visible host process/sensor data.
- A working outgoing mail client. This example config uses **Mutt** (`/usr/bin/mutt`) to send to local alias `root`, assuming root mail reaches your administrator's mailbox. Other users can choose `PRW_MAILER=sendmail` and a real email address, if their local MTA supports it.

## Installation

Clone/download this entire repository to the **Proxmox host**. Review the script and config before running any installer, especially if this is a shared or production system.

```bash
# Preview requirements and planned changes, without writing anything:
bash install.sh --dry-run

# Then, as root, confirm the installation interactively:
bash install.sh
```

On a fresh install, the supplied `/etc/default/proxmox-resource-watch` is based on `config/proxmox-resource-watch.conf.example`. The default mail configuration is:

```ini
PRW_REPORT_TO=root
PRW_MAILER=mutt
```

The installer **does not** install an SMTP server, change firewall rules, alter Proxmox guests, or replace your existing mail configuration. It does:

1. Validate OS, Python, systemd and required files; check that Mutt exists for a new default installation.
2. Back up any existing scripts/units under `/var/backups/proxmox-resource-watch/` and **preserve** the current `/etc/default/proxmox-resource-watch` and all monitoring logs.
3. Install scripts in `/usr/local/sbin`, systemd units in `/etc/systemd/system`, and create a restricted log directory.
4. Set the email destination through the config; existing user-edited settings take precedence.
5. Restart the watcher, deliver the initial date once using durable destination tracking (an already successful date is skipped), check the alert service, and then enable the daily-report, immediate-alert and optional follow-up timers, plus bounded daily-maintenance and opt-in guest-collection timers. The follow-up timer does useful work only with `PRW_FOLLOWUP_ENABLED=true`.
6. Check that the watcher and timers are active, and print their schedules.

If an email test fails, the script exits without enabling new timers. Previously enabled timers are not disabled. A failed upgrade leaves backups of replaced files but does not automatically roll them back; inspect the unit journal before retrying.

### Verify mail separately

If you're using the included `mutt`/`root` defaults, you can first test from a root shell:

```bash
printf 'Resource Watch mail test.\n' | mutt -s 'Resource Watch mail test' -- root
```

Confirm the message arrives in the mailbox you monitor. The installer additionally tests the sandboxed service delivery, which can behave differently from an interactive shell.

### Upgrade from the original gist

The installer replaces the sampler script/service **in place**, preserving existing settings and logs at:

- `/var/log/proxmox-resource-watch.jsonl`
- `/var/log/proxmox-resource-watch.jsonl.1`

New samples go in `/var/log/proxmox-resource-watch/YYYY-MM-DD.jsonl`. The report can read remaining old-format logs. Data already removed by the original 10 MiB rotation cannot be recovered. Installation briefly restarts **only** the resource watcher service, not PVE guests.

## Default alerts

| Trigger | Result |
| --- | --- |
| CPU temperature ≥ 85°C [185°F] for 30s | Warning email |
| CPU temperature ≥ 92°C [197.6°F] in a fresh sample | Critical email |
| Host-wide CPU ≥ 80% for 5 minutes | Warning email |
| Host-wide CPU ≥ 8% briefly, or temperature ≥ 75°C [167°F] | Recorded as a report event only |

The defaults were tuned using a lightly loaded **Ryzen 7 PRO 6850U (8-core/16-thread)** Proxmox host. AMD specifies this CPU's Tjmax as 95°C [203°F]; the 92°C critical *alert* fires earlier. These defaults **are not universal hardware limits**. On another host, review `PRW_CPU_THRESHOLD`, `PRW_TEMP_THRESHOLD`, `PRW_CPU_WARNING_C`, `PRW_CPU_CRITICAL_C`, and sustained-alert settings in `/etc/default/proxmox-resource-watch`. Non-CPU sensors have different thermal limits and are reported without using CPU-specific warning limits.

Warnings repeat no more than once per 30 minutes for the same alert class while continuously active. A full minute below the recovery margin re-arms an alert for a new episode. Critical temperature escalation does not wait for a warning cooldown. The state is saved in `alert-state.json` alongside logs, and no action is taken on guest power or performance.

The watcher samples rather than tracing every process: short-lived processes can be missed. Active processes near a temperature spike provide **correlation, not proof of cause**. One-core per-process CPU percentages may exceed 100%; host CPU percentage spans all logical cores.

Host CPU accounting includes VM guest time once, because Linux already includes it
in the user/nice counters. Samples collected before this correction can overstate
host CPU use on VM workloads. Historical percentages cannot be corrected from the
stored samples alone; upgrades preserve those records unchanged. Per-process and
temperature readings are unaffected by this correction.

## Common commands

```bash
systemctl status proxmox-resource-watch.service --no-pager
systemctl list-timers proxmox-resource-report.timer proxmox-resource-alert.timer
journalctl -u proxmox-resource-scheduled.service -n 30 --no-pager
journalctl -u proxmox-resource-alert.service -n 30 --no-pager

# Preview today's incomplete report without sending mail:
python3 /usr/local/sbin/proxmox-resource-report.py --date "$(date +%F)"

```

## Development and tests

The production tools use the Python standard library. The regression suite also uses only `unittest`:

```bash
python3 -m unittest discover -s tests -v
python3 tests/benchmark_report.py
bash -n install.sh
```

Direct execution runs the same suite: `python3 tests/test_resource_watch.py -v`. Additional checks: `python3 -m py_compile src/*.py tests/*.py`, `shellcheck install.sh`, and `git diff --check`.

The tests use synthetic samples and temporary directories; they do **not** operate on Proxmox, send real email, or change production logs.
The offline benchmark reads three synthetic days (25,920 samples, nine sensors,
five processes per sample), generates a report and Discord payload without sending,
and prints elapsed analysis time and peak RSS. It runs under a 512 MiB address-space
limit; this checks a realistic workload budget, not systemd/cgroup enforcement.

## Security and limitations

- Command lines can contain sensitive tokens or filenames. Log files and directories are restricted to root by default; **never publish raw monitoring logs**.
- The sampler runs as root to see processes and sensors. Systemd uses `NoNewPrivileges`, filesystem hardening, and limited writable paths.
- Mail services may require access to a local mail spool or Mutt-specific cache. The included unit files allow Postfix's usual maildrop location if present; review sandbox exceptions for your actual mail setup.
- Sensors may be absent, and a missing reading means **unavailable**, not zero. Fan RPM isn't exposed by all mini PCs.
- Retention removes matching dated logs beyond 30 calendar days. It does not touch unrelated files or the legacy gist log files. This is not a backup.
- Syncing each sample improves durability but cannot guarantee an intact latest record during sudden power loss, a forced kill, storage failure, or a shutdown that exceeds systemd's stop timeout. Hardware and filesystem behavior also matter; missing or damaged records remain possible.
- When a complete sample follows leading null bytes, scheduled or explicitly sent reports can repair a completed daily log only after validating every record. The original is retained as a private `.prw-original` backup and the report notes that other data may have been lost. Current-day logs are never repaired. If validation or backup fails, the log is left alone and the reader can still recover matching samples while marking the input uncertain. Backups are retained until an administrator removes them; other corruption is not automatically repaired.
- If the machine or local mail system is unavailable, notifications can be delayed or lost; this isn't a safety-critical hardware protection system.

## License

MIT; see [LICENSE](../LICENSE). Initial public preview, provided as-is.

## Temperature events and workload evidence (v0.2.0)

Immediate temperature mail uses fresh required monitoring data and a minimal message. It never reads Jellyfin logs, queries the journal, rereads full history, or reconstructs diagnostic events before delivery. Invalid optional integration settings cannot suppress critical mail. Mail transport itself has a 90-second timeout; delivery still requires working storage, scheduling and a mail system. Cooldowns remain per alert class; escalation is independent of warning cooldown.

Daily reports reconstruct warning and critical episodes separately for each exact CPU sensor identity. Recovery requires consecutive observations from that sensor below threshold minus 3°C for at least 60 seconds, without a gap exceeding 2.5 sampling intervals. Missing or renamed sensors and gaps terminate the segment with recovery **unknown**. An open segment is **unresolved**, and only an observed recovery is **recovered**. Immediate re-arming checks every retained observation in a continuous cool suffix of at least 60 seconds, including samples between checker invocations. Missing or hot observations, sampling gaps, insufficient retained history, and uncertain clock order prevent early re-arming. The responsible sensors and all visible CPU sensors must remain below the warning recovery margin. Old state without sensor identities requires a new observed hot episode to establish identity; it cannot establish recovery from unrelated readings.

The last hot observation and recovery-confirmation timestamp are separate. Thermal duration sums intervals whose endpoints are both hot, capped at the configured cadence. A single hot observation has zero confirmed interval. Recovery time and missing time are excluded. Workloads starting after the last hot observation, or entirely inside cooling dips, are excluded from hot-period overlap. Context from adjacent days can connect episodes across midnight; this is a bounded observation window, not a guarantee that the true start or finish is present.

Duplicate instants are merged before analysis: each sensor retains its highest valid finite temperature, and each process identity retains its highest CPU observation. Distinct sensors and processes are retained. Identical duplicates add no duration. Conflicting records cannot prove cooling. Out-of-order records are sorted; backward clock corrections that reuse an instant become conflicting duplicates. Each source is checked for backward timestamps before sorting. Backward order is conservatively treated as uncertain, including delayed records: recovery is never confirmed anywhere in the affected analysis window. New samples also mark a wall/monotonic time discrepancy greater than two seconds with `clock_discontinuity`. The flag survives normalization and adjacent-day reconstruction. Durations across clock changes remain estimates, not elapsed-time guarantees.

Process core-seconds integrate the preceding measurement interval, clipped to confirmed hot intervals within the first and last hot timestamps. Cooling dips are excluded. New samples include monotonic `sample_seconds` and process `start_ticks` to separate PID reuse. Overlapping intervals are clipped at the previous available timestamp. Legacy records use at most one configured sampling interval; missing time is not filled. Only hot observations contribute, and a single-sample episode has no integration window. CPU activity within those intervals is assumed uniform, so boundary clipping is approximate. Top-five sampling misses short or lower-ranked processes. Legacy PID reuse can still merge unrelated processes. Recovery samples are excluded; attribution is incomplete and does not prove causation.

Canonical rows are normalized once and reused internally. Completed events are streamed into a selection retaining at most 100 completed events; active episodes and their hot-sample references remain proportional to the input history. Evidence is fetched once per report, reused for at most 100 thermal events selected by peak, and printed at most 10 lines per event. A journal query uses a 24-hour margin around the selected window, at most 1,000 records, a three-second command timeout, and at most 1 MiB of parsed output. Optional processing runs in a separate worker with a five-second response deadline; journal output has a 2 MiB file ceiling. Slow optional storage cannot hold the report indefinitely. Worker termination cannot immediately reclaim a process stuck in kernel I/O; the parent does not wait indefinitely for it.

The journal query requests structured JSON records, checking the program identity, PID, timestamp, and message. The parser also accepts the existing short-ISO text format. Backup parsing accepts explicit `vzdump[PID]: INFO: Starting Backup of VM/CT ID`, `Finished Backup of VM/CT ID`, and explicit failure records. Only full unique UPID task starts and ends are paired for **confirmed workload overlap**. Repeated UPID starts are ambiguous. PID/VM records remain separate incomplete evidence for **possible workload correlation**, because these identities can be reused; they never create a paired interval. Other Proxmox/PBS formats remain unknown rather than creating guessed intervals. PBS and Jellyfin are never required.

Jellyfin evidence is disabled unless `PRW_JELLYFIN_LOG` names an absolute, readable regular file under the existing systemd sandbox. Final symlinks and special files are rejected using nonblocking open and descriptor type checks. Only the last 1 MiB is read, dropping a partial first line. TaskManager completion records with or without a thread-number field, and with quoted or unquoted task names, for Audio Normalization and media-library scans with explicit durations can establish **confirmed workload overlap**. A matching Jellyfin completion line that omits its logger name is reported as **possible workload correlation**. Arbitrary keyword messages and name-only start/completion pairs cannot. Rotation, unsupported formats and incomplete tasks leave activity unknown. No guest commands or raw workload lines enter email. Confirmed overlap establishes timing only.

Historical timezone-naive monitoring records are interpreted in the host's local timezone at the record date, including historical daylight-saving rules available on that host. Explicit offsets are honored and mixed records can be compared. Ambiguous fall-back local times use the platform's default fold; original offset information cannot be recovered. Naive workload timestamps use the analysis window's offset, so verify source timezone settings, especially around DST. Corrupt/truncated gzip streams retain already decoded records and report read errors.

Input ceilings are 32 MiB of decompressed data per source, 64 KiB per JSON line and 100,000 accepted records per day. Invalid shapes and nonfinite values are ignored; limit exhaustion is reported as a read/parse error. Reports cap text at 240,000 characters with a truncation notice. These limits trade diagnostic completeness for bounded work. Required historical regular-file reads can still be delayed by slow storage; immediate alerts read only bounded recent tails.

### Service resource limits

The supplied units add per-service cgroup memory limits and disable swap use by
these services. The limits include child processes, such as mailers and optional
evidence workers. They require an available systemd memory controller; cgroup v2
is the intended Proxmox environment. Direct Python invocations do not inherit the
units' resource limits.

| Service | Memory maximum | Oneshot deadline |
| --- | --- | --- |
| Sampler | 128 MiB | Continuous service |
| Immediate alert | 128 MiB | 120 seconds |
| Daily report | 512 MiB | 300 seconds |
| Supplemental follow-up | 256 MiB | 210 seconds |
| Guest collection | 256 MiB | 15 seconds |
| Jellyfin snapshot | 64 MiB | 15 seconds |

Each unit uses `MemorySwapMax=0`, `OOMPolicy=kill` and a ten-second stop timeout.
If the memory limit is exhausted, the unit fails and its remaining processes are
killed rather than allowing a partial worker to continue. The sampler retains
its existing restart-on-failure policy; notification/helper timers invoke their
services again on their normal schedules. Daily delivery uses bounded
per-destination retry policies, and a follow-up claimed before failure is not
retried. These limits
trade availability of an oversized job for containment; inspect the unit journal
for `oom-kill` or timeout failures before considering a host-specific override.
Input limits do not imply a fixed Python RSS ceiling, and a process stuck in
uninterruptible kernel I/O may remain beyond a timeout. Historical reports can
still be incomplete or unavailable after storage failures. No limit makes this a
hardware thermal safety mechanism.

## Installation, upgrades and security boundaries

Installation enforces **root:root ownership and mode 0700** on the real log directory, including upgrades. Sampler startup enforces 0700 and refuses a non-root-owned directory when running as root. Existing configuration and log contents are preserved; directory access for other users is deliberately removed. Existing record modes are not rewritten, but directory access is restricted. The installer rejects a symlink log directory. Add new optional settings manually to a preserved configuration. The regression suite exercises both the extracted directory-policy function and a relocated installer workflow against disposable paths with command shims; it does not exercise real root ownership or a live systemd manager.

Commands use argument arrays without a shell. Mail recipients and subjects reject header newlines; process names and report control characters are sanitized. Alert state uses an exclusive mode-0600 temporary file, flush/fsync, and atomic replacement; final-path symlinks are not followed. Existing systemd filesystem restrictions are retained alongside the resource limits above. Configuration and parent directories must remain administrator-controlled: intermediate directory symlinks, malicious privileged writers, filesystem failures and OS-level blocking cannot be fully defended against here. Existing raw command-line samples can contain secrets; restrict logs and never publish them.

The v0.2.0 candidate has been exercised on a Proxmox host, including sampling, manually triggered report delivery and the optional Jellyfin helper under systemd. Validate your own hardware labels, mail routing, ownership and scheduling before relying on it. The tool is observational and is not a hardware thermal safety mechanism.

## Monitoring health and release verification

The alert checker records `monitoring_health` in its existing state file and logs health transitions to stderr, captured by the systemd journal. No usable recent records, stale samples, missing CPU temperatures, disappearance of responsible sensors, unavailable host CPU usage, and uncertain clock ordering are explicitly reported. A later healthy sample logs availability again. This does not send additional mail or alter temperature cooldowns; diagnostic-output failure cannot suppress a critical alert. If the checker itself cannot start, its service failure must be monitored externally.

The workload fixtures in `tests/fixtures/workloads.json` are synthetic and sanitized; they contain no live-system records. Runtime validation on Python 3.10 and any remaining deployment acceptance checks remain release gates; configured CI checks do not count as completed runs. See [the release checklist](RELEASE_CHECKLIST.md).

## Optional Jellyfin LXC log snapshot

A separately enabled helper can expose bounded Jellyfin log tails as a root-readable host file without executing a command in the container. It resolves a systemd-based container's init process using read-only cgroup-v2 metadata and reads yesterday's and today's Jellyfin dated files through its root filesystem. It accepts the current `.log` and rotated `.log.1` forms, at most 512 KiB per day, and atomically replaces a mode-0400 snapshot. The snapshot contains raw Jellyfin logs and can include sensitive information; it must remain in the root-owned mode-0700 monitoring directory. No Jellyfin or LXC configuration is modified.

The optional helper and its timer are installed but not enabled automatically. To use them, configure `PRW_JELLYFIN_CONTAINER_ID` (digits only), `PRW_JELLYFIN_LOG=/var/log/proxmox-resource-watch/jellyfin-context.log` and `PRW_JELLYFIN_MAX_AGE_SECONDS=1800` in the existing environment file; then start the helper and enable `proxmox-jellyfin-export.timer`. It refreshes about every ten minutes. Existing report-service restrictions do not change; the helper is a separate root service with its own filesystem restrictions and a 15-second deadline.

The helper requires the Proxmox cgroup-v2 `/sys/fs/cgroup/lxc/ID/ns/init.scope` layout. Only logs at `/var/log/jellyfin/jellyfinYYYYMMDD.log` and `.log.1` in the container are supported. Missing days, other rotations, a stopped container, slow storage, different container/host dates, or long logs can leave incomplete context. Export failures leave the last snapshot intact; the optional max-age setting makes the report treat an old snapshot as unavailable. Inspect the helper journal for failures. Existing critical alerts do not depend on the helper.

## Optional Discord notifications (v0.2.0 preview)

Discord receives concise warning/critical embeds and a daily health summary with
the full email report, formatted for Markdown, attached as `resource-report.md` from the **07:00 host-local**
report service. No bot, OAuth, Python packages, or additional service is needed.

1. In Discord, open the target server's text channel settings, then **Integrations →
   Webhooks → New Webhook**. Choose the channel and copy the webhook URL. You need
   permission to manage webhooks. Use a regular text channel, not a forum thread.
   If Discord copies a legacy `https://discordapp.com/` URL, replace only that hostname
   with `discord.com` in the protected file; retain the webhook ID/token exactly.
2. On the monitoring host, use `sudoedit /etc/default/proxmox-resource-watch` to add:

   ```ini
   PRW_DISCORD_ENABLED=true
   PRW_DISCORD_WEBHOOK_URL=<paste the webhook URL here, in the file only>
   ```

3. Keep this file owned by root with mode **0600**. The installer preserves existing
   configuration; add these settings manually after an upgrade. Do not put the URL
   on a command line, in chat, Git, screenshots, diagnostics, or a report.
4. Test explicitly through a temporary sandbox matching the report service:

   ```bash
   systemd-run --wait --collect --unit=prw-discord-test \
     -p EnvironmentFile=/etc/default/proxmox-resource-watch \
     -p NoNewPrivileges=yes -p ProtectSystem=strict \
     -p ProtectHome=read-only -p PrivateTmp=yes \
     /usr/bin/python3 /usr/local/sbin/proxmox-resource-report.py --discord-test
   ```

   This sends one clearly identified TEST embed, no email, changes no thresholds,
   and leaves alert cooldowns untouched. Confirm actual receipt in Discord; an API
   acceptance alone is not human receipt verification.

Email and Discord alerts have separate successful-delivery timestamps in the same
atomic mode-0600 state file. Legacy email timestamps remain compatible. One failed
transport cannot suppress the other's attempt or mark it delivered. Each destination
retains the existing per-alert cooldown and observed recovery rules. Critical
escalation bypasses warning cooldowns. Email is attempted first (existing 90-second
mailer timeout); Discord follows with a five-second network timeout. No immediate
alert runs optional historical/workload analysis.

Failed Discord alerts remain undelivered and are attempted on later checks, with a
60-second failure backoff for that alert class. An HTTP 429 applies a destination-wide
backoff using Retry-After (minimum 60 seconds, capped at 24 hours), including critical
messages because Discord is rate limiting the endpoint. Other warning failures do
not delay critical escalation. Backoff and cooldown survive checker restarts. There
is no loop of immediate retries. Delivery acknowledgement lost during a network or
state-write failure can still lead to a later duplicate; exactly-once delivery cannot
be guaranteed by incoming webhooks.

The daily summary reuses report statistics, thermal-event iteration and workload
evidence, plus a cheap CPU-only count. It reports observed availability, missing
measurements, gaps, CPU activity, peak CPU temperature, and per-sensor thermal
counts (warning and critical counts may overlap). Missing measurements are unknown,
not fabricated zeros. Workload evidence describes overlap/correlation, never cause.
The attachment contains the full email report, including the process and guest
details it reports. Anyone who can read the Discord channel can read the attachment.
Scheduled delivery tracks email and Discord independently and can retry explicit
Discord HTTP failures; ambiguous delivery is not retried. Manually rerunning the
full report sends email again, so use the explicit Discord test to troubleshoot instead.

Diagnostics use fixed error categories/status codes and never log webhook URLs or
response bodies. Requests use validated `https://discord.com/api[/v10]/webhooks/...`
URLs, TLS verification, no redirects or environment proxies, bounded response reads,
and server-confirmed message responses. Text is sanitized and length-limited;
allowed-mentions disables user, role and everyone mentions. The existing systemd
filesystem restrictions remain unchanged and already permit outbound HTTPS. A host
network policy must allow Discord HTTPS; do not change firewall policy implicitly.

For failures, inspect `journalctl -u proxmox-resource-alert.service`,
`journalctl -u proxmox-resource-scheduled.service`, and
`journalctl -u proxmox-resource-daily.service`. Configuration errors mean the URL
is missing or does not match the supported format; 400 means Discord rejected the
payload, 401/403/404 usually means the webhook is invalid or revoked, 429 means rate
limiting, 5xx or connection failures may be temporary. Check DNS, trusted CA
certificates and existing egress policy without printing the URL. If leaked, revoke
the webhook in Discord and replace it securely. To disable only Discord, set
`PRW_DISCORD_ENABLED=false`; email and collection continue unchanged on the next run.

## Optional alert-context follow-ups

Set `PRW_FOLLOWUP_ENABLED=true` in the existing root-only configuration and enable
`proxmox-resource-followup.timer` to receive one supplemental message per alert
**episode and destination**, roughly **one minute** after primary delivery.
Timer cadence, evidence collection and transport latency can add delay.
The installer installs/enables this small timer; it does nothing while the setting
is absent or false. Existing configurations remain compatible and are preserved.

Temperature warnings and escalation to critical share one thermal episode. If the
critical escalation happens before the follow-up, available samples and saved
severity make it a critical follow-up. If context was already sent for that episode,
escalation still sends the immediate critical alert but does not send a second context
message. Sustained CPU warnings have their own episode. Existing observed recovery
rules re-arm follow-ups, independently of the 30-minute repeat-alert cooldown.

The immediate checker performs no context analysis or follow-up network calls. It
first persists its existing delivery state and episode identity, then atomically
publishes private queue files. A separate oneshot service polls for due work, using
bounded recent sample tails and the report's existing bounded workload collector.
It has the same filesystem hardening as alerts, a 210-second unit deadline, and at
most two transport attempts per invocation. Slow diagnostics or email in this worker
cannot block the immediate checker. Busy transports can make a follow-up arrive
later than a minute. No additional historical report or long-running daemon is used.

Context includes sampled CPU contributors (names only), peak temperature and CPU
statistics, incomplete-data notices, **confirmed workload overlap**, **possible
workload correlation**, and explicit **unknown cause** wording. Matching times do
not establish causation. Missing or late task-completion records can leave the cause
unknown; later evidence may be available in the detailed daily report. Analysis
failure yields a short unknown/unavailable follow-up rather than changing the
original alert. No raw command lines, credentials, or workload log lines are sent.

Each destination is queued only after successful primary delivery to that destination.
Email and Discord supplemental attempts remain independent. An exclusive, fsynced
attempt marker is saved **before** transport use; there is no retry after an ambiguous
send or worker crash. This favors at-most-once attempts over guaranteed receipt.
A disabled destination or existing Discord 429 backoff skips its follow-up. Jobs more
than 15 minutes overdue expire; a delayed supplementary explanation is less useful.
These outcomes never reset primary-alert delivery timestamps or cooldowns. Pending
jobs survive checker restarts and observed recovery. Terminal queue records are
retained for 30 days, or longer while their episode remains active, to prevent repeats.

Queue/state files are private beneath `PRW_LOG_DIR/followups`. Setting
`PRW_FOLLOWUP_ENABLED=false` disables only supplemental processing; monitoring,
immediate email/Discord alerts and the daily report remain active. Already queued
jobs may run after re-enabling if still within their expiry window. Inspect
`journalctl -u proxmox-resource-followup.service` for safe error categories.

## Guest attribution, historical comparisons and reliable daily delivery

See [the feature, security, upgrade and troubleshooting guide](GUEST_HISTORY_DELIVERY.md) for optional read-only guest snapshots, coverage-aware per-sensor baselines, controlled catch-up, bounded Discord retries and the offline configuration validator. Features remain observational; no production installation is implied by this development candidate.

## Security review and installation boundaries

The security baseline and threat model are in [SECURITY_BASELINE.md](SECURITY_BASELINE.md). The installer now preflights every installed script and unit destination before writing. It rejects symlinks and special files at those names, and requires their parent directories to be root-owned and not writable by group or others. It also rejects unsafe config, alert-state, log-directory and backup-directory types. These checks depend on privileged parent directories remaining protected; they are not a substitute for reviewing repository content before running it as root. The installer creates a private backup directory and does not delete historical records.

An upgrade still is not automatically rolled back after an I/O, service, or notification failure. Automatic rollback could overwrite alert state or lose evidence of ambiguous delivery. The installer now completes a private backup of installed files, config, alert state and unit drop-ins before replacing code; its location is printed before replacement. Existing configurations remain untouched. Run `python3 src/proxmox-resource-check-config.py --config /path/to/copied-config` on a private copy before upgrade, then compare any errors with actual systemd `EnvironmentFile` syntax. This parser is not equivalent to systemd's, so it is deliberately not an installation gate. See [the compatibility decision](SECURITY_FOLLOWUP.md).

The new preflight stops before writing if an existing destination is not root-owned, is group/other writable, or is a symlink or special file. It also rejects an existing configuration readable by any group other than root or by other users. A previously world-readable or `root:adm 0640` config may therefore block an upgrade; an administrator should inspect its ownership and exposure, rotate any exposed credential if appropriate, then set a private mode such as root:root `0600` before retrying. The installer does not silently chmod or change the group of a preserved config.

All supplied services still run as root. The sampler needs full process and sensor visibility; guest collection uses local root `pvesh`; the Jellyfin helper reads container-root metadata; report and mail compatibility includes existing private root state and the root mail configuration. A dedicated account for individual services is a worthwhile future migration but needs a state ownership plan and real Proxmox/mail validation. The current units already set `NoNewPrivileges`, `ProtectSystem`, `PrivateTmp`, `MemoryMax`, `MemorySwapMax`, and bounded oneshot timeouts. Do not add syscall, address-family, device, capability, or home restrictions until each component's actual `pvesh`, `/proc`, `/sys`, mail, and sensor behavior is exercised in a disposable Proxmox environment.

CI runs the regression suite, ShellCheck, Bandit (all severities), CodeQL Python security-extended, Gitleaks history scanning, Actionlint, and Zizmor. CodeQL does not analyze Bash. The runtime has no third-party Python dependency, so a production Python package audit would add no coverage. The Go/Python CI analyzers and pinned GitHub Actions remain supply-chain dependencies; review their updates. Active ruleset `26641585` now protects `main` with one approval, strict status checks from the verified Actions/CodeQL providers, and force-push/deletion blocks. The Actions default token is read-only. Secret scanning, push protection, Dependabot alerts/security updates and private vulnerability reporting still need separate administrator enablement. See [the verified settings](SECURITY_FOLLOWUP.md).

### Controlled recovery after an incomplete upgrade

Rehearse this on a disposable systemd/Proxmox environment before production use. The installer reports `Recovery backup directory: ...` before replacing files. If it fails, do not rerun it blindly: record the failing command and current unit/timer status. An upgrade may have changed some scripts and units, and previously active timers may still fire.

1. Stop the Resource Watch timers and sampler while preserving their prior enabled/active state in a private operator record. Include report, alert, follow-up, daily, guests, and optional Jellyfin timers. Do not stop unrelated host services.
2. Make a second private copy of the **current** log/state directory before restoring anything. Keep historical JSONL/gzip records and the current alert/delivery state even if some notification failed. Record installed script/unit hashes and the backup directory path.
3. Inspect the private backup for the expected prior `.py`, `.service`, `.timer`, config, alert-state and `.d` entries. For a representative upgrade, restore the backed-up Python files to `/usr/local/sbin` and backed-up units to `/etc/systemd/system`, preserving their ownership and modes. Restore any drop-ins only if they actually changed. A missing prior file needs a manual decision; do not copy an unrelated backup member into a destination.
4. Preserve the current config and alert/delivery state by default: the installer does not rewrite config or alert state. Restore an older state only after reviewing delivery ambiguity, because that can resend alerts or daily reports. Never discard the newly captured state or historical records.
5. Run `systemctl daemon-reload`, restart the sampler, inspect `systemctl status` and the private journal, then test report delivery through an administrator-approved safe message. Re-enable only the timers that were previously enabled or intentionally approved. Check next-run schedules, fresh samples, file modes and recipient delivery. If verification fails, leave affected timers stopped and retain both backups for diagnosis.

For a fresh install that fails partway through, there may be no prior scripts or units to restore. Keep the service disabled and stop its timers; remove only files known to have been created by that failed attempt after reviewing the recorded inventory. This procedure has only been rehearsed with relocated paths and command shims; real systemd recovery remains a deployment gate.
