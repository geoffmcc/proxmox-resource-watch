"""Offline transports only: no live webhook or mail calls."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
from email.parser import BytesParser
from email.policy import default

ROOT = Path(__file__).resolve().parents[1]
def load(filename, name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'src'/filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

report = load('proxmox-resource-report.py', 'discord_report_test')
alert = load('proxmox-resource-alert.py', 'discord_alert_test')
discord = report.discord
# Synthetic credential, never a real Discord endpoint.
URL = 'https://discord.com/api/webhooks/123456/offline_test_token'
ENV = dict(PRW_DISCORD_ENABLED='true', PRW_DISCORD_WEBHOOK_URL=URL, PRW_REPORT_TO='root')
T = dt.datetime(2026, 11, 3, 12, tzinfo=dt.timezone.utc)

def rows(values, cpu=15, start=T):
    return [(start+dt.timedelta(seconds=i*10), dict(time=(start+dt.timedelta(seconds=i*10)).isoformat(),
             temperatures_c={'cpu_k10temp': v} if v is not None else {}, host_cpu_busy_percent=cpu))
            for i, v in enumerate(values)]

class TransportTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.opener = mock.patch.object(discord.urllib.request, 'build_opener')
        self.build = self.opener.start()
        self.addCleanup(self.opener.stop)
        self.open = self.build.return_value.open
        response = self.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = b'{"id":"123"}'

    def send(self):
        return discord.send(discord.payload('WARNING', 'Host @everyone', 0xE67E22))

    def test_success_and_bounded_request(self):
        self.assertEqual(self.send(), (None, 0))
        request = self.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith('?wait=true'))
        self.assertEqual(self.open.call_args.kwargs['timeout'], 5)
        self.assertEqual(json.loads(request.data)['allowed_mentions']['parse'], [])
        self.assertIsInstance(self.build.call_args.args[1], discord.NoRedirect)

    def test_full_report_attachment_is_not_truncated(self):
        body = 'Complete report — CPU temperature\n' + ('detail\n' * 5000)
        self.open.return_value.__enter__.return_value.read.return_value = b'{"id":"123","attachments":[{"filename":"resource-report.md"}]}'
        self.assertEqual(discord.send(discord.payload('Daily', 'Summary', 1), daily=True, attachment=body), (None, 0))
        request = self.open.call_args.args[0]
        message = BytesParser(policy=default).parsebytes(
            b'Content-Type: ' + request.get_header('Content-type').encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.data)
        parts = list(message.iter_parts())
        self.assertEqual(parts[0].get_param('name', header='content-disposition'), 'payload_json')
        payload = json.loads(parts[0].get_payload(decode=True))
        self.assertEqual(payload['allowed_mentions']['parse'], [])
        self.assertIn('Summary', payload['content'])
        self.assertNotIn('embeds', payload)
        self.assertEqual(parts[1].get_filename(), 'resource-report.md')
        self.assertEqual(parts[1].get_payload(decode=True).decode('utf-8'), body)

    def test_missing_report_attachment_is_not_success(self):
        self.assertEqual(discord.send(discord.payload('Daily', 'Summary', 1), attachment='report')[0],
                         'Discord report attachment not confirmed')

    def test_disabled_and_legacy_configuration(self):
        for env in ({}, {'PRW_DISCORD_ENABLED':'false', 'PRW_DISCORD_WEBHOOK_URL':URL}):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(self.send(), (None, 0))
        self.open.assert_not_called()

    def test_missing_or_malformed_urls(self):
        for url in ('', 'http://discord.com/api/webhooks/1/token', URL+'?wait=true', URL+'\n',
                    URL.replace('discord.com', 'discord.com.evil.test'), URL.replace('discord.com','user@discord.com'),
                    URL.replace('discord.com', 'discord.com:443')):
            with self.subTest(url=url), mock.patch.dict(os.environ, PRW_DISCORD_WEBHOOK_URL=url):
                self.assertIn('configuration', self.send()[0])
        self.open.assert_not_called()

    def test_http_errors_and_no_credential_leaks(self):
        for code in (400, 429, 500, 302):
            with self.subTest(code=code):
                self.open.side_effect = HTTPError(URL, code, URL, {'Retry-After':'123'}, io.BytesIO(URL.encode()))
                error, delay = self.send()
                self.assertEqual(error, f'Discord HTTP {code}')
                self.assertNotIn('offline_test_token', error)
                self.assertEqual(delay, 123 if code == 429 else 60)

    def test_timeout_and_connection_errors(self):
        for exc in (TimeoutError(URL), OSError(URL), ValueError(URL)):
            self.open.side_effect = exc
            error, delay = self.send()
            self.assertNotIn(URL, error)
            self.assertEqual(delay, 60)

    def test_malformed_response(self):
        for body in (b'[]', b'{}', b'bad', b'{"id":1}', b'x'*65537):
            self.open.return_value.__enter__.return_value.read.return_value = body
            self.assertIsNotNone(self.send()[0])

    def test_sanitization_limits_unicode(self):
        data = discord.payload('@everyone <@123>\x00', '🔥\ud800\u202e'+URL+' '+('@here '*3000), 1)
        encoded = json.dumps(data)
        self.assertNotIn('offline_test_token', encoded)
        self.assertNotIn('@', encoded)
        self.assertLessEqual(len(data['embeds'][0]['description']), 1800)
        self.assertIn('🔥', data['embeds'][0]['description'])
        self.assertIsNone(discord.NoRedirect().redirect_request(None, None, 302, '', {}, URL))

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(mock.patch.dict(os.environ, ENV))
        self.stack.enter_context(mock.patch.object(alert, 'STATE', Path(self.temp)/'state'))
        self.recent = self.stack.enter_context(mock.patch.object(alert, 'recent', return_value=rows([99])))
        self.clock = self.stack.enter_context(mock.patch.object(alert.dt, 'datetime', wraps=dt.datetime))
        self.clock.now.return_value = T
        self.mail = self.stack.enter_context(mock.patch.object(alert.reporter, 'deliver'))
        self.send = self.stack.enter_context(mock.patch.object(alert.reporter.discord, 'send', return_value=(None,0)))

    def test_both_success_and_restart_cooldowns(self):
        alert.main()
        saved = alert.load_state()
        self.assertIn('temp_critical', saved)
        self.assertIn('discord_temp_critical', saved)
        alert.main() # reloads state from disk as a new invocation would
        self.assertEqual(self.mail.call_count, 1)
        self.assertEqual(self.send.call_count, 1)

    def test_mail_success_discord_failure_retries_only_discord(self):
        self.send.return_value = ('Discord connection or response failure',60)
        alert.main()
        self.assertNotIn('discord_temp_critical', alert.load_state())
        self.clock.now.return_value = T+dt.timedelta(seconds=60)
        self.recent.return_value = rows([99], start=T+dt.timedelta(seconds=60))
        self.send.return_value = (None, 0)
        alert.main()
        self.assertEqual(self.mail.call_count, 1)
        self.assertEqual(self.send.call_count, 2)

    def test_discord_success_mail_failure_retries_only_mail(self):
        self.mail.side_effect = subprocess.TimeoutExpired('mail',90)
        with self.assertRaises(subprocess.TimeoutExpired):
            alert.main()
        state = alert.load_state()
        self.assertNotIn('temp_critical', state)
        self.assertIn('discord_temp_critical', state)
        self.mail.side_effect = None
        alert.main()
        self.assertEqual(self.mail.call_count, 2)
        self.assertEqual(self.send.call_count, 1)

    def test_warning_then_immediate_critical_escalation(self):
        self.recent.return_value = rows([86]*3, start=T-dt.timedelta(seconds=20))
        alert.main()
        self.assertEqual(self.send.call_args.args[0]['embeds'][0]['color'], 0xE67E22)
        self.assertIn('🚨 ALERT', self.send.call_args.args[0]['embeds'][0]['description'])
        self.assertIn('\n\n🌡️ TEMPERATURE', self.send.call_args.args[0]['embeds'][0]['description'])
        self.recent.return_value = rows([99])
        alert.main()
        embed = self.send.call_args.args[0]['embeds'][0]
        self.assertEqual(embed['color'], 0xE74C3C)
        self.assertIn('CRITICAL', embed['title'])
        self.assertIn('99.0°C', embed['description'])
        self.assertIn('\n\n💻 CPU', embed['description'])
        self.assertEqual(self.send.call_count, 2)

    def test_warning_failure_does_not_delay_critical(self):
        self.recent.return_value = rows([86]*3, start=T-dt.timedelta(seconds=20))
        self.send.return_value = ('Discord HTTP 500', 60)
        alert.main()
        self.send.return_value = (None,0)
        self.recent.return_value = rows([99])
        alert.main()
        self.assertEqual(self.send.call_count, 2)

    def test_rate_limit_survives_restart(self):
        self.send.return_value = ('Discord HTTP 429', 120)
        alert.main()
        alert.main()
        self.assertEqual(self.send.call_count, 1)
        self.assertIn('discord_retry_after', alert.load_state())
        self.assertNotIn('discord_temp_critical', alert.load_state())

    def test_recovery_rearms_both_channels(self):
        alert.main()
        self.recent.return_value = rows([70]*8, start=T+dt.timedelta(seconds=10))
        self.clock.now.return_value = T+dt.timedelta(seconds=80)
        alert.main()
        self.assertNotIn('discord_temp_critical', alert.load_state())
        self.assertNotIn('temp_critical', alert.load_state())
        self.recent.return_value = rows([99], start=T+dt.timedelta(seconds=90))
        self.clock.now.return_value = T+dt.timedelta(seconds=90)
        alert.main()
        self.assertEqual(self.send.call_count, 2)

    def test_legacy_email_state_does_not_suppress_discord(self):
        alert.save_state({'temp_critical':T.isoformat()})
        alert.main()
        self.mail.assert_not_called()
        self.send.assert_called_once()

    def test_discord_only(self):
        with mock.patch.dict(os.environ, PRW_REPORT_TO=''):
            alert.main()
        self.mail.assert_not_called()
        self.send.assert_called_once()

class SummaryTests(unittest.TestCase):
    def test_email_wrapping_keeps_words_and_indentation(self):
        source = '    ' + ('long detail ' * 15).rstrip() + '\n'
        wrapped = report.wrap_report_email(source)
        self.assertTrue(all(len(line) <= 78 and line.startswith('    ') for line in wrapped.splitlines()))
        self.assertEqual(' '.join(source.split()), ' '.join(wrapped.split()))
        self.assertEqual(report.wrap_report_email('short\n'), 'short\n')

    def test_markdown_report_has_headings_and_safe_readable_details(self):
        body = ('Proxmox Resource Report | pve | 2026-11-04\n======\n\nWHAT HAPPENED\n-----\n'
                'A warning was observed.\n\nTEMPERATURES\n\n  cpu_sensor: 87.5°C\n'
                '  process <unsafe> [name]: 3%\n\nNOTES\n-----\nUnknown is not zero.\n')
        markdown = report.markdown_report(body)
        self.assertIn('# Proxmox Resource Report', markdown)
        self.assertIn('## WHAT HAPPENED', markdown)
        self.assertIn('## TEMPERATURES', markdown)
        self.assertIn('- cpu\\_sensor: 87.5°C', markdown)
        self.assertIn('&lt;unsafe&gt; \\[name\\]', markdown)
        self.assertIn('## NOTES', markdown)

    def test_shared_analysis_and_unchanged_email(self):
        sample_rows = rows([99, 99, 99])
        summary = {}
        with mock.patch.object(report, 'collect_evidence', return_value=([],[])):
            original = report.report('2026-11-03', sample_rows, 0)
            result = report.report('2026-11-03', sample_rows, 0, summary=summary)
        self.assertEqual(original, result)
        message = report.summary_message('2026-11-03', summary)
        self.assertIn('210.2°F', message['embeds'][0]['description'])
        text = message['embeds'][0]['description']
        self.assertIn('at or above the critical setting', text)
        self.assertIn('WHAT HAPPENED', original[1])
        self.assertIn('📊 COVERAGE', text)
        self.assertIn('\n\n💻 CPU\n', text)
        self.assertIn('\n\n🌡️ TEMPERATURE\n', text)
        self.assertIn('\n\n🔎 MONITORING HEALTH\n', text)
        self.assertIn('Clock uncertain: no', text)
        self.assertEqual(summary['thermal'], {'warning':1, 'critical':1})
        self.assertEqual(summary['sustained_cpu'], 1)

    def test_warning_overview_and_missing_readings_are_plain(self):
        with mock.patch.object(report, 'collect_evidence', return_value=([], [])):
            _, body = report.report('2026-11-03', rows([86]), 0)
        self.assertIn('below the critical setting', body)
        self.assertIn('actual duration is unknown', body)
        self.assertIn('No fan-speed readings were available', body)
        self.assertIn('FAN SPEEDS\n----------\n\n', body)
        self.assertIn('GUEST ACTIVITY\n--------------\n\n', body)
        self.assertIn('WHAT HAPPENED\n-------------\n\n', body)
        self.assertIn('TEMPERATURES\n------------\n\n', body)
        self.assertIn('BRIEF SPIKES (< 30s): 1; showing top 10\n------------\n\n', body)
        self.assertIn('WARNING: ongoing (unresolved)\n\n', body)
        self.assertIn('estimated core-seconds; one-core peak):\n\n', body)
        self.assertIn('range.\n\nCPU thresholds:', body)
        self.assertIn('daily peaks may not coincide with an event', body)

    def test_missing_data_is_unknown(self):
        summary = {}
        report.report('2026-11-03', [], 2, summary=summary)
        text = report.summary_message('2026-11-03', summary)['embeds'][0]['description']
        self.assertIn('unknown', text)
        self.assertNotIn('warning 0', text)

    def test_rows_without_measurements_have_unknown_event_counts(self):
        summary = {}
        with mock.patch.object(report, 'collect_evidence', return_value=([],[])):
            report.report('2026-11-03', rows([None]*3, cpu=None), 0, summary=summary)
        text = report.summary_message('2026-11-03', summary)['embeds'][0]['description']
        self.assertIn('unknown (CPU temperature unavailable)', text)
        self.assertIn('unknown (CPU unavailable)', text)
        self.assertNotIn('warning 0', text)

    def test_context_excludes_unrelated_workloads(self):
        evidence = ([], [dict(start=T-dt.timedelta(hours=1), end=T-dt.timedelta(minutes=30),
                             name='unrelated', status='completed', confidence='confirmed'),
                         dict(start=T, end=T+dt.timedelta(seconds=10), name='Jellyfin test',
                              status='completed', confidence='confirmed')])
        summary = {}
        with mock.patch.object(report, 'collect_evidence', return_value=evidence) as collect:
            report.report('2026-11-03', rows([99]*3), 0, summary=summary)
        collect.assert_called_once()
        text = report.summary_message('2026-11-03', summary)['embeds'][0]['description']
        self.assertIn('Jellyfin test', text)
        self.assertNotIn('unrelated', text)
        self.assertIn('correlation only', text)

    def test_test_mode_no_history_or_email(self):
        with mock.patch.dict(os.environ, ENV), mock.patch('sys.argv', ['report', '--discord-test']), \
             mock.patch.object(report.discord, 'send', return_value=(None,0)) as send, \
             mock.patch.object(report, 'rows_for') as history, mock.patch.object(report, 'deliver') as mail:
            report.main()
        self.assertIn('TEST', send.call_args.args[0]['embeds'][0]['title'])
        history.assert_not_called()
        mail.assert_not_called()

    def test_daily_transport_independence(self):
        for mail_fail, discord_fail in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(mail=mail_fail, discord=discord_fail), mock.patch.dict(os.environ, ENV), \
                 mock.patch.object(report, 'deliver', side_effect=RuntimeError('mail failed') if mail_fail else None) as mail, \
                 mock.patch.object(report, 'summary_message', return_value={}), \
                 mock.patch.object(report.discord, 'send', return_value=('Discord HTTP 500',60) if discord_fail else (None,0)) as send:
                if mail_fail:
                    with self.assertRaises(RuntimeError):
                        report.send_report('subject','body','mutt','2026-11-03',{})
                else:
                    report.send_report('subject','body','mutt','2026-11-03',{})
                mail.assert_called_once()
                send.assert_called_once()
                self.assertEqual(send.call_args.kwargs['attachment'], report.markdown_report(mail.call_args.args[1]))

if __name__ == '__main__':
    unittest.main()
