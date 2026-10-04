"""Bounded text-only GitHub discovery; all remote URLs are constructed locally."""
import json
import re
import signal
from collections import deque
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

MAX_FILES = 48  # Existing registry intake ceiling, shared with discovered files.
MAX_REQUESTS = 144  # Existing worst case: 48 files x (history + two revisions).
MAX_READ_SECONDS = 20
MAX_SECONDS = 1200  # Below the existing 30-minute workflow ceiling.


class IntakeBudgetExceeded(ValueError):
    pass


class ReadBudget:
    def __init__(self, read, clock=time.monotonic):
        self.read, self.clock = read, clock
        self.started, self.requests = clock(), 0

    def check(self):
        if self.requests >= MAX_REQUESTS:
            raise IntakeBudgetExceeded('request_budget_exhausted')
        if self.clock() - self.started >= MAX_SECONDS:
            raise IntakeBudgetExceeded('time_budget_exhausted')

    def __call__(self, url, api=False):
        self.check()
        self.requests += 1
        remaining = MAX_SECONDS - (self.clock() - self.started)
        if remaining <= 0:
            raise IntakeBudgetExceeded('time_budget_exhausted')
        # Linux Actions, main thread: unlike a socket timeout, ITIMER_REAL also
        # interrupts a slow trickle during response.read. No helper survives.
        def expired(signum, frame):
            if remaining <= MAX_READ_SECONDS:
                raise IntakeBudgetExceeded('time_budget_exhausted')
            raise TimeoutError('source_read_deadline_exceeded')
        previous = signal.signal(signal.SIGALRM, expired)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, min(MAX_READ_SECONDS, remaining))
        began = self.clock()
        try:
            return self.read(url, api)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if previous_timer[0]:
                signal.setitimer(signal.ITIMER_REAL,
                    max(0.000001, previous_timer[0] - (self.clock() - began)), previous_timer[1])


def discover_files(now, read, seen, audit):
    # Repository activity is only a search hint. verified_text remains mandatory.
    day = datetime.fromtimestamp(now - 86400, timezone.utc).strftime('%Y-%m-%d')
    query = 'iptv pushed:>=' + day + ' archived:false'
    audit['queries'].append(query)
    result = json.loads(read('https://api.github.com/search/repositories?' + urlencode(
        dict(q=query, sort='updated', order='desc', per_page=20)), True))
    audit['search_incomplete'] = bool(result.get('incomplete_results'))
    audit['search_total_count'] = result.get('total_count')
    items = result.get('items')
    if not isinstance(items, list):
        raise ValueError('invalid_search_response')
    pools = []
    visited = set()
    for repo in items[:20]:
        name, branch = repo.get('full_name', ''), repo.get('default_branch', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', name) or not re.fullmatch(r'[\w.-]+', branch) or branch in ('.', '..'):
            audit['rejected_repository_metadata'] += 1
            continue
        if name in visited:
            continue
        visited.add(name)
        audit['repositories_examined'] += 1
        try:
            tree = json.loads(read('https://api.github.com/repos/' + name + '/git/trees/' + quote(branch, safe='') + '?recursive=1', True))
            if tree.get('truncated'):
                audit['truncated_trees'] += 1
            if not isinstance(tree.get('tree'), list):
                raise ValueError('invalid_tree_response')
            candidates = []
            for entry in sorted(tree['tree'], key=file_priority):
                path = entry.get('path', '')
                if (entry.get('type') != 'blob' or entry.get('mode') not in ('100644', '100755')
                        or not path.lower().endswith(('.m3u', '.m3u8', '.txt')) or 'readme' in path.lower()
                        or not path or any(p in ('', '.', '..') for p in path.split('/'))
                        or any(c in path for c in '\\?#%\r\n') or entry.get('size', 0) > 12*1024*1024):
                    continue
                url = 'https://raw.githubusercontent.com/' + name + '/' + branch + '/' + quote(path, safe='/')
                if url not in seen:
                    candidates.append(dict(url=url, group='大陆', discovery_repository=name, discovery_query=query))
            if candidates:
                # Give every repository its first opportunity before a second
                # file from any repository consumes the scarce remaining slots.
                pool = deque(candidates)
                pools.append(pool)
                yield pool.popleft()
        except IntakeBudgetExceeded:
            raise
        except Exception as exc:
            audit['errors'].append(dict(repository=name, reason=type(exc).__name__))

    while any(pools):
        for pool in pools:
            while pool and pool[0]['url'] in seen:
                pool.popleft()
            if pool:
                yield pool.popleft()


def file_priority(entry):
    path = str(entry.get('path', '')).lower()
    parts = path.split('/')
    name = parts[-1]
    # Heuristics only; freshness still requires per-file content evidence.
    historical = any(p in ('archive', 'archives', 'backup', 'backups', 'test', 'tests', 'examples') for p in parts)
    preferred = name in ('live.m3u', 'live.m3u8', 'ipv4.m3u', 'ipv4.txt',
                         'result.m3u', 'result.m3u8', 'best_sorted.m3u', 'live_cn.m3u')
    return (historical, not preferred, not path.endswith(('.m3u', '.m3u8')), path)
