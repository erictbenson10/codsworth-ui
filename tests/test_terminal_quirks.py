from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_terminal import terminal
from quirks import JOKES


class QuirkTests(unittest.TestCase):
    def test_jokes_are_local_optional_and_keep_the_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            app.input='draft'
            with patch.object(app.client,'submit') as model:
                app.command('/joke');self.assertIn(app.transcript[-1][1],JOKES)
                app.command('/tea');self.assertIn('virtual cuppa',app.transcript[-1][1])
                app.command('/vault');self.assertIn('S.P.E.C.I.A.L.',app.transcript[-1][1])
            model.assert_not_called();self.assertEqual(app.input,'draft')

    def test_aside_does_not_capture_a_streaming_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            app.active_id=2;app.command('/tea')
            app.events.put((2,dict(type='text',delta=' Continuing the actual reply.')));app.drain()
            self.assertTrue(app.transcript[0][1].endswith(' Continuing the actual reply.'))
            self.assertNotIn('actual reply',app.transcript[-1][1])

    def test_bug_command_passes_only_description_to_direct_reporter(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            with patch.object(app,'file_bug') as report:
                app.command('/bug Playback stops unexpectedly')
                report.assert_called_once_with('Playback stops unexpectedly')
            with patch.object(app,'send') as send:
                app.command('/bug');send.assert_not_called()

    def test_settings_reflect_actual_values(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            app.settings.speed=1.1;app.settings.motion='reduced'
            app.command('/settings')
            text=app.transcript[-1][1]
            self.assertIn('Speed: 1.1',text)
            self.assertIn('Motion: reduced',text)
            self.assertIn('Fleet watch: off',text)
