"""Slice 3 — the five failure rules of docs/ui-butlers-tray.md, each exercised.

Every test here drives the real path that fails, not only the renderer that
reports it: a voice notice arriving on the event queue, a conflicting save on
disk, a workstream client emitting an error, an inbox read whose source raises.
"""
import asyncio
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
import terminal
import microphone
from inbox import read_inbox
from task_store import TaskStore
from test_workstreams import FakeClient

# render() patches curses.color_pair to the identity, so a drawn attribute is
# just its pair number: 8 is the failure pair, 2 the soft one, 4 the accent.
FAILURE,SOFT,ACCENT=8,2,4


def render(app,width=110,height=40):
    drawn=[]
    class Screen:
        def getmaxyx(self):return height,width
        def erase(self):pass
        def refresh(self):pass
        def addnstr(self,y,x,text,n,attr):drawn.append((y,x,text[:n],attr))
        def move(self,*args):pass
    app._draw_key=None
    with patch.object(terminal.curses,'color_pair',side_effect=lambda n:n),\
         patch.object(terminal.sys,'stdout',new=io.StringIO()),\
         patch.object(terminal,'kitty_frame',return_value=''):
        app.draw(Screen())
    return drawn


class OptionalChannelTests(unittest.TestCase):
    """Rule 1: a failure in an optional channel never delays the reply."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state'
        self.app=terminal.Terminal('http://localhost:1',muted=True,state_path=self.path)
        self.app.settings.muted=False           # Eric has speech switched on
        self.app.transcript=[('You','ask'),('Codsworth','The whole reply, already on screen.')]

    def voice_failure(self):
        self.app.events.put((self.app.active_id,dict(type='notice',channel='voice',
            message='Speech unavailable: SDL_OpenAudio: No available audio device'
                    ' · Voice service at http://localhost:1 · 2 tries')))
        self.app.drain()

    def test_a_dead_voice_channel_never_writes_the_workspace_mute_setting(self):
        self.voice_failure()
        self.assertFalse(self.app.settings.muted)            # the one detail that matters
        self.assertTrue(self.app.voice_failure)
        self.app.mark_dirty();self.app.save_state(force=True)
        self.assertFalse(json.loads(self.path.read_text())['settings']['muted'])
        reopened=terminal.Terminal('http://localhost:1',muted=False,state_path=self.path)
        self.assertFalse(reopened.state.settings.get('muted',False))

    def test_the_chip_goes_red_with_a_reason_and_clears_when_the_service_returns(self):
        self.voice_failure()
        drawn=render(self.app)
        chip=[(t,attr) for y,x,t,attr in drawn if y==0 and t.startswith('◌ voice off')]
        self.assertEqual(chip[0][0],'◌ voice off · service')
        self.assertEqual(chip[0][1],FAILURE)                         # failure, not accent
        self.assertTrue(any('/doctor' in t and 'voice off' in t for _,_,t,_ in drawn))
        # Playback resumes on its own: the next turn that speaks clears the state.
        self.app.events.put((self.app.active_id,dict(type='speaking')))
        self.app.drain()
        self.assertEqual(self.app.voice_failure,'')
        self.assertTrue(any(t.startswith('◐ voice on') for _,_,t,_ in render(self.app)))

    def test_the_reply_is_already_on_screen_and_the_turn_still_completes(self):
        self.app._reply_pending=True
        self.voice_failure()
        labels=[label for label,_ in self.app.transcript]
        self.assertEqual(labels,['You','Codsworth','Notice'])          # reply first, notice after
        self.assertEqual(self.app.transcript[1][1],'The whole reply, already on screen.')
        self.assertNotEqual(self.app.phase,'error')                    # the turn did not fail
        self.assertIn('Voice service at http://localhost:1',self.app.transcript[2][1])
        self.assertIn('2 tries',self.app.transcript[2][1])
        self.app.events.put((self.app.active_id,dict(type='complete')))
        self.app.drain()
        self.assertEqual(self.app.phase,'ready')                       # never stuck in error

    def test_a_real_voice_failure_travels_with_its_hop_and_try_count(self):
        events=queue.Queue()
        client=terminal.Client('http://localhost:1',terminal.Settings(),events)
        turn=terminal.Turn(1);turn.voice_attempts=3
        self.assertEqual(client.address(),'http://localhost:1')
        detail=client.hop_detail('Voice service',client.address(),turn.voice_attempts,'device gone')
        self.assertEqual(detail,'device gone · Voice service at http://localhost:1 · 3 tries')
        self.assertTrue(client.hop_detail('Hermes gateway','http://h:1',1).endswith('· 1 try'))


class FailedReadTests(unittest.TestCase):
    """Rule 2: a failed read is an error, never an empty list."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state'
        self.app=terminal.Terminal('http://localhost:1',muted=True,state_path=self.path)

    @staticmethod
    def inbox_payload(issues_ok):
        def invoke(mode,args):
            if args['action']=='list' and not issues_ok:
                raise OSError('private-token leaked here')
            kind='issues' if args['action']=='list' else 'pulls'
            return json.dumps({'success':True,'data':[{'number':7,'state':'open','title':'Fix Corner',
                'html_url':f'https://git.example.com/example/workspace/{kind}/7'}]})
        class NoFleet:
            def read(self,args):raise OSError('no fleet')
        return asyncio.run(read_inbox(invoke,NoFleet))

    def test_the_read_itself_reports_its_host_address_and_try_count(self):
        result=self.inbox_payload(issues_ok=False)
        self.assertFalse(result['available']['issues'])
        self.assertEqual(result['issues'],[])                      # empty, but never presented alone
        source=result['sources']['issues']
        self.assertEqual((source['host'],source['ok']),('Gitea',False))
        self.assertEqual(source['address'],'https://git.example.com')
        self.assertEqual(source['tries'],2)                        # it retried once
        self.assertEqual(result['errors'][0]['tries'],2)
        self.assertNotIn('private-token',json.dumps(result))       # still no leak
        self.assertTrue(result['available']['pulls'])              # one dead source, one live

    def deliver(self,result):
        self.app.events.put((-6,dict(token=self.app.ticket_generation,result=result,
                                     command='/inbox',label='Corner work inbox')))
        self.app.drain()

    def test_a_good_read_is_remembered_and_a_later_failure_shows_what_survived(self):
        good=self.inbox_payload(issues_ok=True)
        good['observed_at']='2026-09-07T09:00:00+00:00'
        self.deliver(good)
        self.assertEqual(self.app.tray_data['last_good']['issues']['count'],1)
        bad=self.inbox_payload(issues_ok=False)
        bad['observed_at']='2026-09-07T10:00:00+00:00'
        self.deliver(bad)
        lines=terminal.tray_inbox(self.app.tray_data)
        text=[line for line,_ in lines]
        self.assertIn('Unavailable — not an empty queue.',text)
        self.assertIn('Gitea at https://git.example.com',text)
        self.assertIn('2 tries, all failed',text)
        self.assertIn('Last good 2026-09-07T09:00:00+00:00 · 1 item',text)
        self.assertTrue(any(line.startswith('#7') and line.endswith('(stale)') for line in text))
        # One dead source cannot blank the tray: pull requests are still listed.
        self.assertIn('Pull requests',text)
        self.assertTrue(any(line.startswith('#7') and not line.endswith('(stale)') for line in text))

    def test_without_any_good_read_the_failure_says_so_rather_than_inventing_one(self):
        self.deliver(self.inbox_payload(issues_ok=False))
        text=[line for line,_ in terminal.tray_inbox(self.app.tray_data)]
        self.assertIn('No good read yet in this window.',text)
        glance=[line for line,_ in terminal.tray_glance(self.app.tray_data,[],None,TaskStore())]
        self.assertIn('Issues unavailable — not an empty queue.',glance)
        self.assertIn('Gitea at https://git.example.com',glance)

    def test_the_last_good_read_outlives_the_window_and_old_files_still_load(self):
        good=self.inbox_payload(issues_ok=True)
        good['observed_at']='2026-09-07T09:00:00+00:00'
        self.deliver(good)
        self.app.mark_dirty();self.app.save_state(force=True)
        stored=json.loads(self.path.read_text())['tray_last_good']
        self.assertEqual(stored['issues'],{'at':'2026-09-07T09:00:00+00:00','count':1})
        self.assertNotIn('records',stored['issues'])          # rows belong to their window
        reopened=terminal.Terminal('http://localhost:1',muted=True,state_path=self.path)
        self.assertEqual(reopened.tray_data['last_good']['issues']['count'],1)
        with tempfile.TemporaryDirectory() as directory:
            older=Path(directory)/'state.json'
            older.write_text(json.dumps(dict(version=3,draft='kept',title='Older window')))
            state=terminal.WorkspaceState(directory,older)
            self.assertEqual((state.tray_last_good,state.draft),({}, 'kept'))
        with tempfile.TemporaryDirectory() as directory:
            rubbish=Path(directory)/'state.json'
            rubbish.write_text(json.dumps(dict(version=3,tray_last_good={'issues':{'at':7,'count':'x'},
                                                                        '../etc':{'at':'a','count':1}})))
            self.assertEqual(terminal.WorkspaceState(directory,rubbish).tray_last_good,{})

    def test_a_fleet_failure_names_its_hop_instead_of_a_bare_message(self):
        with patch.object(self.app.client,'stream',side_effect=OSError('Connection refused')):
            self.app.refresh_fleet()
            for _ in range(200):
                if not self.app.events.empty():break
        self.app.drain()
        self.assertIsNone(self.app.tray_data['fleet'])
        text=[line for line,_ in terminal.tray_fleet(self.app.tray_data)]
        self.assertIn('Fleet unavailable.',text)
        self.assertTrue(any('Fleet via Codsworth at http://localhost:1 · 1 try' in line for line in text))
        self.assertIn('No good read yet in this window.',text)


