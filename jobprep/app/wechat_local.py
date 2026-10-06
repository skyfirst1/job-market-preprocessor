"""Local-only, single-article pilot. Limits tool calls, not server HTTP traffic."""

import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import httpx


ENDPOINT = 'http://127.0.0.1:4545/mcp'
TOOL = 'single_article_download'
MAX_RESPONSE_BYTES = 1024 * 1024


class LocalWechatDownload:
    """One lifetime download call per ledger; no reset, retry or remote fallback.

    allowed_url: exact public mp.weixin.qq.com/s article approved for this pilot.
    ledger_path: persistent SQLite file, shared across restarts and workers.
    timeout: bounded client wait (1..120 seconds), NOT server cancellation.
    transport: optional HTTPX transport for offline tests only.
    """

    def __init__(self, allowed_url, ledger_path, *, timeout=60, transport=None):
        parts = urlsplit(allowed_url)
        if (parts.scheme != 'https' or parts.hostname != 'mp.weixin.qq.com'
                or parts.username or parts.password or parts.port not in (None, 443)
                or not (parts.path == '/s' or parts.path.startswith('/s/'))
                or any(ord(char) <= 32 for char in allowed_url)):
            raise ValueError('Expected an HTTPS mp.weixin.qq.com/s article URL')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
            raise ValueError('timeout must be 1..120 seconds')
        self.url, self.path = allowed_url, Path(ledger_path).resolve()
        self.timeout, self.transport = timeout, transport
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS pilot (id INTEGER PRIMARY KEY CHECK(id=1), url TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO pilot VALUES(1,?,0,'ready')", (allowed_url,))
            if db.execute('SELECT url FROM pilot WHERE id=1').fetchone()[0] != allowed_url:
                raise ValueError('Pilot URL is already pinned; changing it requires a new approved pilot')

    def status(self):
        with sqlite3.connect(self.path) as db:
            used, state = db.execute('SELECT used,state FROM pilot WHERE id=1').fetchone()
        return {'download_calls_used': used, 'download_call_limit': 1, 'state': state,
                'endpoint': ENDPOINT, 'remote_fallback': False,
                'server_http_request_limit_verified': False}

    def _reserve(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            used = db.execute('SELECT used FROM pilot WHERE id=1').fetchone()[0]
            if used >= 1:
                raise RuntimeError('Pilot budget exhausted; no automatic retry or reset')
            db.execute("UPDATE pilot SET used=used+1,state='sent_or_unknown' WHERE id=1")

    def _state(self, state):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE pilot SET state=? WHERE id=1', (state,))

    async def _rpc(self, client, method, params, request_id, headers):
        payload = {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}
        async with client.stream('POST', ENDPOINT, json=payload, headers=headers) as response:
            response.raise_for_status()
            session = response.headers.get('mcp-session-id')
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError('MCP response exceeds byte limit')
                chunks.append(chunk)
            message = json.loads(b''.join(chunks))
        if (not isinstance(message, dict) or message.get('id') != request_id
                or message.get('jsonrpc') != '2.0' or 'error' in message
                or not isinstance(message.get('result'), dict)):
            raise ValueError('Invalid MCP response; no fallback or retry')
        if session:
            headers['Mcp-Session-Id'] = session
        return message['result']

    async def run(self, *, execute=False):
        if type(execute) is not bool:
            raise ValueError('execute must be boolean')
        if execute and self.status()['download_calls_used']:
            raise RuntimeError('Pilot budget exhausted; no automatic retry or reset')
        headers = {'Accept': 'application/json, text/event-stream'}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False,
                                     follow_redirects=False, transport=self.transport) as client:
            try:
                await self._rpc(client, 'initialize', {
                    'protocolVersion': '2024-11-05', 'capabilities': {},
                    'clientInfo': {'name': 'jobprep-single-pilot', 'version': '1'}}, 1, headers)
                tools = await self._rpc(client, 'tools/list', {}, 2, headers)
                entries = tools.get('tools')
                if not isinstance(entries, list):
                    raise ValueError('Invalid tools/list schema')
                matches = [entry for entry in entries if isinstance(entry, dict) and entry.get('name') == TOOL]
                if len(matches) != 1:
                    raise ValueError('Expected exactly one local single_article_download tool')
                schema = matches[0].get('inputSchema', {})
                if (schema.get('type') != 'object' or 'url' not in schema.get('properties', {})
                        or set(schema.get('required', [])) - {'url'}):
                    raise ValueError('Single-article tool does not support documented URL-only input')
            except Exception as exc:
                return {**self.status(), 'status': 'unavailable', 'error_type': type(exc).__name__,
                        'message': 'Local MCP unavailable or unsupported; no article download sent'}
            if not execute:
                return {**self.status(), 'status': 'ready', 'tool': TOOL}
            self._reserve()
            try:
                result = await self._rpc(client, 'tools/call', {
                    'name': TOOL, 'arguments': {'url': self.url}}, 3, headers)
                failed = result.get('isError') is True
                state = 'tool_error_no_retry' if failed else 'response_received'
                self._state(state)
                # Do not log arbitrary server content: it can contain credentials.
                return {**self.status(), 'status': 'error' if failed else 'response_received',
                        'download_completion_verified': False,
                        'message': 'Inspect retained GUI and output files; RPC receipt is not download completion'}
            except Exception as exc:
                self._state('outcome_unknown_no_retry')
                return {**self.status(), 'status': 'outcome_unknown', 'error_type': type(exc).__name__,
                        'message': 'Server may still be running; do not retry or close supervision window'}
