# Proxmox Resource Watch

**v0.2.0 — Preview, pending independent review**

Proxmox Resource Watch helps explain what was happening when a host got hot or busy. It samples host resources and processes, then brings together temperature events, workload activity, and recent history in a daily report. Optional read-only VM and container observations add context.

Activity near an event is **evidence, not proof of cause**. Sampling can miss short-lived work, and missing readings remain unknown. This tool monitors and reports; it does not control fans, shut down guests, or replace hardware protection.

## What it does

- Samples host CPU, processes, available sensors and AMD GPU data every 10 seconds by default; retains 30 days of logs.
- Sends a report for the previous day at **07:00 host-local time**, compares it with recent history when enough data exists, and checks for temperature and sustained CPU alerts.
- Optionally adds guest activity, Jellyfin or backup context, Discord notifications, and follow-up messages.
- Uses systemd services and timers; production code requires only Python's standard library.

## Requirements

- Proxmox VE or another Debian/Ubuntu-family Linux host with systemd and Python **3.10+**.
- Root access for installation and host monitoring.
- A working mail client and outgoing mail. The default configuration uses Mutt and the local `root` alias; it assumes root mail reaches your mailbox. The installer does not configure an SMTP server.

## Install

Clone or download the repository to the host. Review the installer and [example configuration](config/proxmox-resource-watch.conf.example), then run:

```bash
# Check requirements and planned changes without writing anything.
bash install.sh --dry-run

# Install as root and follow the prompts.
sudo bash install.sh
```

The installer preserves existing settings and logs, backs up replaced files, restarts the sampler, and tests initial report delivery. A failed upgrade does not automatically roll back; inspect the service journal before retrying. See the [installation and upgrade details](docs/OPERATIONS.md#installation) for mail setup, legacy logs, and security boundaries.

## Default alerts

| Observation | Default behavior |
| --- | --- |
| CPU temperature ≥ 85°C [185°F] for 30 seconds | Warning |
| CPU temperature ≥ 92°C [197.6°F] in a fresh sample | Critical alert |
| Host CPU ≥ 80% for 5 minutes | Warning |
| Host CPU ≥ 8% briefly or a sensor ≥ 75°C [167°F] | Report event only |

These settings are **not universal hardware limits**. Review the thresholds in `/etc/default/proxmox-resource-watch` for your hardware. Alerts are observations, not a safety-critical protection system.

## Common commands

```bash
# Check the sampler and timers.
systemctl status proxmox-resource-watch.service --no-pager
systemctl list-timers 'proxmox-resource-*' --all

# Inspect report and alert service logs.
journalctl -u proxmox-resource-scheduled.service -n 30 --no-pager
journalctl -u proxmox-resource-alert.service -n 30 --no-pager

# Preview today's incomplete report without sending it.
python3 /usr/local/sbin/proxmox-resource-report.py --date "$(date +%F)"
```

## Documentation

- [Operations and technical reference](docs/OPERATIONS.md): installation, upgrades, security, alert behavior, Discord, Jellyfin, follow-ups, resource limits, and troubleshooting.
- [Guest activity, historical comparisons, and daily delivery](docs/GUEST_HISTORY_DELIVERY.md): configuration, coverage, retries, and rollback.
- [Release checklist](docs/RELEASE_CHECKLIST.md): testing and deployment acceptance.

Logs and reports can contain sensitive process or guest information. Keep the log directory restricted and share reports only with trusted recipients.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 tests/benchmark_report.py
bash -n install.sh
```

Tests use synthetic data and temporary directories; they do not operate on Proxmox or send real messages. See the [technical reference](docs/OPERATIONS.md#development-and-tests) for additional checks and benchmark details.

## License

[MIT](LICENSE).
