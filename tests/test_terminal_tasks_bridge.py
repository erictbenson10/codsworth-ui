from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_terminal import terminal


class TaskBridgeTests(unittest.TestCase):
    def app(self,path):return terminal.Terminal('http://localhost:1',muted=True,state_path=path)
    def action(self,op='add',**fields):
        return dict(type='action',name='manage_task',action_id=terminal.uuid.uuid4().hex,change=dict(op=op,**fields))

    def test_real_action_lifecycle_persists_receipts_and_next_turn_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';app=self.app(path);app.active_id=4
            event=self.action(title='Check backups',priority='high',due='2026-10-12')
            app.events.put((4,dict(type='action',action=event)));app.drain()
            item=app.state.task_store.records[0]
            self.assertEqual(item['title'],'Check backups')
            self.assertIn('Saved task',app.transcript[-1][1])
            app.manage_task(self.action('done',task_id=item['id']))
            self.assertEqual(app.tasks,['[done] Check backups'])
            restored=self.app(path)
            self.assertEqual(restored.state.task_store.records[0]['status'],'done')
            with patch.object(restored.client,'submit',return_value=5):
                restored.input='What tasks are saved?';restored.send()
            context=restored.client.workspace_context
            self.assertEqual(context['task_records'][0]['id'],item['id'])
            self.assertEqual(context['task_records'][0]['status'],'done')
            self.assertIn('Updated task',context['task_receipts'][-1])

    def test_duplicate_delivery_and_noop_do_not_duplicate_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(Path(directory)/'state.json');action=self.action(title='Tea')
            app.manage_task(action);app.manage_task(action)
            self.assertEqual(len(app.tasks),1)
            self.assertEqual(sum(role=='Task manager' for role,_ in app.transcript),1)
            ident=app.state.task_store.records[0]['id']
            app.manage_task(self.action('reopen',task_id=ident))
            self.assertIn('No change needed',app.transcript[-1][1])

    def test_invalid_stale_and_failed_disk_writes_do_not_mutate_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json';app=self.app(path)
            for action in [self.action(title='Bad',due='2026-02-31'), self.action(title='Bad',shell='rm'),dict(action_id=[],change={})]:
                app.manage_task(action)
                self.assertEqual(app.tasks,[])
                self.assertIn('failed',app.transcript[-1][1])
            with patch.object(app.state,'save',side_effect=OSError('Disk full')):
                app.manage_task(self.action(title='Not saved'))
            self.assertEqual(app.tasks,[])
            newer=self.app(path);newer.command('/todo Newer task')
            canonical=path.read_bytes()
            app.manage_task(self.action(title='Stale mutation'))
            self.assertEqual(path.read_bytes(),canonical)
            self.assertEqual(app.tasks,[])
            self.assertIn('failed',app.transcript[-1][1])

    def test_context_is_bounded_with_many_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(Path(directory)/'state.json')
            app.tasks[:]=['Long task '+str(index)+'x'*450 for index in range(100)]
            app.state.panels={key:dict(title=key,content='y'*2400) for key in ('notes','today','tasks')}
            with patch.object(app.client,'submit',return_value=1):app.input='List tasks';app.send()
            payload=app.client.workspace_context
            self.assertEqual(payload['task_count'],100)
            self.assertLess(len(payload['task_records']),100)
            self.assertLess(len(terminal.json.dumps(payload)),8000)

    def test_context_has_actual_local_calendar_and_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(Path(directory)/'state.json')
            with patch.object(app.client,'submit',return_value=1):app.input='Add a task due tomorrow';app.send()
            now=terminal.datetime.now().astimezone()
            self.assertEqual(app.client.workspace_context['local_date'],now.date().isoformat())
            self.assertEqual(app.client.workspace_context['timezone'],now.strftime('%Z (%z)'))

    def test_explicit_negative_server_rejection_has_actual_failure_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(Path(directory)/'state.json')
            app.manage_task(dict(action_id='b'*32,error='Your request said not to save tasks; no task change was applied.'))
            self.assertEqual(app.tasks,[])
            self.assertIn('said not to save tasks',app.transcript[-1][1])

    def test_archive_excluded_from_context_and_restores_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'; app=self.app(path)
            app.command('/task add Old task')
            ident=app.state.task_store.records[0]['id']
            app.command('/task done '+ident)
            app.command('/task archive '+ident)
            restored=self.app(path)
            self.assertEqual(restored.tasks,[])
            with patch.object(restored.client,'submit',return_value=1):
                restored.input='Plan today'; restored.send()
            self.assertEqual(restored.client.workspace_context['task_records'],[])
            self.assertEqual(restored.client.workspace_context['task_count'],0)
            restored.command('/task restore '+ident)
            self.assertEqual(restored.tasks,['[done] Old task'])

    def test_legacy_add_at_capacity_does_not_drop_oldest_task(self):
        with tempfile.TemporaryDirectory() as directory:
            app=self.app(Path(directory)/'state.json')
            app.tasks[:]=['Task '+str(i) for i in range(100)]
            original=list(app.tasks)
            app.command('/todo Overflow')
            self.assertEqual(app.tasks,original)
            self.assertIn('Archive',app.status)
