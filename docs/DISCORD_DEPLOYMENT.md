# Discord pre-release deployment record

Candidate: `928f879f75990511e4291b617367016cd811fe94` on
`feat/discord-notifications`. This is an unmerged v0.2.0 preview, not a release.
Installed sampler/alert/report baseline hashes matched
`f2e4e3079dcd289a4c4ccb94c981f08386599f39` before deployment.

## Verification

- 109 mocked offline tests passed on local Python 3.14 and deployment Python 3.13.
- All 87 existing tests and 22 Discord tests passed; both files also passed direct
  invocation locally. Compilation, Bash, ShellCheck, systemd verification and diff
  checks passed. GitHub Actions passed on Python 3.10 and 3.12; GitGuardian passed.
- Independent review identified missing-data counts and unfiltered workload context;
  both were fixed, tested and re-reviewed. No unresolved critical/high/medium findings.
- Initial target staging omitted pyproject.toml: version-metadata test failed. The
  metadata was copied and the complete target suite rerun successfully before deploy.
- Existing installer completed at approximately 13:01 EST on 2026-11-03. Installed
  scripts matched candidate hashes; configuration was unchanged and all recorded
  historical log prefixes were verified intact.
- Sampler active with a fresh sample at 13:02:56 EST; both timers active, daily report
  next scheduled 2026-11-04 at 07:00 EST. Alert state mode 0600. No relevant service
  errors after installation.
- Sandboxed email report succeeded and the operator confirmed actual mailbox receipt.
- Discord initially disabled pending secure webhook setup. An initial configuration
  check found an empty URL; a test invocation exited disabled, with no HTTP request.
  The operator then saved a legacy discordapp.com URL. Its hostname was normalized
  to discord.com in the private configuration, with ID/token preserved. The configured
  file was backed up as `config-before-enable` before enabling. One explicit TEST
  succeeded under the report sandbox and the operator confirmed Discord receipt.
  Automatic 07:00 summary delivery awaits its first scheduled run.

## Backups and rollback

Primary verified backup:
`/var/backups/proxmox-resource-watch/discord-pre-928f879-20261103/`

Contains `installation.tar`, captured unit enablement/activity, baseline/target
revision record, configuration hash, historical log-prefix hashes and `ROLLBACK.txt`.
The archive contains configuration, installed scripts, units/drop-ins and alert state;
its regular files were verified against installed originals before replacement.
Directory mode 0700; backup files are private. Treat backups as credential-bearing.
Installer's additional backup:
`/var/backups/proxmox-resource-watch/20261103-130054-3987009/`.

From a root shell on Proxmox:

1. Stop only `proxmox-resource-alert.timer` and `proxmox-resource-report.timer` and
   wait for any active notification oneshots to finish.
2. Enter the primary backup directory. Restore with
   `tar --same-owner -xpf installation.tar -C /`.
3. The baseline lacked `/usr/local/sbin/prw_discord.py`; remove that added file.
4. Run `systemctl daemon-reload`, then
   `systemctl restart proxmox-resource-watch.service`.
5. Restore the captured enabled/active state (both timers were enabled and active):
   `systemctl enable --now proxmox-resource-alert.timer proxmox-resource-report.timer`.
6. Verify fresh samples, service results and the 07:00 calendar. Keep every JSONL/gzip
   monitoring log. Restored old cooldowns can resend alerts delivered since backup.

No guest, storage, backup schedule, firewall or unrelated service was changed.
