import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
import terminal
from command_catalog import matches
from keyboard import Key


class CommandPickerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(self.temp.name)/'state')

    def key(self,text):
        self.app.handle_key(Key(text))

    def test_effort_sets_and_opens_the_selector(self):
        self.app.command('/effort high')
        self.assertEqual(self.app.settings.effort,'high')
        self.app.command('/effort')
        self.assertTrue(self.app.effort_menu)
        self.app.handle_key(Key('left'))
        self.app.handle_key(Key('\r'))
        self.assertEqual(self.app.settings.effort,'medium')
        self.assertFalse(self.app.effort_menu)

    def test_enum_argument_tab_completes_without_executing(self):
        for draft,expected in [('/voice p','/voice pilot'),('/motion r','/motion reduced'),('/watch o','/watch on'),('/tasks d','/tasks done'),('/effort h','/effort high')]:
            self.app.input=draft
            with patch.object(self.app,'send') as send:
                self.key('\t');send.assert_not_called()
            self.assertEqual(self.app.input,expected)

    def test_current_task_ids_and_titles_drive_completions(self):
        self.app.state.task_store.records=[dict(id='a'*32,title='Review proposal')]
        self.app.input='/task done a'
        self.assertEqual(self.app.completion_options(),[('/task done '+'a'*32,'Review proposal')])
        self.app.state.task_store.records=[dict(id='b'*32,title='New task')]
        self.assertEqual(self.app.completion_options(),[])
        self.app.input='/task done b';self.key('\t')
        self.assertEqual(self.app.input,'/task done '+'b'*32)

    def test_saved_conversation_choices_cached_and_refreshed_for_key(self):
        self.app.input='/switch '
        with patch.object(terminal,'session_choices',return_value=[dict(conversation_id='c'*32,title='Planning')]) as load,patch.object(terminal.time,'monotonic',return_value=100):
            self.assertEqual(self.app.completion_options()[0][1],'Planning')
            self.app.completion_options();load.assert_called_once()
        with patch.object(terminal,'session_choices',return_value=[dict(conversation_id='d'*32,title='New title')]) as load,patch.object(terminal.time,'monotonic',return_value=102):
            self.key('\t');load.assert_called_once()
            self.assertEqual(self.app.input,'/switch '+'d'*32)

    def test_prefix_catalog_and_argument_exclusion(self):
        self.assertEqual([name for name,_ in matches('/spe')],['/speed'])
        self.assertEqual(matches('/speed 1.2'),[])
        self.assertEqual(matches('a /speed'),[])
        self.assertIn('/fleet',[name for name,_ in matches('/')])
        self.assertIn('/questions',[name for name,_ in matches('/')])

    def test_tab_completes_without_execution_and_enter_uses_selection(self):
        self.app.input='/spe'
        with patch.object(self.app,'send') as send:
            self.key('\t')
            self.assertEqual(self.app.input,'/speed')
            send.assert_not_called()
        self.key('\r')
        self.assertTrue(self.app.speed_slider)

    def test_arrow_selection_executes_selected_command_only_on_enter(self):
        self.app.input='/to'
        self.key('down')
        with patch.object(self.app,'send') as send:
            self.key('\r')
            send.assert_called_once()
            self.assertEqual(self.app.input,'/todos')

    def test_paste_never_executes_and_arguments_leave_picker(self):
        with patch.object(self.app,'send') as send:
            self.app.handle_key(Key('/quit',kind='paste'))
            send.assert_not_called()
        self.app.input='/todo buy tea'
        self.app.input_history=['previous']
        self.key('up')
        self.assertEqual(self.app.input,'previous')

    def test_compact_picker_fits_above_composer(self):
        self.app.input='/'
        self.app.glyph_graphics=True
        drawn=[]
        class Screen:
            def getmaxyx(self):return 16,60
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append((y,text[:n]))
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',return_value=0),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''):
            self.app.draw(Screen())
        choices=[(row,text) for row,text in drawn if '/help ' in text or '/speed ' in text]
        self.assertEqual([row for row,_ in choices],[10,11])
        self.assertTrue(any(row==13 and text=='/' for row,text in drawn))
        self.assertTrue(any(row==15 and 'Tab:complete' in text for row,text in drawn))

    def test_command_picker_is_a_framed_dropdown_with_a_selected_bar(self):
        self.app.input='/'
        drawn=[]
        class Screen:
            def getmaxyx(self):return 40,100
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append((y,x,text[:n],attr))
            def move(self,*a):pass
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''):
            self.app.draw(Screen())
        self.assertTrue(any(t.startswith('╭') and t.endswith('╮') for y,x,t,a in drawn if y < 34))
        self.assertTrue(any(t.startswith('├') and t.endswith('┤') for y,x,t,a in drawn))
        selected=[a for y,x,t,a in drawn if '/help' in t]
        self.assertTrue(any(a & terminal.curses.A_REVERSE for a in selected))
        self.assertFalse(any(t.strip().startswith(('›','▸','>')) and '/help' in t for y,x,t,a in drawn))

    def test_archive_restore_and_title_completion(self):
        tasks=[dict(id='a'*32,title='Review backups',status='done'),
               dict(id='b'*32,title='Old backups',status='done',archived=True),
               dict(id='c'*32,title='Open backups',status='open')]
        self.assertEqual(matches('/task archive back',tasks=tasks),[('/task archive '+'a'*32,'Review backups')])
        self.assertEqual(matches('/task restore ',tasks=tasks),[('/task restore '+'b'*32,'Old backups')])
        self.assertEqual(matches('/task done Review back',tasks=tasks),[('/task done '+'a'*32,'Review backups')])
