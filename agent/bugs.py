"""Explicit self-bug submission: user text only, fixed repository, no retries."""
import asyncio
from functools import lru_cache
import importlib.util
import json
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator


@lru_cache(maxsize=1)
def connector():
    path = Path(__file__).parent / 'plugins' / 'cods-gitea' / 'connector.py'
    spec = importlib.util.spec_from_file_location('_cods_bug_transport', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BugRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    description: str = Field(min_length=1, max_length=4000)

    @field_validator('description')
    @classmethod
    def valid_text(cls, value):
        if not value.strip() or any((ord(c) < 32 and c not in '\n\t') or 127 <= ord(c) <= 159 for c in value):
            raise ValueError('Provide nonempty plain text without control characters')
        return value


async def submit_bug(prompt, invoke=None):
    arguments = {'action': 'create', 'repo': 'example/codsworth',
                 'title': prompt.description.strip().splitlines()[0][:100],
                 'body': prompt.description}
    result = {'type': 'bug_report', 'ok': False, 'retryable': False}
    def send():
        function = invoke or connector().invoke
        return json.loads(function('write', arguments))
    try:
        response = await asyncio.to_thread(send)
    except (OSError, ValueError, TypeError, ImportError, AttributeError):
        return dict(result, error='Bug submission could not be verified. Inspect Cods issues before submitting again.', outcome='unknown')
    if not isinstance(response, dict):
        response = {}
    if response.get('success') is not True:
        code = response.get('http_status')
        outcome = 'rejected' if type(code) is int and 400 <= code < 500 else 'unknown'
        return dict(result, error='Gitea rejected the bug report.' if outcome == 'rejected' else 'Bug submission could not be verified. Inspect Cods issues before submitting again.', outcome=outcome)
    data = response.get('data')
    if not isinstance(data, dict):
        data = {}
    number, url = data.get('number'), data.get('html_url')
    if type(number) is not int or number < 1 or url not in (f'https://git.example.com/example/codsworth/issues/{number}', f'http://192.0.2.25:3080/example/codsworth/issues/{number}'):
        return dict(result, error='Gitea returned an unverified issue receipt. Inspect Cods issues before submitting again.', outcome='unknown')
    return {'type': 'bug_report', 'ok': True, 'number': number, 'url': f'https://git.example.com/example/codsworth/issues/{number}'}


def create_bugs_router(invoke=None):
    router = APIRouter()
    @router.post('/api/bugs')
    async def report(prompt: BugRequest):
        return await submit_bug(prompt, invoke)
    return router
