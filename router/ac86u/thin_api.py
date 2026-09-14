"""Bounded GitHub HTTPS transport. Router performs no clone or Git command."""
import base64
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

try:
    from .thin_contract import REPOSITORY, CONTROL_BRANCH, REPORT_BRANCH, MAX_ENVELOPE, encode
except ImportError:
    from thin_contract import REPOSITORY, CONTROL_BRANCH, REPORT_BRANCH, MAX_ENVELOPE, encode

API = 'https://api.github.com/repos/' + REPOSITORY


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('GitHub redirect refused')


def read_token(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError('token file missing or permissions must be 0600')
    raw = path.read_bytes()
    if len(raw) > 1024:
        raise RuntimeError('invalid token file')
    token = raw.decode('ascii').strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{20,1000}', token):
        raise RuntimeError('invalid token format')
    return token


class GithubData:
    def __init__(self, token, opener=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, path, *, payload=None, raw=False, maximum=MAX_ENVELOPE):
        dispatch = path == '/dispatches' and payload == {'event_type': 'home-observations-ready'}
        if (not dispatch and not path.startswith('/contents/')) or '..' in path or '\\' in path:
            raise ValueError('unsupported GitHub path')
        request = urllib.request.Request(API + path,
            data=encode(payload) if payload is not None else None,
            method='POST' if dispatch else ('PUT' if payload is not None else 'GET'), headers={
                'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github.raw+json' if raw else 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'AC86U-Thin-Probe/1.0',
                'Content-Type': 'application/json', 'Cache-Control': 'no-cache',
            })
        try:
            with self.opener.open(request, timeout=20) as response:
                data = response.read(maximum + 1)
                if len(data) > maximum:
                    raise RuntimeError('GitHub response too large')
                return data
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404 and payload is None:
                return None
            # Do not print request headers, response bodies or the credential.
            raise RuntimeError('GitHub HTTP ' + str(code)) from None

    def get(self, branch, path, *, maximum=MAX_ENVELOPE):
        if branch not in (CONTROL_BRANCH, REPORT_BRANCH, 'master'):
            raise ValueError('unsupported read branch')
        safe = urllib.parse.quote(path, safe='/')
        return self.request('/contents/' + safe + '?ref=' + branch, raw=True, maximum=maximum)

    def put_immutable(self, path, raw):
        if not path.startswith(('observations/', 'migrations/')) or not path.endswith('.json') or len(raw) > MAX_ENVELOPE:
            raise ValueError('unsupported observation destination')
        safe = urllib.parse.quote(path, safe='/')
        payload = {'branch': REPORT_BRANCH, 'message': 'Receive bounded household evidence',
                   'content': base64.b64encode(raw).decode('ascii')}
        try:
            self.request('/contents/' + safe, payload=payload)
            return
        except (RuntimeError, OSError):
            # Lost replies and duplicate deliveries are safe only for identical bytes.
            existing = self.get(REPORT_BRANCH, path)
            if existing == raw:
                return
            if existing is not None:
                raise RuntimeError('immutable observation content differs') from None
            raise

    def task(self, probe_id):
        raw = self.get(CONTROL_BRANCH, 'tasks/' + probe_id + '.json')
        return json.loads(raw) if raw else None

    def notify(self):
        # This event is handled on master even when the data branch has no
        # workflow file. Scheduled polling recovers a lost notification.
        try:
            self.request('/dispatches', payload={'event_type': 'home-observations-ready'})
        except (RuntimeError, OSError):
            return False
        return True
