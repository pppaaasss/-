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
    # Three bounded, target-oriented searches; no broad worldwide fallback.
    repositories = {}
    successes = 0
    audit['search_incomplete'] = False
    audit['query_results'] = []
    for terms in ('iptv cctv', 'iptv china', 'iptv 卫视'):
        query = terms + ' pushed:>=' + day + ' archived:false'
        audit['queries'].append(query)
        try:
            result = json.loads(read('https://api.github.com/search/repositories?' + urlencode(
                dict(q=query, sort='updated', order='desc', per_page=20)), True))
            items = result.get('items')
            if not isinstance(items, list):
                raise ValueError('invalid_search_response')
            successes += 1
            audit['search_incomplete'] |= bool(result.get('incomplete_results'))
            audit['query_results'].append(dict(query=query, total_count=result.get('total_count'), returned=len(items[:20])))
            for repo in items[:20]:
                name = repo.get('full_name', '')
                if name not in repositories:
                    repositories[name] = dict(repo, discovery_query=query)
        except IntakeBudgetExceeded:
            raise
        except Exception as exc:
            audit['errors'].append(dict(query=query, reason=type(exc).__name__))
    if not successes:
        raise ValueError('all_search_queries_failed')
    items = sorted(repositories.values(), key=lambda r: (-repository_relevance(r), r.get('full_name', '')))
    audit['search_unique_repositories'] = len(items)
    audit['repository_scan_limited'] = len(items) > 20
    pools = []
    visited = set()
    for repo in items[:20]:
        name, branch = repo.get('full_name', ''), repo.get('default_branch', '')
        query = repo['discovery_query']
        if getattr(read, 'requests', 0) >= MAX_REQUESTS - 3:
            audit['repository_scan_limited'] = True
            break
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
                    candidates.append(dict(url=url, group='大陆', discovery_repository=name, discovery_query=query, discovery_path=path))
            if candidates:
                # Give every repository its first opportunity before a second
                # file from any repository consumes the scarce remaining slots.
                pool = deque(candidates)
                pools.append((repository_relevance(repo), pool))
        except IntakeBudgetExceeded:
            raise
        except Exception as exc:
            audit['errors'].append(dict(repository=name, reason=type(exc).__name__))

    # Rank each repository by its best actual file hint before spending any
    # file slots; within equal relevance retain cross-repository round robin.
    while any(pool for _, pool in pools):
        active = [(score, pool) for score, pool in pools if pool]
        def rank(pair):
            return (*file_priority({'path': pair[1][0]['discovery_path']})[:2], -pair[0])
        active.sort(key=rank)
        best = rank(active[0])
        for _, pool in [pair for pair in active if rank(pair) == best]:
            while pool and pool[0]['url'] in seen:
                pool.popleft()
            if pool:
                yield pool.popleft()


def target_relevance(text):
    text = text.casefold()
    if any(word in text for word in ('cctv', '央视', '卫视', '中国', '大陆', '国内')):
        return 2
    if re.search(r'(?:^|[^a-z])(china|chinese|cn)(?:$|[^a-z])', text):
        return 1
    return 0


def repository_relevance(repo):
    return target_relevance(' '.join([str(repo.get('full_name', '')),
        str(repo.get('description') or ''), ' '.join(str(x) for x in repo.get('topics', []))]))


def file_priority(entry):
    path = str(entry.get('path', '')).lower()
    parts = path.split('/')
    name = parts[-1]
    # Heuristics only; freshness still requires per-file content evidence.
    historical = any(p in ('archive', 'archives', 'backup', 'backups', 'test', 'tests', 'examples') for p in parts)
    preferred = name in ('live.m3u', 'live.m3u8', 'ipv4.m3u', 'ipv4.txt',
                         'result.m3u', 'result.m3u8', 'best_sorted.m3u', 'live_cn.m3u')
    return (historical, -target_relevance(path), not preferred, not path.endswith(('.m3u', '.m3u8')), path)
