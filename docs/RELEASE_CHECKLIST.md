# v0.2.0 release acceptance

Discord work is on `feat/discord-notifications`, based on merged main `f2e4e30`. No live validation or installation is implied by passing offline tests.

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
