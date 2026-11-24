"""The butler's tray: its keys, its persisted state and its pure renderers."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
import terminal
from keyboard import Key
from task_store import TaskStore


class TrayKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(self.temp.name)/'state')

    def test_control_t_toggles_in_both_protocol_states_and_never_types(self):
        """^T decodes to \\x14 with and without the protocol, so one test serves both."""
        for event in (Key('\x14',code=20),Key('\x14',modifiers=4,code=116)):
            self.app.handle_key(event)
            self.assertTrue(self.app.tray_open)
            self.assertEqual(self.app.tray_view,'glance')
            self.app.handle_key(event)
            self.assertFalse(self.app.tray_open)
        self.assertEqual(self.app.input,'')

    def test_alt_accelerators_choose_a_view_only_under_the_key_protocol(self):
        self.app.key_protocol=True
        self.app.handle_key(Key('2',modifiers=2,code=50))
        self.assertEqual((self.app.tray_open,self.app.tray_view),(True,'fleet'))
        self.app.handle_key(Key('3',modifiers=2,code=51))
        self.assertEqual((self.app.tray_open,self.app.tray_view),(True,'inbox'))
        self.app.handle_key(Key('\\',modifiers=2,code=92))   # the toggle accelerator
        self.assertFalse(self.app.tray_open)
        self.assertEqual(self.app.input,'')                  # nothing reached the draft

    def test_without_the_protocol_alt_is_not_registered_at_all(self):
        """Alt is erased there and a bare Escape lands first, which stops speech."""
        self.app.key_protocol=False
        self.app.handle_key(Key('\\',modifiers=2,code=92))
        self.assertFalse(self.app.tray_open)
        self.app.handle_key(Key('1',modifiers=2,code=49))
        self.assertFalse(self.app.tray_open)
        self.assertEqual(self.app.input,'\\1')

    def test_commands_open_the_tray_on_the_matching_view(self):
        for command,view in [('/tray fleet','fleet'),('/streams','streams'),('/tasks','tasks'),
                             ('/tray inbox','inbox'),('/tray glance','glance')]:
            self.app.tray_open=False
            self.app.command(command)
            self.assertEqual((self.app.tray_open,self.app.tray_view),(True,view),command)
        self.app.command('/tray')                            # bare /tray toggles
        self.assertFalse(self.app.tray_open)
        with self.assertRaises(ValueError):
            self.app.set_tray('nonsense')

    def test_opening_the_tray_never_changes_where_enter_goes(self):
        self.app.command('/stream new Billing')
        target=self.app.composer_target()
        self.app.command('/tray streams')
        self.assertEqual(self.app.composer_target(),target)
        self.assertEqual(self.app.workstreams.selected[:8]+' Billing',target)


class TrayDataTests(unittest.TestCase):
    """draw() renders cached state; the fetches that fill it happen elsewhere."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(self.temp.name)/'state')
        self.app.tray_open=True
        self.app.tray_view='fleet'

    def render(self):
        drawn=[]
        class Screen:
            def getmaxyx(self):return 40,110
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append(text[:n])
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),\
             patch.object(terminal.sys,'stdout',new=io.StringIO()),\
             patch.object(terminal,'kitty_frame',return_value=''):
            self.app.draw(Screen())
        return drawn

    def test_a_snapshot_arriving_through_drain_changes_the_next_frame(self):
        self.assertIn('No Fleet data yet.',self.render())
        snapshot={'observed_at':'2026-09-07T10:00:00+00:00','available':{'questions':True,'attempts':True},
                  'counts':{'running':1},'questions':[{'id':'q7','text':'Ship it?','options':['yes','no']}],
                  'attempts':[{'id':'a9','state':'running','name':'Merge gate'}]}
        self.app.events.put((-3,dict(type='fleet_snapshot',token=self.app.fleet_generation,
                                     snapshot=snapshot,questions_only=False,background=False)))
        self.app.drain()
        self.assertIs(self.app.tray_data['fleet'],snapshot)
        drawn=self.render()                       # the frame is not stale
        self.assertIn('! [q7] Ship it?',drawn)
        self.assertIn('a. yes',drawn)
        self.assertIn('◐ a9 Merge gate',drawn)

    def test_codsworth_opens_the_tray_for_blocking_work_but_not_mid_draft(self):
        snapshot={'observed_at':'now','available':{'questions':True,'attempts':True,'health':True,'status':True},
                  'counts':{'running':0},'questions':[{'id':'q7','text':'Ship it?'}],'attempts':[],
                  'health':{'daemon':'ok'}}
        self.app.tray_open=False
        self.app.input='half a thought'
        self.app.events.put((-3,dict(type='fleet_snapshot',token=self.app.fleet_generation,
                                     snapshot=snapshot,questions_only=False,background=True)))
        self.app.drain()
        self.assertFalse(self.app.tray_open)          # never over a draft in progress
        self.app.input=''
        self.app.fleet_watch.path.unlink(missing_ok=True)
        self.app.events.put((-3,dict(type='fleet_snapshot',token=self.app.fleet_generation,
                                     snapshot=snapshot,questions_only=False,background=True)))
        self.app.drain()
        self.assertEqual((self.app.tray_open,self.app.tray_view),(True,'fleet'))
        self.assertIn(('Notice','◇ tray  opened by Codsworth · fleet'),self.app.transcript)

    def test_a_failed_refresh_is_an_error_in_the_tray_not_an_empty_one(self):
        self.app.events.put((-3,dict(type='fleet_error',token=self.app.fleet_generation,
                                     message='Connection refused to 127.0.0.1:18042',background=False)))
        self.app.drain()
        drawn=self.render()
        self.assertIn('Fleet unavailable.',drawn)
        self.assertIn('Connection refused to 127.0.0.1:18042',drawn)


