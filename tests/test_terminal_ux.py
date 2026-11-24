import array
from contextlib import ExitStack
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
import terminal
from keyboard import Key,decode,Decoder

class UXTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(self.temp.name)/'state')
    def expand_all(self):
        self.app._expanded_messages.update((self.app.state.conversation_id,'',index)
                                           for index in range(len(self.app.transcript)))

    def key(self,text,modifiers=0):
        self.app.handle_key(Key(text,modifiers=modifiers))
    def test_cursor_insert_delete_and_line_navigation(self):
        self.app.input='hello world'
        self.key('home');self.key('right');self.key('X')
        self.assertEqual(self.app.input,'hXello world')
        self.key('delete');self.assertEqual(self.app.input,'hXllo world')
        self.key('\x7f');self.assertEqual(self.app.input,'hllo world')
        self.key('end');self.key('\r',1)
        self.key('second');self.key('home');self.assertEqual(self.app.cursor,len('hllo world\n'))
        self.key('end');self.assertEqual(self.app.cursor,len(self.app.input))
    def test_history_returns_unsent_draft(self):
        self.app.input_history=['first','second']
        self.app.input='my draft'
        self.key('up');self.assertEqual(self.app.input,'second')
        self.key('up');self.assertEqual(self.app.input,'first')
        self.key('down');self.key('down');self.assertEqual(self.app.input,'my draft')
    def test_composer_metrics_reserve_preview_and_meta_rows(self):
        shown,top,divider,previews,meta,pad=terminal.composer_metrics(40,1,0,True)
        self.assertEqual((shown,previews,meta,pad),(1,0,1,1))
        self.assertEqual(divider,top-pad-1)
        shown,top,divider,previews,meta,pad=terminal.composer_metrics(40,1,1,True)
        self.assertEqual((previews,meta,pad),(4,1,0))
        self.assertEqual(divider,top-previews-1)
        self.assertEqual(terminal.composer_metrics(16,1,1,True)[3:],(0,0,0))

    def test_composer_meta_keeps_home_and_falls_back_to_a_short_meter(self):
        kind,start,shown,meter,text=terminal.composer_meta(
            'Home',('ctx 7k/256k · 2%','ctx 2%'),'1×',100,6,True)
        self.assertEqual((kind,shown,meter),('inner','Home','ctx 7k/256k · 2%'))
        self.assertIn('Home',text)
        kind,start,shown,meter,text=terminal.composer_meta(
            'Home',('ctx 220k/256k · 85%','ctx 85%'),'voice off · /doctor',60,6,False)
        self.assertIn(kind,('rule','split'))
        self.assertTrue('Home' in shown or meter.startswith('ctx'))

    def test_wrapping_keeps_cursor_and_unicode_display_width(self):
        lines,row,column=terminal.composer_layout('abc界d',4,5)
        self.assertEqual(lines,['abc界','d'])
        self.assertEqual((row,column),(1,0))
        lines,row,column=terminal.composer_layout('abc\ndef',5,20)
        self.assertEqual((lines,row,column),(['abc','def'],1,1))
    def test_multiline_draft_fits_bottom_and_preserves_big_active_glyph(self):
        self.app.input='First line\nSecond line\nThird line'
        self.app.status='Speaking · Esc to interrupt';self.app.glyph_graphics=True
        self.app.tray_open=True  # shut, the tray draws no sprite at all
        drawn=[];positions=[]
        class Screen:
            def getmaxyx(self):return 40,100
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append((y,x,text[:n]))
            def move(self,y,x):positions.append((y,x))
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value='') as glyph:
            self.app.draw(Screen())
        self.assertEqual(glyph.call_args.args[3:5],(19,11))  # the tray sprite rectangle
        self.assertEqual([y for y,x,t in drawn if t in ('First line','Second line','Third line')],[34,35,36])
        self.assertEqual(positions[-1],(36,16))
        self.assertFalse(any('Your personal' in t for _,_,t in drawn))
    def test_bracketed_paste_is_one_draft_and_never_submits(self):
        decoder=Decoder()
        events=[]
        for char in '\x1b[200~first\n/speed 2\n\x1b[201~':
            events.extend(decoder.feed(char))
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].kind,'paste')
        with patch.object(self.app,'send') as send:
            self.app.handle_key(events[0])
            send.assert_not_called()
        self.assertEqual(self.app.input,'first\n/speed 2\n')
        self.assertEqual(self.app.settings.speed,terminal.Settings().speed)

    def test_rename_opens_current_title_for_editing_and_persists(self):
        self.app.command('/rename GPU checks')
        self.app.input='/rename';self.app.send()
        self.assertEqual(self.app.input,'/rename GPU checks')
        self.assertEqual(self.app.cursor,len(self.app.input))
        self.app.input='/rename Fleet health';self.app.send()
        restored=terminal.Terminal('http://localhost:1',True,self.app.state.path)
        self.assertEqual(restored.state.title,'Fleet health')

    def test_color_picker_previews_cancels_confirms_and_restores_per_conversation(self):
        first_id=self.app.state.conversation_id
        self.app.command('/color')
        self.key('right')
        self.assertEqual(self.app._color_choice,'cyan')
        self.assertEqual(self.app.settings.color,'green')
        self.assertTrue(any('cyan' in t and a & terminal.curses.A_BOLD for y,x,t,a in self.render_attrs()))
        self.assertEqual(terminal.WorkspaceState(self.app.cwd,self.app.state.path).settings['color'],'green')
        self.key('\x1b')
        self.assertFalse(self.app.color_menu)
        self.assertEqual(self.app.settings.color,'green')
        self.app.command('/color');self.key('right');self.key('\r')
        self.assertFalse(self.app.color_menu)
        self.assertEqual(self.app.settings.color,'cyan')
        self.assertEqual(terminal.Terminal('http://localhost:1',True,self.app.state.path).settings.color,'cyan')
        self.app.command('/new')
        self.app.command('/color purple')
        self.app.command('/switch '+first_id)
        self.assertEqual(self.app.settings.color,'cyan')
        self.app.command('/color invalid')
        self.assertEqual(self.app.settings.color,'cyan')
        self.assertIn('Choose a color',self.app.status)
        self.assertEqual(terminal.command_matches('/color c'),[('/color cyan','Use cyan')])

    def test_accent_preview_updates_terminal_pairs_and_cancel_restores_them(self):
        with patch.object(terminal.curses,'COLORS',256,create=True), \
             patch.object(terminal.curses,'init_pair') as pairs:
            self.render_attrs()
            pairs.assert_any_call(4,114,-1)
            self.app.command('/color');self.key('right')
            self.render_attrs()
            pairs.assert_any_call(4,80,-1)
            pairs.assert_any_call(7,80,234)
            self.key('\x1b')
            pairs.reset_mock()
            self.render_attrs()
            pairs.assert_any_call(4,114,-1)

    def test_speed_and_color_menus_fit_and_show_the_selected_value(self):
        for width,height in ((60,16),(100,40),(180,52)):
            for menu in ('speed','color'):
                self.app.color_menu=menu=='color'
                self.app._color_choice='cyan'
                self.app.speed_slider=menu=='speed'
                self.app.settings.speed=1
                drawn=self.render_attrs(width=width,height=height)
                title='Speech speed' if menu=='speed' else 'Conversation color'
                row=next(y for y,x,t,a in drawn if t==title)
                marker=next((y,x) for y,x,t,a in drawn if t=='▲')
                self.assertEqual(marker[0],row+3)
                self.assertTrue(any('Enter:confirm' in t and 'Esc:cancel' in t for y,x,t,a in drawn))
                self.assertFalse(any('█' in t for y,x,t,a in drawn))
                for y,x,t,a in drawn:
                    self.assertLess(y,height)
                    self.assertLess(x+len(t),width)
                if menu=='speed':
                    self.app.settings.speed=2
                    moved=next(x for y,x,t,a in self.render_attrs(width=width,height=height) if t=='▲')
                    self.assertGreater(moved,marker[1])
                    self.assertTrue(any(t=='Slower' for y,x,t,a in drawn))
                    self.assertTrue(any(t=='Faster' for y,x,t,a in drawn))

    def test_slider_bounds_cancel_and_commit(self):
        self.app.input='/speed';self.key('\r')
        self.assertTrue(self.app.speed_slider)
        for _ in range(30):self.key('right')
        self.assertEqual(self.app.settings.speed,2)
        self.key('\x1b');self.assertEqual(self.app.settings.speed,terminal.Settings().speed)
        self.app.input='/speed';self.key('\r');self.key('left');self.key('\r')
        self.assertEqual(self.app.settings.speed,round(terminal.Settings().speed-.1,1))
        self.assertFalse(self.app.speed_slider)

    def test_idle_redraw_is_skipped_and_scroll_stays_anchored(self):
        self.app.glyph_graphics=True;self.app.tray_open=True
        self.app.transcript=[('You','\n'.join('line '+str(i) for i in range(80)))]
        self.expand_all()
        calls=[]
        class Screen:
            def getmaxyx(self):return 40,100
            def erase(self):calls.append('erase')
            def refresh(self):pass
            def addnstr(self,*args):pass
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',return_value=0),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value='') as glyph,patch.object(terminal.time,'monotonic',return_value=100):
            screen=Screen();self.app.draw(screen);self.app.draw(screen)
            self.assertEqual(calls,['erase'])
            self.assertEqual(glyph.call_count,1)
            self.app.scroll=16;self.app.draw(screen)
            self.app.transcript.append(('Codsworth','new streamed text'))
            self.app.draw(screen)
            self.assertEqual(self.app.scroll,18)  # blank separator + body
            self.assertEqual(glyph.call_count,1)

    def test_short_window_reclaims_header_rows_and_phase_invalidates_cache(self):
        self.app.glyph_graphics=True
        self.app.transcript=[('Codsworth','\n'.join('line '+str(i) for i in range(20)))]
        self.expand_all()
        drawn=[]
        class Screen:
            def getmaxyx(self):return 16,60
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append((y,text[:n]))
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',return_value=0),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value='') as glyph,patch.object(terminal.time,'monotonic',return_value=100):
            screen=Screen();self.app.draw(screen)
            # One header row and one rule leave the transcript almost the whole window.
            self.assertGreaterEqual(sum(text.startswith('line ') for y,text in drawn),10)
            self.assertTrue(any(y==0 and text.startswith('Codsworth') for y,text in drawn))
            self.app.phase='speaking'
            self.app.draw(screen)
            # 60 columns cannot hold the tray, so no sprite is emitted at all,
            # but the phase change still invalidates the cached frame.
            self.assertEqual(glyph.call_count,0)
            self.assertTrue(any('Speaking' in text for y,text in drawn if y==15))

    def test_reduced_motion_keeps_idle_static_and_active_slower(self):
        self.app.settings.motion='reduced';self.app.glyph_graphics=True;self.app.tray_open=True
        class Screen:
            def getmaxyx(self):return 40,100
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,*args):pass
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',return_value=0),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value='') as glyph:
            with patch.object(terminal.time,'monotonic',return_value=100):self.app.draw(Screen())
            with patch.object(terminal.time,'monotonic',return_value=110):self.app.draw(Screen())
            self.assertEqual(glyph.call_count,1)
            self.app.phase='speaking'
            with patch.object(terminal.time,'monotonic',return_value=110):self.app.draw(Screen())
            self.assertEqual(glyph.call_args.args[2],int(110/.32)%32)

    def test_ready_command_result_remains_visible(self):
        self.app.phase='ready';self.app.status='Copied conversation to clipboard'
        self.app.glyph_graphics=True
        drawn=[]
        class Screen:
            def getmaxyx(self):return 40,100
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append(text[:n])
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',return_value=0),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''):
            self.app.draw(Screen())
        self.assertIn('Copied conversation to clipboard',drawn)

    def test_large_mid_draft_paste_preserves_existing_suffix(self):
        self.app.input='prefix SUFFIX';self.app.cursor=7
        self.app.handle_key(Key('x'*9000,kind='paste'))
        self.assertEqual(len(self.app.input),8000)
        self.assertTrue(self.app.input.endswith('SUFFIX'))
        self.assertIn('Paste shortened',self.app.status)

    def test_multiline_navigation_does_not_replace_draft_with_history(self):
        self.app.input_history=['unrelated previous prompt']
        self.app.input='first line\nsecond\nthird'
        self.app.cursor=14
        self.key('up');self.assertEqual(self.app.cursor,3)
        self.key('down');self.assertEqual(self.app.cursor,14)
        self.assertEqual(self.app.input,'first line\nsecond\nthird')
        self.key('up',2);self.assertEqual(self.app.input,'unrelated previous prompt')

    def test_double_control_c_exits_but_repeat_and_control_d_do_not(self):
        with patch.object(terminal.time,'monotonic',return_value=100):
            self.key('\x03')
            self.assertTrue(self.app.running)
            self.assertIn('Ctrl-C again',self.app.status)
            self.app.handle_key(Key('\x03',kind='repeat'))
            self.assertTrue(self.app.running)
            self.key('\x03');self.assertFalse(self.app.running)
        self.app.running=True
        self.app.input='abc';self.app.cursor=1
        self.key('\x04')
        self.assertTrue(self.app.running)
        self.assertEqual(self.app.input,'ac')

    def test_double_control_c_expires_or_disarms_after_editing(self):
        with patch.object(terminal.time,'monotonic',return_value=100):self.key('\x03')
        with patch.object(terminal.time,'monotonic',return_value=104):self.key('\x03')
        self.assertTrue(self.app.running)
        self.key('x')
        with patch.object(terminal.time,'monotonic',return_value=105):self.key('\x03')
        self.assertTrue(self.app.running)

    def test_page_down_modifiers_and_kitty_page_code(self):
        self.assertEqual(decode('\x1b[57355u').text,'page_down')
        event=decode('\x1b[6;5~')
        self.assertEqual(event.modifiers,4)
        self.app.scroll=200;self.app.handle_key(event)
        self.assertEqual(self.app.scroll,0)

    def test_legacy_and_kitty_edit_keys(self):
        for sequence,name in [('\x1b[D','left'),('\x1b[C','right'),('\x1b[H','home'),('\x1b[F','end'),('\x1b[3~','delete'),('\x1b[57350u','left')]:
            self.assertEqual(decode(sequence).text,name)
        event=decode('\x1b[13;2u');self.assertEqual(event.modifiers,1)
        self.assertEqual(decode('\x1b[1;1:2D').kind,'repeat')
        self.assertEqual(decode('\x1b[1;1:3D').kind,'release')


    def test_composer_has_a_continuous_visible_frame_around_every_draft_row(self):
        """The input stays bounded at compact sizes and with wrapped drafts."""
        for height,width,draft,slider in [(40,100,'',False),(16,60,'one\ntwo\nthree',False),
                                          (40,100,'A long draft '*60,False),(16,60,'',True)]:
            self.app.input=draft;self.app.glyph_graphics=True
            self.app.speed_slider=slider
            self.app._draw_key=None
            drawn=[]
            class Screen:
                def getmaxyx(self):return height,width
                def erase(self):pass
                def refresh(self):pass
                def addnstr(self,y,x,text,n,attr):
                    self_test.assertGreaterEqual(y,0)
                    self_test.assertLess(y,height)
                    self_test.assertLessEqual(x+n,width-1)
                    drawn.append((y,x,text[:n],attr))
                def move(self,y,x):
                    self.cursor=(y,x)
            self_test=self
            with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''):
                self.app.draw(Screen())
            rules=[item for item in drawn if set(item[2])=={'─'}]
            header_rule=next(r for r in rules if r[0]==1)
            self.assertEqual((header_rule[1],len(header_rule[2])),(1,width-2))
            shown,composer_top,top,previews,meta,padding=terminal.composer_metrics(
                height,len(terminal.composer_layout(draft,0,width-10)[0]),0,False)
            bottom=height-2
            # Inspect final cells, so a later chip/slider cannot erase a corner.
            cells={}
            for y,x,text,attr in drawn:
                for offset,char in enumerate(text):
                    cells[y,x+offset]=(char,attr)
            for y,left,right in ((top,'╭','╮'),(bottom,'╰','╯')):
                self.assertEqual(cells[y,2],(left,4))
                self.assertEqual(cells[y,width-3],(right,4))
            for y in range(top+1,bottom):
                self.assertEqual(cells[y,2],('│',4))
                self.assertEqual(cells[y,width-3],('│',4))
            self.assertTrue(any(self.app.composer_target() in t for _,_,t,_ in drawn))
            self.assertTrue(any(y==composer_top and x==4 and t=='›' for y,x,t,_ in drawn))
            if padding:
                # The row under the top rule is empty padding; the last inner row
                # is the meta line (target, context, speed).
                for x in range(3,width-3):
                    self.assertEqual(cells[top+1,x][0],' ')
                    self.assertIn(cells[top+1,x][1] & ~terminal.curses.A_BOLD,(6,7))
            self.assertEqual(sum(char in '›▸>' for (y,x),(char,attr) in cells.items()
                                 if top<=y<=bottom),1)

    def test_every_row_stays_inside_the_window_at_each_size(self):
        """Nothing may run off the edge, at any supported size, in any state."""
        cases=[('ready','',True,False,False,False),
               ('ready','A drafted request that is far too long to fit on one line '*8,False,False,False,False),
               ('thinking','',False,False,False,False),
               ('speaking','',False,False,False,False),
               ('recording','',False,False,True,False),
               ('ready','/spe',False,True,False,False),
               # Slice 3: the same sizes again with every failure state lit at once —
               # a service-muted chip, a held Enter, a blind tray and a dead stream.
               ('ready','',False,False,False,True),
               ('error','A draft the other window will not let me send '*4,False,False,False,True)]
        trays=[(False,'glance')]+[(True,view) for view in terminal.TRAY_VIEWS]
        for height,width in ((16,60),(24,80),(40,110),(50,200)):
            for (tray_open,tray_view),(phase,draft,muted,slider,recording,failing) in zip(
                    trays*len(cases),[case for case in cases for _ in trays]):
                with self.subTest(size=(width,height),phase=phase,slider=slider,tray=(tray_open,tray_view),failing=failing):
                    self.app.tray_open,self.app.tray_view=tray_open,tray_view
                    self.app.tray_data=self.tray_sample(failing)
                    self.app.state.title='A conversation title long enough to crowd the header row'
                    self.app.transcript=[('You','ask'),('Codsworth','A reply. '*40)]
                    self.app.input=draft;self.app.cursor=len(draft)
                    self.app.phase=phase;self.app.settings.muted=muted
                    self.app.speed_slider=slider;self.app.settings.speed=2 if slider else 1
                    self.app.recording=object() if recording else None
                    # The context meter shares the composer rule with the target
                    # label and the chip; it must never push either off the edge.
                    self.app.context=terminal.context_usage(int(262144*.86),262144) if failing else None
                    self.app.voice_failure=('Speech unavailable: SDL_OpenAudio: No available audio device'
                        ' · Voice service at http://localhost:1 · 2 tries') if failing else ''
                    self.app.conflict=self.tray_sample(True)['windows'] if failing else None
                    self.app._alert_until=200 if failing else None   # monotonic is pinned at 100
                    self.app.workstreams.records=[{'id':'a'*32,'title':'Billing reconciliation with a long title',
                        'status':'error','summary':'Hermes gateway at http://127.0.0.1:18042 · 2 tries',
                        'transcript':[],'draft':'','unread':True}] if failing else []
                    self.app.glyph_graphics=True
                    self.app._draw_key=None
                    drawn=[];self_test=self
                    class Screen:
                        def getmaxyx(self):return height,width
                        def erase(self):pass
                        def refresh(self):pass
                        def addnstr(self,y,x,text,n,attr):
                            self_test.assertGreaterEqual(y,0)
                            self_test.assertLess(y,height)
                            self_test.assertGreaterEqual(x,0)
                            self_test.assertLessEqual(x+n,width-1)
                            drawn.append((y,x,text[:n]))
                        def move(self,y,x):
                            self_test.assertLess(y,height)
                            self_test.assertLess(x,width)
                    with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''),patch.object(terminal.time,'monotonic',return_value=100):
                        self.app.draw(Screen())
                    rows={y for y,_,_ in drawn}
                    self.assertIn(0,rows)          # header
                    self.assertIn(1,rows)          # header rule
                    self.assertIn(height-2,rows)   # composer rule
                    self.assertIn(height-1,rows)   # footer
                    # The footer never crowds /help off the right edge.
                    self.assertTrue(any(y==height-1 and t=='/help' for y,_,t in drawn))
                    self.assertTrue(any(y==height-2 and t=='╰'+'─'*(width-6)+'╯' for y,_,t in drawn))
                    self.assertTrue(any(self.app.composer_target() in t for _,_,t in drawn))
                    if failing:
                        # Rule 3: the reason is on the footer of every frame, at every size.
                        self.assertTrue(any(y==height-1 and t.startswith('Another window is ahead')
                                            for y,_,t in drawn))
        self.app.recording=None
        self.app.context=None
        self.app.workstreams.records=[]
        self.app.conflict=None
        self.app.voice_failure=''
        self.app._alert_until=None

    def tray_sample(self,failing=False):
        """Realistic, deliberately over-long cached state for the tray renderers."""
        if failing:
            # Every source blind at once, with stale-but-good rows still shown.
            stale=[{'number':1234+n,'title':'A very long issue title '*4} for n in range(9)]
            source={'host':'Gitea','address':'https://git.example.com','tries':2,'ok':False}
            return {'fleet':None,
                'fleet_error':'Connection refused · Fleet via Codsworth at http://127.0.0.1:18042 · 1 try',
                'inbox':{'observed_at':'2026-09-07T10:00:00+00:00',
                    'available':{'issues':False,'pulls':False},'issues':[],'pulls':[],
                    'sources':{'issues':source,'pulls':dict(source)},
                    'errors':[{'source':'issues','message':'Corner issues unavailable; this is not an empty queue.',
                               'host':'Gitea','address':'https://git.example.com','tries':2}]},
                'last_good':{'issues':{'at':'2026-09-07T09:00:00+00:00','count':9,'records':stale},
                             'pulls':{'at':'2026-09-07T09:00:00+00:00','count':0,'records':[]},
                             'fleet':{'at':'2026-09-07T09:00:00+00:00','count':2,'records':[]}},
                'doctor':[{'name':'Voice service','status':'unavailable',
                           'detail':'No valid health response; check the service or tunnel. '*2}],
                'windows':{'reason':'Another window saved newer context while this one held a draft.',
                    'snapshot':'/Users/developer/.local/state/cods/workspaces/recovery/'+'b'*20+'-'+'c'*32+'.json',
                    'pid':4242,'other_pid':4243,'other_alive':True,'session_id':'e'*32}}
        return {'fleet':{'observed_at':'2026-09-07T10:00:00+00:00',
                    'available':{'questions':True,'attempts':True,'health':True,'status':True},
                    'counts':{'running':12,'queued':3},
                    'questions':[{'id':'q-1','text':'A question long enough to wrap several times '*3,
                                  'options':['An option that also wraps '*3,'A shorter option']}],
                    'attempts':[{'id':'a-1','state':'running','name':'An attempt with a long name '*3}]},
                'inbox':{'observed_at':'2026-09-07T10:00:00+00:00',
                    'available':{'issues':True,'pulls':False},
                    'issues':[{'number':1234+n,'title':'A very long issue title '*4} for n in range(9)],
                    'pulls':[],'errors':[{'source':'Gitea','message':'timed out after 3 tries'}]},
                'doctor':[{'name':'Voice service','status':'unavailable',
                           'detail':'No valid health response; check the service or tunnel. '*2}]}

    def test_tray_hint_names_only_the_key_this_terminal_can_deliver(self):
        """Rule 4: never print a hint for a key that cannot arrive."""
        without=[t for _,_,t in self.render()]
        self.assertIn('^T tray',without)
        self.assertTrue(any('^T:tray' in t for t in without))
        self.assertFalse(any('⌥' in t for t in without))
        self.app.key_protocol=True
        with_protocol=[t for _,_,t in self.render()]
        self.assertIn('⌥\\ tray',with_protocol)
        self.assertTrue(any('⌥\\:tray' in t for t in with_protocol))
        self.assertFalse(any('^T tray' in t for t in with_protocol))
        self.assertFalse(any('^T:tray' in t for t in with_protocol))

    def test_shut_tray_is_a_pull_tab_and_open_tray_takes_its_columns(self):
        self.app.tray_data=self.tray_sample()
        self.app.transcript=[('Codsworth','A reply that runs along the transcript.')]
        shut=self.render(width=110)
        self.assertEqual([t for y,x,t in shut if y==3 and x==107],['^T'])
        # Two columns is all the tab has, so a count above nine becomes a plain '+'.
        self.assertEqual([t for y,x,t in shut if x==107 and y in (5,6,7)],['!1','◐+','▲9'])
        self.assertTrue(any(y==2 and x==105 and t=='│' for y,x,t in shut))
        transcript=[x for y,x,t in shut if t.startswith('A reply')]
        self.app.tray_open=True
        opened=self.render(width=110)
        self.assertTrue(any(y==2 and x==84 and t=='At a glance' for y,x,t in opened))
        self.assertTrue(any(y==1 and x==82 and t=='┬' for y,x,t in opened))
        self.assertTrue(any(t.startswith('ctx') for _,_,t in opened))
        self.assertTrue(any(y==34 and x==82 and t=='┴' for y,x,t in opened))
        self.assertFalse(any(x==107 and y<34 for y,x,t in opened))
        self.assertEqual(transcript,[5])
        self.app.tray_view='fleet'
        wide=self.render(width=110)
        self.assertTrue(any(y==1 and x==69 and t=='┬' for y,x,t in wide))

    def test_transcript_sits_above_the_composer_not_at_the_top(self):
        self.app.transcript=[('You','one'),('Codsworth','two')]
        drawn=self.render()
        rows=sorted(y for y,_,t in drawn if y and t in ('You','one','Codsworth','two'))
        self.assertEqual(rows,[29,32])   # user padding plus gaps between turns and the composer
        self.app.transcript=[('You','\n'.join('line '+str(i) for i in range(200)))]
        self.expand_all()
        full=self.render()
        self.assertEqual(min(y for y,_,t in full if t.startswith('line ')),2)
        newest=max(t for _,_,t in full if t.startswith('line '))
        self.app.handle_key(Key('page_up'))
        scrolled=[t for _,_,t in self.render() if t.startswith('line ')]
        self.assertNotIn(newest,scrolled)              # PgUp still walks back
        self.assertIn('line 191',scrolled)

    def test_conversation_uses_prompt_and_dot_without_speaker_labels(self):
        self.app.transcript=[('You','My request'),('Codsworth','His reply')]
        drawn=self.render_attrs()
        user=next(y for y,x,t,a in drawn if t=='My request')
        reply=next(y for y,x,t,a in drawn if t=='His reply')
        self.assertIn((user,3,'›',12),drawn)
        self.assertIn((reply,3,'●',4),drawn)
        self.assertIn((user,5,'My request',13),drawn)
        self.assertIn((reply,5,'His reply',1),drawn)
        self.assertEqual(reply-user,3)
        self.assertFalse(any(y>0 and t in ('You','Codsworth') for y,x,t,a in drawn))

    def test_sent_message_background_covers_wrapping_and_inline_styles_only(self):
        for width in (60,100,160):
            with self.subTest(width=width):
                self.app.tray_open=True
                self.app.transcript=[('You','Please **check** `config.py`.\n\n'+'More details '*20),
                                     ('Codsworth','I will check.')]
                drawn=self.render_attrs(width=width,height=60)
                first=next(y for y,x,t,a in drawn if t.startswith('Please check'))
                reply=next(y for y,x,t,a in drawn if t=='I will check.')
                backgrounds=[(y,x,len(t)) for y,x,t,a in drawn if a==13 and t and not t.strip()]
                self.assertEqual([y for y,x,n in backgrounds],list(range(first-1,reply-1)))
                for padding_y in (first-1,reply-2):
                    self.assertFalse(any(y==padding_y and x<width-35 and t.strip()
                                         for y,x,t,a in drawn))
                    self.assertEqual(self.app._message_cells[padding_y],'')
                self.assertTrue(all(x==3 and x+n<width-3 for y,x,n in backgrounds))
                self.assertTrue(any(t=='check' and a==14|terminal.curses.A_BOLD for y,x,t,a in drawn))
                self.assertTrue(any(t=='config.py' and a==15 for y,x,t,a in drawn))
                self.assertTrue(any(t=='I will check.' and a==1 for y,x,t,a in drawn))

    def test_hint_is_muted_gray_and_sent_message_palette_has_a_shared_background(self):
        self.app.running=False
        with ExitStack() as stack:
            for name in ('curs_set','noecho','raw','noraw','mousemask','mouseinterval',
                         'set_escdelay','start_color','use_default_colors'):
                stack.enter_context(patch.object(terminal.curses,name,create=True))
            stack.enter_context(patch.object(terminal.curses,'has_colors',return_value=True))
            stack.enter_context(patch.object(terminal.curses,'COLORS',256,create=True))
            stack.enter_context(patch.object(terminal.curses,'color_pair',side_effect=lambda n:n))
            pairs=stack.enter_context(patch.object(terminal.curses,'init_pair'))
            stack.enter_context(patch.object(terminal.sys,'stdout',new=io.StringIO()))
            stack.enter_context(patch.object(self.app,'stop'))
            self.app.run(Mock())
        palette={call.args[0]:call.args[1:] for call in pairs.call_args_list}
        self.assertEqual(palette[9],(243,234))
        self.assertEqual(palette[1],(252,-1))
        for pair,foreground in ((12,180),(13,252),(14,255),(15,222)):
            self.assertEqual(palette[pair],(foreground,234))

    def test_replies_code_and_tools_have_distinct_indentation(self):
        self.app.transcript=[('Codsworth','The result is **ready**.\n```sh\nfleet status\n```'),
                             ('Tool','Ran tests.run')]
        drawn=self.render_attrs()
        body=next((y,x,a) for y,x,t,a in drawn if t=='The result is ready.')
        code=next((y,x,a) for y,x,t,a in drawn if t=='fleet status')
        tool_label=next((y,x,a) for y,x,t,a in drawn if t=='Tool')
        command=next((y,x,a) for y,x,t,a in drawn if t=='Ran tests.run')
        self.assertEqual(body[1:],(5,1))
        self.assertEqual(code[1:],(9,5))
        self.assertEqual(tool_label[1],7)
        self.assertEqual(command[1:],(9,5))
        self.assertIn((code[0],7,'',4),drawn)
        self.assertTrue(any(t=='ready' and x==19 and a==11|terminal.curses.A_BOLD for y,x,t,a in drawn))
        for width in (60,80,110):
            self.app.transcript=[('Codsworth','```sh\n'+'argument '*80+'\n```')]
            self.expand_all()
            for y,x,t,a in self.render_attrs(width=width):
                if a==5 and x==9:
                    self.assertLessEqual(x+sum(terminal.cell_width(c) for c in t),width-4)

    def test_only_completed_turns_get_a_divider(self):
        self.app.transcript=[('You','Question'),('Codsworth','Answer'),
                             ('Worked for','12s'),('You','Next question')]
        rows=self.render_attrs()
        dividers=[(y,t) for y,x,t,a in rows if 2<y<34 and t.startswith('── Worked for')]
        self.assertEqual(len(dividers),1)
        self.assertIn('Worked for 12s',dividers[0][1])
        self.assertFalse(any(2<y<34 and x<90 and t and set(t)=={'─'} for y,x,t,a in rows))
        rule=dividers[0][0]
        self.assertFalse(any(t.strip() for y,x,t,a in rows if y in (rule-1,rule+1) and x<90))
        self.app.transcript=[('You','Question'),('Codsworth','Answer')]
        self.assertFalse(any(2<y<34 and x<90 and '─' in t for y,x,t in self.render()))

    def test_supporting_summaries_nest_under_the_reply_with_neutral_prose(self):
        self.app.transcript=[('You','How are the GPUs?'),
                             ('Codsworth','Both endpoints are **serving**. Check `nvidia-smi`.'),
                             ('GPU/model health','Two endpoints active.'),
                             ('Fleet status','No open questions.'),('Worked for','31s')]
        drawn=self.render_attrs()
        for title,body in [('GPU/model health','Two endpoints active.'),
                           ('Fleet status','No open questions.')]:
            heading=next(y for y,x,t,a in drawn if t==title)
            self.assertIn((heading,7,title,5|terminal.curses.A_BOLD),drawn)
            self.assertIn((heading,5,'└',5),drawn)
            self.assertIn((heading+1,9,body,1),drawn)
            self.assertIn((heading+1,7,'│',3),drawn)
        self.assertTrue(any(t=='serving' and a==11|terminal.curses.A_BOLD for y,x,t,a in drawn))
        self.assertTrue(any(t=='nvidia-smi' and a==5 for y,x,t,a in drawn))
        self.assertTrue(any(t.startswith('Both endpoints') and a==1 for y,x,t,a in drawn))
        self.assertEqual(sum(t.startswith('── Worked for') for y,x,t,a in drawn),1)

    def test_standalone_panels_do_not_inherit_a_previous_reply_group(self):
        for boundary in [None,('Worked for','12s'),('You','/fleet'),('Notice','Interrupted'),('Error','Failed')]:
            with self.subTest(boundary=boundary):
                self.app.transcript=([('Codsworth','Previous reply'),boundary] if boundary else [])
                self.app.transcript.append(('Fleet status','Queue is empty.'))
                drawn=self.render_attrs()
                heading=next(y for y,x,t,a in drawn if t=='Fleet status')
                self.assertIn((heading,5,'Fleet status',4|terminal.curses.A_BOLD),drawn)
                self.assertIn((heading+1,7,'Queue is empty.',1),drawn)

    def test_supporting_panel_colors_counts_but_keeps_reply_prose_neutral(self):
        self.app.transcript=[('Codsworth','There are 35 queued.'),
                             ('Fleet status','24h: 35 queued / 23 PR\n2 open: qf1f9bf (#1838)')]
        drawn=self.render_attrs()
        heading=next(y for y,x,t,a in drawn if t=='Fleet status')
        self.assertIn((heading,7,'Fleet status',5|terminal.curses.A_BOLD),drawn)
        for token in ('24h','35','23','2'):
            self.assertTrue(any(t==token and a==4 for y,x,t,a in drawn))
        for token in ('qf1f9bf','#1838'):
            self.assertTrue(any(t==token and a==5 for y,x,t,a in drawn))
        reply=next(y for y,x,t,a in drawn if t=='There are 35 queued.')
        self.assertFalse(any(y==reply and t=='35' for y,x,t,a in drawn))

    def test_long_scrolled_turn_keeps_user_background_and_has_no_repeated_markers(self):
        for speaker,color in [('You',13),('Codsworth',1)]:
            self.app.transcript=[(speaker,'\n'.join('line '+str(i) for i in range(100)))]
            self.expand_all()
            self.app.scroll=12
            drawn=self.render_attrs()
            body=[(y,a) for y,x,t,a in drawn if t.startswith('line ')]
            self.assertGreater(len(body),20)
            for y,attr in body:
                self.assertEqual(attr,color)
                self.assertFalse(any(yy==y and x==3 and t.strip() for yy,x,t,a in drawn))

    def test_completed_turn_keeps_total_time_across_tools_and_reload(self):
        with patch.object(self.app.client,'submit',return_value=7), patch.object(terminal.time,'monotonic',return_value=100):
            self.app.input='Review this';self.app.send()
        with patch.object(terminal.time,'monotonic',return_value=130):
            self.app.events.put((7,dict(type='tool',tool={'state':'running'})));self.app.drain()
        self.assertTrue(any('Working (1m 12s)' in t for _,_,t in self.render(now=172)))
        with patch.object(terminal.time,'monotonic',return_value=172):
            self.app.events.put((7,dict(type='text',delta='Done')))
            self.app.events.put((7,dict(type='complete')));self.app.drain()
            self.app.events.put((7,dict(type='complete')));self.app.drain()
        self.assertEqual(self.app.transcript.count(('Worked for','1m 12s')),1)
        restored=terminal.Terminal('http://localhost:1',True,self.app.state.path)
        self.assertIn(('Worked for','1m 12s'),restored.transcript)
        self.assertTrue(any(t.startswith('── Worked for 1m 12s ─') for _,_,t in self.render()))

    def test_cancelled_and_failed_turns_never_claim_completed_work(self):
        for cancelled in (True,False):
            with patch.object(self.app.client,'submit',return_value=7):
                self.app.input='Try';self.app.send()
            if cancelled:self.app.stop()
            else:
                self.app.events.put((7,dict(type='error',message='Failed')));self.app.drain()
            self.app.events.put((7,dict(type='complete')));self.app.drain()
            self.assertFalse(any(label=='Worked for' for label,_ in self.app.transcript))

    def test_message_rules_inline_emphasis_and_input_background(self):
        self.app.transcript=[('You','Please check'),('Codsworth','# Result\nUse **care** with `config.py`.\n```py\n  value = 1\n```')]
        drawn=self.render_attrs()
        self.assertFalse(any(2<y<34 and x<90 and t and set(t)=={'─'} for y,x,t,a in drawn))
        self.assertTrue(any(t=='care' and a==11|terminal.curses.A_BOLD for y,x,t,a in drawn))
        self.assertTrue(any(t=='config.py' and a==5 for y,x,t,a in drawn))
        self.assertTrue(any(t=='Result' and a==4|terminal.curses.A_BOLD for y,x,t,a in drawn))
        self.assertTrue(any(t=='  value = 1' and a==5 for y,x,t,a in drawn))
        self.assertIn((37,3,' '*94,6),drawn)
        self.assertTrue(any(y==36 and x==6 and a==9 for y,x,t,a in drawn))
        self.app.recording=object()
        self.assertTrue(any('Listening' in t and a==10 for y,x,t,a in self.render_attrs()))
        self.app.recording=None

    def test_click_expands_inline_and_drag_copies_without_changing_draft(self):
        self.app.input='Unsent draft'
        self.app.transcript=[('You','Question'),('Codsworth','\n'.join('line '+str(i) for i in range(100)))]
        drawn=self.render()
        self.assertTrue(any('click to expand' in t for _,_,t in drawn))
        y=next(y for y,x,t in drawn if 'click to expand' in t)
        self.app.handle_key(Key('click',kind='mouse',x=10,y=y))
        self.app.handle_key(Key('release',kind='mouse',x=8,y=y))
        self.assertTrue(self.app._expanded_messages)
        expanded=self.render()
        self.assertTrue(any(t=='line 99' for _,_,t in expanded))
        self.assertFalse(any('click to expand' in t for _,_,t in expanded))
        self.app.handle_key(Key('wheel_up',kind='mouse'))
        self.assertEqual(self.app.scroll,3)
        expanded=self.render()
        y=next(y for y,x,t in expanded if t.startswith('line '))
        self.app.handle_key(Key('click',kind='mouse',x=5,y=y))
        self.app.handle_key(Key('drag',kind='mouse',x=8,y=y))
        self.assertEqual(self.app.selected_text(),'line')
        drawn=self.render_attrs()
        self.assertTrue(any(a & terminal.curses.A_REVERSE for _,_,_,a in drawn))
        with patch.object(terminal.sys,'stdout',new=io.StringIO()) as output:
            self.app.handle_key(Key('release',kind='mouse',x=8,y=y))
            self.assertEqual(output.getvalue(),'\x1b]52;c;bGluZQ==\x07')
        self.assertEqual(self.app.input,'Unsent draft')
        self.app.handle_key(Key('click',kind='mouse',x=8,y=37))
        self.assertIsNone(self.app._selection_start)
        self.assertIsNone(self.app._selection_end)
        self.assertEqual(len(self.app._expanded_messages),1)
        self.app.handle_key(Key('wheel_down',kind='mouse'))
        self.assertEqual(self.app.scroll,0)

    def test_ghostty_uses_compatible_curses_capabilities_and_restores_environment(self):
        with patch.dict(terminal.os.environ,TERM='xterm-ghostty'), patch.object(terminal.curses,'wrapper') as wrapper:
            wrapper.side_effect=lambda run:self.assertEqual(terminal.os.environ['TERM'],'xterm-256color')
            terminal.run_terminal(self.app)
            wrapper.assert_called_once_with(self.app.run)
            self.assertEqual(terminal.os.environ['TERM'],'xterm-ghostty')
        with patch.dict(terminal.os.environ,TERM='screen-256color'), patch.object(terminal.curses,'wrapper') as wrapper:
            wrapper.side_effect=lambda run:self.assertEqual(terminal.os.environ['TERM'],'screen-256color')
            terminal.run_terminal(self.app)

    def test_sgr_mouse_sequences_do_not_become_draft_text(self):
        decoder=Decoder()
        cases=[('\x1b[<64;10;12M',Key('wheel_up',kind='mouse',x=9,y=11)),
               ('\x1b[<65;10;12M',Key('wheel_down',kind='mouse',x=9,y=11)),
               ('\x1b[<0;10;12M',Key('click',kind='mouse',x=9,y=11))]
        for sequence,expected in cases:
            events=[event for char in sequence for event in decoder.feed(char)]
            self.assertEqual(events,[expected])
        self.assertEqual(decode('\x1b[<0;10;12m'),Key('release',kind='mouse',x=9,y=11))
        self.assertEqual(decode('\x1b[<32;10;12M'),Key('drag',kind='mouse',x=9,y=11))

    def test_playback_spectrum_comes_from_the_pcm_handed_to_the_player(self):
        """The level is measured from real samples, not invented from a clock."""
        levels=terminal.PlaybackLevels()
        self.assertIsNone(levels.window(10.,20))
        loud=array.array('h',[30000 if i%2 else -30000 for i in range(48000)]).tobytes()
        quiet=array.array('h',[300 if i%2 else -300 for i in range(48000)]).tobytes()
        levels.add(quiet,9.)   # one second of near-silence, audible from t=9
        levels.add(loud,10.)   # then a second of loud speech
        bar=levels.window(10.5,20,span=1.5)
        self.assertEqual(len(bar),20)
        self.assertEqual(bar[0],'▁')
        self.assertEqual(bar[-1],'█')
        self.assertIsNone(levels.window(20.,20))   # silence returns to a plain rule
        levels.add(loud,30.);levels.clear()
        self.assertIsNone(levels.window(30.5,20))

    def test_speaking_row_shows_the_spectrum_and_a_new_level_redraws(self):
        self.app.phase='speaking'
        erases=[];drawn=[]
        class Levels:
            value=None
            def window(self,now,cells,span=1.6):return self.value*cells if self.value else None
        class Screen:
            def getmaxyx(self):return 40,100
            def erase(self):erases.append(1)
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append((y,x,text[:n]))
            def move(self,*args):pass
        self.app.client.levels=Levels()
        screen=Screen();self.app._draw_key=None
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''),patch.object(terminal.time,'monotonic',return_value=100):
            self.app.draw(screen)
            self.assertTrue(any(y==38 and t=='╰'+'─'*94+'╯' for y,_,t in drawn))
            self.app.draw(screen)
            self.assertEqual(len(erases),1)   # silence still changes nothing
            drawn.clear()
            self.app.client.levels.value='▅'
            self.app.draw(screen)
            self.assertEqual(len(erases),2)   # a new level invalidates the frame
            self.assertTrue(any(y==38 and t=='▅'*94 for y,_,t in drawn))

    def test_working_shimmer_moves_across_letters_and_reduced_motion_is_static(self):
        frames=[terminal.shimmer_levels('Working',frame) for frame in range(32)]
        self.assertGreater(len({tuple(levels) for levels in frames}),10)
        for index in range(7):
            self.assertTrue(any(levels[index]==2 for levels in frames))
        self.assertEqual({tuple(terminal.shimmer_levels('Working',frame,True))
                          for frame in range(32)},{(0,)*7})

    def test_working_shimmers_at_reply_start_and_stops_when_ready(self):
        self.app.transcript=[('You','Go'),('Codsworth','')]
        with patch.object(terminal.time,'monotonic',return_value=0):
            self.app.set_phase('thinking','Thinking…')
        frames=[]
        for now in (0,.4,.8,1.2,1.6,2):
            drawn=self.render_attrs(now=now)
            heading=next(y for y,x,t,a in drawn if y>0 and t.startswith('Working ('))
            self.assertTrue(any(y==heading and x==5 and t.startswith('Working (')
                                for y,x,t,a in drawn))
            letters=[(x,t,a) for y,x,t,a in drawn if y==heading and 5<=x<12 and len(t)==1]
            self.assertEqual(''.join(t for x,t,a in letters),'Working')
            frames.append(tuple(a for x,t,a in letters))
            self.assertFalse(any(t.startswith('● Working') for y,x,t,a in drawn))
            self.assertTrue(any(y==39 and 'Esc stop' in t for y,x,t,a in drawn))
        self.assertGreater(len(set(frames)),3)
        self.app.settings.motion='reduced'
        for now in (0,1.1,1.8):
            drawn=self.render_attrs(now=now)
            self.assertTrue(all(a==2|terminal.curses.A_BOLD for y,x,t,a in drawn
                                if y==heading and 5<=x<12 and len(t)==1))
        self.app.set_phase('ready','Ready')
        self.assertFalse(any('Working' in t for y,x,t,a in self.render_attrs()))

    def test_working_moves_to_footer_when_reply_start_is_scrolled_out(self):
        self.app.transcript=[('Codsworth','Earlier reply'),('Worked for','1s'),
                             ('You','Go'),('Codsworth','\n'.join('line '+str(i) for i in range(100)))]
        self.expand_all()
        self.app.scroll=12
        self.app.set_phase('thinking','Thinking…')
        drawn=self.render_attrs()
        self.assertTrue(any(y==39 and t.startswith('Working (') and 'Esc stop' in t
                            for y,x,t,a in drawn))
        self.assertFalse(any(y!=39 and 'Working' in t for y,x,t,a in drawn))

    def test_shimmer_redraws_between_seconds_without_new_events(self):
        self.app.transcript=[('Codsworth','')]
        self.app.set_phase('thinking','Thinking…')
        screen=Mock()
        screen.getmaxyx.return_value=(40,100)
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n), \
             patch.object(terminal.sys,'stdout',new=io.StringIO()), \
             patch.object(terminal,'kitty_frame',return_value=''), \
             patch.object(terminal.time,'monotonic') as clock:
            clock.return_value=.4
            self.app.draw(screen)
            first=[call.args for call in screen.addnstr.call_args_list
                   if call.args[0]>0 and 5<=call.args[1]<12 and len(call.args[2])==1]
            screen.reset_mock()
            clock.return_value=.8
            self.app.draw(screen)
            screen.erase.assert_called_once()
            second=[call.args for call in screen.addnstr.call_args_list
                    if call.args[0]>0 and 5<=call.args[1]<12 and len(call.args[2])==1]
        self.assertNotEqual(first,second)

    def render(self,height=40,width=100,now=100.0):
        drawn=[]
        class Screen:
            def getmaxyx(self):return height,width
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append((y,x,text[:n]))
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''),patch.object(terminal.time,'monotonic',return_value=now):
            self.app._draw_key=None
            self.app.draw(Screen())
        return drawn

    def test_empty_composer_rotates_suggestions_and_holds_under_reduced_motion(self):
        start=terminal.SUGGESTION_SECONDS*len(terminal.SUGGESTIONS)*2
        first=[t for _,_,t in self.render(now=start)]
        later=[t for _,_,t in self.render(now=start+2*terminal.SUGGESTION_SECONDS)]
        self.assertIn(terminal.SUGGESTIONS[0],first)
        self.assertIn(terminal.SUGGESTIONS[2],later)
        self.assertTrue(all(item.startswith(('Ask','Try "')) for item in terminal.SUGGESTIONS))
        self.app.settings.motion='reduced'
        self.assertIn(terminal.SUGGESTIONS[0],[t for _,_,t in self.render(now=start+2*terminal.SUGGESTION_SECONDS)])
        self.app.settings.motion='full'
        self.app.input='drafting'
        self.assertNotIn(terminal.SUGGESTIONS[0],[t for _,_,t in self.render(now=start)])

    def test_speech_chip_sits_in_the_header_and_the_footer_pins_help_right(self):
        self.app.settings.muted=False
        self.app.settings.voice='expanded'
        drawn=self.render()
        # Row 0 is the one place speech mode is reported.
        header=next((x,t) for y,x,t in drawn if y==0 and '◐' in t)
        self.assertEqual(header[1],'◐ voice on · 1×')
        self.assertEqual(header[0]+len(header[1]),100-3)
        # Target, context and speed sit inside the box; the header is the one
        # place speech mode is reported in full.
        self.assertTrue(any('Home' in t and 'ctx' in t for _,_,t in drawn))
        footer=[(x,t) for y,x,t in drawn if y==39]
        self.assertTrue(any('Enter:send' in t for x,t in footer))
        self.assertTrue(any('hold Space:talk' in t for x,t in footer))
        self.assertIn((100-8,'/help'),footer)
        self.assertTrue(any(t=='At your service.' for _,_,t in drawn))
        self.app.settings.muted=True
        muted=self.render()
        self.assertTrue(any(y==0 and t=='☎ voice off · muted' for y,_,t in muted))
        self.assertTrue(any('Shift+Tab:unmute' in t for y,_,t in muted))
        self.assertFalse(any('Message' in t for _,_,t in muted))

    def test_shift_tab_cycles_speech_output_even_with_the_picker_open(self):
        self.assertEqual(decode('\x1b[Z'),Key('\t',modifiers=1))
        self.app.settings.muted=False
        self.app.input='/vo'
        self.assertTrue(self.app.completion_options())
        self.app.handle_key(Key('\t',modifiers=1))
        self.assertEqual(self.app.input,'/vo')
        self.assertTrue(self.app.settings.muted)
        self.assertIn('Shift+Tab cycles',self.app.status)
        self.app.handle_key(Key('\t',modifiers=1))
        self.assertFalse(self.app.settings.muted)
        self.app.handle_key(Key('\t'))
        self.assertEqual(self.app.input,'/voice')

    def test_active_status_reports_elapsed_seconds_and_the_interrupt_key(self):
        with patch.object(terminal.time,'monotonic',return_value=100):
            self.app.set_phase('thinking','Thinking…')
        drawn=self.render(now=112)
        # A compact indicator leaves the elapsed time and interrupt hint readable.
        waiting=next(t for y,_,t in drawn if y>0 and 'Working' in t)
        self.assertIn('Working (12s)',waiting)
        self.assertNotIn(self.app.state.conversation_id[:8],waiting)
        self.assertTrue(any(y==39 and t=='Esc stop' for y,x,t in drawn))
        self.assertNotIn('█',waiting)
        self.assertNotIn('%',waiting)
        with patch.object(terminal.time,'monotonic',return_value=140):
            self.app.set_phase('ready','Ready')
        self.assertTrue(any(t=='At your service.' for _,_,t in self.render(now=180)))



    def test_failures_render_red_while_ordinary_prose_is_neutral(self):
        self.app.transcript=[('Codsworth','All is well.'),('Error','Chat failed: upstream refused the turn')]
        self.app.set_phase('error','Chat failed: upstream refused the turn')
        drawn=[(x,t,a) for _,x,t,a in self.render_attrs()]
        label=next(a for _,t,a in drawn if t=='Error')
        body=next(a for _,t,a in drawn if t.startswith('Chat failed') )
        self.assertEqual(label & ~terminal.curses.A_BOLD,8)
        self.assertEqual(body,8)
        healthy=next(a for _,t,a in drawn if t=='Codsworth' and a!=8)
        self.assertEqual(healthy & ~terminal.curses.A_BOLD,4)
        self.assertEqual(next(a for _,t,a in drawn if t=='All is well.'),1)
        self.app.set_phase('ready','Ready')
        self.assertFalse(any(a==8 and t=='At your service.' for _,t,a in [(x,t,a) for _,x,t,a in self.render_attrs()]))

    def render_attrs(self,height=40,width=100,now=100.0):
        drawn=[]
        class Screen:
            def getmaxyx(self):return height,width
            def erase(self):pass
            def refresh(self):pass
            def addnstr(self,y,x,text,n,attr):drawn.append((y,x,text[:n],attr))
            def move(self,*args):pass
        with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),patch.object(terminal.sys,'stdout',new=io.StringIO()),patch.object(terminal,'kitty_frame',return_value=''),patch.object(terminal.time,'monotonic',return_value=now):
            self.app._draw_key=None
            self.app.draw(Screen())
        return drawn



    def test_quiet_upstream_turn_warns_then_reports_a_failure(self):
        with patch.object(terminal.time,'monotonic',return_value=100):
            self.app._reply_pending=True
            self.app._silent_since=100
            self.app.set_phase('thinking','Thinking…')
            self.app.check_stall()
        self.assertEqual(self.app.status,'Thinking…')
        with patch.object(terminal.time,'monotonic',return_value=100+terminal.STALL_NOTICE):
            self.app.check_stall()
        self.assertTrue(self.app.status.startswith('Waiting for Hermes'))
        self.assertIn('/new',self.app.status)
        self.assertTrue(self.app._reply_pending)
        with patch.object(terminal.time,'monotonic',return_value=100+terminal.STALL_LIMIT):
            self.app.check_stall()
        self.assertEqual(self.app.phase,'error')
        self.assertEqual(self.app.transcript[-1][0],'Error')
        self.assertIn('compacting',self.app.transcript[-1][1])
        self.assertNotIn('Reply interrupted',' '.join(t for _,t in self.app.transcript))
        self.assertFalse(self.app._reply_pending)
        self.assertIsNone(self.app._silent_since)

    def test_streaming_events_keep_a_slow_turn_alive(self):
        self.app.transcript=[('You','go'),('Codsworth','')]
        self.app.active_id=7
        self.app._reply_pending=True
        self.app._silent_since=100-terminal.STALL_LIMIT  # quiet long enough to fail
        with patch.object(terminal.time,'monotonic',return_value=100):
            self.app.events.put((7,{'type':'text','delta':'still working'}))
            self.app.drain()
        self.assertEqual(self.app._silent_since,100)  # the delta restarted the clock
        with patch.object(terminal.time,'monotonic',return_value=100+terminal.STALL_LIMIT-5):
            self.app.check_stall()
        self.assertNotEqual(self.app.phase,'error')
        self.assertIn('still working',self.app.transcript[-1][1])


    def test_context_survives_restart_resume_and_switch_but_resets_for_new_conversation(self):
        self.app.active_id=3
        self.app.events.put((3,dict(type='context',used=86016,limit=262144)))
        self.app.drain()
        self.app.save_state(force=True)
        original_id=self.app.state.conversation_id
        expected=(86016,262144,86016/262144)
        reopened=terminal.Terminal('http://localhost:1',True,self.app.state.path)
        self.assertEqual(reopened.context,expected)
        self.assertEqual(reopened.state.context,expected)
        reopened.context=None
        reopened.resume()
        self.assertEqual(reopened.context,expected)
        reopened.command('/new')
        self.assertIsNone(reopened.context)
        self.assertIsNone(terminal.WorkspaceState(reopened.cwd,reopened.state.path).context)
        reopened.command('/switch '+original_id)
        self.assertEqual(reopened.context,expected)

    def test_missing_context_remains_visible_without_inventing_a_reading(self):
        self.assertIsNone(self.app.context)
        for width,height in ((60,16),(100,40),(180,52)):
            drawn=self.render_attrs(width=width,height=height)
            self.assertTrue(any(t=='ctx —' for y,x,t,a in drawn))
            self.assertFalse(any(t.startswith('ctx ') and '%' in t for y,x,t,a in drawn))
        self.app.context=terminal.context_usage(86016,262144)
        self.assertTrue(any(t=='ctx 84k/256k · 32%' for y,x,t,a in self.render_attrs()))

    def test_context_meter_keeps_space_when_the_target_name_is_long(self):
        with patch.object(self.app,'composer_target',return_value='Long workstream name '*4):
            for width in (60,80,100):
                for context in (None,terminal.context_usage(262144,262144)):
                    self.app.context=context
                    drawn=self.render_attrs(width=width)
                    self.assertTrue(any(t.startswith('ctx ') for y,x,t,a in drawn))

    def test_context_usage_appears_in_the_composer_and_warns_before_it_overflows(self):
        self.app.settings.muted=False
        self.app.active_id=3
        self.app.events.put((3,{'type':'context','used':7131,'limit':262144}))
        self.app.drain()
        self.assertEqual(self.app.context,(7131,262144,7131/262144))
        # The meter sits on the inner meta line; the header remains the one
        # place speech mode is reported.
        meter=next(t for _,_,t in self.render() if t.startswith('ctx '))
        self.assertEqual(meter,'ctx 7k/256k · 2%')
        self.assertEqual(next(a for _,_,t,a in self.render_attrs() if t.startswith('ctx ')),9)
        self.assertNotIn('%',self.app.status)
        self.app.events.put((3,{'type':'context','used':int(262144*.86),'limit':262144}))
        self.app.drain()
        self.assertIn('85% of the model context',self.app.status)
        self.assertIn('/new',self.app.status)
        self.assertEqual(next(a for _,_,t,a in self.render_attrs() if t.startswith('ctx ')),10)
        # A narrow window keeps the shortest useful form rather than nothing, and
        # the meter never pushes anything past the right-hand edge.
        seen=set()
        for width in (60,80,110,200):
            rows=self.render(height=24,width=width)
            short=[(x,t) for y,x,t in rows if t.startswith('ctx ')]
            for x,text in short:
                seen.add(text)
                self.assertIn(text,('ctx 220k/256k · 85%','ctx 85%'))  # 220/256 is 85%; 225/256 was not
                self.assertLessEqual(x+len(text),width-1)
        self.assertIn('ctx 220k/256k · 85%',seen)
        self.assertTrue(seen <= {'ctx 220k/256k · 85%','ctx 85%'})
        self.assertIsNone(terminal.context_usage(None,262144))
        self.assertEqual(terminal.context_usage(300000,262144)[2],1.0)


