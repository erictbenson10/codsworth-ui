import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))
import keyboard
import microphone
spec = importlib.util.spec_from_file_location('ptt_terminal', ROOT / 'agent/terminal.py')
terminal = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = terminal
spec.loader.exec_module(terminal)


def wav(seconds=.3):
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b'\0\0' * int(16000*seconds))
    return data.getvalue()


class PushToTalkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = terminal.Terminal('http://localhost:1', muted=True, state_path=Path(self.directory.name)/'state')

    def tearDown(self):
        self.app.stop()
        self.directory.cleanup()

    def key(self, sequence):
        for character in sequence:
            for event in self.app.decoder.feed(character):
                self.app.handle_key(event)

    def test_real_press_repeat_release_protocol_not_repeat_timeout(self):
        self.key('\x1b[?27u')
        self.assertTrue(self.app.key_protocol)
        with patch.object(self.app, 'begin_capture') as begin, patch.object(self.app, 'end_capture') as end:
            self.key('\x1b[32;1:1u')
            self.key('\x1b[32;1:2u')
            self.key('\x1b[32;1:2u')
            self.assertEqual(begin.call_count, 1)
            end.assert_not_called()
            self.assertTrue(self.app.ptt_held)
            self.key('\x1b[32;1:3u')
            self.assertEqual(end.call_count, 1)
            self.assertFalse(self.app.ptt_held)

    def test_typed_space_unchanged_and_unsupported_terminal_never_records(self):
        with patch.object(self.app, 'begin_capture') as begin:
            self.key(' ')
            begin.assert_not_called()
            self.assertEqual(self.app.input, ' ')
            self.app.input = 'Plan'
            self.key('\x1b[?27u')
            self.key('\x1b[32;1u')
            self.key('\x1b[32;1:3u')
            begin.assert_not_called()
            self.assertEqual(self.app.input, 'Plan ')

    def test_associated_unicode_text_and_control_keys_decode(self):
        self.key('\x1b[57441;2u')  # Physical Shift must not become private-use text.
        self.assertEqual(self.app.input, '')
        self.key('\x1b[97;1;233u')
        self.assertEqual(self.app.input, 'é')
        self.key('\x1b[117;5u')
        self.assertEqual(self.app.input, '')

    def test_focus_loss_discards_recording_and_pending_transcription(self):
        class Recorder:
            cancelled = False
            def cancel(self):
                self.cancelled = True
            def finish(self):
                return None
        recording = Recorder()
        self.app.recording = recording
        previous = self.app.capture_generation
        self.key('\x1b[O')
        self.assertTrue(recording.cancelled)
        self.assertGreater(self.app.capture_generation, previous)
        self.assertIsNone(self.app.recording)
        self.app.events.put((-2, dict(type='transcribed', token=previous, text='stale')))
        with patch.object(self.app.client, 'submit') as submit:
            self.app.drain()
            submit.assert_not_called()

    def test_empty_transcript_and_mic_failure_never_submit(self):
        with patch.object(self.app.client, 'submit') as submit:
            self.app.events.put((-2, dict(type='transcribed', token=0, text='')))
            self.app.drain()
            self.app.events.put((-2, dict(type='microphone_error', token=0, message=microphone.MIC_HELP)))
            self.app.drain()
            submit.assert_not_called()
            self.assertIn('Microphone', self.app.status)

    def test_release_finishes_wav_then_posts_stt_and_submits_text(self):
        class Recorder:
            finished = False
            def finish(self):
                self.finished = True
                return wav()
            def cancel(self):
                pass
        recording = Recorder()
        self.app.recording = recording
        def stream(path, payload, turn, **kwargs):
            self.assertTrue(recording.finished)
            self.assertEqual(path, '/api/transcribe')
            self.assertEqual(kwargs['content_type'], 'application/octet-stream')
            self.assertAlmostEqual(microphone.validate_wav(payload), .3)
            yield {'text':'Plan my afternoon.'}
        self.app.client.stream = stream
        with patch.object(self.app.client, 'submit', return_value=3) as submit:
            self.app.end_capture()
            deadline = time.monotonic()+1
            while self.app.events.empty() and time.monotonic() < deadline:
                time.sleep(.01)
            self.app.drain()
            submit.assert_called_once_with('Plan my afternoon.')

    def test_microphone_format_and_duration_bounds(self):
        self.assertAlmostEqual(microphone.validate_wav(wav()), .3)
        for data in [wav(.01), wav(61)]:
            with self.assertRaises(ValueError):
                microphone.validate_wav(data)

    def test_recording_finalizes_wav_on_release_and_cleans_temporary_files(self):
        class Process:
            returncode = None
            def __init__(self, args, **kwargs):
                Path(args[-1]).write_bytes(wav())
                self.stdin = io.BytesIO()
                self.stopped_with_q = False
            def poll(self):
                return self.returncode
            def wait(self, timeout=None):
                self.stopped_with_q = self.stdin.getvalue() == b'q\n'
                self.returncode = 0
                return 0
            def kill(self):
                self.returncode = -9
        with patch.object(microphone.subprocess, 'Popen', side_effect=Process):
            recording = microphone.Recording(executable='/fake/ffmpeg')
            directory = Path(recording.directory.name)
            data = recording.finish()
            self.assertAlmostEqual(microphone.validate_wav(data), .3)
            self.assertTrue(recording.process.stopped_with_q)
            self.assertFalse(directory.exists())


if __name__ == '__main__':
    unittest.main()
