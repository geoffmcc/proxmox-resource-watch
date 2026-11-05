# Security policy

## Supported versions and environment

Security fixes target the current `main` branch and the latest reviewed release candidate. Older snapshots are not maintained as separate security branches. The supported installation environment is Proxmox VE or another Debian/Ubuntu-family system with systemd and Python 3.10 or later. Features that use local `pvesh` or LXC cgroups require a compatible Proxmox environment.

## Private reporting

Use GitHub's **Report a vulnerability** option on this repository when available. If it is unavailable, open a private communication channel with the repository owner before sharing exploit details; do not put credentials, host data, webhook URLs, or a working exploit against a live host in a public issue. No response deadline is promised.

If a webhook or other credential is suspected to have leaked, revoke or rotate it through its provider and inspect delivery history. Removing a value from Git history alone does not invalidate it.

## Production deployment

Review the exact source commit, installer, unit changes, and configuration in a pull request. Run the offline checks and rehearse installation in a disposable environment. Production installation remains an explicit administrator action. The CI workflows never connect to a Proxmox host. Keep config, logs, installed-file hashes, and unit/drop-in backups before upgrade. A passing CI run is not approval to deploy.
