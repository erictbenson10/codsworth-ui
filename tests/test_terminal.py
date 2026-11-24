import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODULE = Path(__file__).resolve().parents[1] / 'agent/terminal.py'
spec = importlib.util.spec_from_file_location('cods_terminal', MODULE)
terminal = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = terminal
spec.loader.exec_module(terminal)


class TerminalTests(unittest.TestCase):
    def test_one_player_prefetches_next_clause_before_previous_playback_finishes(self):
        written, waited, requests = bytearray(), [], []
        class Input:
            closed = False
            def write(self, data): written.extend(data)
            def flush(self): pass
            def close(self): self.closed = True
        class Player:
            stdin, returncode = Input(), None
            def poll(self): return self.returncode
            def wait(self, timeout=None):
                waited.append(True)
                self.returncode = 0
                return 0
        client = terminal.Client('http://localhost:1', terminal.Settings(speed=1.3), queue.Queue(), player_path='/fake/ffplay')
        def stream(path, payload, turn, **kwargs):
            requests.append(payload['text'])
            self.assertEqual(waited, [], 'Next clause waited for ffplay to finish')
            yield dict(type='audio', pcm=base64.b64encode(payload['text'].encode()*2).decode())
            yield dict(type='done')
        client.stream = stream
        speech = queue.Queue()
        for text in ('one', 'two', 'three', None): speech.put(text)
        with patch.object(terminal.subprocess, 'Popen', return_value=Player()) as launch:
            client.speak(terminal.Turn(1), speech)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(requests, ['one', 'two', 'three'])
        self.assertEqual(written, b'oneonetwotwothreethree')
        self.assertTrue(waited)

    def test_player_failure_does_not_deadlock_chat_with_more_than_four_clauses(self):
        class Input:
            closed = False
            def write(self, data): raise BrokenPipeError('device vanished')
            def close(self): self.closed = True
        class Player:
            stdin, returncode = Input(), 1
            def poll(self): return 1
            def wait(self, timeout=None): return 1
        events = queue.Queue()
        client = terminal.Client('http://localhost:1', terminal.Settings(speed=1), events, player_path='/fake/ffplay')
        def stream(path, payload, turn, **kwargs):
            if path == '/api/chat':
                yield dict(type='start', session_id='a'*32, turn_id='b'*32)
                for index in range(20):
                    yield dict(type='text', delta=f'This is sentence number {index}. ')
                yield dict(type='done')
            else:
                yield dict(type='audio', pcm=base64.b64encode(b'\0\0'*100).decode())
                yield dict(type='done')
        client.stream = stream
        with patch.object(terminal.subprocess, 'Popen', return_value=Player()):
            worker = threading.Thread(target=client.run, args=(terminal.Turn(1), 'Test'), daemon=True)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), 'Playback failure blocked the bounded clause producer')
        received = []
        while not events.empty(): received.append(events.get_nowait()[1])
        self.assertEqual(sum(event['type']=='notice' for event in received), 1)
        self.assertTrue(any(event['type']=='complete' for event in received))
        self.assertEqual(sum(event['type']=='text' for event in received), 20)

    def test_accelerated_lead_covers_measured_slow_supply_without_losing_tail(self):
        # Cadence representative of the live1.5× trace: 100ms media every130ms.
        lead = terminal.PlaybackLead('x'*120, 1.5)
        raw, played, start, supplied, deficit = [], [], None, 0, 0
        for index in range(45):
            pcm, arrival = bytes([index])*9600, .4+index*.13
            raw.append(pcm)
            outgoing = lead.push(pcm, arrival)
            if outgoing:
                if start is None:
                    start = arrival
                deficit = max(deficit, arrival-start-supplied/96000)
                supplied += len(outgoing)
                played.append(outgoing)
        played.append(lead.finish())
        self.assertEqual(b''.join(played), b''.join(raw))
        self.assertEqual(deficit, 0)
        self.assertGreater(start, .4)
        self.assertLess(start, .4+44*.13)  # Playback began before generation ended.

    def test_normal_speed_has_no_added_lead_and_faster_lead_is_bounded(self):
        pcm = b'\0\1'*4800
        self.assertEqual(terminal.PlaybackLead('A line.', 1).push(pcm, 0), pcm)
        lead = terminal.PlaybackLead('x'*180, 2)
        for index in range(30):
            lead.push(pcm, index*.2)
            self.assertLessEqual(len(lead.buffer), 192000)
        self.assertTrue(lead.started)

    def test_short_recording_flushes_buffered_tail_exactly_once(self):
        lead = terminal.PlaybackLead('Short.', 1.5)
        data = b'\0\1'*4800
        self.assertEqual(lead.push(data, 0), b'')
        self.assertEqual(lead.finish(), data)
        self.assertEqual(lead.finish(), b'')

    def test_responsive_layout_keeps_actual_glyph_and_readable_conversation(self):
        for width in (60, 80, 86, 92, 120, 180):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as directory:
                left, conversation, panel = terminal.layout_columns(width)
                self.assertGreater(left, 0)
                self.assertGreaterEqual(conversation, 36)
                self.assertEqual(left+conversation+panel+4, width)
                class Screen:
                    def getmaxyx(self): return 36, width
                    def erase(self): pass
                    def addnstr(self, *args): pass
                    def move(self, *args): pass
                    def refresh(self): pass
                app = terminal.Terminal('http://localhost:1', muted=True, state_path=Path(directory)/'state')
                app.glyph_graphics = True
                app.tray_open = True  # the sprite lives at the foot of the open tray
                with patch.object(terminal.curses, 'color_pair', return_value=0), \
                     patch.object(terminal, 'kitty_frame', return_value='ACTUAL_GLYPH') as frame, \
                     patch.object(terminal.sys, 'stdout', new=io.StringIO()) as output:
                    app.draw(Screen())
                    # The sprite lives at the foot of the tray column, which is only
                    # reserved once the transcript can spare it.
                    if width >= 96:
                        frame.assert_called_once()
                        self.assertIn('ACTUAL_GLYPH', output.getvalue())
                    else:
                        frame.assert_not_called()
        self.assertEqual(terminal.layout_columns(80)[2], 0)

    def test_untrusted_text_cannot_emit_terminal_controls(self):
        text = terminal.safe_text('\x1b[31mRed\x1b[0m\x1b]0;forged title\x07\x00\u202e!\nNext')
        self.assertEqual(text, 'Red!\nNext')

    def test_clauses_release_before_whole_turn_and_bound_long_sentences(self):
        ready, remainder = terminal.clauses('Very good, sir. Let us plan')
        self.assertEqual(ready, ['Very good, sir.'])
        self.assertEqual(remainder, 'Let us plan')
        ready, remainder = terminal.clauses('word ' * 70)
        self.assertTrue(ready)
        self.assertLessEqual(len(ready[0]), 180)
        self.assertEqual(terminal.clauses(remainder, final=True)[1], '')

    def test_settings_validate_before_mutation(self):
        settings = terminal.Settings()
        for command in ['/speed 0.1', '/speed nan', '/speed 9', '/voice fake', '/humor 5']:
            with self.assertRaises(ValueError):
                terminal.setting_command(command, settings)
        self.assertEqual(settings.speed, 1.0)
        terminal.setting_command('/speed 1.4', settings)
        terminal.setting_command('/voice pilot', settings)
        self.assertEqual((settings.speed, settings.voice), (1.4, 'pilot'))

    def test_short_opener_does_not_delay_following_complete_sentence(self):
        ready, pending = terminal.clauses('Yes. Let us review the plan. The next')
        self.assertEqual(ready, ['Yes. Let us review the plan.'])
        self.assertEqual(pending, 'The next')

    def test_large_model_delta_never_exceeds_speech_clause_limit(self):
        text = 'word ' * 160 + '. Next sentence. '
        ready, pending = terminal.clauses(text, final=True)
        self.assertEqual(pending, '')
        self.assertTrue(all(len(clause) <= 180 for clause in ready))
        self.assertEqual(' '.join(ready).split(), text.split())

    def test_workspace_panels_persist_without_touching_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            app = terminal.Terminal('http://localhost:1', muted=True, state_path=path)
            app.command('/todo Make tea')
            app.update_panel('today', 'Today', 'Plan a short walk')
            restored = terminal.Terminal('http://localhost:1', muted=True, state_path=path)
            self.assertEqual(restored.tasks, ['Make tea'])
            self.assertEqual(restored.state.panels['today']['content'], 'Plan a short walk')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restored.active_id = 8
            restored.events.put((8, dict(type='session', session_id='a'*32, model='Qwen', runtime='Hermes')))
            restored.drain()
            reopened = terminal.Terminal('http://localhost:1', muted=True, state_path=path)
            self.assertEqual(reopened.client.session_id, 'a'*32)
            self.assertEqual(restored.backend_label, 'Hermes · Qwen')
            restored.command('/done 1')
            self.assertEqual(terminal.WorkspaceState('/unused', path).tasks, ['[done] Make tea'])

    def test_chat_retries_busy_session_after_interruption(self):
        client = terminal.Client('http://localhost:1', terminal.Settings(muted=True), queue.Queue())
        attempts = []
        def stream(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise terminal.ServiceError(409, 'Previous turn cancelling')
            yield dict(type='done')
        client.stream = stream
        self.assertEqual(list(client.chat_events({}, terminal.Turn(1))), [dict(type='done')])
        self.assertEqual(len(attempts), 2)

    def test_chat_resends_when_the_socket_drops_before_any_event_arrives(self):
        client = terminal.Client('http://localhost:1', terminal.Settings(muted=True), queue.Queue())
        attempts = []
        def stream(*args, **kwargs):
            attempts.append(True)
            if len(attempts) < 3:
                raise ConnectionResetError(54, 'Connection reset by peer')
            yield dict(type='done')
        client.stream = stream
        turn = terminal.Turn(1)
        self.assertEqual(list(client.chat_events({}, turn)), [dict(type='done')])
        self.assertEqual((len(attempts), turn.chat_attempts), (3, 3))

    def test_chat_gives_up_after_three_dropped_sockets_and_counts_them(self):
        client = terminal.Client('http://localhost:1', terminal.Settings(muted=True), queue.Queue())
        def stream(*args, **kwargs):
            raise ConnectionResetError(54, 'Connection reset by peer')
            yield
        client.stream = stream
        turn = terminal.Turn(1)
        with self.assertRaises(ConnectionResetError):
            list(client.chat_events({}, turn))
        self.assertEqual(turn.chat_attempts, 3)

    def test_chat_never_resends_once_text_has_reached_the_screen(self):
        client = terminal.Client('http://localhost:1', terminal.Settings(muted=True), queue.Queue())
        attempts = []
        def stream(*args, **kwargs):
            attempts.append(True)
            yield dict(type='text', delta='Half a ')
            raise ConnectionResetError(54, 'Connection reset by peer')
        client.stream = stream
        with self.assertRaises(ConnectionResetError):
            list(client.chat_events({}, terminal.Turn(1)))
        self.assertEqual(len(attempts), 1)

    def test_a_reset_on_the_tunnel_port_names_the_machine_behind_it(self):
        reset = ConnectionResetError(54, 'Connection reset by peer')
        with patch.dict(os.environ, {'CODS_TUNNEL_TARGET': '192.0.2.192:8042'}):
            detail = terminal.Client.hop_detail('Hermes gateway', 'http://127.0.0.1:18042', 3, reset)
            self.assertEqual(detail, '[Errno 54] Connection reset by peer · Hermes gateway at http://127.0.0.1:18042 '
                             '· 3 tries · the SSH tunnel is up but nothing answered at 192.0.2.192:8042 behind it; '
                             'is the voice gateway container running?')
            refused = ConnectionRefusedError(61, 'Connection refused')
            self.assertTrue(terminal.Client.hop_detail('Hermes gateway', 'http://127.0.0.1:18042', 1, refused)
                            .endswith('· 1 try · nothing is listening on the local port; the SSH tunnel is not up'))
            # A real remote address is its own explanation, and so is a non-socket error.
            self.assertTrue(terminal.Client.hop_detail('Hermes gateway', 'http://192.0.2.192:8042', 1, reset)
                            .endswith('· 1 try'))
            self.assertTrue(terminal.Client.hop_detail('Hermes gateway', 'http://127.0.0.1:18042', 1, 'device gone')
                            .endswith('· 1 try'))
        with patch.dict(os.environ, {'CODS_TUNNEL_TARGET': ''}):
            self.assertTrue(terminal.Client.hop_detail('Hermes gateway', 'http://127.0.0.1:18042', 1, reset)
                            .endswith('· 1 try · the SSH tunnel is up but its far end did not answer'))

    def test_stale_model_events_and_unapproved_panels_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            app = terminal.Terminal('http://localhost:1', muted=True, state_path=Path(directory)/'state')
            app.active_id = 2
            initial = app.panel
            app.events.put((1, dict(type='action', action=dict(name='show_panel', panel_id='today', title='bad', content='stale'))))
            app.events.put((2, dict(type='action', action=dict(name='show_panel', panel_id='shell', title='bad', content='execute'))))
            app.drain()
            self.assertEqual(app.panel, initial)
            app.events.put((2, dict(type='action', action=dict(name='show_panel', panel_id='today', title='Today', content='\x1b[31mTea'))))
            app.drain()
            self.assertEqual(app.panel, ('Today', 'Tea'))

    def test_cancellation_shuts_socket_and_kills_audio_immediately(self):
        left, right = socket.socketpair()
        class Connection:
            sock = left
            def close(self):
                self.sock.close()
        class Player:
            killed = False
            def poll(self):
                return None
            def kill(self):
                self.killed = True
        try:
            turn, player = terminal.Turn(1), Player()
            turn.connections[Connection()] = 'chat'
            turn.players.append(player)
            turn.cancel()
            self.assertTrue(turn.cancelled.is_set())
            self.assertTrue(player.killed)
            right.settimeout(.1)
            self.assertEqual(right.recv(1), b'')
        finally:
            left.close()
            right.close()

    def test_pcm_reaches_player_before_tts_done(self):
        events = queue.Queue()
        client = terminal.Client('http://localhost:1', terminal.Settings(speed=1), events, player_path='/fake/ffplay')
        written = bytearray()
        first_write = threading.Event()
        class Input:
            def write(self, value):
                written.extend(value)
                first_write.set()
            def flush(self):
                pass
            def close(self):
                pass
        class Player:
            stdin, returncode = Input(), 0
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0
        def stream(*args, **kwargs):
            yield dict(type='start', sample_rate=48000)
            yield dict(type='audio', pcm=base64.b64encode(b'\x00\x01'*100).decode())
            self.assertTrue(first_write.wait(1), 'PCM was not played while generation remained open')
            self.assertEqual(len(written), 200)
            yield dict(type='done')
        client.stream = stream
        speech = queue.Queue()
        speech.put('Very good, sir.')
        speech.put(None)
        with patch.object(terminal.subprocess, 'Popen', return_value=Player()) as launch:
            client.speak(terminal.Turn(1), speech)
        self.assertEqual(launch.call_args.args[0][0], '/fake/ffplay')
        command = launch.call_args.args[0]
        self.assertNotIn('-ac', command)  # Removed by the installed FFplay 9.
        self.assertEqual(command[command.index('-ch_layout')+1], 'mono')
        self.assertEqual(events.get_nowait()[1]['type'], 'speaking')

    def test_broken_playback_pipe_is_explicitly_closed_and_player_reaped(self):
        class Input:
            closed = False
            def write(self, data):
                raise BrokenPipeError('player was interrupted')
            def close(self):
                self.closed = True
                raise BrokenPipeError('buffer flush after player exit')
        class Player:
            stdin, returncode = Input(), None
            waited = False
            def poll(self):
                return self.returncode
            def kill(self):
                self.returncode = -9
            def wait(self, timeout=None):
                self.waited = True
                return self.returncode
        player = Player()
        client = terminal.Client('http://localhost:1', terminal.Settings(speed=1), queue.Queue(), player_path='/fake/ffplay')
        client.stream = lambda *args, **kwargs: iter([
            dict(type='audio', pcm=base64.b64encode(b'\0\0').decode()), dict(type='done')])
        speech, turn = queue.Queue(), terminal.Turn(1)
        speech.put('Very good, sir.')
        speech.put(None)
        with patch.object(terminal.subprocess, 'Popen', return_value=player):
            client.speak(turn, speech)
        self.assertTrue(player.stdin.closed)
        self.assertTrue(player.waited)
        self.assertEqual(turn.players, [])

    def test_speech_busy_after_interrupt_retries_without_duplicate_audio(self):
        client = terminal.Client('http://localhost:1', terminal.Settings(), queue.Queue())
        calls = []
        def stream(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise terminal.ServiceError(409, 'Generation is finishing cancellation')
            yield dict(type='audio', pcm='AAAA')
            yield dict(type='done')
        client.stream = stream
        events = list(client.voice_events({'text':'Hello'}, terminal.Turn(1)))
        self.assertEqual(len(calls), 2)
        self.assertEqual([event['type'] for event in events], ['audio', 'done'])

    def test_actual_player_diagnostic_replaces_opaque_broken_pipe(self):
        class Input:
            closed = False
            def write(self, data):
                raise BrokenPipeError('player exited')
            def close(self):
                self.closed = True
        class Player:
            stdin, returncode = Input(), 1
            stderr = io.BytesIO(b'SDL_OpenAudio: No available audio device\n')
            def poll(self): return 1
            def wait(self, timeout=None): return 1
        events = queue.Queue()
        client = terminal.Client('http://localhost:1', terminal.Settings(speed=1), events, player_path='/fake/ffplay')
        client.stream = lambda *args, **kwargs: iter([
            dict(type='audio', pcm=base64.b64encode(b'\0\0').decode()), dict(type='done')])
        speech = queue.Queue()
        speech.put('Very good, sir.')
        speech.put(None)
        with patch.object(terminal.subprocess, 'Popen', return_value=Player()):
            client.speak(terminal.Turn(1), speech)
        notices = []
        while not events.empty():
            event = events.get_nowait()[1]
            if event['type'] == 'notice':
                notices.append(event['message'])
        # The player's own words survive, and the notice now also names the hop
        # it was talking to and how many tries it made (slice 3, rule "under all five").
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0].startswith('Speech unavailable: SDL_OpenAudio: No available audio device · '))
        self.assertIn('Voice service at http://localhost:1', notices[0])
        self.assertTrue(notices[0].endswith('· 1 try'))

    def test_playback_diagnostic_capture_is_bounded(self):
        diagnostics = terminal.PlaybackDiagnostics(io.BytesIO(b'x'*100_000+b' final failure'))
        message = diagnostics.message()
        self.assertLessEqual(len(diagnostics.buffer), 4096)
        self.assertLessEqual(len(message), 600)
        self.assertTrue(message.endswith('final failure'))

    def test_speech_retry_is_cancellable_bounded_and_never_replays_started_audio(self):
        client = terminal.Client('http://localhost:1', terminal.Settings(), queue.Queue())
        turn, calls = terminal.Turn(1), []
        def cancelled(*args, **kwargs):
            calls.append(True)
            turn.cancelled.set()
            raise terminal.ServiceError(409, 'Busy')
            yield
        client.stream = cancelled
        self.assertEqual(list(client.voice_events({}, turn)), [])
        self.assertEqual(len(calls), 1)
        def started(*args, **kwargs):
            yield dict(type='audio', pcm='AAAA')
            raise terminal.ServiceError(409, 'Late failure')
        client.stream = started
        events = client.voice_events({}, terminal.Turn(2))
        self.assertEqual(next(events)['type'], 'audio')
        with self.assertRaises(terminal.ServiceError):
            next(events)
        def busy(*args, **kwargs):
            raise terminal.ServiceError(409, 'Busy')
            yield
        client.stream = busy
        with patch.object(terminal.time, 'monotonic', side_effect=[0, 4]):
            with self.assertRaises(terminal.ServiceError):
                list(client.voice_events({}, terminal.Turn(3)))


