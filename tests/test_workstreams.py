import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import uuid

from agent.workstreams import WorkstreamManager, DisabledWorkstreams


class FakeClient:
    def __init__(self, events):
        self.events = events
        self.settings = SimpleNamespace(muted=False)
        self.sequence = 0
        self.cancelled = []
        self.sent = []

    def submit(self, text):
        self.sequence += 1
        self.sent.append(text)
        return self.sequence

    def interrupt(self, reason):
        self.cancelled.append(reason)

    def emit(self, kind, **values):
        self.events.put((self.sequence, dict(type=kind, **values)))


class WorkstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'streams.json'
        self.clients = []
        self.manager = WorkstreamManager(self.path, self.factory, {'cwd': '/repo', 'task_records': [{'id': 'x'}]})

    def factory(self, events):
        client = FakeClient(events)
        self.clients.append(client)
        return client

    def test_concurrent_sessions_are_independent_and_stop_is_scoped(self):
        a = self.manager.create('Feature A')
        self.manager.send('Implement A')
        b = self.manager.create('Bug B')
        self.manager.send('Investigate B')
        ids = [uuid.uuid4().hex, uuid.uuid4().hex]
        for index, client in enumerate(self.clients):
            client.emit('session', session_id=ids[index])
            client.emit('text', delta=f'Answer {index}')
        self.manager.poll()
        self.assertEqual([a['session_id'], b['session_id']], ids)
        self.assertEqual(a['transcript'][-1], ('Codsworth', 'Answer 0'))
        self.assertEqual(b['transcript'][-1], ('Codsworth', 'Answer 1'))
        self.assertTrue(a['unread'])
        self.assertFalse(b['unread'])
        self.manager.stop(a['id'])
        self.assertEqual(self.clients[0].cancelled, ['workstream_stop'])
        self.assertEqual(self.clients[1].cancelled, [])
        self.assertEqual(b['status'], 'running')

    def test_work_duration_is_per_stream_and_survives_reload(self):
        with patch('agent.workstreams.time.monotonic',return_value=100):
            a=self.manager.create('A');self.manager.send('first')
        with patch('agent.workstreams.time.monotonic',return_value=120):
            b=self.manager.create('B');self.manager.send('second')
        with patch('agent.workstreams.time.monotonic',return_value=175):
            for client in self.clients:client.emit('complete')
            self.manager.poll()
        self.assertEqual(a['transcript'][-1],('Worked for','1m 15s'))
        self.assertEqual(b['transcript'][-1],('Worked for','55s'))
        restored=WorkstreamManager(self.path,self.factory)
        self.assertEqual(restored.find(a['id'])['transcript'][-1],('Worked for','1m 15s'))

    def test_busy_send_refused_without_cancelling(self):
        self.manager.create('A')
        self.manager.send('first')
        with self.assertRaises(ValueError):
            self.manager.send('second')
        self.assertEqual(self.clients[0].sent, ['first'])
        self.assertEqual(self.clients[0].cancelled, [])

    def test_only_real_completion_marks_completed_and_errors_survive_complete(self):
        record = self.manager.create('A')
        self.manager.send('go')
        self.clients[0].emit('text', delta='I am done')
        self.manager.poll()
        self.assertEqual(record['status'], 'running')
        self.clients[0].emit('error', message='Timed out')
        self.clients[0].emit('complete')
        self.manager.poll()
        self.assertEqual(record['status'], 'error')
        self.manager.send('try again')
        self.clients[0].emit('complete')
        self.manager.poll()
        self.assertEqual(record['status'], 'completed')

    def test_restart_preserves_draft_session_focus_but_never_reruns(self):
        record = self.manager.create('A')
        self.manager.send('go')
        native = uuid.uuid4().hex
        self.clients[0].emit('session', session_id=native)
        self.manager.poll()
        self.manager.set_draft('next request')
        restored = WorkstreamManager(self.path, self.factory)
        self.assertEqual(len(self.clients), 1)
        loaded = restored.find(record['id'][:8])
        self.assertEqual(loaded['status'], 'interrupted')
        self.assertEqual(loaded['draft'], 'next request')
        self.assertEqual(loaded['session_id'], native)
        self.assertEqual(restored.selected, record['id'])
        restored.send('continue')
        self.assertEqual(self.clients[-1].session_id, native)

    def test_private_atomic_persistence_and_stale_writer_refusal(self):
        a = self.manager.create('A')
        other = WorkstreamManager(self.path, self.factory)
        self.manager.set_draft('new')
        with self.assertRaisesRegex(ValueError, 'Another terminal'):
            other.create('B')
        self.assertEqual(len(other.records), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text())['records'][0]['draft'], 'new')

    def test_background_tasks_not_applied_and_no_speech(self):
        record = self.manager.create('A')
        self.manager.send('create task')
        self.assertTrue(self.clients[0].settings.muted)
        self.assertNotIn('task_records', self.clients[0].workspace_context)
        self.clients[0].emit('action', action={'name': 'manage_task', 'op': 'add'})
        self.manager.poll()
        self.assertIn('not applied', record['transcript'][-1][1])

    def test_late_cancelled_and_old_turn_events_ignored(self):
        record = self.manager.create('A')
        self.manager.send('first')
        self.manager.stop()
        self.clients[0].emit('text', delta='stale')
        self.clients[0].emit('complete')
        self.manager.send('second')
        self.assertFalse(self.manager.poll())
        self.assertEqual(record['status'], 'running')
        self.assertNotIn('stale', str(record['transcript']))

    def test_tools_unread_focus_and_ignored_cosmetic_actions(self):
        record = self.manager.create('A')
        self.manager.send('go')
        self.manager.focus(None)
        self.clients[0].emit('tool', tool={'state': 'running', 'summary': 'Reading files'})
        self.manager.poll()
        self.assertEqual(record['status'], 'tool')
        self.assertTrue(record['unread'])
        self.manager.focus(record['id'][:8])
        self.assertFalse(record['unread'])
        self.clients[0].emit('action', action={'name': 'expression', 'expression': 'pleased'})
        self.assertFalse(self.manager.poll())

    def test_limits_names_and_explicit_remove(self):
        for title in ('', 'bad\nname', 'bad\x1bname', 'x'*81):
            with self.assertRaises(ValueError):
                self.manager.create(title)
        for n in range(8):
            self.manager.create(str(n))
        with self.assertRaises(ValueError):
            self.manager.create('ninth')
        self.manager.remove(self.manager.selected)
        self.assertIsNone(self.manager.selected)
        self.manager.create('replacement')
        with self.assertRaises(ValueError):
            self.manager.find('a')

    def test_live_owner_cannot_be_mistaken_for_crashed_terminal(self):
        self.manager.create('A')
        self.manager.send('go')
        payload = json.loads(self.path.read_text())
        payload['owner_pid'] = 999999
        self.path.write_text(json.dumps(payload))
        before = self.path.read_bytes()
        with patch('agent.workstreams.os.kill', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Another terminal is running'):
                WorkstreamManager(self.path, self.factory)
        self.assertEqual(self.path.read_bytes(), before)
        with patch('agent.workstreams.os.kill', side_effect=ProcessLookupError):
            restored = WorkstreamManager(self.path, self.factory)
        self.assertEqual(restored.records[0]['status'], 'interrupted')

    def test_tools_and_text_keep_chronological_order(self):
        record = self.manager.create('A')
        self.manager.send('go')
        self.clients[0].emit('tool', tool={'state': 'running', 'summary': 'Reading'})
        self.clients[0].emit('text', delta='Found it')
        self.clients[0].emit('tool', tool={'state': 'running', 'summary': 'Checking'})
        self.clients[0].emit('text', delta='Verified')
        self.manager.poll()
        self.assertEqual(record['transcript'], [('You', 'go'), ('Tool', 'Reading'),
                         ('Codsworth', 'Found it'), ('Tool', 'Checking'), ('Codsworth', 'Verified')])

    def test_disabled_manager_never_writes_or_starts_work(self):
        disabled = DisabledWorkstreams('Owned by another terminal')
        self.assertEqual(disabled.records, [])
        self.assertIsNone(disabled.selected)
        self.assertFalse(disabled.poll())
        self.assertIsNone(disabled.focus(None))
        self.assertIsNone(disabled.save())
        for method in (disabled.create, disabled.focus, disabled.send,
                       disabled.stop, disabled.set_draft, disabled.find):
            with self.assertRaisesRegex(ValueError, 'Owned by another terminal'):
                method('anything')
        self.assertFalse(self.path.exists())

    def test_corrupt_state_is_not_overwritten(self):
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):
            WorkstreamManager(self.path, self.factory)
        self.assertEqual(self.path.read_text(), '{broken')


if __name__ == '__main__':
    unittest.main()
