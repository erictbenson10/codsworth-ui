from pathlib import Path
import tempfile
import unittest
import uuid
from test_terminal import terminal
from task_store import TaskStore


class TaskTests(unittest.TestCase):
    def test_archive_preserves_metadata_through_restart_and_legacy_reconcile(self):
        store=TaskStore()
        store.command('add Tea --priority high --due 2026-09-12')
        ident=store.records[0]['id']
        store.command('done '+ident)
        store.command('archive '+ident)
        snapshot=dict(store.records[0])
        self.assertEqual(store.legacy(), [])
        self.assertEqual(store.active_records(), [])
        self.assertNotIn(ident[:8],store.listing('all'))
        self.assertIn(ident[:8],store.listing('archived'))
        reopened=TaskStore(store.records)
        reopened.reconcile(['Tea'])
        self.assertEqual(reopened.find(ident,archived=True), snapshot)
        self.assertNotEqual(reopened.active_records()[0]['id'],ident)
        reopened.command('restore '+ident)
        self.assertEqual(reopened.find(ident),{k:v for k,v in snapshot.items() if k!='archived'})
        self.assertEqual(reopened.legacy(),['Tea','[done] Tea'])
        reopened.command('reopen '+ident)
        self.assertEqual(reopened.find(ident)['status'],'open')

    def test_archive_requires_done_and_stale_model_actions_cannot_mutate_it(self):
        store=TaskStore();store.command('add Tea')
        ident=store.records[0]['id']
        with self.assertRaisesRegex(ValueError,'Complete'):
            store.command('archive '+ident)
        store.command('done '+ident);store.command('archive '+ident)
        snapshot=[dict(item) for item in store.records]
        for change in ({'op':'done','task_id':ident},{'op':'reopen','task_id':ident},
                       {'op':'rename','task_id':ident,'title':'Changed'},
                       {'op':'priority','task_id':ident,'priority':'low'},
                       {'op':'due','task_id':ident,'due':None}):
            with self.assertRaises(ValueError):store.apply_change(change)
            self.assertEqual(store.records,snapshot)

    def test_capacity_excludes_archives_and_restore_failure_is_atomic(self):
        store=TaskStore()
        for index in range(100):store.command('add Task '+str(index))
        ident=store.records[0]['id']
        store.command('done '+ident);store.command('archive '+ident)
        store.apply_change({'op':'add','title':'Replacement'})
        self.assertEqual(len(store.records),101)
        self.assertEqual(len(store.active_records()),100)
        snapshot=[dict(item) for item in store.records]
        for command in ('add Overflow','restore '+ident):
            with self.assertRaisesRegex(ValueError,'100 active'):store.command(command)
            self.assertEqual(store.records,snapshot)

    def test_archive_limit_and_oversized_load_never_drop_valid_records(self):
        records=[dict(id=uuid.uuid4().hex,title='Archive '+str(index),status='done',
                      priority='normal',due=None,archived=True) for index in range(1000)]
        store=TaskStore(records);store.command('add Current')
        ident=store.records[-1]['id'];store.command('done '+ident)
        snapshot=[dict(item) for item in store.records]
        with self.assertRaisesRegex(ValueError,'1000 archived'):store.command('archive '+ident)
        self.assertEqual(store.records,snapshot)
        # Older oversized files are retained, not sliced on load or reconciliation.
        records += [dict(id=uuid.uuid4().hex,title='Active '+str(index),status='open',
                         priority='normal',due=None) for index in range(101)]
        loaded=TaskStore(records);loaded.reconcile(loaded.legacy())
        self.assertEqual(len(loaded.records),1101)
        self.assertEqual({item['id'] for item in loaded.records},{item['id'] for item in records})

    def test_terminal_archive_survives_restart_and_conversation_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'workspace.json'
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=path)
            app.command('/task add Tea --priority high')
            ident=app.state.task_store.records[0]['id'];conversation=app.state.conversation_id
            app.command('/task done '+ident);app.command('/task archive '+ident)
            app.command('/new');app.command('/switch '+conversation[:8])
            reopened=terminal.Terminal('http://localhost:1',muted=True,state_path=path)
            self.assertEqual(reopened.tasks,[])
            self.assertEqual(reopened.state.task_store.find(ident,archived=True)['priority'],'high')
            reopened.command('/task restore '+ident)
            self.assertEqual(reopened.tasks,['[done] Tea'])

    def test_full_lifecycle_and_validation_are_atomic(self):
        store=TaskStore()
        store.command('add Prepare proposal --priority high --due 2026-09-12')
        item=store.records[0];ident=item['id'][:8]
        self.assertEqual((item['title'],item['priority'],item['due']),('Prepare proposal','high','2026-09-12'))
        for invalid in ['due '+ident+' 2026-02-31','priority '+ident+' urgent','add Bad --unknown value','rename '+ident]:
            before=[dict(record) for record in store.records]
            with self.assertRaises(ValueError):store.command(invalid)
            self.assertEqual(store.records,before)
        store.command('done '+ident);self.assertEqual(item['status'],'done')
        self.assertNotIn('Prepare proposal',store.listing('open'))
        store.command('reopen '+ident);self.assertEqual(item['status'],'open')
        store.command('priority '+ident+' low');self.assertEqual(item['priority'],'low')
        store.command('due '+ident+' none');self.assertIsNone(item['due'])
        store.command('rename '+ident+' Write proposal');self.assertEqual(item['title'],'Write proposal')

    def test_legacy_commands_preserve_ids_and_priority_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'workspace.json'
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=path)
            app.command('/todo Make tea')
            ident=app.state.task_store.records[0]['id']
            app.command('/task priority '+ident[:8]+' high')
            app.command('/done 1')
            self.assertEqual(app.tasks,['[done] Make tea'])
            reopened=terminal.Terminal('http://localhost:1',muted=True,state_path=path)
            item=reopened.state.task_store.records[0]
            self.assertEqual((item['id'],item['priority'],item['status']),(ident,'high','done'))
            reopened.command('/task reopen '+ident[:8])
            self.assertEqual(reopened.tasks,['Make tea'])
            reopened.command('/tasks all')
            self.assertIn(ident[:8],reopened.panel[1])

    def test_workspace_tasks_keep_metadata_across_conversation_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            app=terminal.Terminal('http://localhost:1',muted=True,state_path=Path(directory)/'state.json')
            app.command('/task add Check backup --priority high --due 2026-09-10')
            ident=app.state.task_store.records[0]['id']
            aid=app.state.conversation_id
            app.command('/new')
            app.command('/task done '+ident[:8])
            app.command('/switch '+aid[:8])
            item=app.state.task_store.records[0]
            self.assertEqual((item['id'],item['priority'],item['status'],item['due']),(ident,'high','done','2026-09-10'))

    def test_legacy_duplicate_titles_keep_distinct_stable_ids(self):
        store=TaskStore();store.reconcile(['Tea','Tea'])
        ids=[item['id'] for item in store.records]
        self.assertEqual(len(set(ids)),2)
        store.reconcile(['[done] Tea','Tea'])
        self.assertEqual([item['id'] for item in store.records],ids)
        self.assertEqual([item['status'] for item in store.records],['done','open'])
