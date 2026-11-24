"""Structured workspace tasks with a backwards-compatible numbered text view."""
from datetime import date
import shlex
import re
import unicodedata
import uuid


class TaskStore:
    ACTIVE_LIMIT = 100
    ARCHIVE_LIMIT = 1000

    @classmethod
    def validate_change(cls, change):
        if not isinstance(change, dict):
            raise ValueError('Task change must be an object')
        op = change.get('op')
        required = {'add': {'op','title'}, 'done': {'op','task_id'}, 'reopen': {'op','task_id'},
                    'priority': {'op','task_id','priority'}, 'due': {'op','task_id','due'},
                    'rename': {'op','task_id','title'}}
        if op not in required:
            raise ValueError('Unknown task operation')
        allowed = required[op] | ({'priority','due'} if op=='add' else set())
        if not required[op] <= set(change) or set(change)-allowed:
            raise ValueError('Invalid fields for task operation')
        if 'task_id' in change and (not isinstance(change['task_id'],str) or not re.fullmatch('[0-9a-f]{32}',change['task_id'])):
            raise ValueError('Use the full task ID from workspace context')
        if 'title' in change:
            title=change['title']
            if not isinstance(title,str) or not title.strip() or len(title)>500 or any(unicodedata.category(c).startswith('C') for c in title):
                raise ValueError('Task title must be 1–500 plain text characters')
        if 'priority' in change and change['priority'] not in ('low','normal','high'):
            raise ValueError('Priority must be low, normal, or high')
        if 'due' in change:
            cls.validate_due(change['due'])
        return dict(change)

    def apply_change(self, change):
        change=self.validate_change(change)
        op=change['op']
        if op=='add':
            command='add '+shlex.quote(change['title'])+' --priority '+change.get('priority','normal')
            if change.get('due') is not None:command+=' --due '+change['due']
            self.command(command)
            item=self.records[-1]
            return dict(changed=True,task_id=item['id'],message='Saved task '+item['id'][:8]+': '+item['title'])
        item=self.find(change['task_id'])
        field,value = ('status','done' if op=='done' else 'open') if op in ('done','reopen') else (
            ('title',change['title']) if op=='rename' else (op,change[op]))
        changed=item[field]!=value
        if changed:item[field]=value
        return dict(changed=changed,task_id=item['id'],message=(
            'Updated task ' if changed else 'No change needed for task ')+item['id'][:8]+': '+item['title'])

    def __init__(self, records=None):
        self.records = []
        # Preserve every valid saved record, including older oversized stores.
        # Capacity limits reject new mutations rather than truncating user data.
        for item in (records if isinstance(records, list) else []):
            try:
                record = dict(id=uuid.UUID(item['id']).hex, title=str(item['title'])[:500],
                              status=item.get('status', 'open'), priority=item.get('priority', 'normal'),
                              due=item.get('due'))
                if record['status'] not in ('open','done') or record['priority'] not in ('low','normal','high'):
                    continue
                self.validate_due(record['due'])
                if item.get('archived') is True:
                    record['archived'] = True
                self.records.append(record)
            except (KeyError, TypeError, ValueError):
                continue

    @staticmethod
    def validate_due(value):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('Due date must be YYYY-MM-DD, or none to clear it') from None
        return value

    def legacy(self):
        return [('[done] ' if item['status']=='done' else '')+item['title'] for item in self.active_records()]

    def active_records(self):
        return [item for item in self.records if not item.get('archived', False)]

    def reconcile(self, legacy):
        """Retain stable IDs/metadata when old /todo or /done edits the text view."""
        available = self.active_records()
        archived = [item for item in self.records if item.get('archived', False)]
        records = []
        for text in legacy:
            done = text.startswith('[done] ')
            title = text[7:] if done else text
            match = next((item for item in available if item['title']==title), None)
            if match:
                available.remove(match)
            else:
                match = dict(id=uuid.uuid4().hex,title=title[:500],priority='normal',due=None)
            match['status'] = 'done' if done else 'open'
            records.append(match)
        self.records = records + archived

    def find(self, prefix, archived=False):
        matches = [item for item in self.records if bool(item.get('archived', False))==archived
                   and item['id'].startswith(prefix.lower())] if len(prefix)>=4 else []
        if len(matches)!=1:
            raise ValueError('Task ID is missing or ambiguous. Use '+('/tasks archived' if archived else '/tasks')+' for IDs.')
        return matches[0]

    def listing(self, selected='open'):
        if selected not in ('all','open','done','archived'):
            raise ValueError('Use /tasks open, done, all, or archived')
        records = ([item for item in self.records if item.get('archived', False)] if selected=='archived'
                   else [item for item in self.active_records() if selected=='all' or item['status']==selected])
        records.sort(key=lambda item: (item['status']=='done', {'high':0,'normal':1,'low':2}[item['priority']],item['due'] or '9999-12-31'))
        return '\n'.join(f"{item['id'][:8]}  {'✓' if item['status']=='done' else '□'}  {item['title']}  [{item['priority']}]"+
                         (f"  due {item['due']}" if item['due'] else '') for item in records) or 'No matching tasks. /task add Buy tea creates one.'

    def command(self, text):
        try:
            parts = shlex.split(text)
        except ValueError:
            raise ValueError('Unclosed quote in task command') from None
        if not parts:
            raise ValueError('Use /task add TEXT, done ID, reopen ID, archive ID, restore ID, priority ID high|normal|low, due ID YYYY-MM-DD|none, or rename ID TEXT')
        action, *args = parts
        if action=='add':
            title, priority, due = [], 'normal', None
            index=0
            while index<len(args):
                value=args[index]
                if value in ('--priority','--due'):
                    if index+1>=len(args):raise ValueError(value+' needs a value')
                    if value=='--priority':priority=args[index+1]
                    else:due=args[index+1]
                    index+=2
                elif value.startswith('--'):
                    raise ValueError('Unknown task option '+value)
                else:
                    title.append(value);index+=1
            title=' '.join(title).strip()
            if not title:raise ValueError('Use /task add followed by a task title')
            if len(title)>500:raise ValueError('Task titles must be at most 500 characters')
            if priority not in ('low','normal','high'):raise ValueError('Priority must be low, normal, or high')
            self.validate_due(due)
            if len(self.active_records())>=self.ACTIVE_LIMIT:raise ValueError('This workspace already has 100 active tasks. Archive completed tasks first.')
            item=dict(id=uuid.uuid4().hex,title=title,status='open',priority=priority,due=due)
            self.records.append(item)
            return 'Added task '+item['id'][:8]
        if not args:raise ValueError('Include a task ID from /tasks')
        item=self.find(args[0], archived=action=='restore')
        if action=='archive' and len(args)==1:
            if item['status']!='done':
                raise ValueError('Complete the task before archiving it')
            if sum(bool(record.get('archived', False)) for record in self.records)>=self.ARCHIVE_LIMIT:
                raise ValueError('This workspace already has 1000 archived tasks. Nothing was removed.')
            item['archived']=True
            return 'Archived task '+item['id'][:8]
        elif action=='restore' and len(args)==1:
            if len(self.active_records())>=self.ACTIVE_LIMIT:
                raise ValueError('This workspace already has 100 active tasks. Archive completed tasks first.')
            item.pop('archived', None)
            return 'Restored completed task '+item['id'][:8]
        elif action in ('done','reopen') and len(args)==1:
            item['status']='done' if action=='done' else 'open'
        elif action=='priority' and len(args)==2:
            if args[1] not in ('low','normal','high'):raise ValueError('Priority must be low, normal, or high')
            item['priority']=args[1]
        elif action=='due' and len(args)==2:
            item['due']=self.validate_due(None if args[1]=='none' else args[1])
        elif action=='rename' and len(args)>=2:
            title=' '.join(args[1:]).strip()
            if not title or len(title)>500:raise ValueError('Task title must contain 1–500 characters')
            item['title']=title
        else:
            raise ValueError('Unknown task action or arguments. Use /help for task commands.')
        return 'Updated task '+item['id'][:8]