class ConflictTests(unittest.TestCase):
    """Rule 3: never overwrite, never silently replay."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.json'
        self.first=terminal.Terminal('http://localhost:1',muted=True,state_path=self.path)
        self.stale=terminal.Terminal('http://localhost:1',muted=True,state_path=self.path)
        self.first.client.session_id='c'*32
        self.first.transcript.append(('Codsworth','Newer conversation'))
        self.first.mark_dirty();self.first.save_state(force=True)

    def test_enter_is_held_and_the_draft_is_kept_in_a_named_snapshot(self):
        self.stale.input='Unsent question';self.stale.cursor=3
        with patch.object(self.stale.client,'submit') as submit:
            self.stale.send()
            submit.assert_not_called()
        self.assertIsNotNone(self.stale.conflict)
        self.assertEqual(self.stale.input,'Unsent question')          # nothing was thrown away
        snapshot=Path(self.stale.conflict['snapshot'])
        self.assertEqual(snapshot.parent.name,'recovery')
        # Enter stays held on every later attempt, not only the first.
        with patch.object(self.stale.client,'submit') as submit:
            self.stale.send()
            self.stale.send()
            submit.assert_not_called()
        self.stale.stop()
        self.assertEqual(json.loads(snapshot.read_text())['draft'],'Unsent question')

    def test_the_reason_sits_on_the_footer_of_every_frame(self):
        self.stale.input='Unsent question'
        self.stale.send()
        for attempt in range(3):
            self.stale.status='An unrelated operation detail'   # cannot displace the reason
            footer=[t for y,_,t,_ in render(self.stale) if y==39]
            self.assertTrue(any(line.startswith('Another window is ahead') for line in footer),attempt)
            self.assertTrue(any('/resume' in line for line in footer),attempt)

    def test_the_windows_view_names_both_windows_and_the_snapshot(self):
        self.stale.input='Unsent question';self.stale.send()
        self.stale.command('/windows')
        self.assertEqual((self.stale.tray_open,self.stale.tray_view),(True,'windows'))
        text=[line for line,_ in terminal.tray_windows(self.stale.tray_data)]
        self.assertIn('The other window is ahead.',text)
        self.assertIn('This window',text)
        self.assertIn('Other window',text)
        self.assertIn('Transcript, draft, settings and session id:',text)
        self.assertIn(self.stale.conflict['snapshot'],text)
        self.assertTrue(any(line.startswith('pid '+str(self.stale.conflict['pid'])) for line in text))
        self.assertIn('Enter is held. /resume adopts the newer conversation and keeps this draft.',text)
        # With no conflict at all the view says so instead of raising an alert.
        quiet=terminal.tray_windows({})
        self.assertEqual(quiet[0],('This window owns the conversation.','soft'))
        self.assertFalse(any(role=='alert' for _,role in quiet))

    def test_resume_releases_the_hold_and_keeps_the_draft(self):
        self.stale.input='Unsent question';self.stale.cursor=3;self.stale.send()
        self.assertTrue(self.stale.resume())
        self.assertIsNone(self.stale.conflict)
        self.assertNotIn('windows',self.stale.tray_data)
        self.assertEqual((self.stale.input,self.stale.cursor),('Unsent question',3))
        self.assertIn(('Codsworth','Newer conversation'),self.stale.transcript)
        footer=[t for y,_,t,_ in render(self.stale) if y==39]
        self.assertFalse(any(line.startswith('Another window is ahead') for line in footer))
        with patch.object(self.stale.client,'submit',return_value=1) as submit:
            self.stale.send()
            submit.assert_called_once()


class MicrophoneTests(unittest.TestCase):
    """Rule 4: say what to do, and say it in red only once."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=terminal.Terminal('http://localhost:1',muted=True,
                                   state_path=Path(self.temp.name)/'state')

    def test_the_help_names_the_pane_and_the_reopen_people_miss(self):
        self.assertIn('System Settings → Privacy & Security → Microphone',microphone.MIC_HELP)
        self.assertIn('reopen this window',microphone.MIC_HELP)
        self.app.events.put((-2,dict(type='microphone_error',token=self.app.capture_generation,
                                     message=microphone.MIC_HELP)))
        self.app.drain()
        self.assertIn('reopen this window',self.app.status)

    def empty_capture(self):
        self.app.set_phase('transcribing')
        self.app.events.put((-2,dict(type='transcribed',token=self.app.capture_generation,text='')))
        self.app.drain()

    def test_an_empty_capture_is_red_once_then_fades_to_the_status_row(self):
        with patch.object(terminal.time,'monotonic',return_value=100):
            self.empty_capture()
            self.assertEqual(self.app.phase,'ready')          # never an error phase
            self.assertEqual(self.app._alert_until,100+terminal.NOTICE_FADE)
            footer=[(t,attr) for y,_,t,attr in render(self.app) if y==39]
            red=[attr for t,attr in footer if t.startswith('No speech recognized')]
            self.assertEqual(red,[FAILURE])
        with patch.object(terminal.time,'monotonic',return_value=100+terminal.NOTICE_FADE+1):
            self.app.drain()                                  # the queue is empty: it fades
            self.assertIsNone(self.app._alert_until)
            footer=[(t,attr) for y,_,t,attr in render(self.app) if y==39]
            soft=[attr for t,attr in footer if t.startswith('No speech recognized')]
            self.assertEqual(soft,[SOFT])
            # A second empty capture in a row does not raise the alarm again.
            self.empty_capture()
            self.assertIsNone(self.app._alert_until)
            self.assertIn('No speech recognized',self.app.status)

    def test_recognised_speech_re_arms_the_one_off_alert(self):
        with patch.object(terminal.time,'monotonic',return_value=100):
            self.empty_capture()
            self.app._alert_until=None
            self.app.input='a typed draft'
            self.app.events.put((-2,dict(type='transcribed',token=self.app.capture_generation,text='hello')))
            self.app.drain()
            self.assertFalse(self.app._empty_capture_seen)
            self.empty_capture()
            self.assertEqual(self.app._alert_until,100+terminal.NOTICE_FADE)


