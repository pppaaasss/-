"""Small, data-only task and observation envelopes. No history or decisions."""
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

REPOSITORY = 'pppaaasss/-'
CONTROL_BRANCH = 'home-control'
REPORT_BRANCH = 'home-reports'
ROUTE_CONTEXT = 'living-room-path-equivalent'
TASK_SCHEMA = 'iptv-home-task/v1'
RESULT_SCHEMA = 'iptv-home-observations/v1'
MAX_ENVELOPE = 96 * 1024
MAX_ROUTES = 4
SHA = re.compile(r'^[0-9a-f]{64}$')
PROBE = re.compile(r'^[a-z0-9][a-z0-9-]{2,47}$')
ZONE = timezone(timedelta(hours=8))
KINDS = ('primary-0200', 'recheck-1300', 'peak-2000')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def epoch(value):
    if not isinstance(value, str):
        raise ValueError('timestamp missing')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp lacks timezone')
    return parsed.timestamp()


def bounded_number(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError('invalid bounded number')
    return value


def slot(now):
    local = datetime.fromtimestamp(now, ZONE)
    for kind, start, end in ((KINDS[0], 2, 8), (KINDS[1], 13, 16), (KINDS[2], 20, 23)):
        if start <= local.hour < end:
            return kind, local.strftime('%Y%m%d'), local.replace(hour=end, minute=0, second=0, microsecond=0).timestamp()
    return None


def seal_task(value):
    value = dict(value)
    value.pop('batch_id', None)
    value['batch_id'] = digest(value)
    return value


def validate_task(value, probe_id, now):
    if len(encode(value)) > MAX_ENVELOPE or value.get('schema') != TASK_SCHEMA:
        raise ValueError('invalid task envelope')
    if value.get('probe_id') != probe_id or not PROBE.fullmatch(probe_id):
        raise ValueError('wrong probe')
    if value.get('route_context') != ROUTE_CONTEXT:
        raise ValueError('wrong household path')
    if value.get('batch_id') != seal_task(value)['batch_id'] or not SHA.fullmatch(str(value.get('cycle_id', ''))):
        raise ValueError('task hash mismatch')
    active = slot(now)
    if not active or active[0] != value.get('run_kind'):
        raise ValueError('outside task window')
    created, expires = epoch(value['created_utc']), epoch(value['expires_utc'])
    if not created <= now < expires <= active[2] or expires - created > 6 * 3600:
        raise ValueError('task expired or future')
    binding = value['formal_playlist']
    if binding.get('url') != 'https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u' or not SHA.fullmatch(str(binding.get('sha256', ''))):
        raise ValueError('invalid formal binding')
    if not SHA.fullmatch(str(value.get('feedback_sha256', ''))):
        raise ValueError('invalid feedback binding')
    limits = value['limits']
    bounded_number(limits['seconds'], 1, 240)
    bounded_number(limits['bytes'], 1, 64 * 1024 * 1024)
    tasks = value['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_ROUTES:
        raise ValueError('too many tasks')
    seen = set()
    for task in tasks:
        url = urlsplit(task['url'])
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or len(task['url']) > 2048:
            raise ValueError('invalid media URL')
        if any(ord(c) < 32 for c in task['url']):
            raise ValueError('control character in URL')
        if task.get('role') not in ('current', 'candidate', 'backup'):
            raise ValueError('invalid task role')
        if active[0] == KINDS[1] and task['role'] != 'current':
            raise ValueError('afternoon must only probe current routes')
        if active[0] != KINDS[0] and task['role'] == 'candidate':
            raise ValueError('discovery outside primary window')
        identity = task.get('task_id')
        if not SHA.fullmatch(str(identity)) or identity in seen:
            raise ValueError('invalid task identity')
        if identity != digest([value['cycle_id'], task['role'], task['channel_key'], task['url'], task['attempt']]):
            raise ValueError('task identity does not match route and attempt')
        seen.add(identity)
        expected = hashlib.sha256((task['channel_key'] + '\n' + task['url'] + '\n').encode()).hexdigest()
        if task.get('candidate_id') != expected:
            raise ValueError('route identity mismatch')
        bounded_number(task['sample_bytes'], 65536, 6 * 1024 * 1024)
        bounded_number(task['min_height'], 1, 8640)
        if not isinstance(task.get('viewer_accepted_quality', False), bool):
            raise ValueError('invalid viewer quality policy')
        if task.get('viewer_accepted_quality') and task['role'] != 'current':
            raise ValueError('viewer acceptance only applies to exact current routes')
        for key in ('minimum_h264_stream_mbps', 'minimum_hevc_stream_mbps', 'minimum_other_stream_mbps'):
            if key in task:
                bounded_number(task[key], 0.1, 500)
        if task.get('metadata') is not True or task.get('attempt') not in (1, 2):
            raise ValueError('invalid measurement requirements')
    return value


def validate_observations(value, task, now):
    if len(encode(value)) > MAX_ENVELOPE or value.get('schema') != RESULT_SCHEMA:
        raise ValueError('invalid observation envelope')
    for key in ('batch_id', 'cycle_id', 'probe_id', 'route_context'):
        if value.get(key) != task.get(key):
            raise ValueError('observation binding mismatch: ' + key)
    start, end = epoch(value['started_utc']), epoch(value['finished_utc'])
    if not epoch(task['created_utc']) <= start <= end <= min(now + 60, epoch(task['expires_utc'])) or end - start > task['limits']['seconds'] + 30:
        raise ValueError('invalid observation time')
    rows = value.get('results')
    if not isinstance(rows, list) or len(rows) > len(task['tasks']):
        raise ValueError('invalid observation count')
    expected = {r['task_id']: r for r in task['tasks']}
    seen = set()
    for row in rows:
        identity = row['task_id']
        if identity not in expected or identity in seen:
            raise ValueError('unrequested or repeated result')
        seen.add(identity)
        began, ended = epoch(row['started_utc']), epoch(row['finished_utc'])
        if not start <= began <= ended <= end or ended > epoch(task['expires_utc']):
            raise ValueError('measurement outside task window')
        result = row['result']
        if result.get('url') != expected[identity]['url'] or result.get('channel_key') != expected[identity]['channel_key']:
            raise ValueError('measured route mismatch')
        if result.get('observed_status') not in ('GOOD', 'DEGRADED', 'UNAVAILABLE', 'UNKNOWN'):
            raise ValueError('invalid observed status')
        bounded_number(result.get('sample_count'), 0, 2)
        if len(str(result.get('error', ''))) > 500:
            raise ValueError('error is too long')
    bounded_number(value['usage']['downloaded_bytes'], 0, task['limits']['bytes'])
    bounded_number(value['usage']['runtime_s'], 0, task['limits']['seconds'] + 30)
    return value