class ChatStreamingTests(unittest.TestCase):
    def test_disconnect_interrupts_close_delimited_stream_read(self):
        release = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'{"type":"start"}\n')
                self.wfile.flush()
                release.wait(3)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started, ended = threading.Event(), threading.Event()
        client = terminal.Client(f'http://127.0.0.1:{server.server_port}', terminal.Settings(muted=True), queue.Queue())
        turn = terminal.Turn(1)
        def consume():
            try:
                for event in client.stream('/api/chat', {}, turn):
                    started.set()
            finally:
                ended.set()
        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            # Allow the reader to enter its next blocking read; no server release.
            time.sleep(.03)
            turn.cancel()
            self.assertTrue(ended.wait(.5), 'Cancel left a streaming HTTP reader alive')
        finally:
            release.set()
            worker.join(2)
            server.shutdown()
            server.server_close()

    def test_real_http_stream_delivers_text_context_and_session(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson')
                self.end_headers()
                for event in [dict(type='start', session_id='a'*32, turn_id='b'*32),
                              dict(type='text', delta='Very good, sir. '),
                              dict(type='action', name='show_panel', panel_id='today', title='Today', content='Tea'),
                              dict(type='done')]:
                    self.wfile.write((json.dumps(event)+'\n').encode())
                    self.wfile.flush()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            events = queue.Queue()
            client = terminal.Client(f'http://127.0.0.1:{server.server_port}', terminal.Settings(muted=True), events)
            context = dict(cwd='/work', panels={'today': dict(title='Today', content='Buy tea')})
            client.workspace_context = context
            turn_id = client.submit('Plan my day')
            received = []
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                local_id, event = events.get(timeout=2)
                self.assertEqual(local_id, turn_id)
                received.append(event)
                if event['type'] == 'complete':
                    break
            self.assertEqual(requests[0]['workspace_context'], context)
            self.assertEqual(client.session_id, 'a'*32)
            self.assertEqual([event['delta'] for event in received if event['type']=='text'], ['Very good, sir. '])
            self.assertTrue(any(event['type']=='action' for event in received))
            self.assertFalse(any(event['type']=='error' for event in received))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
