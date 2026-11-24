import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_terminal import terminal


class SessionTests(unittest.TestCase):
    def app(self, path):
        return terminal.Terminal('http://127.0.0.1:1', muted=True, state_path=path)

    def test_restores_transcript_draft_settings_history_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            app = self.app(path)
            app.transcript = [('You', 'What next?'), ('Codsworth', 'Tea, sir.')]
            app.input = 'unfinished\ndraft'
            app.input_history = ['What next?']
            app.settings.speed = 1.7
            app.settings.voice = 'pilot'
            app.client.session_id = 'a'*32
            app.mark_dirty(); app.save_state(force=True)
            restored = self.app(path)
            self.assertEqual(restored.transcript, app.transcript)
            self.assertEqual(restored.input, 'unfinished\ndraft')
            self.assertEqual(restored.input_history, ['What next?'])
            self.assertEqual(restored.settings.speed, 1.7)
            self.assertEqual(restored.settings.voice, 'pilot')
            self.assertEqual(restored.client.session_id, 'a'*32)

    def test_legacy_state_keeps_tasks_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            path.write_text(json.dumps(dict(version=1,tasks=['Tea'],panels={},session_id='b'*32)))
            app = self.app(path)
            self.assertEqual(app.tasks, ['Tea'])
            self.assertEqual(app.client.session_id, 'b'*32)
            self.assertEqual(app.phase, 'ready')

    def test_interrupted_reply_is_honest_once_and_new_keeps_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            app = self.app(path)
            app.transcript = [('Codsworth', 'Partial')]
            app.tasks.append('Tea')
            app._reply_pending = True
            app.set_phase('thinking')
            app.stop(); app.stop()
            self.assertEqual(sum('incomplete' in text for _,text in app.transcript), 1)
            restored = self.app(path)
            self.assertEqual(restored.transcript, app.transcript)
            restored.command('/new')
            self.assertEqual(restored.tasks, ['Tea'])
            self.assertEqual(restored.phase, 'ready')
            self.assertEqual(restored.client.session_id, '')
            self.assertEqual(len(restored.transcript), 1)

    def test_crash_recovery_marks_pending_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            app = self.app(path)
            app.transcript = [('Codsworth', 'Partial')]
            app.set_phase('thinking'); app.save_state(force=True)
            restored = self.app(path)
            self.assertEqual(restored.phase, 'interrupted')
            self.assertIn('incomplete', restored.transcript[-1][1])

    def test_error_survives_complete_then_new_turn_recovers_and_tools_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory)/'state.json')
            app.active_id = 4
            app._reply_pending = True
            app.events.put((4, dict(type='error',message='Backend disconnected')))
            app.events.put((4, dict(type='complete')))
            app.drain()
            self.assertEqual(app.phase, 'error')
            self.assertIn('incomplete', app.transcript[-1][1])
            with patch.object(app.client, 'submit', return_value=5):
                app.input = 'Try again'; app.send()
            self.assertEqual(app.phase, 'thinking')
            app.events.put((5, dict(type='tool',tool=dict(state='running',summary='Running terminal…'))))
            app.drain()
            self.assertEqual(app.phase, 'tool')
            self.assertEqual(app.interaction_state(), 'THINKING')
            app.expression = 'pleased'; app.status = 'Custom status'
            self.assertEqual(app.interaction_state(), 'THINKING')

    def test_saves_are_debounced_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory)/'state.json')
            with patch.object(app.state, 'save') as save, patch.object(terminal.time, 'monotonic', return_value=10):
                for _ in range(20):
                    app.mark_dirty(); app.save_state()
                self.assertEqual(save.call_count, 1)
                app.save_state(force=True)
                self.assertEqual(save.call_count, 2)
            app.transcript = [('Codsworth', 'x'*30000)]*100
            app.input_history = ['x'*8000]*100
            app.mark_dirty(); app.save_state(force=True)
            data = json.loads(app.state.path.read_text())
            self.assertLessEqual(sum(len(item[1]) for item in data['transcript']), 64000)
            self.assertLessEqual(sum(map(len,data['history'])), 64000)

    def test_export_is_complete_private_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory)/'state.json')
            app.transcript = [('You', 'Keep indentation'), ('Codsworth', '    example\nnext line')]
            target = Path(directory)/'conversation.txt'
            app.command('/export '+str(target))
            self.assertEqual(target.read_text(), app.transcript_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            app.transcript.append(('You', 'Do not replace'))
            app.command('/export '+str(target))
            self.assertEqual(app.phase, 'error')
            self.assertNotIn('Do not replace', target.read_text())

    def test_empty_transcription_and_microphone_failure_leave_transcribing(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.app(Path(directory)/'state.json')
            for event, expected in [(dict(type='transcribed',text=''), 'ready'),
                                    (dict(type='microphone_error',message='Permission denied'), 'error')]:
                app.set_phase('transcribing')
                app.events.put((-2, dict(event, token=app.capture_generation)))
                app.drain()
                self.assertEqual(app.phase, expected)

    def test_cursor_and_scroll_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            app = self.app(path)
            app.input = 'draft in progress'; app.cursor = 4; app.scroll = 9
            app.mark_dirty(); app.save_state(force=True)
            restored = self.app(path)
            self.assertEqual((restored.input, restored.cursor, restored.scroll), ('draft in progress', 4, 9))

    def test_stale_window_cannot_overwrite_or_send_and_resume_keeps_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            first, stale = self.app(path), self.app(path)
            first.client.session_id = 'c'*32
            first.transcript.append(('Codsworth', 'Newer conversation'))
            first.tasks.append('Keep task')
            first.mark_dirty(); first.save_state(force=True)
            canonical = path.read_bytes()
            stale.input = 'Unsent question'; stale.cursor = 3
            with patch.object(stale.client, 'submit') as submit:
                stale.send()
                submit.assert_not_called()
            stale.stop(); stale.save_state(force=True)
            self.assertEqual(path.read_bytes(), canonical)
            recovery = json.loads(stale.state.recovery_path.read_text())
            self.assertEqual(recovery['draft'], 'Unsent question')
            self.assertTrue(stale.resume())
            self.assertEqual(stale.client.session_id, 'c'*32)
            self.assertIn(('Codsworth', 'Newer conversation'), stale.transcript)
            self.assertEqual((stale.input,stale.cursor), ('Unsent question',3))
            self.assertEqual(stale.tasks, ['Keep task'])
            # The formerly leading window now cannot clobber the resumed state.
            first.mark_dirty(); first.save_state(force=True)
            self.assertIn('/resume', first.status)

    def test_other_live_window_is_not_mislabelled_crashed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            app = self.app(path)
            app.set_phase('thinking'); app.save_state(force=True)
            payload = json.loads(path.read_text()); payload['owner_pid'] = 123456
            path.write_text(json.dumps(payload))
            canonical = path.read_bytes()
            with patch.object(terminal.os, 'kill', return_value=None):
                other = self.app(path)
                self.assertIn('Another window', other.status)
                self.assertFalse(any('terminal closed' in content for _,content in other.transcript))
                other.input = 'My draft'; other.mark_dirty(); other.save_state(force=True)
                self.assertEqual(path.read_bytes(), canonical)
                self.assertFalse(other.resume())
                self.assertEqual(other.input, 'My draft')

    def test_named_switch_preserves_native_id_draft_and_workspace_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            app = self.app(path)
            app.client.session_id = 'd'*32
            app.transcript.append(('Codsworth', 'First conversation context'))
            app.input = 'first unfinished draft'; app.cursor = 5
            app.input_history = ['first prompt']
            app.settings.speed = 1.0
            app.command('/rename Tea planning')
            first_id = app.state.conversation_id
            app.tasks.append('Buy tea')
            app.command('/new')
            second_id = app.state.conversation_id
            self.assertNotEqual(first_id,second_id)
            self.assertEqual(app.input_history, [])
            app.client.session_id = 'e'*32
            app.transcript.append(('Codsworth', 'Second conversation context'))
            app.input = 'second draft'; app.cursor = 3
            app.settings.speed = .9
            app.tasks.append('Buy biscuits')
            app.command('/rename Biscuits')
            app.command('/switch '+first_id[:8])
            self.assertEqual(app.client.session_id, 'd'*32)
            self.assertIn(('Codsworth', 'First conversation context'),app.transcript)
            self.assertEqual((app.input,app.cursor), ('first unfinished draft',5))
            self.assertEqual(app.input_history, ['first prompt'])
            self.assertEqual(app.settings.speed,1.0)
            self.assertEqual(app.tasks,['Buy tea','Buy biscuits'])
            app.command('/switch '+second_id[:8])
            self.assertEqual(app.client.session_id,'e'*32)
            self.assertEqual((app.input,app.cursor),('second draft',3))
            self.assertEqual(app.settings.speed,.9)
            reopened=self.app(path)
            self.assertEqual(reopened.state.conversation_id,second_id)
            self.assertEqual(reopened.state.title,'Biscuits')
            reopened.command('/sessions')
            self.assertIn(first_id[:8],reopened.panel[1])
            self.assertIn('Tea planning',reopened.panel[1])

    def test_stale_window_does_not_clobber_archived_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            first,stale=self.app(path),self.app(path)
            first.command('/rename Newer title')
            ident=first.state.conversation_id
            archive=first.state.conversations.directory/(ident+'.json')
            original=archive.read_bytes()
            stale.command('/rename Stale title')
            self.assertEqual(archive.read_bytes(),original)
            self.assertIn('/resume',stale.status)
            stale.command('/switch '+ident[:8])
            self.assertIn('/resume',stale.status)

    def test_switch_rejects_path_and_unknown_identifier_without_losing_context(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(Path(directory)/'state.json')
            app.command('/rename Existing')
            ident=app.state.conversation_id
            for value in ['../../secret','ffff']:
                app.command('/switch '+value)
                self.assertEqual(app.state.conversation_id,ident)
                self.assertEqual(app.phase,'error')

    def test_motion_setting_validates_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            app=self.app(path)
            app.command('/motion reduced')
            self.assertEqual(app.settings.motion,'reduced')
            self.assertEqual(self.app(path).settings.motion,'reduced')
            app.command('/motion impossible')
            self.assertEqual(app.settings.motion,'reduced')
            self.assertEqual(app.phase,'error')

    def test_conversation_notes_do_not_leak_across_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(Path(directory)/'state.json')
            app.update_panel('notes','Notes','Conversation A codename: amber kettle')
            aid=app.state.conversation_id
            app.command('/todo Shared task')
            app.command('/new')
            self.assertNotIn('notes',app.state.panels)
            app.update_panel('notes','Notes','Conversation B codename: silver saucer')
            app.command('/switch '+aid[:8])
            self.assertEqual(app.state.panels['notes']['content'],'Conversation A codename: amber kettle')
            self.assertEqual(app.tasks,['Shared task'])