if __name__=='__main__':unittest.main()


class ContextLabelTests(unittest.TestCase):
    def test_the_printed_pair_agrees_with_its_own_percentage(self):
        # A pair and a percentage on one line must describe one fraction. Mixed
        # divisors printed 225k/250k beside 87%, which reads as an arithmetic bug.
        for used, limit in ((7131, 262144), (225000, 262144), (131072, 262144),
                            (1000, 131072), (261000, 262144)):
            label = terminal.context_label((used, limit, used/limit))
            with self.subTest(label=label):
                shown, cap = (int(part[:-1]) for part in label.split()[1].split('/'))
                stated = int(label.rsplit('· ', 1)[1].rstrip('%'))
                self.assertLessEqual(abs(round(shown/cap*100)-stated), 1)
        # 262144 is "256k" everywhere it is written down.
        self.assertEqual(terminal.context_label((7131, 262144, 7131/262144)),
                         'ctx 7k/256k · 2%')


class MessageSpacingTests(unittest.TestCase):
    def test_a_reply_that_opens_with_newlines_does_not_leave_dead_rows(self):
        # The model paces its own prose and often streams '\n\n\nI must object'.
        # Rendered verbatim that put three empty rows between the speaker label
        # and the first thing they said.
        rendered = terminal.render_message("\n\n\nI must object.", 60)
        self.assertEqual(rendered[0], ('I must object.', 'body'))

    def test_one_blank_line_still_separates_paragraphs(self):
        rendered = terminal.render_message('First.\n\nSecond.', 60)
        self.assertEqual([line for line, _ in rendered], ['First.', '', 'Second.'])

    def test_a_run_of_blank_lines_collapses_to_one_and_trailing_ones_go(self):
        rendered = terminal.render_message('First.\n\n\n\nSecond.\n\n\n', 60)
        self.assertEqual([line for line, _ in rendered], ['First.', '', 'Second.'])

    def test_blank_lines_inside_a_code_fence_are_kept_verbatim(self):
        rendered = terminal.render_message('```\na\n\n\nb\n```', 60)
        self.assertEqual([line for line, role in rendered if role == 'code'],
                         ['a', '', '', 'b'])


class ResizeAndJointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = terminal.Terminal('http://localhost:1', True,
                                     Path(self.temp.name)/'state.json')

    def render(self, height, width, **kw):
        drawn = []
        app = self.app
        for key, value in kw.items():
            setattr(app, key, value)
        class Screen:
            def getmaxyx(self): return height, width
            def erase(self): pass
            def clear(self): drawn.append(('CLEAR', 0, '', 0))
            def refresh(self): pass
            def move(self, *a): pass
            def addnstr(self, y, x, t, n, a=0): drawn.append((y, x, t[:n], a))
        with patch.object(terminal.curses, 'color_pair', side_effect=lambda n: n), \
             patch.object(terminal.sys, 'stdout', new=io.StringIO()), \
             patch.object(terminal, 'kitty_frame', return_value=''):
            app.glyph_graphics = True
            app.draw(Screen())
        return drawn

    def test_the_tray_joint_never_lands_inside_the_context_meter(self):
        # 'ctx 312k/25┴k · 122%' was a real frame: the joint is painted after the
        # rule, and it used to overwrite whatever the meter had put there.
        self.app.tray_open, self.app.tray_view = True, 'glance'
        self.app.context = terminal.context_usage(240000, 262144)
        for width in range(90, 220, 2):
            drawn = self.render(40, width)
            row = [(x, t) for y, x, t, _ in drawn if y == 40-4]
            meter = next(((x, t) for x, t in row if t.startswith('ctx ')), None)
            if meter is None:
                continue
            with self.subTest(width=width):
                self.assertNotIn('┴', meter[1])
                joints = [x for x, t in row if t == '┴']
                for j in joints:
                    self.assertFalse(meter[0] <= j < meter[0]+len(meter[1]))

    def test_a_reported_prompt_over_the_limit_still_reads_as_a_hundred(self):
        self.assertEqual(terminal.context_usage(320000, 262144)[2], 1.0)
        self.assertIn('100%', terminal.context_label(terminal.context_usage(320000, 262144)))

    def test_resize_drops_the_memoised_frame_and_repaints_from_a_clean_slate(self):
        # Without this curses keeps the old geometry and every later row lands
        # in the wrong place: the terminal scrolls and fragments survive.
        self.render(40, 110)
        self.assertIsNotNone(self.app._draw_key)
        self.app._glyph_key = ('stale',)
        cleared = []
        class Screen:
            def getmaxyx(self): return 24, 80
            def erase(self): pass
            def clear(self): cleared.append(True)
            def refresh(self): pass
            def move(self, *a): pass
            def addnstr(self, *a): pass
        with patch.object(terminal.curses, 'color_pair', side_effect=lambda n: n), \
             patch.object(terminal.sys, 'stdout', new=io.StringIO()), \
             patch.object(terminal, 'kitty_frame', return_value=''):
            self.app.resize(Screen())
        self.assertTrue(cleared)                      # the slate is wiped
        self.assertIsNone(self.app._glyph_key)        # the sprite is re-emitted
        self.assertIsNotNone(self.app._draw_key)      # and a frame was drawn


