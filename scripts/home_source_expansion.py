"""Bounded text-only GitHub discovery; all remote URLs are constructed locally."""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

MAX_FILES = 48  # Existing registry intake ceiling, shared with discovered files.
MAX_REQUESTS = 144  # Existing worst case: 48 files x (history + two revisions).
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
        return self.read(url, api)


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
    for repo in items[:20]:
        name, branch = repo.get('full_name', ''), repo.get('default_branch', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', name) or not re.fullmatch(r'[\w.-]+', branch) or branch in ('.', '..'):
            audit['rejected_repository_metadata'] += 1
            continue
        audit['repositories_examined'] += 1
        try:
            tree = json.loads(read('https://api.github.com/repos/' + name + '/git/trees/' + quote(branch, safe='') + '?recursive=1', True))
            if tree.get('truncated'):
                audit['truncated_trees'] += 1
            if not isinstance(tree.get('tree'), list):
                raise ValueError('invalid_tree_response')
            for entry in sorted(tree['tree'], key=lambda x: str(x.get('path', ''))):
                path = entry.get('path', '')
                if (entry.get('type') != 'blob' or entry.get('mode') not in ('100644', '100755')
                        or not path.lower().endswith(('.m3u', '.m3u8', '.txt')) or 'readme' in path.lower()
                        or not path or any(p in ('', '.', '..') for p in path.split('/'))
                        or any(c in path for c in '\\?#%\r\n') or entry.get('size', 0) > 12*1024*1024):
                    continue
                url = 'https://raw.githubusercontent.com/' + name + '/' + branch + '/' + quote(path, safe='/')
                if url not in seen:
                    yield dict(url=url, group='大陆', discovery_repository=name, discovery_query=query)
        except IntakeBudgetExceeded:
            raise
        except Exception as exc:
            audit['errors'].append(dict(repository=name, reason=type(exc).__name__))
