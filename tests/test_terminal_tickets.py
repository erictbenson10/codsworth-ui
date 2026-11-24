from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_terminal import terminal


class TicketInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = terminal.Terminal('http://localhost:1', True, Path(self.temp.name)/'state')

    def drain_worker(self):
        event = self.app.events.get(timeout=2)
        self.app.events.put(event)
        self.app.drain()

    def test_read_only_detail_preserves_draft_and_exposes_link(self):
        self.app.input = 'unfinished draft'
        result = dict(type='ticket_inbox', ok=True, tickets=[dict(number=3, title='Playback', state='open',
            url='https://git.example.com/example/codsworth/issues/3', body='Cuts off', body_truncated=True)])
        with patch.object(self.app.client, 'stream', return_value=iter([result])) as stream, patch.object(self.app.client, 'submit') as submit:
            self.app.command('/tickets 3')
            self.drain_worker()
            self.assertEqual(stream.call_args.args[:2], ('/api/tickets', {'issue':3}))
            submit.assert_not_called()
        self.assertEqual(self.app.input, 'unfinished draft')
        self.assertIn('Cuts off', self.app.transcript[-1][1])
        self.assertIn('abbreviated', self.app.transcript[-1][1])
        self.app.command('/links')
        self.assertIn('/issues/3', self.app.panel[1])

    def test_failed_read_is_not_empty_inbox(self):
        with patch.object(self.app.client, 'stream', side_effect=TimeoutError):
            self.app.command('/tickets')
            self.drain_worker()
        self.assertIn('Could not load', self.app.transcript[-1][1])
        self.assertNotIn('No matching', self.app.transcript[-1][1])

    def test_pagination_and_empty_result(self):
        for more in (True, False):
            result=dict(type='ticket_inbox',ok=True,tickets=[],state='closed',page=2,possibly_more=more)
            with patch.object(self.app.client,'stream',return_value=iter([result])) as stream:
                self.app.command('/tickets closed 2'); self.drain_worker()
                self.assertEqual(stream.call_args.args[1],{'state':'closed','page':2})
            self.assertIn('/issues closed 3' if more else 'No matching', self.app.transcript[-1][1])

    def test_invalid_and_cancelled_requests(self):
        with patch.object(self.app.client,'stream') as stream:
            for text in ('/tickets 0','/tickets all 10001','/tickets open wrong','/tickets 2 3'):
                self.app.command(text)
            stream.assert_not_called()
        self.app.ticket_turn=terminal.Turn(-6); self.app.ticket_generation=1
        self.app.stop()
        self.app.events.put((-6,dict(token=1,result=dict(ok=True,tickets=[]))))
        self.app.drain()
        self.assertFalse(any(role=='Corner issues' for role,_ in self.app.transcript))

    def test_explicit_maintenance_command_preserves_scope_in_pagination(self):
        result=dict(type='ticket_inbox',ok=True,tickets=[],state='open',page=1,possibly_more=True)
        with patch.object(self.app.client,'stream',return_value=iter([result])) as stream:
            self.app.command('/bugs');self.drain_worker()
        self.assertEqual(stream.call_args.args[1],{'repo':'example/codsworth'})
        self.assertEqual(self.app.transcript[-1][0],'Codsworth maintenance')
        self.assertIn('/bugs open 2',self.app.transcript[-1][1])

    def test_combined_inbox_uses_no_model_and_keeps_draft(self):
        result=dict(type='work_inbox',available={'issues':True,'pulls':True},issues=[],pulls=[],errors=[])
        self.app.input='Keep this draft'
        with patch.object(self.app.client,'stream',return_value=iter([result])) as stream,patch.object(self.app.client,'submit') as submit:
            self.app.command('/inbox'); self.drain_worker()
            self.assertEqual(stream.call_args.args[:2],('/api/inbox',{}))
            submit.assert_not_called()
        self.assertEqual(self.app.input,'Keep this draft')
        self.assertEqual(self.app.transcript[-1][0],'Corner work inbox')
        self.assertIn('No open issues',self.app.transcript[-1][1])