class SpokenTextTests(unittest.TestCase):
    def test_markup_is_read_as_prose_not_punctuation(self):
        spoken = terminal.speakable(
            '1. #1613 [critical] No pull_request job may get a repo secret — infra-ci exposes it')
        for symbol in ('#', '[', ']', '—', '_'):
            self.assertNotIn(symbol, spoken)
        self.assertIn('number 1613', spoken)
        self.assertIn('critical', spoken)
        self.assertIn('pull request', spoken)

    def test_arrows_tildes_and_field_colons_become_words(self):
        self.assertIn(' to ', terminal.speakable('infer removed->added successions'))
        self.assertIn('about 20,698', terminal.speakable('the ~20,698 owners'))
        self.assertIn('severity s3', terminal.speakable('[high, severity:s3-degraded] Places'))

    def test_a_line_of_box_drawing_is_not_spoken_at_all(self):
        self.assertEqual(terminal.speakable('│ ◐ ────────'), '')
        self.assertEqual(terminal.speakable('---'), '')

    def test_links_and_emphasis_keep_the_words_and_drop_the_syntax(self):
        spoken = terminal.speakable('**Bold** and `code` and [a link](http://x/y)')
        self.assertIn('Bold', spoken)
        self.assertIn('a link', spoken)
        self.assertNotIn('http', spoken)
        self.assertNotIn('*', spoken)

    def test_the_transcript_itself_is_untouched(self):
        # speakable() is only on the speech path; what you read keeps every character.
        raw = '#1613 [critical] — infra-ci'
        self.assertEqual([line for line, _ in terminal.render_message(raw, 80)][0], raw)


class WaitingWordTests(unittest.TestCase):
    def test_a_long_or_multipart_question_says_he_is_taking_a_look(self):
        self.assertEqual(terminal.waiting_word('why is it waiting'), 'thinking')
        self.assertEqual(terminal.waiting_word('what broke? and can you fix it?'), 'taking a look')
        self.assertEqual(terminal.waiting_word('a'*200), 'taking a look')
        self.assertEqual(terminal.waiting_word('one\ntwo\nthree'), 'taking a look')
