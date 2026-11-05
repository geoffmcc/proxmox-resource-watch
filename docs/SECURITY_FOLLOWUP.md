# PR #13 follow-up security evidence

Baseline `origin/main`: `431fd7df505338fdbe4c77d06c2e67cc1c77abb6`. Original PR head: `0df66300157720756071591f96431589dbc324ed`. Read the [threat model](SECURITY_BASELINE.md). This is an offline assessment, not production certification.

## Installer and recovery

The installer uses fixed absolute destinations. Before writing, it checks every script, unit, config and alert-state destination for a regular file or absence. The mandatory script and unit directories must already exist; missing log and backup directories may be created. Existing log, backup and unit drop-in directories must be real directories. Existing targets, existing directories and every ancestor must be root-owned and not group/other writable. Existing config must not be readable by other users or a non-root group; `root:root 0640` remains accepted for compatibility. Symlinks and FIFOs at these checked paths are rejected. The backup is created privately, and all existing scripts, units, drop-ins, config and alert state are copied before the first replacement. A failed backup leaves installed files untouched. The backup location is printed before replacement. Historical log records are never deleted or restored by the installer.

These checks prevent redirection by an ordinary unprivileged local user when the fixed parent paths are root-controlled. An actor who can write those directories, replace the reviewed source during root installation, or otherwise act as root has a stronger capability. The checks do not eliminate all time-of-check/time-of-use races. Run from a trusted, immutable checkout.

Replacement remains sequential. Interruption, I/O failure, failed daemon reload, restart, or notification can leave mixed installed code or stopped monitoring. Automatic rollback could rewind alert or delivery state after a successful but ambiguously acknowledged send, causing duplicates. Preserve current state and records, and use controlled recovery in [OPERATIONS.md](OPERATIONS.md). Drop-ins are backed up, though the installer does not replace them.

`tests/test_installer_workflow.py` runs the installer shell workflow and real file operations against relocated disposable paths. It covers fresh/repeated installs, upgrade from valid prior script/unit files, backup integrity, preserved config/log/alert state/drop-ins, existing/missing files, symlinks, FIFOs, sockets, writable directories/files, and injected install, reload, restart, report-start and alert-start failures. It asserts no service calls after preflight failure, no timer enablement after these service failures, backups, and controlled file restoration. It shims `systemctl`, `chown` and root ownership. The prior files are syntactically valid but are not started. It does **not** validate a real manager, privileged filesystem, Proxmox, mail, signal interruption or power loss. Those integration scenarios remain unverified.

## Configuration decision: Option C, explicit manual validation

The preserved config is a systemd `EnvironmentFile`: it is data and does not run shell syntax. The current validator opens a regular file with `O_NOFOLLOW`, limits size to 64 KiB, avoids printing values, checks known ranges and paths, and warns about duplicate and unknown keys. Synthetic tests cover ordinary quotes, comments, duplicate last-wins settings, supported values, malformed lines, shell-like text without execution, unsafe `/tmp` log paths, and redaction.

It is not a systemd-equivalent parser. Systemd ignores lines without `=`, accepts multiline quotes, removes escapes in unquoted values, and preserves interior whitespace according to its own rules. The validator rejects or interprets some of these differently. See [systemd.exec(5), EnvironmentFile](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html). A mandatory installer gate could reject a functioning installation or validate a different value than the service receives. The installer therefore preserves existing config without running this parser before modifications. Before upgrade, make a private copy, run `python3 src/proxmox-resource-check-config.py --config <private-copy>`, compare errors with systemd syntax, inspect effective paths and settings without printing credentials, and rehearse a restart in a disposable equivalent environment. A parser delegating to systemd or proven equivalent may support future gating. The remaining risk is that bad preserved config can fail the restarted service. Config ownership and write permissions are now checked before installation. Root-controlled config can still select application paths or destinations; systemd's lack of shell expansion is not a universal safety guarantee.

## Runtime privileges

All eight supplied service units lack `User=` and therefore run as root. They statically specify `NoNewPrivileges=true`, `ProtectSystem=strict`, `PrivateTmp=true`, `UMask=0077`, memory and swap limits, and timeouts where appropriate. Timers are schedules, not distinct security principals. `systemd-analyze verify` checks syntax but not effective runtime enforcement.

