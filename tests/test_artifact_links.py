import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
import terminal
from artifact_links import extract_links,valid_url


class LinkTests(unittest.TestCase):
    def test_markdown_balanced_punctuation_and_dedup(self):
        text='Read [docs](https://example.com/a_(b)). Again https://example.com/a_(b), then https://example.org/?q=yes&x=2.'
        self.assertEqual(extract_links([('Codsworth',text)]),['https://example.com/a_(b)','https://example.org/?q=yes&x=2'])

    def test_untrusted_schemes_credentials_and_controls_rejected(self):
        for url in ['file:///tmp/test','javascript:alert(1)','https://user:password@example.org','https://@example.org','https://example.org/\nopen','https://example.org/\u202eevil','https://example.org\\evil']:
            self.assertFalse(valid_url(url),url)
        self.assertTrue(valid_url('https://example.org:8443/path?x=1#part'))
        self.assertEqual(len(extract_links([('Codsworth',' '.join(f'https://example.org/{i}' for i in range(100)))])),50)

    def test_explicit_snapshot_open_only_and_stale_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
            app.client.session_id='session-a'
            app.transcript=[('Codsworth','Use https://example.org/docs.')]
            with patch.object(terminal.shutil,'which',return_value='/usr/bin/open'),patch.object(terminal.subprocess,'run') as run:
                app.command('/links');run.assert_not_called()
                app.command('/open 1')
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0],['/usr/bin/open','https://example.org/docs'])
                self.assertNotIn('shell',run.call_args.kwargs)
                run.reset_mock()
                app.client.session_id='session-b'
                app.command('/open 1');run.assert_not_called()
                self.assertIn('/links',app.status)

    def test_out_of_range_and_missing_opener_do_not_open(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
            app.transcript=[('Codsworth','https://example.org')]
            with patch.object(terminal.shutil,'which',return_value=None),patch.object(terminal.subprocess,'run') as run:
                app.command('/links');app.command('/open 99');run.assert_not_called()
                self.assertIn('No link',app.status)
                app.command('/open 1');run.assert_not_called()
                self.assertIn('No system browser opener',app.status)

    def test_new_local_conversation_invalidates_links_before_native_session_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
            app.client.session_id=None
            app.transcript=[('Codsworth','https://example.org/old')]
            app.command('/links')
            app.state.conversation_id='f'*32
            with patch.object(terminal.subprocess,'run') as run:
                app.command('/open 1')
                run.assert_not_called()
            self.assertIn('/links',app.status)
