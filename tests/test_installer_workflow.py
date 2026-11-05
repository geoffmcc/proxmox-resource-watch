"""Offline installer rehearsal with relocated paths and disposable command shims.

The installer shell workflow and file copies really execute. This does not
emulate root ownership, a live systemd manager, Proxmox, or mail delivery.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
SCRIPTS = tuple(p.name for p in (SOURCE / 'src').glob('*.py'))
UNITS = tuple(p.name for p in (SOURCE / 'systemd').iterdir() if p.suffix in ('.service', '.timer'))


class InstallerWorkflow(unittest.TestCase):
    @staticmethod
    def old_script(name):
        return (SOURCE / 'src' / name).read_bytes() + b'\n# prior installed build\n'

    @staticmethod
    def old_unit(name):
        return (SOURCE / 'systemd' / name).read_bytes() + b'\n# prior installed build\n'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'source'
        shutil.copytree(SOURCE, self.repo, ignore=shutil.ignore_patterns('.git', '__pycache__'))
        self.sbin = self.root / 'usr/local/sbin'
        self.units = self.root / 'etc/systemd/system'
        self.config = self.root / 'etc/default/proxmox-resource-watch'
        self.logs = self.root / 'var/log/proxmox-resource-watch'
        self.backups = self.root / 'var/backups/proxmox-resource-watch'
        for path in (self.sbin, self.units, self.config.parent, self.logs.parent, self.backups):
            path.mkdir(parents=True, exist_ok=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'systemctl-calls'
        self.calls.touch()
        self._shim('systemctl', '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$PRW_CALL_LOG"
if [[ -n ${PRW_FAIL_SYSTEMCTL:-} && $* == *"$PRW_FAIL_SYSTEMCTL"* ]]; then exit 1; fi
''')
        self._shim('mutt', '#!/usr/bin/env bash\nexit 0\n')
        self._shim('chown', '#!/usr/bin/env bash\nexit 0\n')
        self._shim('stat', '''#!/usr/bin/env bash
if [[ -n ${PRW_FAKE_GID:-} && $1 == -c && $2 == %g ]]; then
  printf '%s\\n' "$PRW_FAKE_GID"
  exit 0
fi
exec /usr/bin/stat "$@"
''')
        self._shim('cp', '''#!/usr/bin/env bash
if [[ -n ${PRW_FAIL_BACKUP:-} && $* == *"$PRW_FAIL_BACKUP"* ]]; then exit 1; fi
exec /usr/bin/cp "$@"
''')
        self._shim('install', '''#!/usr/bin/env python3
import os, subprocess, sys
args = sys.argv[1:]
if os.environ.get('PRW_FAIL_INSTALL') and args[-1].endswith(os.environ['PRW_FAIL_INSTALL']):
    sys.exit(1)
filtered = []
i = 0
while i < len(args):
    if args[i] in ('-o', '-g'):
        i += 2
    else:
        filtered.append(args[i])
        i += 1
sys.exit(subprocess.run(['/usr/bin/install', *filtered], check=False).returncode)
''')
        paths = {
            '/usr/local/sbin': str(self.sbin),
            '/etc/systemd/system': str(self.units),
            '/etc/default/proxmox-resource-watch': str(self.config),
            '/var/log/proxmox-resource-watch': str(self.logs),
            '/var/backups/proxmox-resource-watch': str(self.backups),
        }
        installer = (self.repo / 'install.sh').read_text()
        for old, new in paths.items():
            installer = installer.replace(old, new)
        installer = installer.replace('[[ -d /run/systemd/system ]]', '[[ -d "$PROJECT_DIR" ]]')
        installer = installer.replace('((EUID == 0))', '((1 == 1))')
        (self.repo / 'install.sh').write_text(installer)
        helper = (self.repo / 'installer/preflight.sh').read_text()
        helper = helper.replace('owner == 0', f'owner == {os.getuid()}')
        helper = helper.replace('group == 0', f'group == {os.getgid()}')
        helper = helper.replace('[[ $part == / ]] && break', f'[[ $part == {self.root} ]] && break')
        (self.repo / 'installer/preflight.sh').write_text(helper)
        for directory in self.root.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o755)

    def _shim(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def run_install(self, *, args=('--yes',), **extra):
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', PRW_CALL_LOG=str(self.calls), **extra)
        return subprocess.run(['bash', str(self.repo / 'install.sh'), *args], env=env,
                              text=True, capture_output=True, timeout=30)

    def old_install(self):
        self.logs.mkdir(exist_ok=True)
        self.logs.chmod(0o755)
        (self.logs / 'history.jsonl').write_bytes(b'historical data\n')
        (self.logs / 'alert-state.json').write_bytes(b'{"last": 123}\n')
        (self.logs / 'alert-state.json').chmod(0o600)
        self.config.write_bytes(b'PRW_REPORT_TO=root\n# private synthetic config\n')
        self.config.chmod(0o600)
        for name in SCRIPTS:
            (self.sbin / name).write_bytes(self.old_script(name))
            (self.sbin / name).chmod(0o750)
        for name in UNITS:
            (self.units / name).write_bytes(self.old_unit(name))
            (self.units / name).chmod(0o644)
        dropin = self.units / 'proxmox-resource-watch.service.d'
        dropin.mkdir()
        dropin.chmod(0o755)
        (dropin / 'override.conf').write_bytes(b'[Service]\nEnvironment=PRW_INTERVAL=20\n')

    def backup(self):
        candidates = list(self.backups.iterdir())
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].stat().st_mode & 0o777, 0o700)
        return candidates[0]

    def test_fresh_install_and_repeat(self):
        first = self.run_install()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.logs.stat().st_mode & 0o777, 0o700)
        for name in SCRIPTS:
            self.assertEqual((self.sbin / name).read_bytes(), (SOURCE / 'src' / name).read_bytes())
        for name in UNITS:
            self.assertEqual((self.units / name).read_bytes(), (SOURCE / 'systemd' / name).read_bytes())
        self.assertIn('enable --now proxmox-resource-report.timer', self.calls.read_text())
        config_before = self.config.read_bytes()
        second = self.run_install()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual(len(list(self.backups.iterdir())), 2)

    def test_upgrade_preserves_private_state_and_dropins(self):
        self.old_install()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = self.backup()
        self.assertEqual((backup / 'proxmox-resource-watch.py').read_bytes(), self.old_script('proxmox-resource-watch.py'))
        self.assertEqual((backup / 'proxmox-resource-watch.service').read_bytes(), self.old_unit('proxmox-resource-watch.service'))
        self.assertEqual((backup / 'proxmox-resource-watch.service.d/override.conf').read_bytes(),
                         b'[Service]\nEnvironment=PRW_INTERVAL=20\n')
        self.assertEqual((backup / 'alert-state.json').read_bytes(), b'{"last": 123}\n')
        self.assertEqual((backup / self.config.name).read_bytes(), self.config.read_bytes())
        self.assertEqual((self.logs / 'history.jsonl').read_bytes(), b'historical data\n')
        self.assertEqual((self.logs / 'alert-state.json').read_bytes(), b'{"last": 123}\n')
        self.assertEqual((self.units / 'proxmox-resource-watch.service.d/override.conf').read_bytes(),
                         b'[Service]\nEnvironment=PRW_INTERVAL=20\n')

    def test_unsafe_existing_destinations_stop_before_service_changes(self):
        for location, kind in ((self.sbin / SCRIPTS[0], 'symlink'),
                               (self.units / UNITS[0], 'fifo'),
                               (self.config, 'symlink'),
                               (self.logs / 'alert-state.json', 'fifo'),
                               (self.units / 'proxmox-resource-watch.service.d', 'symlink')):
            with self.subTest(location=location, kind=kind):
                location.parent.mkdir(parents=True, exist_ok=True)
                if kind == 'symlink':
                    location.symlink_to(self.root / 'unrelated')
                else:
                    os.mkfifo(location)
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls.read_text(), '')
                self.assertEqual(list(self.backups.iterdir()), [])
                location.unlink()

    def test_socket_destination_rejected(self):
        location = self.sbin / SCRIPTS[1]
        try:
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(location))
        except PermissionError:
            self.skipTest('sandbox forbids creating UNIX sockets')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text(), '')
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_backup_directory_symlink_rejected(self):
        self.backups.rmdir()
        self.backups.symlink_to(self.root / 'unrelated')
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text(), '')
        self.assertFalse((self.root / 'unrelated').exists())

    def test_missing_required_destination_directory_stops_before_writes(self):
        for path in (self.sbin, self.units):
            with self.subTest(path=path):
                path.rmdir()
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(self.backups.iterdir()), [])
                self.assertEqual(self.calls.read_text(), '')
                self.assertFalse(self.logs.exists())
                path.mkdir()
                path.chmod(0o755)

    def test_writable_parent_and_backup_directory_rejected(self):
        for path in (self.sbin, self.units, self.logs, self.backups):
            with self.subTest(path=path):
                if path == self.logs:
                    path.mkdir(exist_ok=True)
                path.chmod(0o777)
                result = self.run_install()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls.read_text(), '')
                path.chmod(0o755)

    def test_group_writable_existing_file_rejected(self):
        target = self.sbin / SCRIPTS[0]
        target.write_text('old')
        target.chmod(0o666)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), 'old')
        self.assertEqual(self.calls.read_text(), '')

    def test_dry_run_checks_destinations_without_writing(self):
        good = self.run_install(args=('--dry-run',))
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertFalse(self.config.exists())
        self.sbin.chmod(0o777)
        bad = self.run_install(args=('--dry-run',))
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.calls.read_text(), '')

    def test_publicly_readable_config_rejected_without_changing_it(self):
        self.config.write_bytes(b'PRW_REPORT_TO=root\n')
        self.config.chmod(0o644)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.config.read_bytes(), b'PRW_REPORT_TO=root\n')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.calls.read_text(), '')

    def test_root_group_readable_config_is_preserved(self):
        self.config.write_bytes(b'PRW_REPORT_TO=root\n')
        self.config.chmod(0o640)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_bytes(), b'PRW_REPORT_TO=root\n')
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)

    def test_non_root_group_readable_config_rejected(self):
        self.config.write_bytes(b'PRW_REPORT_TO=root\n')
        self.config.chmod(0o640)
        result = self.run_install(PRW_FAKE_GID='65534')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(self.calls.read_text(), '')

    def test_backup_failure_precedes_all_replacements(self):
        self.old_install()
        result = self.run_install(PRW_FAIL_BACKUP='proxmox-resource-watch.py')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.sbin / 'proxmox-resource-watch.py').read_bytes(), self.old_script('proxmox-resource-watch.py'))
        self.assertEqual((self.units / 'proxmox-resource-watch.service').read_bytes(), self.old_unit('proxmox-resource-watch.service'))
        self.assertEqual(self.calls.read_text(), '')

    def test_retry_after_failed_upgrade_preserves_history(self):
        self.old_install()
        failed = self.run_install(PRW_FAIL_SYSTEMCTL='daemon-reload')
        self.assertNotEqual(failed.returncode, 0)
        recovered = self.run_install()
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual((self.logs / 'history.jsonl').read_bytes(), b'historical data\n')
        self.assertEqual((self.logs / 'alert-state.json').read_bytes(), b'{"last": 123}\n')
        self.assertEqual(self.config.read_bytes(), b'PRW_REPORT_TO=root\n# private synthetic config\n')

    def test_controlled_file_recovery_after_failed_upgrade(self):
        self.old_install()
        failed = self.run_install(PRW_FAIL_SYSTEMCTL='daemon-reload')
        self.assertNotEqual(failed.returncode, 0)
        backup = self.backup()
        for name in SCRIPTS:
            shutil.copy2(backup / name, self.sbin / name)
            self.assertEqual((self.sbin / name).read_bytes(), self.old_script(name))
        for name in UNITS:
            shutil.copy2(backup / name, self.units / name)
            self.assertEqual((self.units / name).read_bytes(), self.old_unit(name))
        self.assertEqual((self.logs / 'history.jsonl').read_bytes(), b'historical data\n')
        self.assertEqual((self.logs / 'alert-state.json').read_bytes(), b'{"last": 123}\n')
        self.assertEqual(self.config.read_bytes(), b'PRW_REPORT_TO=root\n# private synthetic config\n')
        self.assertEqual((self.units / 'proxmox-resource-watch.service.d/override.conf').read_bytes(),
                         b'[Service]\nEnvironment=PRW_INTERVAL=20\n')

    def assert_failed_upgrade(self, **failure):
        self.old_install()
        result = self.run_install(**failure)
        self.assertNotEqual(result.returncode, 0)
        backup = self.backup()
        self.assertEqual((backup / 'proxmox-resource-watch.py').read_bytes(), self.old_script('proxmox-resource-watch.py'))
        self.assertEqual((backup / 'proxmox-resource-watch.service.d/override.conf').read_bytes(),
                         b'[Service]\nEnvironment=PRW_INTERVAL=20\n')
        self.assertEqual((self.logs / 'history.jsonl').read_bytes(), b'historical data\n')
        self.assertEqual((self.logs / 'alert-state.json').read_bytes(), b'{"last": 123}\n')
        self.assertNotIn('enable --now', self.calls.read_text())
        # Controlled recovery restores code and units; live state is retained.
        shutil.copy2(backup / 'proxmox-resource-watch.py', self.sbin / 'proxmox-resource-watch.py')
        shutil.copy2(backup / 'proxmox-resource-watch.service', self.units / 'proxmox-resource-watch.service')
        self.assertEqual((self.sbin / 'proxmox-resource-watch.py').read_bytes(), self.old_script('proxmox-resource-watch.py'))

    def test_failed_script_install(self):
        self.assert_failed_upgrade(PRW_FAIL_INSTALL='proxmox-resource-watch.py')

    def test_failed_daemon_reload(self):
        self.assert_failed_upgrade(PRW_FAIL_SYSTEMCTL='daemon-reload')

    def test_failed_sampler_restart(self):
        self.assert_failed_upgrade(PRW_FAIL_SYSTEMCTL='restart proxmox-resource-watch.service')

    def test_failed_report_initialization(self):
        self.assert_failed_upgrade(PRW_FAIL_SYSTEMCTL='start proxmox-resource-report.service')

    def test_failed_alert_initialization(self):
        self.assert_failed_upgrade(PRW_FAIL_SYSTEMCTL='start proxmox-resource-alert.service')
