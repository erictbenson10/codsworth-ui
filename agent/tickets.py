"""Bounded, read-only Gitea issue inbox, independent of model conversations."""
import asyncio
import json
from typing import Literal
import unicodedata

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from agent.bugs import connector

PAGE_SIZE = 20
BODY_LIMIT = 12000


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    repo: Literal['example/workspace', 'example/codsworth'] = 'example/workspace'
    state: Literal['open', 'closed', 'all'] = 'open'
    issue: int | None = Field(default=None, ge=1, le=2147483647)
    page: int = Field(default=1, ge=1, le=10000)


def plain_text(value, limit, multiline=False):
    if not isinstance(value, str):
        raise ValueError('Expected text')
    # Remote issue text must not inject terminal controls or bidi formatting.
    text = ''.join(c for c in value if not unicodedata.category(c).startswith('C')
                   or (multiline and c in '\n\t'))
    if not multiline:
        text = ' '.join(text.split())
    return text[:limit]


def summarize(data, detail=False, expected_issue=None, repo='example/workspace'):
    if not isinstance(data, dict) or data.get('pull_request') is not None:
        raise ValueError('Expected issue')
    number, state = data.get('number'), data.get('state')
    if type(number) is not int or number < 1 or state not in ('open', 'closed'):
        raise ValueError('Invalid issue')
    if expected_issue is not None and number != expected_issue:
        raise ValueError('Wrong issue')
    url = f'https://git.example.com/{repo}/issues/{number}'
    if data.get('html_url') not in (url, f'http://192.0.2.25:3080/{repo}/issues/{number}'):
        raise ValueError('Unverified issue URL')
    title = plain_text(data.get('title'), 255)
    if not title:
        raise ValueError('Empty issue title')
    ticket = {'number': number, 'title': title, 'state': state, 'url': url}
    if detail:
        body = data.get('body')
        if body is None:
            body = ''
        ticket.update(body=plain_text(body, BODY_LIMIT, multiline=True),
                      body_truncated=len(body) > BODY_LIMIT)
    return ticket


async def read_tickets(prompt, invoke=None):
    arguments = {'action': 'get' if prompt.issue is not None else 'list',
                 'repo': prompt.repo}
    if prompt.issue is not None:
        arguments['issue'] = prompt.issue
    else:
        arguments.update(state=prompt.state, page=prompt.page, limit=PAGE_SIZE)
    def fetch():
        return json.loads((invoke or connector().invoke)('read', arguments))
    error = {'type': 'ticket_inbox', 'ok': False,
             'error': 'Gitea issues could not be read. Check the Gitea connector and try again.'}
    try:
        response = await asyncio.to_thread(fetch)
        if not isinstance(response, dict) or response.get('success') is not True:
            if isinstance(response, dict) and response.get('http_status') == 404 and prompt.issue is not None:
                return dict(error, error='That Gitea issue was not found or is not accessible.')
            return error
        data = response.get('data')
        if prompt.issue is not None:
            tickets = [summarize(data, detail=True, expected_issue=prompt.issue, repo=prompt.repo)]
            more = False
        else:
            if not isinstance(data, list):
                raise ValueError('Expected issue list')
            tickets = [summarize(item, repo=prompt.repo) for item in data[:PAGE_SIZE]]
            if prompt.state != 'all' and any(t['state'] != prompt.state for t in tickets):
                raise ValueError('Unexpected issue state')
            more = len(data) >= PAGE_SIZE
        return {'type': 'ticket_inbox', 'ok': True, 'repo': prompt.repo, 'state': prompt.state,
                'page': prompt.page, 'tickets': tickets, 'possibly_more': more}
    except (OSError, ValueError, TypeError, ImportError, AttributeError):
        return error


def create_tickets_router(invoke=None):
    router = APIRouter()
    @router.post('/api/tickets')
    async def inbox(prompt: TicketRequest):
        return await read_tickets(prompt, invoke)
    return router
