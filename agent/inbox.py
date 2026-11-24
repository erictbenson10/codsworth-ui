"""Read-only Corner work inbox. Gitea owns tickets; Fleet owns execution."""
import asyncio
from datetime import datetime, timezone
import json
import os
from urllib.parse import urlsplit

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from agent.bugs import connector
from agent.fleet_status import overview
from agent.tickets import PAGE_SIZE, TicketRequest, plain_text, read_tickets

REPO = 'example/workspace'
SOURCE_TIMEOUT = 25
# Rule 2: a failed read is an error, and an error states how many times it tried.
SOURCE_ATTEMPTS = 2
GITEA_HOSTS = ('https://git.example.com', 'http://192.0.2.25:3080')


def gitea_address():
    """The hop this inbox actually reads, matching the connector's own allowlist."""
    base = os.environ.get('CODS_GITEA_BASE_URL', GITEA_HOSTS[0]).rstrip('/')
    return base if base in GITEA_HOSTS else GITEA_HOSTS[0]


def fleet_address():
    """Scheme, host and port only: a configured Fleet URL may carry a credential."""
    raw = os.environ.get('CODS_FLEET_URL', '').strip()
    if not raw:
        return 'not configured'
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return 'unreadable'
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return 'unreadable'
    return parsed.scheme + '://' + parsed.hostname + (':' + str(port) if port else '')


class InboxRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def pull_summary(row):
    if not isinstance(row, dict):
        raise ValueError('Invalid pull request')
    number = row.get('number')
    if type(number) is not int or number < 1 or row.get('state') != 'open':
        raise ValueError('Invalid open pull request')
    url = f'https://git.example.com/{REPO}/pulls/{number}'
    if row.get('html_url') not in (url, f'http://192.0.2.25:3080/{REPO}/pulls/{number}'):
        raise ValueError('Unverified pull request URL')
    title = plain_text(row.get('title'), 255)
    if not title:
        raise ValueError('Empty pull request title')
    return {'number': number, 'title': title, 'state': 'open', 'url': url}


async def read_inbox(invoke=None, client_factory=None):
    async def issues():
        return await read_tickets(TicketRequest(repo=REPO), invoke)

    async def pulls():
        def fetch():
            return json.loads((invoke or connector().invoke)('read', {
                'action': 'pulls', 'repo': REPO, 'state': 'open',
                'page': 1, 'limit': PAGE_SIZE}))
        response = await asyncio.to_thread(fetch)
        if not isinstance(response, dict) or response.get('success') is not True or not isinstance(response.get('data'), list):
            raise ValueError('Pull requests unavailable')
        rows = response['data']
        return {'pulls': [pull_summary(row) for row in rows[:PAGE_SIZE]],
                'possibly_more': len(rows) >= PAGE_SIZE}

    async def bounded(read, succeeded):
        """Retry a failed read, then report the outcome with the number of tries."""
        result, tries = None, 0
        for _ in range(SOURCE_ATTEMPTS):
            tries += 1
            try:
                result = await asyncio.wait_for(read(), SOURCE_TIMEOUT)
            except Exception:
                # Connector exceptions may contain credentials or upstream content.
                result = None
            if succeeded(result):
                return result, tries
        return result, tries

    (issue_result, issue_tries), (pull_result, pull_tries), (fleet, fleet_tries) = await asyncio.gather(
        bounded(issues, lambda value: isinstance(value, dict) and value.get('ok') is True),
        bounded(pulls, lambda value: value is not None),
        bounded(lambda: overview(client_factory),
                lambda value: isinstance(value, dict) and not value.get('errors')))
    issue_ok = isinstance(issue_result, dict) and issue_result.get('ok') is True
    pull_ok = pull_result is not None
    fleet_ok = isinstance(fleet, dict) and not fleet.get('errors')
    gitea, fleet_host = gitea_address(), fleet_address()
    # Each section names its hop and address and counts its tries, so a reader
    # never has to guess whether an empty section is quiet or blind.
    sources = {'issues': {'host': 'Gitea', 'address': gitea, 'tries': issue_tries, 'ok': issue_ok},
               'pulls': {'host': 'Gitea', 'address': gitea, 'tries': pull_tries, 'ok': pull_ok},
               'fleet': {'host': 'Fleet', 'address': fleet_host, 'tries': fleet_tries, 'ok': fleet_ok}}
    errors = []
    for key, message in (('issues', 'Corner issues unavailable; this is not an empty queue.'),
                         ('pulls', 'Corner pull requests unavailable; retry the inbox.'),
                         ('fleet', 'Fleet is partially or fully unavailable; available results are shown.')):
        if not sources[key]['ok']:
            errors.append(dict(source=key, message=message, host=sources[key]['host'],
                               address=sources[key]['address'], tries=sources[key]['tries']))
    return {'type': 'work_inbox', 'repo': REPO,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'issues': issue_result['tickets'] if issue_ok else [],
            'pulls': pull_result['pulls'] if pull_ok else [],
            'possibly_more': {'issues': issue_result.get('possibly_more', False) if issue_ok else False,
                              'pulls': pull_result['possibly_more'] if pull_ok else False},
            # Fleet's counters/questions are global, never presented as Corner-only.
            'fleet': fleet, 'fleet_scope': 'global',
            'available': {'issues': issue_ok, 'pulls': pull_ok, 'fleet': fleet_ok},
            'sources': sources, 'errors': errors}


def create_inbox_router(invoke=None, client_factory=None):
    router = APIRouter()

    @router.post('/api/inbox')
    async def inbox(prompt: InboxRequest):
        return await read_inbox(invoke, client_factory)

    return router
