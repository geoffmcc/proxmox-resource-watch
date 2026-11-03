# Alert-context follow-up candidate deployment

Runtime candidate: `0f1d8ba3221c5b1c2a275660892adaf01b23a998` on
`feat/alert-context-followups`, based on main `5fd22e5`.
PR: https://github.com/geoffmcc/proxmox-resource-watch/pull/6 (ready for review).
No merge or release has been performed for this feature.

## Checks and approval

- 136 mocked regressions passed locally and on Proxmox Python 3.13: 109 existing
  tests and 27 follow-up tests. Tests send no real notifications.
- The same suite passed in a temporary systemd unit with NoNewPrivileges,
  ProtectSystem=strict, ProtectHome=read-only and PrivateTmp.
- Python compilation, Bash syntax, ShellCheck, systemd unit verification and diff
  whitespace checks passed. CI passed on Python 3.10/3.12; GitGuardian passed.
- Focused review checked primary-state persistence before queue publication,
  exclusive attempt markers, escalation/recovery, privacy, bounded analysis and
  independent destinations. A timing edge case was fixed/tested: due time starts
  after primary transports finish, rather than before a potentially slow mail send.
- Automatic approval review initially blocked production deployment because the
  feature-development request did not explicitly authorize rollout. No production
  change occurred on that rejected attempt. The operator then explicitly approved
  “Deploy and enable follow-ups”; the same backup-first plan completed successfully.

## Installed behavior and health

- Installed alert checker, follow-up helper/entry point and two follow-up units;
  enabled `PRW_FOLLOWUP_ENABLED=true` and the follow-up timer.
- Targeted deployment avoided the full installer's sampler restart and report send.
  Only the immediate-alert timer was briefly paused while its checker was inactive.
  The sampler and daily report service were neither restarted nor modified.
- Installed target files matched staged candidate hashes. Existing non-follow-up
  settings, including credentials, were unchanged. Historical log prefixes matched.
- Sampler and all three notification timers active; follow-up oneshot exited 0.
  Daily report remains 07:00 host-local. Configuration and primary alert state stay
  root-private 0600. Fresh samples continue; no new alert/follow-up service errors.
- No fabricated production warning/critical event or live follow-up test was sent.
  Real follow-up receipt awaits the next qualifying natural episode.

## Backup and rollback

Verified private backup:
`/var/backups/proxmox-resource-watch/followup-pre-0f1d8ba-20261103/`

Contains verified `installation.tar`, original timer status, log-prefix hashes,
baseline/target revisions and `ROLLBACK.txt`. Baseline runtime was the verified
Discord candidate `928f879` (runtime identical to merged main `5fd22e5`). Treat the
archive as credential-bearing. Previous Discord rollback backups remain intact.

1. Stop/disable `proxmox-resource-followup.timer` and stop its oneshot service.
2. Stop `proxmox-resource-alert.timer` and wait for any immediate delivery to finish.
3. From the backup directory, restore with
   `tar --same-owner -xpf installation.tar -C /`.
4. Remove only added `/usr/local/sbin/prw_followup.py`,
   `/usr/local/sbin/proxmox-resource-followup.py`, and the added
   `/etc/systemd/system/proxmox-resource-followup.service` and `.timer`.
5. Run `systemctl daemon-reload`, then
   `systemctl enable --now proxmox-resource-alert.timer`.
6. Verify sampler, both original timers, fresh samples and service results. Keep
   monitoring history and private context-queue records. Restored old primary state
   may resend alerts delivered after the backup.

The original sampler/report services require no restart for this rollback. No guest,
backup schedule, storage, firewall, authentication or unrelated service was changed.
