import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_terminal import terminal
from test_workstreams import FakeClient


class TerminalWorkstreamsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state'
        self.app=terminal.Terminal('http://localhost:1',True,self.path)
        self.clients=[]
        def factory(events):
            c=FakeClient(events);self.clients.append(c);return c
        self.app.workstreams.client_factory=factory

    def test_focus_and_send_keep_two_workers_and_home_separate(self):
        self.app.input='Home draft';self.app.cursor=4
        original=list(self.app.transcript)
        self.app.command('/stream new Fix filters');a=self.app.workstreams.selected
        self.app.input='Investigate filters';self.app.send()
        self.app.command('/stream new Billing');b=self.app.workstreams.selected
        self.app.input='Investigate billing';self.app.send()
        self.assertEqual([c.sent for c in self.clients],[['Investigate filters'],['Investigate billing']])
        self.assertTrue(all(not c.cancelled for c in self.clients))
        self.clients[0].emit('text',delta='Filters result');self.clients[0].emit('complete')
        self.app.drain()
        self.assertEqual(self.app.workstreams.selected,b)
        self.assertIn('Filters result',str(self.app.workstreams.find(a)['transcript']))
        self.assertNotIn('Filters result',str(self.app.workstreams.find(b)['transcript']))
        self.assertEqual(self.app.transcript,original)
        self.app.command('/focus home')
        self.assertEqual((self.app.input,self.app.cursor),('Home draft',4))

    def test_drafts_persist_without_overwriting_home_draft(self):
        self.app.input='Main draft';self.app.cursor=3
        self.app.command('/stream new Work');ident=self.app.workstreams.selected
        self.app.edit_input('Stream draft',5);self.app.save_state(force=True)
        restored=terminal.Terminal('http://localhost:1',True,self.path)
        self.assertEqual(restored.input,'Stream draft')
        self.assertEqual(restored.workstreams.selected,ident)
        restored.command('/focus home')
        self.assertEqual((restored.input,restored.cursor),('Main draft',3))

    def test_escape_only_stops_focused_stream(self):
        for title in ('One','Two'):
            self.app.command('/stream new '+title);self.app.input='Work';self.app.send()
        with patch.object(self.app.client,'interrupt') as home:
            self.app.stop();home.assert_not_called()
        self.assertEqual(self.clients[0].cancelled,[])
        self.assertEqual(self.clients[1].cancelled,['workstream_stop'])
        self.assertEqual([r['status'] for r in self.app.workstreams.records],['running','interrupted'])

    def test_busy_send_preserves_draft(self):
        self.app.command('/stream new One');self.app.input='First';self.app.send()
        self.app.input='Do not lose this';self.app.send()
        self.assertEqual(self.app.input,'Do not lose this')
        self.assertEqual(self.clients[0].sent,['First'])

    def test_global_command_returns_to_home_without_cancelling_worker(self):
        self.app.command('/stream new One');self.app.input='Work';self.app.send()
        self.app.command('/help')
        self.assertIsNone(self.app.workstreams.selected)
        self.assertEqual(self.clients[0].cancelled,[])

    def test_composer_rule_names_the_focused_stream_and_no_bar_survives(self):
        self.app.command('/stream new Billing');self.app.glyph_graphics=True
        self.app.tray_open=True;self.app.tray_view='streams'
        drawn=[]
        class Screen:
            def getmaxyx(self):return 40,100
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append((y,x,text[:n]))
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''):
            self.app.draw(Screen())
        ident=self.app.workstreams.selected[:8]
        # The composer meta line names where Enter goes; nothing else claims to.
        self.assertTrue(any(ident in t and 'Billing' in t for _,_,t in drawn))
        self.assertFalse(any('Message' in t for _,_,t in drawn))
        # The workstream bar is gone -- streams belong in the tray -- and its row
        # went back to the transcript.
        self.assertFalse(any('/streams' in t for _,_,t in drawn))
        # The input frame remains separate from the tray and transcript gutters.
        self.assertTrue(any(t.startswith('╭') and t.endswith('╮') for _,_,t in drawn))
        self.assertTrue(any(x==100-26-2 for _,x,t in drawn if t=='│'))
        # The tray's streams view agrees with the composer rule and the footer.
        self.assertTrue(any(t=='→ '+ident+' Billing' for _,_,t in drawn))
        self.assertTrue(any(t=='  Home' for _,_,t in drawn))
        # The focused stream owns the footer status line.
        self.assertTrue(any(y==39 and t.startswith('Billing · ') for y,x,t in drawn))
        self.app.focus_stream(None)
        self.app.tray_open=True;self.app.tray_view='streams';self.app._draw_key=None
        drawn.clear()
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''):
            self.app.draw(Screen())
        self.assertTrue(any(t=='→ Home' for _,_,t in drawn))
        self.assertTrue(any(t=='  '+ident+' Billing' for _,_,t in drawn))

    def test_title_completion_and_ctrl_tab_focus(self):
        self.app.command('/stream new Billing');ident=self.app.workstreams.selected
        self.app.input='/focus Bill'
        self.assertEqual(self.app.completion_options()[0][0],'/focus '+ident)
        self.app.handle_key(terminal.Key('\t',modifiers=4))
        self.assertIsNone(self.app.workstreams.selected)

    def test_unavailable_store_keeps_home_usable(self):
        with patch.object(terminal,'WorkstreamManager',side_effect=ValueError('Another terminal owns work')):
            app=terminal.Terminal('http://localhost:1',True,Path(self.temp.name)/'other')
        app.command('/stream new Test')
        self.assertIn('Another terminal',app.status)
        self.assertEqual(app.workstreams.records,[])
        app.command('/help')
        self.assertTrue(app.transcript)
