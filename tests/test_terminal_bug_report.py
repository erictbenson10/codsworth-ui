from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_terminal import terminal


class BugReportTests(unittest.TestCase):
    def test_report_sends_description_without_session_or_logs_and_records_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            app.transcript.append(('You','Private conversation'))
            app.input='keep my draft'
            result={'type':'bug_report','ok':True,'number':123,'url':'https://git.example/cods/issues/123'}
            with patch.object(app.client,'stream',return_value=iter([result])) as stream:
                app.file_bug('Playback cuts out')
                event=app.events.get(timeout=2);app.events.put(event);app.drain()
            args=stream.call_args.args
            self.assertEqual(args[0],'/api/bugs');self.assertEqual(args[1],{'description':'Playback cuts out'})
            self.assertEqual(app.input,'keep my draft')
            self.assertIn('#123',app.transcript[-1][1]);self.assertIsNone(app.bug_turn)

    def test_unknown_outcome_is_not_retried_or_claimed_success(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            with patch.object(app.client,'stream',side_effect=TimeoutError) as stream:
                app.file_bug('Broken control')
                event=app.events.get(timeout=2);app.events.put(event);app.drain()
            stream.assert_called_once()
            self.assertIn('before retrying',app.transcript[-1][1])
            self.assertNotEqual(app.status,'Bug filed')

    def test_cancelled_write_keeps_honest_notice_and_ignores_late_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            app.bug_turn=terminal.Turn(-4);app.bug_generation=1
            app.stop()
            app.events.put((-4,dict(token=1,result={'ok':True,'number':123,'url':'url'})))
            app.drain()
            self.assertIn('may already exist',app.transcript[-1][1])
            self.assertNotIn('Filed',app.transcript[-1][1])
