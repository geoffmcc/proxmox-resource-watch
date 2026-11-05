"""Document validator limits against documented systemd EnvironmentFile syntax."""
from pathlib import Path
import contextlib
import io
from unittest import mock
import sys
import tempfile
import unittest

from test_guest_history_daily import config


class ConfigCompatibility(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config'
            path.write_text(text)
            return config.load(path)

    def test_common_existing_assignments(self):
        values, warnings = self.load_text(
            '# comment\n; another comment\n\nPRW_REPORT_TO="root"\n'
            'PRW_MAILER=sendmail\nPRW_REPORT_TO=root\n'
            'PRW_FOLLOWUP_ENABLED=false\nPRW_INTERVAL=10\n'
            'PRW_JELLYFIN_LOG="/var/log/proxmox-resource-watch/media log"\n')
        self.assertEqual(values['PRW_REPORT_TO'], 'root')
        self.assertEqual(values['PRW_JELLYFIN_LOG'], '/var/log/proxmox-resource-watch/media log')
        self.assertEqual(len(warnings), 1)  # Last duplicate wins, as in systemd.
        self.assertEqual(config.validate(values)[0], [])

    def test_systemd_ignored_line_is_rejected_by_custom_parser(self):
        # systemd.exec(5): lines without '=' are ignored. This validator gates
        # more narrowly, so it cannot safely be mandatory for old configs.
        with self.assertRaises(ValueError):
            self.load_text('PRW_REPORT_TO=root\nlegacy note without assignment\n')

    def test_systemd_unquoted_backslash_and_multiline_differ(self):
        # systemd removes the backslash from an unquoted escaped space and
        # allows multiline quoted values; this parser does neither reliably.
        values, _ = self.load_text('PRW_REPORT_TO=root\nPRW_JELLYFIN_LOG=/var/log/media\\ log\n')
        self.assertEqual(values['PRW_JELLYFIN_LOG'], '/var/log/media\\ log')
        with self.assertRaises(ValueError):
            self.load_text('PRW_REPORT_TO=root\nUNUSED="line one\nline two"\n')

    def test_shell_text_never_executes_and_errors_redact_value(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'marker'
            secret = f'$(touch {marker})'
            values, _ = self.load_text(f'PRW_REPORT_TO=root\nUNUSED={secret}\n')
            self.assertEqual(values['UNUSED'], secret)
            self.assertFalse(marker.exists())
            errors, warnings = config.validate(values)
            self.assertFalse(errors)
            self.assertNotIn(secret, str(warnings))
            self.assertTrue(config.validate({'PRW_REPORT_TO': 'root', 'PRW_LOG_DIR': '/tmp'})[0])

    def test_cli_error_never_prints_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config'
            secret = 'synthetic-secret-never-print'
            path.write_text(f'PRW_REPORT_TO="unterminated-{secret}\n')
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['check-config', '--config', str(path)]), contextlib.redirect_stdout(output):
                self.assertEqual(config.main(), 2)
            self.assertIn('details omitted', output.getvalue())
            self.assertNotIn(secret, output.getvalue())


if __name__ == '__main__':
    unittest.main()
