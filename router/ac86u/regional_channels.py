"""Exact regional station identities shared by intake and household contracts."""
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CATALOG_PATH = Path(__file__).resolve().parents[2] / 'config/regional-channels.json'
CHANNELS = {r['channel_key']: r for r in json.loads(CATALOG_PATH.read_text(encoding='utf-8'))['channels']}
REJECTED_PATH = CATALOG_PATH.with_name('regional-rejected-routes.json')


def label_key(value):
    text = unicodedata.normalize('NFKC', str(value or '')).casefold().strip()
    # Resolution is a display suffix, never a station/region discriminator.
    text = re.sub(r'\s*\(?\d{3,4}p\)?\s*$', '', text)
    text = re.sub(r'\s+(?:hd|sd)\s*$', '', text)
    return re.sub(r'[\s_-]+', '', text)


ALIASES = {}
for _key, _row in CHANNELS.items():
    for _name in [_row['name'], _key, *_row['aliases']]:
        _alias = label_key(_name)
        if _alias in ALIASES and ALIASES[_alias] != _key:
            raise ValueError('ambiguous regional channel alias: ' + _name)
        ALIASES[_alias] = _key


def regional_key(name):
    return ALIASES.get(label_key(name))


def regional_group(key):
    return CHANNELS[key]['group'] if key in CHANNELS else '卫视台'


def route_equivalence_key(key, url):
    """Collapse regional HLS variants/known display params, retain routing IDs."""
    if key not in CHANNELS:
        return url
    p = urlsplit(url)
    host = p.hostname or ''
    if ':' in host:
        host = '[' + host + ']'
    if p.port and p.port != (443 if p.scheme == 'https' else 80):
        host += ':' + str(p.port)
    path = re.sub(r'/(?:master|playlist|index|chunklist[^/]*)\.m3u8$', '/<hls>', p.path)
    query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True)
             if k not in ('playlive', 'authid', 'blog.ntnas.top', 'checkedby:iptvcat.org')]
    return urlunsplit((p.scheme.lower(), host.lower(), path, urlencode(sorted(query)), ''))


def rejected_regional_route(key, url):
    if key not in CHANNELS or not REJECTED_PATH.exists():
        return False
    identity = route_equivalence_key(key, url)
    return any(row['channel_key'] == key and route_equivalence_key(key, row['url']) == identity
               for row in json.loads(REJECTED_PATH.read_text(encoding='utf-8'))['routes'])