| Unit(s) | Required access and root rationale | Decision |
| --- | --- | --- |
| `proxmox-resource-watch.service` | Full `/proc` process visibility, `/sys` sensors, private logs; root may be required for full attribution and existing state. | Retain pending real visibility and ownership tests. `ProtectHome=true`; no mail/API. |
| `proxmox-resource-report.service`, `proxmox-resource-scheduled.service`, `proxmox-resource-daily.service` | Private history, journal and optional Jellyfin log; root Mutt configuration, Postfix maildrop and optional Discord HTTPS. | Retain for current state/mail compatibility; read-only home and maildrop exception. |
| `proxmox-resource-alert.service`, `proxmox-resource-followup.service` | Private alert/follow-up state, mail, optional Discord and history. | Retain for state/mail compatibility; read-only home and maildrop exception. |
| `proxmox-resource-guests.service` | Fixed local `pvesh get /cluster/resources`, private guest snapshots. | Retain until a disposable Proxmox test proves narrower API identity works. |
| `proxmox-jellyfin-export.service` | `/proc`, cgroup metadata, container-root log and private snapshot. | Retain; `ProtectHome=true`. |

No new syscall, device, address-family or capability filters were added. They might disrupt `pvesh`, sensors, local mail or Proxmox internals. Effective properties and actual function remain unverified without a disposable Proxmox/systemd environment.

## All Bandit 1.9.4 findings

Bandit scanned all 12 production Python files; full JSON is a CI artifact. All nine findings were reproduced. Line numbers below are unchanged by this follow-up. Each call is reachable except B108, which is a forbidden-path literal.

| Rule and location | Trigger, input, controls and disposition |
| --- | --- |
| B110 Low, `proxmox-resource-alert.py:308` | Broad `except Exception: pass` contains supplemental follow-up scheduling failure so primary delivery state can be saved. Unexpected errors are hidden, but no untrusted data executes here. Retain; a future redacted diagnostic could improve visibility. |
| B108 Medium, `proxmox-resource-check-config.py:54` | `/tmp` occurs only in a tuple of **forbidden** log directories. No temporary file is created. False positive; CI exception requires exact rule and exact reviewed source line. |
| B404 Low, `proxmox-resource-report.py:16` | Import of `subprocess` for bounded journal and mail calls. Import alone harmless; call sites below reviewed. Retain. |
| B607 Low, `proxmox-resource-report.py:509` | `journalctl` resolves through PATH. The system manager provides normal PATH unless root-controlled config changes it. Fixed arguments and a 3-second timeout. Malicious root-controlled PATH is a remaining misconfiguration risk. Retain for compatibility. |
| B603 Low, `proxmox-resource-report.py:509` | Journal query receives application-generated timestamps; fixed flags, no shell, 1,000-entry limit and 3-second deadline. Production calls run in `_evidence_worker`, which sets `RLIMIT_FSIZE` to 2 MiB before launching `journalctl`; the worker reads at most 1 MiB from its temporary file and is subject to a 5-second parent deadline. Journal text remains data. Retain. Direct unit tests of `_collect_evidence` do not apply the worker limit, but do not represent the service call path. |
| B603 Low, `proxmox-resource-report.py:956` | Mutt receives recipient after `--`, subject after `-s`, body on stdin, no shell, 90-second timeout. Recipient rejects leading `-`, CR/LF/NUL. PATH is root-controlled under normal operation. Retain; real mail integration still needs testing. |
| B603 Low, `proxmox-resource-report.py:958` | Absolute `/usr/sbin/sendmail`, fixed `-t -oi`, `EmailMessage` constructs headers, body on stdin, no shell, 90-second timeout. Recipient validation above applies. Retain. |
| B404 Low, `prw_guests.py:10` | Import of `subprocess` for fixed local `pvesh`; harmless by itself. Retain. |
| B603 Low, `prw_guests.py:51` | Absolute `/usr/bin/pvesh`, fixed read-only GET route/options, no guest metadata in arguments, no shell. Selector deadline 8 seconds, `MAX_BYTES` cap, process-group kill on exit/failure; JSON treated as untrusted. Retain. |

No blanket `# nosec` or scanner exclusion was added. These are reviewed dispositions, not a guarantee that every possible parser or subprocess failure is excluded.

## CI and repository settings observed at original head

GitHub reported successful `tests (3.10)`, `tests (3.12)`, `Static analysis`, `CodeQL Python`, `Secret detection`, `Workflow security`, and separate `CodeQL` for `0df6630`. The CodeQL Python analysis ran 50 rules with zero results and no error; open code-scanning alerts were empty. ShellCheck covers the two maintained Bash scripts. Bandit recursively scans `src`; Medium/High findings block except the exact reviewed B108 line. Gitleaks scans full Git history. Actionlint and Zizmor run with no broad suppressions. Third-party Actions use immutable 40-character SHAs and executed successfully. Permissions are read-only except CodeQL's `security-events: write`; checkout credentials are not persisted. No workflow has production credentials or deployment. CI tools use fixed Go/PyPI versions, though package hashes are not pinned; this remains a supply-chain dependency.

