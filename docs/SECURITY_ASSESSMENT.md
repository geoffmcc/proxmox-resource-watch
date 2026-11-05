# Production hardening assessment

Baseline: `431fd7df505338fdbe4c77d06c2e67cc1c77abb6` on `origin/main`, fetched before the feature branch. Read [the baseline and threat model](SECURITY_BASELINE.md) first. This is an offline assessment, not production certification.

## Confirmed findings and disposition

| Severity | Finding | Prerequisite and impact | Disposition |
| --- | --- | --- | --- |
| HIGH | Root installer accepted symlinks/special files at installed script and unit names. | Privileged destination tampering or a malformed existing installation could redirect or interrupt replacement. | Fixed: preflight checks all destination types and root-controlled parents before any write. Helper tests cover regular, absent, symlink, FIFO and unsafe parent cases. Full privileged integration remains unverified. |
| MEDIUM | Failed installation could leave mixed installed versions. | I/O, restart, or delivery failure during upgrade; monitoring can be disrupted. | Open: backup and controlled recovery retained. No automated rollback of ambiguous alert/delivery state. A disposable systemd upgrade/failure rehearsal is required. |
| MEDIUM | Existing config is not validated before service restart. | A broken or malicious preserved config can fail the restarted service. | Open pending compatibility review: validator is available for private preflight, but imposing its syntax on existing deployments may reject functioning EnvironmentFile settings. |
| LOW | GitHub Actions used moving major tags and lacked security checks. | Compromised or retargeted action tag, or undetected regression. | Fixed in branch: immutable action SHAs; ShellCheck, Bandit, CodeQL, Gitleaks, Actionlint and Zizmor jobs. The draft PR CI passed after this change; repository protection settings remain an administrator decision. |
| INFORMATIONAL | Eight services run as root. | Code execution in any unit would carry root identity despite filesystem restrictions. | Retained for compatibility; separate migration needs real Proxmox, private-state, and mail validation. |

No confirmed shell or argument injection was found. Fixed argument arrays are used for `pvesh`, `journalctl`, mailers and Discord. Historical records, guest responses, optional diagnostics and webhook transport already have bounded or sanitizing controls. This review did not prove every parser safe against every input; adversarial coverage should be expanded in a subsequent pass.

## Scanner review

Bandit 1.9.4 scanned all 12 production Python files and reported nine findings: eight Low, one Medium. The Medium B108 finding is a false positive: `/tmp` appears in a list of **forbidden** state directories in `proxmox-resource-check-config.py`, not a created temporary file. The CI checker records every finding and allows only that exact reviewed location; new Medium/High findings block. The Low findings concern fixed `pvesh`/`journalctl`/mailer subprocesses and a deliberately contained exception handler; their safety depends on the fixed argument vectors, deadlines, bounded output and journal behavior described in operations. None is hidden from the report. There are no production third-party Python dependencies.

## Independent review

A separate agent inspected the actual diff. It found a High fresh-install regression: the first draft checked an alert-state child before its log directory existed. That check was changed to run only if the directory already exists. The reviewer also identified missing full installer integration and failure-recovery tests, scanner filters, a disabled Actionlint ShellCheck integration, and an incorrect root-service count. Filters were removed, Actionlint now runs its shell checks, and the count was corrected. Full installer/systemd integration remains open.

## Verification and production gate

Locally, 199 regression tests passed. Synthetic report/history benchmarks, Python and Bash syntax, ShellCheck, Bandit with reviewed finding gate, Gitleaks history scan and separate synthetic detection, Actionlint, Zizmor, `git diff --check`, and `systemd-analyze verify` passed. Systemd verification needed a read-only unsandboxed invocation because the restricted sandbox could not create systemd's verification sockets. GitHub Actions passed on draft PR #13 for commit `b9b2d52`: both Python regression jobs, Static analysis, Secret detection, Workflow security and CodeQL Python. The separate CodeQL status check passed as well. No full installer integration test ran: this environment denies user namespaces, and the repository must not be executed as root on an actual host for testing. Do not declare this candidate production-ready until a disposable, authorized systemd/Proxmox environment exercises install, upgrade, failure, recovery, `pvesh`, mail and effective cgroup restrictions. Never use the production hypervisor for those tests.