class WorkstreamFailureTests(unittest.TestCase):
    """Rule 5: one failure never halts the rest."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=terminal.Terminal('http://localhost:1',True,Path(self.temp.name)/'state')
        self.clients=[]
        def factory(events):
            client=FakeClient(events);self.clients.append(client);return client
        self.app.workstreams.client_factory=factory
        self.app.command('/stream new Fix filters');self.doomed=self.app.workstreams.selected
        self.app.input='Investigate filters';self.app.send()
        self.app.command('/stream new Billing');self.healthy=self.app.workstreams.selected
        self.app.input='Investigate billing';self.app.send()
        self.app.command('/focus home')
        self.home=list(self.app.transcript)

    def fail_the_first(self):
        self.clients[0].emit('error',message='Connection reset · Hermes gateway at http://127.0.0.1:18042 · 2 tries')
        self.app.drain()

    def test_a_dead_stream_is_one_red_line_in_home_with_its_cause(self):
        self.fail_the_first()
        added=[row for row in self.app.transcript if row not in self.home]
        red=[row for row in added if row[0] in terminal.ERROR_LABELS]
        self.assertEqual(len(red),1)
        self.assertIn(self.doomed[:8],red[0][1])
        self.assertIn('Hermes gateway at http://127.0.0.1:18042 · 2 tries',red[0][1])
        self.assertIn('/focus '+self.doomed[:8],red[0][1])
        # It is red in the transcript because the label is an error label.
        self.assertTrue(any(t==red[0][0] and attr==FAILURE|terminal.curses.A_BOLD
                            for _,_,t,attr in render(self.app)))
        self.app.drain();self.app.drain()
        self.assertEqual([row for row in self.app.transcript if row[0] in terminal.ERROR_LABELS],red)

    def test_the_other_stream_keeps_running_and_nothing_steals_focus_or_speech(self):
        phase=self.app.phase
        self.fail_the_first()
        self.assertIsNone(self.app.workstreams.selected)            # still Home
        self.assertEqual(self.app.phase,phase)                      # no phase change, no speech
        self.assertEqual(self.app.workstreams.find(self.healthy)['status'],'running')
        self.clients[1].emit('text',delta='Billing result');self.clients[1].emit('complete')
        self.app.drain()
        self.assertEqual(self.app.workstreams.find(self.healthy)['status'],'completed')

    def test_the_failed_transcript_stays_readable_through_focus(self):
        self.fail_the_first()
        self.app.command('/focus '+self.doomed[:8])
        self.assertEqual(self.app.workstreams.selected,self.doomed)
        self.assertIn('Connection reset',str(self.app.focused_stream()['transcript']))
        rows=terminal.stream_rows(self.app.workstreams.records,self.doomed)
        failed=[row for row in rows if row[0].strip().startswith('[error]')]
        self.assertEqual(len(failed),1)
        self.assertEqual(failed[0][1],'alert')
        self.assertIn('Connection reset',failed[0][0])
        # A running stream is not an alert.
        self.assertTrue(all(role!='alert' for line,role in rows if '[running]' in line))


class HopNamingTests(unittest.TestCase):
    """Under all five: name the hop and its address, count the tries."""
    def test_a_hermes_failure_is_no_longer_a_generic_apology(self):
        events=queue.Queue()
        client=terminal.Client('http://localhost:1',terminal.Settings(muted=True),events,player_path=None)
        def stream(*args,**kwargs):
            raise OSError('Connection reset by peer')
            yield
        client.stream=stream
        client.run(terminal.Turn(1),'hello')
        messages=[]
        while not events.empty():
            event=events.get_nowait()[1]
            if event['type']=='error':
                messages.append(event['message'])
        self.assertEqual(len(messages),1)
        self.assertIn('Connection reset by peer',messages[0])
        self.assertIn('Hermes gateway at http://localhost:1',messages[0])
        self.assertTrue(messages[0].endswith('· 1 try'))
        self.assertNotIn('Traceback',messages[0])

    def test_a_quiet_hermes_turn_names_the_gateway_it_waited_on(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
            app._reply_pending=True
            with patch.object(terminal.time,'monotonic',return_value=100):
                app._silent_since=100-terminal.STALL_NOTICE-1
                app.check_stall()
                self.assertIn('Waiting for Hermes at http://localhost:1',app.status)
                app._silent_since=100-terminal.STALL_LIMIT-1
                app._reply_pending=True
                app.check_stall()
            failure=[text for label,text in app.transcript if label=='Error']
            self.assertTrue(failure[-1].endswith('Hermes gateway at http://localhost:1 · 1 try'))

    def test_a_ticket_read_failure_names_the_service_it_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state')
            with patch.object(app.client,'stream',side_effect=OSError('refused')):
                app.refresh_tickets('',inbox=True)
                for _ in range(200):
                    if not app.events.empty():break
            app.drain()
            reported=[text for label,text in app.transcript if label=='Corner work inbox']
            self.assertIn('Codsworth service at http://localhost:1 · 1 try',reported[-1])


if __name__ == '__main__':
    unittest.main()