At first follow-up head `31552c3`, the separate PR `CodeQL` check failed on two High `Clear-text storage of sensitive information` annotations at `tests/test_config_compatibility.py:17,61`. The new tests intentionally wrote a **synthetic** marker to a disposable config file to verify parsing and output redaction; no real credential or production path was involved. We reviewed both flows and retained the assertions while naming the test value as a canary, to make its role clear to readers and CodeQL. No CodeQL finding was suppressed or dismissed. The final-head analysis and PR check must be inspected again after this correction.

Zizmor's default regular persona reports six filtered pedantic findings, not repository suppressions. A separate pedantic run showed one informational unnamed regression job and five Low concurrency-limit suggestions. Its initial permission-comment suggestion was resolved by documenting CodeQL's `security-events: write` inline. Job concurrency would reduce redundant CI consumption but is not a privilege boundary. These are documented, not hidden. No `.zizmor.yml` or inline ignore is present.

| Setting | Verified status | Recommended administrator action |
| --- | --- | --- |
| `main` protection/rulesets | Disabled: `protected:false`, protection endpoint 404, rulesets empty. | Create `main` ruleset with PR review and required checks; block force pushes and deletion. |
| Required checks/reviews | None enforced. | Require `tests (3.10)`, `tests (3.12)`, `Static analysis`, `CodeQL Python`, `Secret detection`, `Workflow security`; require one independent approval. |
| Force-push/deletion | Not restricted by branch policy. | Prohibit both in `main` ruleset. |
| Actions default token | Read-only; Actions PR approval disabled. | Keep read-only. |
| Actions allowed sources/SHA policy | All actions allowed; SHA pin enforcement disabled. | Consider allowlist or SHA enforcement after workflow testing. |
| Secret scanning/push protection | Both disabled per repository API. | Enable if available; test with synthetic data. |
| Dependabot alerts/security updates | Alerts API reports disabled; security updates disabled. | Enable both in Settings → Advanced Security. |
| Code scanning | Analysis and alerts API available; zero open alerts at original head. | Keep CodeQL; inspect final-head analysis/alerts. |
| Private vulnerability reporting | Disabled per API. | Enable in Settings → Advanced Security. |

No repository security settings were changed. Disabled-feature plan availability was not independently verified; if absent from the UI, record the plan limit instead of claiming the control is enabled.

## Independent review of this follow-up

A separate read-only agent, which did not implement the changes, reviewed the full PR diff, threat model, installer, tests, workflows, scanner dispositions and limitations. It found no confirmed Critical or High defect. Its Medium test-fidelity finding was an unrealistic placeholder upgrade fixture; the fixture now uses valid prior script and unit content, and tests restore the full installed file set after an injected failed upgrade. The reviewer initially suspected unbounded journal temporary-file writes, then checked the full call chain and withdrew that finding: `_evidence_worker` sets a 2 MiB file-size limit before the journal subprocess runs. Its Low findings were an early dry-run exit and missing CLI error-redaction test; dry-run now executes destination preflight, and a synthetic canary test exercises the CLI failure path. The reviewer also found that missing mandatory install directories could cause a partial installation and that group-readable config was accepted without checking the group ID. Both now fail before writes and have focused tests. The reviewer challenged root execution from a user-writable source checkout; staging requirements are now concrete in the release checklist. A second independent read-only agent rechecked the final installer corrections after the first reviewer reached a usage limit. Review findings are independent; local implementation review is not counted as a separate review.

## Release and readiness limits

The deployment model is administrator installation from a reviewed commit; CI never deploys. Record exact commit and Git origin, verify signature where policy requires, export that **fixed commit SHA** to a root-owned, non-group/other-writable staging directory, compare staged file hashes with the reviewed source, and ensure an unprivileged user cannot replace staged scripts or `installer/preflight.sh` between verification and execution. Do not run root installation directly from a user-writable checkout. Capture pre/post installed file hashes, preserve config/state/unit/drop-in backups, and use the [release checklist](RELEASE_CHECKLIST.md). A checksum or signed tag helps only if independently produced and verified; neither replaces review of root-executed source. An SBOM has limited value for this stdlib-only runtime, and provenance can wait for a packaged release. The installer does not verify signed tags or installed hashes.

Production gates still include real disposable Proxmox/systemd install and upgrade; effective unit, `pvesh`, sensor and mail validation; recovery under a real manager; repository protections; and verification of Actions results for the final candidate commit. A second independent read-only agent rechecked the final two installer corrections, found no new Critical or High defect, and identified a stale operations statement about installer test coverage; that statement was corrected. An external review of PR #13 remains appropriate before merge. CI success alone is insufficient.