class TrayStateTests(unittest.TestCase):
    def test_an_older_state_file_loads_shut_on_the_glance(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            path.write_text(json.dumps(dict(version=3,draft='kept',transcript=[['You','hello']],
                                            title='Older window',cursor=5,scroll=0)))
            state=terminal.WorkspaceState(directory,path)
            self.assertEqual((state.tray_open,state.tray_view),(False,'glance'))
            self.assertEqual(state.draft,'kept')
            self.assertEqual(state.title,'Older window')

    def test_rubbish_tray_values_fall_back_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            path.write_text(json.dumps(dict(version=3,tray_open='yes',tray_view='../etc')))
            state=terminal.WorkspaceState(directory,path)
            self.assertEqual((state.tray_open,state.tray_view),(False,'glance'))

    def test_the_tray_position_survives_a_save_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            state=terminal.WorkspaceState(directory,path)
            state.tray_open,state.tray_view=True,'doctor'
            state.save()
            self.assertEqual(json.loads(path.read_text())['tray_view'],'doctor')
            reloaded=terminal.WorkspaceState(directory,path)
            self.assertEqual((reloaded.tray_open,reloaded.tray_view),(True,'doctor'))


class TrayRenderTests(unittest.TestCase):
    """Every renderer is a pure function of state someone else already fetched."""
    def setUp(self):
        self.tasks=TaskStore()
        self.tasks.apply_change({'op':'add','title':'Read the proposal'})

    def test_nothing_fetched_yet_says_so_rather_than_showing_an_empty_list(self):
        for view in terminal.TRAY_VIEWS:
            lines=terminal.tray_content(view,{},[],None,TaskStore())
            self.assertTrue(lines,view)
            text=' '.join(line for line,_ in lines)
            if view in ('fleet','inbox','doctor'):
                self.assertTrue('not loaded' in text.lower() or 'no checks run' in text.lower()
                                or 'no fleet data' in text.lower(),view)
            self.assertFalse(any(role == 'alert' for _,role in lines),view)

    def test_an_unavailable_source_is_never_reported_as_an_empty_inbox(self):
        blind={'fleet':{'available':{'questions':False},'questions':[]}}
        glance=terminal.tray_glance(blind,[],None,TaskStore())
        self.assertIn(('Questions unavailable — this is not an empty inbox.','alert'),glance)
        self.assertIsNone(terminal.fleet_questions(blind['fleet']))
        quiet={'fleet':{'available':{'questions':True},'questions':[]}}
        self.assertIn(('Nothing waiting.','soft'),terminal.tray_glance(quiet,[],None,TaskStore()))
        self.assertEqual(terminal.tray_badges(quiet,0),[])

    def test_red_marks_only_work_waiting_on_eric(self):
        busy={'fleet':{'available':{'questions':True,'attempts':True},'questions':[],
                       'counts':{'running':4},'attempts':[{'id':'a1','state':'running','name':'Fix'}]}}
        self.assertEqual(terminal.tray_badges(busy,2),[('◐4','accent'),('□2','soft')])
        self.assertFalse(any(role == 'alert' for _,role in terminal.tray_fleet(busy)))
        busy['fleet']['questions']=[{'id':'q1','text':'Which option?','options':['One','Two']}]
        self.assertEqual(terminal.tray_badges(busy,0)[0],('!1','alert'))
        lines=terminal.tray_fleet(busy)
        self.assertIn(('! [q1] Which option?','alert'),lines)
        self.assertIn(('a. One','body'),lines)
        self.assertIn(('b. Two','body'),lines)
        self.assertIn(('◐ a1 Fix','body'),lines)

    def test_the_streams_view_marks_the_same_target_the_composer_names(self):
        records=[{'id':'abcdef1234','title':'Billing','status':'running','unread':True}]
        self.assertEqual(terminal.stream_rows(records,None)[0],('→ Home','accent'))
        focused=terminal.stream_rows(records,'abcdef1234')
        self.assertEqual(focused[0],('  Home','body'))
        self.assertEqual(focused[1],('→ abcdef12 Billing · new','accent'))
        self.assertEqual(focused[2],('    [running]','soft'))

    def test_tasks_and_doctor_read_from_the_stores_that_already_hold_them(self):
        lines=terminal.tray_tasks(self.tasks)
        self.assertTrue(any('Read the proposal' in line for line,_ in lines))
        self.assertEqual(terminal.tray_tasks(TaskStore())[0],('No open tasks.','soft'))
        checks=[{'name':'Voice service','status':'unavailable','detail':'No valid health response'},
                {'name':'Python','status':'info','detail':'3.14'}]
        rendered=terminal.tray_doctor({'doctor':checks})
        self.assertEqual(rendered[0],('Voice service','title'))
        self.assertEqual(rendered[1][1],'alert')
        self.assertEqual(rendered[3][1],'body')


if __name__ == '__main__':
    unittest.main()
