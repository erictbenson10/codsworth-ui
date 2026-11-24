"""Per-workspace Fleet notification cursor, independent of conversation archives."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile


class FleetWatch:
    def __init__(self, path):
        self.path = Path(path)
        self.next_poll = 0.0

    def read(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, change):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.path)+'.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.read()
            result = change(state)
            fd, name = tempfile.mkstemp(prefix='.fleet-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(state, file)
                os.replace(name, self.path)
            finally:
                if os.path.exists(name): os.unlink(name)
            return result

    @property
    def enabled(self):
        return self.read().get('enabled') is True

    def enable(self, enabled):
        def change(state):
            state['enabled'] = bool(enabled)
            if enabled and 'since' not in state:
                state['since'] = datetime.now(timezone.utc).isoformat()
        self.update(change)
        self.next_poll = 0

    def since(self):
        return self.read().get('since')

    def observe(self, snapshot):
        """Claim one notification per changed successful snapshot across windows."""
        if snapshot.get('errors'):
            return None
        signature = {'counts': snapshot.get('counts', {}),
            'paused': snapshot.get('health', {}).get('paused'),
            'attempts': sorted((a.get('id'), a.get('state')) for a in snapshot.get('attempts', [])),
            'questions': sorted(q.get('id') for q in snapshot.get('questions', []))}
        # Normalize tuples before comparing against persisted JSON lists.
        signature = json.loads(json.dumps(signature))
        def change(state):
            before = state.get('snapshot')
            state['snapshot'] = signature
            state['observed_at'] = snapshot.get('observed_at')
            return None if before == signature else ('baseline' if before is None else 'changed')
        return self.update(change)

    def consume_updates(self, snapshot):
        if snapshot.get('errors'):
            return []
        def change(state):
            seen = state.get('seen_ids', [])
            known = set(seen)
            events = [event for event in snapshot.get('events', []) if event.get('id') not in known]
            state['seen_ids'] = (seen+[event['id'] for event in events])[-1000:]
            # A truncated page must never advance past events the API omitted.
            if not snapshot.get('truncated') and snapshot.get('next_since'):
                candidate = snapshot['next_since']
                previous = state.get('since')
                if not previous or datetime.fromisoformat(candidate) >= datetime.fromisoformat(previous):
                    state['since'] = candidate
            return events
        return self.update(change)


def updates_text(snapshot, events):
    if snapshot.get('errors'):
        return 'Fleet updates unavailable. Your previous position is preserved; try /updates again.'
    lines = []
    for event in events:
        target = event.get('attempt_id') or 'Fleet'
        lines.append(f"{target} · {event.get('kind', 'event')} · {event.get('summary', '')}")
    if not lines:
        lines.append('No new Fleet events in this check.')
    if snapshot.get('truncated'):
        lines.append('More events exist than this view can return. Position was not advanced; inspect Fleet for the full history.')
    return '\n'.join(lines)
