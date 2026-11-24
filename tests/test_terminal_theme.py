"""Layout regressions: compact footer, contextual draft hint, prominent real glyph."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
import terminal


class ThemeTests(unittest.TestCase):
    def test_composer_glyph_and_status_fit_compact_and_large_windows(self):
        for width,height in ((60,16),(80,30),(120,40),(180,52)):
            with self.subTest(size=(width,height)), tempfile.TemporaryDirectory() as directory:
                drawn=[]
                class Screen:
                    def getmaxyx(self): return height,width
                    def erase(self): pass
                    def refresh(self): pass
                    def addnstr(self,y,x,text,n,attr):
                        self_test.assertGreaterEqual(y,0)
                        self_test.assertLess(y,height)
                        self_test.assertLessEqual(x+n,width-1)
                        drawn.append((y,x,text[:n]))
                    def move(self,y,x):
                        self_test.assertEqual(y,height-3-(1 if height>=24 else 0))
                        self_test.assertLess(x,width-3)
                self_test=self
                app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
                app.input='A real typed request'
                if width>=80:
                    app.transcript=[('Codsworth','This reply uses the full conversation width comfortably.')]
                app.key_protocol=True
                app.glyph_graphics=True
                app.tray_open=True
                with patch.object(terminal.curses,'color_pair',return_value=0), patch.object(terminal.sys,'stdout',new=io.StringIO()), patch.object(terminal,'kitty_frame',return_value='GLYPH') as glyph:
                    app.draw(Screen())
                # The sprite is off the top of the screen; it sits at the foot of the
                # open tray, and the tray cannot open on a narrow window.
                if width>=terminal.TRAY_COLUMNS+70:
                    glyph.assert_called_once()
                    self.assertEqual(glyph.call_args.args[3],19)
                else:
                    glyph.assert_not_called()
                visible='\n'.join(text for _,_,text in drawn)
                self.assertIn('Codsworth',visible)
                self.assertNotIn('Your personal Mr. Handy',visible)
                self.assertNotIn('CONVERSATION',visible)
                self.assertNotIn('TODAY',visible)
                self.assertNotIn('Message',visible)
                self.assertIn('A real typed request',visible)
                self.assertIn('Home',visible)          # the composer names its target
                self.assertIn('Enter:send' if width>=80 else 'Enter',visible)
                if width>=80:
                    self.assertIn('Shift+Enter:newline',visible)  # key hint only when it fits
                    self.assertIn('This reply uses the full conversation width comfortably.',visible)
                self.assertTrue(any(y==height-3-(1 if height>=24 else 0) and 'typed request' in text for y,_,text in drawn))

    def test_welcome_avatar_is_larger_than_old_ten_column_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
            app.glyph_graphics=True
            app.tray_open=True
            class Screen:
                def getmaxyx(self): return 40,100
                def erase(self): pass
                def refresh(self): pass
                def addnstr(self,*args): pass
                def move(self,*args): pass
            with patch.object(terminal.curses,'color_pair',return_value=0), patch.object(terminal.sys,'stdout',new=io.StringIO()), patch.object(terminal,'kitty_frame',return_value='GLYPH') as glyph:
                app.draw(Screen())
            # Re-anchored to the open tray rectangle: 19 columns, aspect kept by fit_cells.
            self.assertEqual(glyph.call_args.args[3:5],terminal.fit_cells(19,12))
            self.assertEqual(glyph.call_args.args[3],19)
            greeting_size=glyph.call_args.args[3:5]
            app.transcript.append(('You','Please explain the proposal'))
            app.input='Another draft'
            with patch.object(terminal.curses,'color_pair',return_value=0), patch.object(terminal.sys,'stdout',new=io.StringIO()), patch.object(terminal,'kitty_frame',return_value='GLYPH') as active_glyph:
                app.draw(Screen())
            # Draft changes preserve the already placed image; no redundant PNG upload.
            self.assertEqual(app._glyph_key[3:5],greeting_size)
            if active_glyph.called:
                self.assertEqual(active_glyph.call_args.args[3:5],greeting_size)

    def test_expression_does_not_override_active_voice_state(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
            app.expression='pleased'
            for status,expected in [('Thinking…','THINKING'),('Transcribing your speech…','TRANSCRIBING'),
                                    ('Speaking · Esc to interrupt','SPEAKING'),('Interrupted · ready','INTERRUPTED'),
                                    ('Speech unavailable: player failed','ERROR'),('Ready','READY')]:
                app.phase=expected.lower()
                app.status='An unrelated operation detail'
                self.assertEqual(app.interaction_state(),expected)

    def test_panels_flow_into_chat_and_stream_stays_with_assistant(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
            app.command('/todo Finish the proposal')
            self.assertIn('Finish the proposal',app.transcript[-1][1])
            app.command('/help')
            self.assertEqual(app.transcript[-1][0],'Controls')
            app.active_id=5
            app.events.put((5,dict(type='text',delta=' More speech.')))
            app.drain()
            self.assertTrue(app.transcript[0][1].endswith(' More speech.'))
            self.assertFalse(app.transcript[-1][1].endswith(' More speech.'))

if __name__=='__main__': unittest.main()
