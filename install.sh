#!/usr/bin/env bash
# Proxmox Resource Watch installer — preserve existing configuration and logs.
set -Eeuo pipefail
umask 077

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DST=/usr/local/sbin
UNIT_DST=/etc/systemd/system
CONFIG_DST=/etc/default/proxmox-resource-watch
LOG_DST=/var/log/proxmox-resource-watch
DRY_RUN=0
ASSUME_YES=0

usage() {
  printf 'Usage: %s [--dry-run] [--yes]\n' "$0"
  printf '  --dry-run  Check prerequisites and show planned changes without writing.\n'
  printf '  --yes      Accept installation confirmation; use root as recipient on fresh installs.\n'
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

[[ -r /etc/os-release ]] || fail 'Cannot identify Linux distribution.'
# shellcheck disable=SC1091
source /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
  *debian*|*ubuntu*) ;;
  *) fail 'Supported host OS: Debian/Ubuntu (including Proxmox VE).';;
esac
for command_name in python3 systemctl install cp date; do
  command -v "$command_name" >/dev/null || fail "Missing required command: $command_name"
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || fail 'Python 3.10+ is required.'
[[ -d /run/systemd/system ]] || fail 'This host is not running systemd.'
[[ -f "$PROJECT_DIR/src/proxmox-resource-watch.py" ]] || fail 'Run installer from the complete repository.'

script_names=(prw_discord.py prw_followup.py proxmox-resource-report.py
              proxmox-resource-alert.py proxmox-resource-followup.py
              proxmox-resource-watch.py proxmox-jellyfin-export.py)
unit_names=(proxmox-resource-watch.service proxmox-resource-report.service proxmox-resource-report.timer
            proxmox-resource-alert.service proxmox-resource-alert.timer
            proxmox-jellyfin-export.service proxmox-jellyfin-export.timer
            proxmox-resource-followup.service proxmox-resource-followup.timer)
for item in "${script_names[@]}"; do
  [[ -r "$PROJECT_DIR/src/$item" ]] || fail "Missing src/$item"
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$PROJECT_DIR/src/$item" || fail "Invalid Python: $item"
done
for item in "${unit_names[@]}"; do
  [[ -r "$PROJECT_DIR/systemd/$item" ]] || fail "Missing systemd/$item"
done
[[ -r "$PROJECT_DIR/config/proxmox-resource-watch.conf.example" ]] || fail 'Missing example config.'

if [[ -f $CONFIG_DST ]]; then
  say "Existing configuration will be preserved: $CONFIG_DST"
else
  say 'New installation: default email is addressed to root via Mutt.'
fi
say 'Install will preserve existing logs, back up replaced files, restart only the watcher service,'
say 'send one initial report email and enable both daily-report and near-real-time alert timers.'
if ((DRY_RUN)); then
  say 'DRY RUN — no files, services, timers or email have been changed.'
  exit 0
fi
((EUID == 0)) || fail 'Run as root (or sudo) to install.'

if (( ! ASSUME_YES )); then
  read -r -p 'Proceed with installation? [y/N] ' answer
  [[ $answer == [yY] || $answer == [yY][eE][sS] ]] || fail 'Cancelled; no changes made.'
fi

# Choose the transport from the existing or new config, without executing config text.
# systemd reads EnvironmentFile syntax itself when the units start.
if [[ ! -f $CONFIG_DST ]]; then
  command -v mutt >/dev/null || fail 'Mutt is not installed. Install/configure a mailer or prepare an existing config before installing.'
fi

backup_dir="/var/backups/proxmox-resource-watch/$(date +%Y%m%d-%H%M%S)-$$"
install -d -o root -g root -m 0700 "$backup_dir"
# Shared upgrade policy: preserve records, enforce a private real directory.
secure_log_directory() {
  local target=$1
  [[ ! -L $target ]] || fail 'Log directory must not be a symlink.'
  if [[ ! -d $target ]]; then
    mkdir -m 0700 -- "$target"
  fi
  chown root:root -- "$target"
  chmod 0700 -- "$target"
}
secure_log_directory "$LOG_DST"
backup_if_exists() {
  local target=$1
  if [[ -e $target ]]; then
    cp -a -- "$target" "$backup_dir/$(basename -- "$target")"
  fi
}

for item in "${script_names[@]}"; do
  backup_if_exists "$SCRIPT_DST/$item"
  install -o root -g root -m 0750 "$PROJECT_DIR/src/$item" "$SCRIPT_DST/$item"
done
for item in "${unit_names[@]}"; do
  backup_if_exists "$UNIT_DST/$item"
  install -o root -g root -m 0644 "$PROJECT_DIR/systemd/$item" "$UNIT_DST/$item"
done
backup_if_exists "$CONFIG_DST"
backup_if_exists "$LOG_DST/alert-state.json"
preserve_configuration() {
  local source=$1 target=$2
  [[ ! -L $target ]] || fail 'Configuration must not be a symlink.'
  if [[ -e $target ]]; then
    [[ -f $target ]] || fail 'Configuration must be a regular file.'
  else
    install -o root -g root -m 0600 "$source" "$target"
  fi
}
preserve_configuration "$PROJECT_DIR/config/proxmox-resource-watch.conf.example" "$CONFIG_DST"

systemctl daemon-reload
systemctl enable proxmox-resource-watch.service
systemctl restart proxmox-resource-watch.service
systemctl is-active --quiet proxmox-resource-watch.service || fail 'Sampler did not start; inspect journalctl -u proxmox-resource-watch.service.'

# Validate report delivery before enabling recurring notifications.
say 'Sending one daily report now to verify the configured mail transport...'
systemctl start proxmox-resource-report.service || fail 'Email delivery failed; inspect journalctl -u proxmox-resource-report.service. Timers not enabled.'
systemctl start proxmox-resource-alert.service || fail 'Alert check failed; inspect journalctl -u proxmox-resource-alert.service. Timers not enabled.'
systemctl enable --now proxmox-resource-report.timer proxmox-resource-alert.timer proxmox-resource-followup.timer
systemctl is-active --quiet proxmox-resource-report.timer || fail 'Daily timer is not active.'
systemctl is-active --quiet proxmox-resource-alert.timer || fail 'Alert timer is not active.'

systemctl is-active --quiet proxmox-resource-followup.timer || fail 'Follow-up timer is not active.'

say 'Installation complete. Monitoring, primary notification timers and the optional context timer are active.'
say "Original logs were not deleted. Backups of replaced files: $backup_dir"
say 'Confirm the installation report arrived in your mailbox.'
systemctl list-timers proxmox-resource-report.timer proxmox-resource-alert.timer proxmox-resource-followup.timer --no-pager
