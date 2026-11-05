"""Disposable destination-type tests; no privileged host path is changed."""
import pathlib
import os
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HELPER = ROOT / 'installer' / 'preflight.sh'


class InstallerPreflight(unittest.TestCase):
    def check(self, function, path):
        return subprocess.run(['bash', '-c', 'source "$1"; "$2" "$3"', 'test',
                               str(HELPER), function, str(path)],
                              capture_output=True, text=True).returncode

    def test_existing_targets(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            regular = root / 'regular'
            regular.write_text('keep')
            missing = root / 'missing'
            symlink = root / 'link'
            symlink.symlink_to(regular)
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            # A file owned by the test user is intentionally rejected by the
            # production helper; the workflow rehearsal tests regular files.
            self.assertNotEqual(self.check('prw_safe_target_type', regular), 0)
            self.assertEqual(self.check('prw_safe_target_type', missing), 0)
            self.assertNotEqual(self.check('prw_safe_target_type', symlink), 0)
            self.assertNotEqual(self.check('prw_safe_target_type', fifo), 0)
            self.assertEqual(regular.read_text(), 'keep')

    def test_unprivileged_parent_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertNotEqual(self.check('prw_safe_parent', pathlib.Path(td) / 'file'), 0)

    def test_root_owned_protected_parent_accepted(self):
        # Read-only check of a standard root-controlled directory; no file is created.
        if os.stat('/etc/systemd/system').st_uid != 0:
            self.skipTest('sandbox maps host root to a nonzero uid')
        self.assertEqual(self.check('prw_safe_parent', pathlib.Path('/etc/systemd/system/prw-absent-test')), 0)


if __name__ == '__main__':
    unittest.main()
