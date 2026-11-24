from pathlib import Path
import tempfile
import unittest
from test_terminal import terminal
from fleet_watch import FleetWatch, updates_text


class WatchTests(unittest.TestCase):
    def test_stop_clears_refresh_status_even_without_model_work(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            app.fleet_turn=terminal.Turn(-3);app.status='Refreshing Fleet…'
            app.stop()
            self.assertEqual(app.phase,'interrupted')
            self.assertNotIn('Refreshing',app.status)

    def test_watch_is_opt_in_persistent_and_claims_changes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'watch.json';watch=FleetWatch(path);other=FleetWatch(path)
            self.assertFalse(watch.enabled);watch.enable(True)
            self.assertTrue(other.enabled);self.assertTrue(watch.since())
            snapshot=dict(counts=dict(running=1,queued=0),health=dict(paused=False),attempts=[],questions=[])
            self.assertEqual(watch.observe(snapshot),'baseline');self.assertIsNone(other.observe(snapshot))
            snapshot['questions']=[{'id':'q1'}]
            self.assertEqual(other.observe(snapshot),'changed');self.assertIsNone(watch.observe(snapshot))
            watch.enable(False);self.assertFalse(other.enabled)

    def test_update_ids_deduplicate_and_truncated_cursor_does_not_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            watch=FleetWatch(Path(directory)/'watch.json')
            initial={'events':[dict(id=1,kind='run.end')], 'next_since':'2026-09-07T12:00:00Z','truncated':False}
            self.assertEqual(len(watch.consume_updates(initial)),1)
            self.assertEqual(watch.consume_updates(initial),[])
            capped={'events':[dict(id=2)],'next_since':'2026-09-07T13:00:00Z','truncated':True}
            self.assertEqual(len(watch.consume_updates(capped)),1)
            self.assertEqual(watch.since(),initial['next_since'])
            watch.consume_updates({'events':[], 'next_since':'2026-09-07T11:00:00Z'})
            self.assertEqual(watch.since(),initial['next_since'])
            self.assertIn('Position was not advanced',updates_text(capped,[]))
            self.assertEqual(watch.consume_updates({'events':[dict(id=3)],'errors':['offline']}),[])

    def test_background_refresh_preserves_user_state_and_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',True,Path(directory)/'state')
            app.input='keep typing';app.phase='speaking';app.status='Speaking'
            snapshot=dict(type='fleet_snapshot',counts=dict(running=0,queued=0),health={},attempts=[],questions=[],errors=[])
            for _ in range(2):
                app.events.put((-3,dict(type='fleet_snapshot',token=0,background=True,snapshot=snapshot)))
                app.drain()
            self.assertEqual(sum(role=='Fleet watch' for role,_ in app.transcript),1)
            self.assertEqual(app.input,'keep typing');self.assertEqual(app.phase,'speaking');self.assertEqual(app.status,'Speaking')
