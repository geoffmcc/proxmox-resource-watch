# Proxmox Resource Watch

**v0.1.0 — Initial public preview**

A small Python 3 monitoring utility for Proxmox VE (or other Debian/Ubuntu Linux hosts). It samples host resources every 10 seconds, retains 30 days of daily JSONL logs, emails a readable daily summary, and checks for high-temperature/high-CPU alerts. It **does not** control fans or automatically shut down virtual machines, containers, or the host.

## Features

- Host-wide CPU percentage, 1/5/15-minute load, and up to five busiest observed processes.
- Available AMD GPU activity/VRAM counters, per-sensor temperatures and fan RPM.
- 30 days of dated logs, compression of older dates, and preservation of legacy v1 gist logs.
- Daily plain-text report at **07:00 host-local time** with Celsius and Fahrenheit (`83.0°C [181.4°F]`), top processes, sampling gaps, and brief/sustained spike analysis.
- Near-real-time warnings with a roughly 10-second check interval, durable 30-minute per-alert cooldown, and re-arm after temperature/CPU recovery.
- Native systemd services/timers, a conservative installer, and Python standard-library regression tests.

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
5. Restart the watcher, send one initial report as a mail-path test, check the alert service, and then enable **both** timers.
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

## Common commands

```bash
systemctl status proxmox-resource-watch.service --no-pager
systemctl list-timers proxmox-resource-report.timer proxmox-resource-alert.timer
journalctl -u proxmox-resource-report.service -n 30 --no-pager
journalctl -u proxmox-resource-alert.service -n 30 --no-pager

# Preview today's incomplete report without sending mail:
python3 /usr/local/sbin/proxmox-resource-report.py --date "$(date +%F)"

# Stop email timers without stopping collection:
systemctl disable --now proxmox-resource-report.timer proxmox-resource-alert.timer
```

## Development and tests

The production tools use the Python standard library. The regression suite also uses only `unittest`:

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
```

The tests use synthetic samples and temporary directories; they do **not** operate on Proxmox, send real email, or change production logs.

## Security and limitations

- Command lines can contain sensitive tokens or filenames. Log files and directories are restricted to root by default; **never publish raw monitoring logs**.
- The sampler runs as root to see processes and sensors. Systemd uses `NoNewPrivileges`, filesystem hardening, and limited writable paths.
- Mail services may require access to a local mail spool or Mutt-specific cache. The included unit files allow Postfix's usual maildrop location if present; review sandbox exceptions for your actual mail setup.
- Sensors may be absent, and a missing reading means **unavailable**, not zero. Fan RPM isn't exposed by all mini PCs.
- Retention removes matching dated logs beyond 30 calendar days. It does not touch unrelated files or the legacy gist log files. This is not a backup.
- If the machine or local mail system is unavailable, notifications can be delayed or lost; this isn't a safety-critical hardware protection system.

## License

MIT; see [LICENSE](LICENSE). Initial public preview, provided as-is.
