"""Deterministic follow-up tests; HTTP/mail/evidence transports are mocked."""
import contextlib
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('followup_alert_test', ROOT/'src/proxmox-resource-alert.py')
alert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alert)
followups = alert.followups
report = alert.reporter
T = dt.datetime(2026, 11, 3, 12, tzinfo=dt.timezone.utc)


def samples(value=94, cpu=88, count=10):
    return [(T+dt.timedelta(seconds=i*10-30), dict(time=(T+dt.timedelta(seconds=i*10-30)).isoformat(),
            temperatures_c={'cpu_k10temp':value} if value is not None else {}, host_cpu_busy_percent=cpu,
            top_processes=[{'name':'kvm', 'pid':1, 'cpu_percent_one_core':50}])) for i in range(count)]


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.dict(os.environ, {'PRW_FOLLOWUP_ENABLED':'true',
            'PRW_DISCORD_ENABLED':'true', 'PRW_REPORT_TO':'root'}))
        self.state = {'temp_critical':T.isoformat(), 'discord_temp_critical':T.isoformat()}
        self.event = alert.candidate(T, [(T, samples()[0][1])])
        self.mail = self.stack.enter_context(mock.patch.object(report, 'deliver'))
        self.discord = self.stack.enter_context(mock.patch.object(report.discord, 'send', return_value=(None,0)))
        self.evidence = self.stack.enter_context(mock.patch.object(report, 'collect_evidence', return_value=([],[])))
        self.rows = samples()

    def queue(self, key='temp_critical'):
        self.event['key'] = key
        self.event['severity'] = 'CRITICAL' if key == 'temp_critical' else 'WARNING'
        followups.prepare(self.event, self.state)
        self.persist()
        followups.queue(T, self.event, self.state, self.directory)

    def persist(self):
        (self.directory/'alert-state.json').write_text(json.dumps(self.state))

    def run_jobs(self, now=T+dt.timedelta(seconds=60), loader=None):
        followups.run(report, self.directory/'followups', loader or (lambda:self.rows), now)

    def test_disabled_does_not_create_jobs(self):
        with mock.patch.dict(os.environ, PRW_FOLLOWUP_ENABLED='false'):
            self.queue()
        self.assertFalse((self.directory/'followups').exists())

    def test_due_after_one_minute_and_transport_independence(self):
        self.queue()
        self.run_jobs(T+dt.timedelta(seconds=59))
        self.mail.assert_not_called()
        self.discord.assert_not_called()
        self.run_jobs()
        self.mail.assert_called_once()
        self.discord.assert_called_once()
        self.evidence.assert_called_once() # Shared analysis across both channels.

    def test_restart_and_repeated_primary_do_not_repeat(self):
        self.queue()
        self.run_jobs()
        self.state = json.loads((self.directory/'alert-state.json').read_text())
        self.queue()
        self.run_jobs(T+dt.timedelta(minutes=31))
        self.mail.assert_called_once()
        self.discord.assert_called_once()

    def test_failed_email_does_not_suppress_discord_or_retry_email(self):
        self.queue()
        self.mail.side_effect = TimeoutError('transport')
        self.run_jobs()
        self.run_jobs()
        self.mail.assert_called_once()
        self.discord.assert_called_once()

    def test_failed_discord_does_not_repeat_email(self):
        self.queue()
        self.discord.return_value = ('Discord HTTP 500',60)
        self.run_jobs()
        self.run_jobs()
        self.mail.assert_called_once()
        self.discord.assert_called_once()

    def test_failed_primary_destination_gets_no_followup(self):
        self.state.pop('discord_temp_critical')
        self.queue()
        self.run_jobs()
        self.mail.assert_called_once()
        self.discord.assert_not_called()

    def test_late_primary_destination_queues_one_followup(self):
        self.state.pop('discord_temp_critical')
        self.queue()
        self.run_jobs()
        self.state['discord_temp_critical'] = (T+dt.timedelta(seconds=70)).isoformat()
        followups.queue(T+dt.timedelta(seconds=70), self.event, self.state, self.directory)
        self.run_jobs(T+dt.timedelta(seconds=129))
        self.discord.assert_not_called()
        self.run_jobs(T+dt.timedelta(seconds=130))
        self.discord.assert_called_once()
        self.mail.assert_called_once()

    def test_warning_escalation_shares_thermal_episode_and_uses_critical(self):
        self.state = {'temp_warning':T.isoformat(), 'discord_temp_warning':T.isoformat()}
        self.queue('temp_warning')
        token = self.state['followup_temp_episode']
        self.state.update(temp_critical=T.isoformat(), discord_temp_critical=T.isoformat())
        self.queue('temp_critical')
        self.assertEqual(token, self.state['followup_temp_episode'])
        self.assertEqual(len(list((self.directory/'followups').glob('*.json'))),2)
        self.run_jobs()
        self.assertIn('CRITICAL', self.mail.call_args.args[0])
        self.assertIn('CRITICAL', self.discord.call_args.args[0]['embeds'][0]['title'])

    def test_no_second_followup_for_escalation_after_warning_followup(self):
        self.state = {'temp_warning':T.isoformat(), 'discord_temp_warning':T.isoformat()}
        self.queue('temp_warning')
        self.run_jobs()
        self.state.update(temp_critical=T.isoformat(), discord_temp_critical=T.isoformat())
        self.queue('temp_critical')
        self.run_jobs()
        self.mail.assert_called_once()
        self.discord.assert_called_once()

    def test_recovery_rearms_new_thermal_episode_without_losing_pending_job(self):
        self.queue()
        old = self.state['followup_temp_episode']
        self.state['temp_sensors'] = ['cpu_k10temp']
        cool = report.normalize_rows(samples(value=70, cpu=10))
        alert.update_recovery(cool[-1][0], cool, self.state)
        self.assertNotIn('followup_temp_episode', self.state)
        self.run_jobs() # Pending context still sent after recovery.
        self.state.update(temp_critical=T.isoformat(), discord_temp_critical=T.isoformat())
        self.queue()
        self.assertNotEqual(old,self.state['followup_temp_episode'])
        self.run_jobs()
        self.assertEqual(self.mail.call_count,2)
        self.assertEqual(self.discord.call_count,2)

    def test_cpu_warning_episode_is_separate(self):
        self.queue()
        thermal = self.state['followup_temp_episode']
        self.state['cpu_warning'] = T.isoformat()
        self.state['discord_cpu_warning'] = T.isoformat()
        self.queue('cpu_warning')
        self.assertNotEqual(thermal,self.state['followup_cpu_episode'])
        self.run_jobs()
        self.run_jobs()
        self.assertEqual(self.mail.call_count,2)
        self.assertEqual(self.discord.call_count,2)

    def test_analysis_failure_sends_unknown_without_retries(self):
        self.queue()
        self.run_jobs(loader=mock.Mock(side_effect=OSError('private URL must not be logged')))
        self.assertIn('Cause: unknown', self.mail.call_args.args[1])
        self.assertNotIn('private URL', self.mail.call_args.args[1])
        self.discord.assert_called_once()

    def test_evidence_failure_and_missing_samples_are_explicit(self):
        self.evidence.return_value = (['Workload evidence timed out (activity unknown)'], [])
        self.queue()
        self.rows = []
        self.run_jobs()
        body = self.mail.call_args.args[1]
        self.assertIn('Cause: unknown',body)
        self.assertIn('Sampling incomplete',body)
        self.assertIn('activity unknown',body)
        self.assertIn('unavailable',body)

    def test_overlap_and_correlation_are_not_claimed_as_cause(self):
        self.evidence.return_value = ([],[
            dict(name='vzdump backup',start=T,end=T+dt.timedelta(seconds=30),confidence='confirmed',status='completed'),
            dict(name='Jellyfin task',start=T,end=None,confidence='possible',status='running')])
        self.queue()
        self.run_jobs()
        body = self.mail.call_args.args[1]
        self.assertIn('Confirmed workload overlap',body)
        self.assertIn('Possible workload correlation',body)
        self.assertIn('Cause: unknown',body)
        discord_body = self.discord.call_args.args[0]['embeds'][0]['description']
        self.assertIn('🔎 OBSERVATION', discord_body)
        self.assertIn('\n\n📊 COVERAGE', discord_body)
        self.assertIn('\n\n💻 CPU', discord_body)
        self.assertIn('\n\n🌡️ TEMPERATURE', discord_body)
        self.assertIn('\n\n📋 WORKLOAD CONTEXT', discord_body)
        self.assertNotIn('🔎 OBSERVATION', body)

    def test_long_discord_followup_includes_complete_attachment(self):
        long_body = 'Cause: unknown.\n' + ('Guest activity detail.\n' * 120) + 'Final context survives.'
        self.queue()
        with mock.patch.object(followups, 'context', return_value=('WARNING', long_body)):
            self.run_jobs()
        message = self.discord.call_args.args[0]
        attachment = self.discord.call_args.kwargs['attachment']
        self.assertIn('Full follow-up attached', message['embeds'][0]['description'])
        self.assertIn('Final context survives.', attachment)
        self.assertIn('Final context survives.', self.mail.call_args.args[1])

    def test_mentions_secrets_and_raw_commands_excluded(self):
        self.rows[0][1]['top_processes'][0].update(name='@everyone',cmdline='secret process command')
        self.evidence.return_value = (['https://discord.com/api/webhooks/123/private_token @everyone'],[])
        self.queue()
        self.run_jobs()
        message = self.discord.call_args.args[0]
        body = json.dumps(message)
        self.assertNotIn('private_token',body)
        self.assertNotIn('secret process command',body)
        self.assertNotIn('@everyone',body)
        self.assertEqual(message['allowed_mentions']['parse'],[])

    def test_expired_job_is_not_sent(self):
        self.queue()
        self.run_jobs(T+dt.timedelta(hours=1))
        self.mail.assert_not_called()
        self.discord.assert_not_called()

    def test_claim_before_send_prevents_retry_after_crash(self):
        self.queue()
        path = next((self.directory/'followups').glob('*.email.json'))
        path.with_suffix('.attempted').write_text('prior attempt')
        self.run_jobs()
        self.mail.assert_not_called()
        self.discord.assert_called_once()

    def test_rate_limit_skips_discord_but_still_sends_email(self):
        self.state['discord_retry_after'] = (T+dt.timedelta(seconds=120)).isoformat()
        self.queue()
        self.run_jobs()
        self.mail.assert_called_once()
        self.discord.assert_not_called()

    def test_jobs_and_attempt_markers_private(self):
        self.queue()
        self.run_jobs()
        directory = self.directory/'followups'
        self.assertEqual(directory.stat().st_mode & 0o777,0o700)
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

    def test_does_not_retroactively_queue_prior_installation_alerts(self):
        followups.prepare(self.event,self.state,delivered=False)
        followups.queue(T,self.event,self.state,self.directory)
        self.assertFalse((self.directory/'followups').exists())

    def test_queue_failure_cannot_suppress_primary_delivery_or_state(self):
        with mock.patch.object(alert,'STATE',self.directory/'alert-state.json'), \
             mock.patch.object(alert,'LOG_DIR',self.directory), \
             mock.patch.object(alert,'recent',return_value=[(T,samples()[0][1])]), \
             mock.patch.object(alert.dt,'datetime',wraps=dt.datetime) as clock, \
             mock.patch.object(followups,'queue',side_effect=OSError('queue unavailable')):
            clock.now.return_value=T
            alert.main()
        self.mail.assert_called_once()
        self.discord.assert_called_once()
        saved=json.loads((self.directory/'alert-state.json').read_text())
        self.assertIn('temp_critical',saved)
        self.assertIn('discord_temp_critical',saved)
        self.assertIn('followup_temp_episode',saved)

    def test_queue_failure_retries_same_identity_without_duplicate(self):
        followups.prepare(self.event,self.state)
        token=self.state['followup_temp_episode']
        self.persist()
        followups.prepare(self.event,self.state,delivered=False)
        self.assertEqual(self.state['followup_temp_episode'],token)
        followups.queue(T,self.event,self.state,self.directory)
        self.run_jobs()
        followups.queue(T,self.event,self.state,self.directory)
        self.run_jobs()
        self.mail.assert_called_once()
        self.discord.assert_called_once()

    def test_primary_state_must_save_before_any_job_is_published(self):
        with mock.patch.object(alert,'recent',return_value=[(T,samples()[0][1])]), \
             mock.patch.object(alert,'save_state',side_effect=OSError('disk full')), \
             mock.patch.object(followups,'queue') as queue, \
             mock.patch.object(alert.dt,'datetime',wraps=dt.datetime) as clock:
            clock.now.return_value=T
            with self.assertRaises(OSError):
                alert.main()
        self.mail.assert_called_once()
        self.discord.assert_called_once()
        queue.assert_not_called()
        self.evidence.assert_not_called()

    def test_worker_does_not_change_primary_state(self):
        self.queue()
        before=(self.directory/'alert-state.json').read_bytes()
        self.run_jobs()
        self.assertEqual(before,(self.directory/'alert-state.json').read_bytes())

    def test_symlink_queue_directory_rejected(self):
        followups.prepare(self.event,self.state)
        elsewhere=self.directory/'elsewhere'
        elsewhere.mkdir()
        (self.directory/'followups').symlink_to(elsewhere)
        with self.assertRaises(OSError):
            followups.queue(T,self.event,self.state,self.directory)
        self.assertEqual(list(elsewhere.iterdir()),[])

    def test_failed_primary_email_still_queues_discord_context(self):
        with mock.patch.object(alert,'STATE',self.directory/'alert-state.json'), \
             mock.patch.object(alert,'LOG_DIR',self.directory), \
             mock.patch.object(alert,'recent',return_value=[(T,samples()[0][1])]), \
             mock.patch.object(alert.dt,'datetime',wraps=dt.datetime) as clock:
            self.mail.side_effect=TimeoutError('mail timeout')
            clock.now.return_value=T
            with self.assertRaises(TimeoutError):
                alert.main()
        jobs=list((self.directory/'followups').glob('*.json'))
        self.assertEqual(len(jobs),1)
        self.assertTrue(jobs[0].name.endswith('.discord.json'))
        self.evidence.assert_not_called()

    def test_delay_starts_after_slow_primary_transports_finish(self):
        with mock.patch.object(alert,'STATE',self.directory/'alert-state.json'), \
             mock.patch.object(alert,'LOG_DIR',self.directory), \
             mock.patch.object(alert,'recent',return_value=[(T,samples()[0][1])]), \
             mock.patch.object(alert.dt,'datetime',wraps=dt.datetime) as clock:
            clock.now.side_effect=[T,T+dt.timedelta(seconds=90)]
            alert.main()
        for path in (self.directory/'followups').glob('*.json'):
            self.assertEqual(report.timestamp(json.loads(path.read_text())['due']),T+dt.timedelta(seconds=150))



if __name__ == '__main__':
    unittest.main()
