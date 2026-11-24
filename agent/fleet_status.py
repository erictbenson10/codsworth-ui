"""Read-only, bounded Fleet overview using the native connector's HTTP transport."""
import asyncio
from datetime import datetime, timezone
from functools import lru_cache
import importlib.util
import math
from pathlib import Path
import re

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator
from urllib.parse import urlsplit


@lru_cache(maxsize=1)
def client_class():
    source = Path(__file__).parent / 'plugins' / 'cods-fleet' / 'connector.py'
    spec = importlib.util.spec_from_file_location('_cods_fleet_overview_transport', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FleetClient


def text(value, limit=1000):
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value[:limit*2] if (ord(c) >= 32 and not 127 <= ord(c) <= 159 and c not in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069') or c in '\n\t')[:limit]


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value) and value not in ('.', '..') else None


def count(value):
    return value if type(value) is int and value >= 0 else None


async def overview(client_factory=None):
    async def read(view):
        def fetch():
            factory = client_factory or client_class()
            result = factory().read({'view': view})
            if not isinstance(result, dict) or result.get('ok') is not True or not isinstance(result.get('data'), dict):
                return None
            return result['data']
        try:
            return await asyncio.to_thread(fetch)
        except (OSError, ValueError, TypeError, ImportError, AttributeError):
            return None
    health, status, question_data = await asyncio.gather(*(read(v) for v in ('health', 'status', 'questions')))
    available = {'health': health is not None, 'status': status is not None,
                 'attempts': status is not None and isinstance(status.get('active'), list),
                 'questions': question_data is not None and isinstance(question_data.get('questions'), list)}
    errors = [name.capitalize() + ' unavailable; retry the overview.' for name, ok in available.items() if not ok]
    result = {'type': 'fleet_snapshot', 'observed_at': datetime.now(timezone.utc).isoformat(),
              'health': {}, 'counts': {'running': None, 'queued': None}, 'attempts': [], 'questions': [],
              'available': available, 'errors': errors, 'truncated': {'attempts': False, 'questions': False}}
    if health is not None:
        result['health'] = {k: text(health.get(k), 80) for k in ('daemon', 'node')}
        for key in ('paused', 'draining'):
            result['health'][key] = health.get(key) if type(health.get(key)) is bool else None
        age = health.get('tick_age_s')
        result['health']['tick_age_s'] = age if type(age) in (int, float) and math.isfinite(age) and age >= 0 else None
    if status is not None:
        work = status.get('work', {})
        if isinstance(work, dict):
            result['counts'] = {k: count(work.get(k)) for k in ('running', 'queued')}
    if available['attempts']:
        attempts = [a for a in status['active'] if isinstance(a, dict) and identifier(a.get('id'))]
        result['truncated']['attempts'] = len(attempts) > 10
        for a in attempts[:10]:
            result['attempts'].append({'id': a['id'], 'name': text(a.get('name'), 160),
                'state': text(a.get('state'), 40), 'repo': text(a.get('repo'), 160), 'issue': count(a.get('issue'))})
    if available['questions']:
        questions = [q for q in question_data['questions'] if isinstance(q, dict) and identifier(q.get('qid')) and q.get('answered') is not True]
        if any(not isinstance(q, dict) or not identifier(q.get('qid')) for q in question_data['questions']):
            errors.append('Some Fleet questions were invalid and omitted; inspect Fleet directly.')
        result['truncated']['questions'] = len(questions) > 20
        for q in questions[:20]:
            options = q.get('options', [])
            result['questions'].append({'id': q['qid'], 'attempt_id': identifier(q.get('attempt_id')),
                'text': text(q.get('summary')), 'options': [text(o, 300) for o in options[:8] if isinstance(o, str)] if isinstance(options, list) else [],
                'options_truncated': isinstance(options, list) and (len(options) > 8 or any(isinstance(o, str) and len(o) > 300 for o in options)),
                'repo': text(q.get('repo'), 160), 'issue': count(q.get('issue'))})
    return result


class UpdatesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    since: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=30, ge=1, le=100)

    @field_validator('since')
    @classmethod
    def timestamp(cls, value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('since must include a timezone')
        return parsed.astimezone(timezone.utc).isoformat()


def event_link(payload):
    value = payload.get('url') or payload.get('html_url')
    if not isinstance(value, str) or len(value) > 1000 or text(value, 1000) != value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == 'https' and parsed.hostname in ('git.example.com', 'fleet.example.com') and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


async def updates(prompt, client_factory=None):
    result = {'type': 'fleet_updates', 'events': [], 'errors': [], 'truncated': False,
              'observed_at': datetime.now(timezone.utc).isoformat(), 'next_since': prompt.since}
    def fetch():
        factory = client_factory or client_class()
        return factory().read({'view': 'events', 'limit': prompt.limit, 'since': prompt.since})
    try:
        response = await asyncio.to_thread(fetch)
        rows = response.get('data', {}).get('events') if isinstance(response, dict) and response.get('ok') is True and isinstance(response.get('data'), dict) else None
        if not isinstance(rows, list):
            raise ValueError('Invalid event response')
    except (OSError, ValueError, TypeError, ImportError, AttributeError):
        result['errors'].append('Fleet updates unavailable; cursor preserved. Retry later.')
        return result
    result['truncated'] = len(rows) >= prompt.limit
    invalid = False
    for row in rows[:prompt.limit]:
        if not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] < 1 or not identifier(row.get('kind')):
            invalid = True
            continue
        try:
            timestamp = UpdatesRequest.timestamp(row.get('ts'))
            if timestamp is None:
                raise ValueError('Missing timestamp')
        except (ValueError, TypeError, AttributeError):
            invalid = True
            continue
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        summary = next((text(payload[k], 400) for k in ('summary', 'reason', 'detail') if isinstance(payload.get(k), str) and payload[k].strip()), '')
        if not summary:
            summary = text(payload.get('state') or payload.get('verdict') or row['kind'], 400)
        result['events'].append({'id': row['id'], 'ts': timestamp, 'attempt_id': identifier(row.get('attempt_id')),
                                 'kind': row['kind'], 'summary': summary, 'link': event_link(payload)})
    # Preserve chronological presentation but keep exact IDs for client dedup.
    result['events'].sort(key=lambda event: event['id'])
    if result['truncated']:
        result['errors'].append('Fleet event limit reached; older updates may be missing. Cursor preserved; inspect Fleet history.')
    if invalid:
        result['errors'].append('Some Fleet events were invalid; cursor preserved.')
    if not result['truncated'] and not invalid and result['events']:
        newest = max(event['ts'] for event in result['events'])
        result['next_since'] = max(prompt.since or newest, newest)
    return result


def create_fleet_router(client_factory=None):
    router = APIRouter()
    @router.get('/api/fleet/overview')
    @router.post('/api/fleet/overview')
    async def snapshot():
        return await overview(client_factory)
    @router.post('/api/fleet/updates')
    async def changes(prompt: UpdatesRequest):
        return await updates(prompt, client_factory)
    return router
