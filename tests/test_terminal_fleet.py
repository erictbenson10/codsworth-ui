import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
from test_terminal import terminal
from fleet_view import overview_text


SNAPSHOT = {'type': 'fleet_snapshot', 'observed_at': '2026-09-07T21:00:00Z',
            'available': dict(health=True, status=True, attempts=True, questions=True),
            'health': dict(daemon='up', paused=False), 'counts': dict(running=1, queued=2),
            'attempts': [dict(id='run1.2', name='Fix the widget', state='working')],
            'questions': [dict(id='q17', attempt_id='run1.2', text='Which option?', options=['Keep', 'Replace'])],
            'errors': []}


class FleetViewTests(unittest.TestCase):
    def app(self, directory):
        return terminal.Terminal('http://127.0.0.1:1', muted=True, state_path=Path(directory)/'state')

    def test_overview_renders_exact_ids_and_question_options(self):
        text = overview_text(SNAPSHOT)
        self.assertIn('Checked Sep 07,', text)
        self.assertNotIn('T21:00:00Z', text)
        for value in ('Running: 1', 'Queued: 2', 'run1.2', '[q17]', '1. Keep', '2. Replace'):
            self.assertIn(value, text)
        self.assertNotIn('Running:', overview_text(SNAPSHOT, True))

    def test_failed_question_fetch_does_not_claim_empty_inbox(self):
        text = overview_text(dict(available={'questions': False}, questions=[], errors=['unavailable']))
        self.assertIn('Questions unavailable', text)
        self.assertNotIn('No open Fleet questions', text)
        self.assertIn('No open Fleet questions', overview_text(dict(available={'questions': True}, questions=[])))

    def test_unknown_counts_and_truncation_are_explicit(self):
        snapshot = dict(available=dict(health=True, status=True, questions=True),
            health={'daemon': 'up', 'paused': None}, counts={'running': None, 'queued': None},
            questions=[], truncated={'attempts': False, 'questions': False})
        text = overview_text(snapshot)
        self.assertIn('admissions unknown', text)
        self.assertIn('Running: ?', text)
        self.assertNotIn('More results', text)
        snapshot['questions'] = [dict(id='q1', text='Choose', options=['Abbreviated'], options_truncated=True)]
        self.assertIn('Options abbreviated', overview_text(snapshot))
        snapshot['questions'] = []
        snapshot['errors'] = ['Some Fleet questions were invalid and omitted']
        self.assertNotIn('No open Fleet questions', overview_text(snapshot))

    def test_async_refresh_keeps_draft_and_does_not_start_model_or_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory); app.input = 'keep this draft'
            fetched = threading.Event()
            def stream(path, payload, turn):
                self.assertEqual(path, '/api/fleet/overview')
                self.assertEqual(payload, {})
                yield SNAPSHOT
                fetched.set()
            with patch.object(app.client, 'stream', side_effect=stream), patch.object(app.client, 'submit') as model:
                app.command('/fleet');self.assertTrue(fetched.wait(2));app.drain()
            model.assert_not_called()
            self.assertEqual(app.input, 'keep this draft')
            self.assertEqual(app.transcript[-1][0], 'Fleet')
            self.assertIn('[q17]', app.transcript[-1][1])
            self.assertEqual(app.phase, 'ready')

    def test_cancel_discards_late_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory);entered=threading.Event();release=threading.Event()
            def stream(*args):
                entered.set();release.wait(2);yield SNAPSHOT
            with patch.object(app.client, 'stream', side_effect=stream):
                app.refresh_fleet();self.assertTrue(entered.wait(2));old=app.fleet_generation
                app.stop();release.set()
                app.events.put((-3, dict(type='fleet_snapshot', token=old, snapshot=SNAPSHOT, questions_only=False)))
                app.drain()
            self.assertFalse(any(role=='Fleet' for role,text in app.transcript))

    def test_error_preserves_conversation_and_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory);app.input='draft'
            app.events.put((-3, dict(type='fleet_error',token=app.fleet_generation,message='Service HTTP 503')))
            app.drain()
            self.assertEqual(app.input,'draft');self.assertEqual(app.phase,'ready')
            self.assertIn('Could not refresh Fleet',app.transcript[-1][1])

    def test_empty_or_wrong_protocol_becomes_visible_error(self):
        for response in ([], [{'detail': 'unexpected'}]):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                app = self.app(directory)
                with patch.object(app.client, 'stream', return_value=iter(response)):
                    app.refresh_fleet()
                    event = app.events.get(timeout=2)
                    self.assertEqual(event[1]['type'], 'fleet_error')
                    app.events.put(event)
                    app.drain()
                self.assertIn('Could not refresh Fleet', app.transcript[-1][1])

    def test_refresh_does_not_steal_streamed_assistant_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(directory);app.active_id=3
            app.events.put((-3, dict(type='fleet_snapshot',token=0,snapshot=SNAPSHOT,questions_only=True)))
            app.events.put((3, dict(type='text',delta=' Continue response.')))
            app.drain()
            self.assertTrue(app.transcript[0][1].endswith(' Continue response.'))
            self.assertEqual(app.transcript[-1][0],'Fleet questions')
