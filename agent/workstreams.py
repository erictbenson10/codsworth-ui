"""Independent, resumable Hermes conversations owned by one terminal process.

The factory receives an event queue and must return a fresh terminal Client.
Workers are text-only: speech belongs to the foreground conversation, and local
workspace task mutations require the foreground task manager's acknowledgement.
"""
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import queue
import tempfile
import time
import uuid


try:
    from .turn_progress import work_duration
except ImportError:  # terminal.py also runs directly from the agent directory
    from turn_progress import work_duration


BUSY = frozenset(('running', 'tool'))
STATUSES = BUSY | {'idle', 'completed', 'error', 'interrupted'}


def clean(value, limit):
    if not isinstance(value, str):
        raise ValueError('Expected text')
    return ''.join(c for c in value if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)[:limit]


def identifier(value):
    if not isinstance(value, str):
        raise ValueError('Invalid workstream ID')
    return uuid.UUID(value).hex


class WorkstreamManager:
    def __init__(self, path, client_factory, workspace_context=None):
        self.path = Path(path)
        self.client_factory = client_factory
        self.workspace_context = deepcopy(workspace_context or {})
        self.records = []
        self.selected = None
        self._clients = {}
        self._queues = {}
        self._turns = {}
        self._started = {}
        self._fingerprint = self.path.read_bytes() if self.path.exists() else None
        recovered = False
        if self._fingerprint is not None:
            try:
                data = json.loads(self._fingerprint)
                if data.get('version') != 1 or not isinstance(data.get('records'), list) or len(data['records']) > 8:
                    raise ValueError('Unsupported workstream file')
                owner = data.get('owner_pid')
                if (isinstance(owner, int) and owner > 0 and owner != os.getpid()
                        and any(r.get('status') in BUSY for r in data['records'])):
                    try:
                        os.kill(owner, 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        raise ValueError('Another terminal is running these workstreams; use that window')
                    else:
                        raise ValueError('Another terminal is running these workstreams; use that window')
                for raw in data['records']:
                    record = self._validate(raw)
                    if record['id'] in {r['id'] for r in self.records}:
                        raise ValueError('Duplicate workstream ID')
                    if record['status'] in BUSY:
                        record.update(status='interrupted', summary='Terminal closed while working; review before continuing.')
                        record['transcript'].append(('Notice', record['summary']))
                        recovered = True
                    self.records.append(record)
                self.selected = data.get('selected')
                if self.selected is not None and self.selected not in {r['id'] for r in self.records}:
                    raise ValueError('Unknown selected workstream')
            except (TypeError, KeyError, AttributeError, json.JSONDecodeError) as error:
                raise ValueError('Invalid workstream file; retained without changes') from error
        if recovered:
            self.save()

    @staticmethod
    def _validate(raw):
        ident = identifier(raw['id'])
        title = clean(raw['title'], 80).strip()
        if not title or '\n' in title or '\t' in title:
            raise ValueError('Workstream title must be one line')
        status = raw.get('status', 'idle')
        if status not in STATUSES:
            raise ValueError('Invalid workstream status')
        transcript = raw.get('transcript', [])
        if not isinstance(transcript, list):
            raise ValueError('Invalid workstream transcript')
        rows = []
        for row in transcript[-100:]:
            if not isinstance(row, (tuple, list)) or len(row) != 2:
                raise ValueError('Invalid workstream message')
            rows.append((clean(row[0], 80), clean(row[1], 30000)))
        return dict(id=ident, title=title, status=status,
                    session_id=identifier(raw['session_id']) if raw.get('session_id') else '',
                    transcript=rows, draft=clean(raw.get('draft', ''), 8000),
                    summary=clean(raw.get('summary', ''), 300), unread=bool(raw.get('unread', False)))

    def resolve(self, stream_id=None):
        ident = stream_id if stream_id is not None else self.selected
        if not isinstance(ident, str) or len(ident) < 4 or any(c not in '0123456789abcdef' for c in ident):
            raise ValueError('Choose a workstream by its ID (at least four characters)')
        matches = [r for r in self.records if r['id'].startswith(ident)]
        if len(matches) != 1:
            raise ValueError('Workstream ID is unknown or ambiguous')
        return matches[0]

    def find(self, stream_id):
        return self.resolve(stream_id)

    def _check_current(self):
        current = self.path.read_bytes() if self.path.exists() else None
        if current != self._fingerprint:
            raise ValueError("Another terminal changed the workstreams; reopen before editing them")

    def create(self, title):
        self._check_current()
        if len(self.records) >= 8:
            raise ValueError('Eight workstreams already exist; remove a finished stream first')
        if not isinstance(title, str) or any(ord(c) < 32 or ord(c) == 127 for c in title):
            raise ValueError('Workstream title must be plain text on one line')
        if not title.strip() or len(title.strip()) > 80:
            raise ValueError('Workstream title must contain 1–80 characters')
        record = self._validate(dict(id=uuid.uuid4().hex, title=title.strip()))
        self.records.append(record)
        self.selected = record['id']
        self.save()
        return record

    def focus(self, stream_id):
        self._check_current()
        record = self.resolve(stream_id) if stream_id is not None else None
        self.selected = record['id'] if record else None
        if record:
            record['unread'] = False
        self.save()
        return record

    def set_draft(self, text, stream_id=None):
        self._check_current()
        self.resolve(stream_id)['draft'] = clean(text, 8000)
        self.save()

    def _client(self, record):
        ident = record['id']
        if ident not in self._clients:
            events = queue.Queue()
            client = self.client_factory(events)
            if any(client is existing for existing in self._clients.values()):
                raise ValueError('Workstreams require independent clients')
            if hasattr(client, 'settings'):
                client.settings = deepcopy(client.settings)
                client.settings.muted = True
            client.session_id = record['session_id']
            client.workspace_context = deepcopy(self.workspace_context)
            # Never supply canonical task records to an unacknowledged background
            # mutation path. The UI still explicitly reports attempted actions.
            for key in ('task_records', 'task_count', 'task_receipts'):
                client.workspace_context.pop(key, None)
            self._queues[ident], self._clients[ident] = events, client
        return self._clients[ident]

    def send(self, text, stream_id=None, images=None):
        self._check_current()
        record = self.resolve(stream_id)
        if record['status'] in BUSY:
            raise ValueError('This workstream is still running; stop it explicitly before sending again')
        text = clean(text, 8000).strip()
        if not text:
            raise ValueError('Enter a workstream request')
        client = self._client(record)
        record['transcript'] = (record['transcript'] + [('You', text), ('Codsworth', '')])[-100:]
        record.update(status='running', summary='Thinking…', draft='', unread=False)
        self.save()  # Fail before starting network work if persistence conflicts.
        try:
            self._started[record['id']] = time.monotonic()
            self._turns[record['id']] = client.submit(text, images=images) if images else client.submit(text)
        except Exception as error:
            self._started.pop(record['id'], None)
            record.update(status='error', summary=clean(str(error), 300))
            record['transcript'].append(('Notice', record['summary']))
            self.save()
            raise
        return record

    def stop(self, stream_id=None):
        self._check_current()
        record = self.resolve(stream_id)
        if record['status'] not in BUSY:
            return record
        client = self._clients.get(record['id'])
        if client:
            client.interrupt(reason='workstream_stop')
        self._turns.pop(record['id'], None)
        self._started.pop(record['id'], None)
        record.update(status='interrupted', summary='Stopped. Partial output is incomplete; tool side effects may already exist.')
        record['transcript'].append(('Notice', record['summary']))
        self.save()
        return record

    def remove(self, stream_id):
        self._check_current()
        record = self.resolve(stream_id)
        if record['status'] in BUSY:
            raise ValueError('Stop the workstream before removing it')
        self.records.remove(record)
        for mapping in (self._clients, self._queues, self._turns, self._started):
            mapping.pop(record['id'], None)
        if self.selected == record['id']:
            self.selected = None
        self.save()

    def poll(self):
        changed = False
        for record in self.records:
            events = self._queues.get(record['id'])
            if events is None:
                continue
            for _ in range(1000):
                try:
                    turn_id, event = events.get_nowait()
                except queue.Empty:
                    break
                if turn_id != self._turns.get(record['id']):
                    continue
                kind = event.get('type')
                if kind not in ('session', 'text', 'tool', 'error', 'notice', 'action', 'complete'):
                    continue
                if kind == 'action' and event.get('action', {}).get('name') != 'manage_task':
                    continue
                changed = True
                if kind == 'session':
                    try:
                        record['session_id'] = identifier(event['session_id'])
                    except (ValueError, KeyError):
                        record.update(status='error', summary='Invalid Hermes session ID')
                elif kind == 'text':
                    if not record['transcript'] or record['transcript'][-1][0] != 'Codsworth':
                        record['transcript'].append(('Codsworth', ''))
                    record['transcript'][-1] = ('Codsworth', (record['transcript'][-1][1] + clean(event.get('delta', ''), 8000))[-30000:])
                elif kind == 'tool':
                    tool = event.get('tool', {})
                    if record['status'] != 'error':
                        record.update(status='tool' if tool.get('state') == 'running' else 'running',
                                      summary=clean(tool.get('summary', 'Working…'), 300))
                    if record['transcript'] and record['transcript'][-1] == ('Codsworth', ''):
                        record['transcript'].pop()
                    record['transcript'].append(('Tool', clean(tool.get('summary', 'Working…'), 1000)))
                elif kind in ('error', 'notice'):
                    message = clean(event.get('message', 'Workstream failed'), 600)
                    record['transcript'].append(('Notice', message))
                    if kind == 'error':
                        record.update(status='error', summary=message[:300])
                elif kind == 'action' and event.get('action', {}).get('name') == 'manage_task':
                    record['transcript'].append(('Notice', 'Local task change was not applied. Use the foreground task manager to save it.'))
                elif kind == 'complete':
                    started = self._started.pop(record['id'], None)
                    if record['status'] != 'error':
                        if started is not None:
                            record['transcript'].append(('Worked for', work_duration(time.monotonic()-started)))
                        record.update(status='completed', summary='Reply completed; review the output and verification.')
                    self._turns.pop(record['id'], None)
                record['transcript'] = record['transcript'][-100:]
                if self.selected != record['id']:
                    record['unread'] = True
        if changed:
            self.save()
        return changed

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.path.read_bytes() if self.path.exists() else None
            if current != self._fingerprint:
                raise ValueError('Another terminal changed the workstreams; reopen before editing them')
            payload = (json.dumps(dict(version=1, owner_pid=os.getpid(), selected=self.selected, records=self.records), ensure_ascii=False) + '\n').encode()
            fd, temporary = tempfile.mkstemp(prefix=self.path.name + '.', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                self._fingerprint = payload
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


class DisabledWorkstreams:
    """Read-only unavailable state; never forks or overwrites another owner's file."""
    def __init__(self, reason):
        self.reason = clean(str(reason), 600)
        self.records = []
        self.selected = None

    def poll(self):
        return False

    def save(self):
        return None

    def focus(self, stream_id):
        if stream_id is None:
            return None
        raise ValueError(self.reason)

    def _unavailable(self, *args, **kwargs):
        raise ValueError(self.reason)

    create = send = stop = set_draft = remove = resolve = find = _unavailable
