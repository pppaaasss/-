#!/opt/bin/python3
"""One diagnostic pass over managed channels in the actual TV subscription.

No candidates, backups, decisions, pairing, cron changes or production reports.
Core supplies the channel scope only; each tested URL comes from tv.m3u.
"""
import collections
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time

try:
    from . import home_probe as probe
except ImportError:
    import home_probe as probe

TV_URL = 'https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u'
CORE_URL = 'https://raw.githubusercontent.com/pppaaasss/-/master/tv-core.m3u'


class ScanStopped(BaseException):
    pass


def stop(signum, _frame):
    raise ScanStopped('time_budget' if signum == signal.SIGALRM else 'interrupted')


try:
    from . import home_resources
except ImportError:
    import home_resources


def manual_resources(config):
    return home_resources.sample_resources(config, probe.system_resources)


def check_resources(config, record):
    reason, resources = manual_resources(config)
    record['last_resources'] = resources
    print('RESOURCES: ' + json.dumps(resources), flush=True)
    return reason


def plan(tv, core):
    expected = {}
    for name, url in probe.parse_playlist(core):
        key = probe.station_key(name)
        if not key or key in expected:
            raise ValueError('invalid_core_scope')
        expected[key] = (name, url)
    if not expected:
        raise ValueError('empty_core_scope')
    actual = collections.defaultdict(dict)
    entries = probe.parse_playlist(tv)
    for name, url in entries:
        key = probe.station_key(name)
        if key in expected:
            actual[key].setdefault(url, name)
    rows = []
    for key, (name, core_url) in expected.items():
        urls = actual[key]
        for url, tv_name in (urls or {'': name}).items():
            rows.append(dict(name=tv_name, channel_key=key, url=url,
                route_id=key + ':' + hashlib.sha256(url.encode()).hexdigest(),
                scope_issue='' if url else 'missing_from_tv',
                differs_from_core=bool(url and url != core_url)))
    return entries, rows


def public_result(raw, item):
    row = {k: raw.get(k) for k in ('status', 'observed_status', 'sample_count', 'height',
        'codec', 'deep_checked', 'min_download_mbps', 'stream_mbps', 'headroom_ratio')}
    row.update(name=item['name'], channel_key=item['channel_key'], route_id=item['route_id'],
               url_sha256=hashlib.sha256(item['url'].encode()).hexdigest(),
               differs_from_core=item['differs_from_core'])
    row['error'] = re.sub(r'https?://\S+', '<url>', str(raw.get('error', '')))[:240]
    if row['status'] == 'GOOD' and not row['deep_checked']:
        row['status'] = 'UNKNOWN'
        row['error'] = row['error'] or 'quality_not_verified'
    return row


def scan(config, output, budget=1200):
    record = dict(schema='iptv-manual-tv-scan-v1', started_utc=probe.utc_text(),
                  route_verified=False, production_use=False, state='RUNNING',
                  rows=[], untested=[], temporary_rule_cleaned=False)
    deadline = time.monotonic() + min(budget, 1200)
    transport, planned, completed = None, [], set()
    config = dict(config, runtime_transport='merlinclash-marked', lan_dns_server='192.168.50.1')
    # A prior acceptance flag must not hide this diagnostic's quality readings.
    config['viewer_accepted_quality'] = False
    try:
        blocked = check_resources(config, record)
        if blocked:
            record.update(state='STOPPED', reason=blocked)
            return record
        with probe.transport_context(config) as transport:
            tv, _, _ = probe.fetch_playlist(TV_URL)
            core, _, _ = probe.fetch_playlist(CORE_URL)
            entries, planned = plan(tv, core)
            record.update(tv_rows=len(entries), managed_routes=len(planned),
                managed_channels=len({r['channel_key'] for r in planned}),
                tv_sha256=hashlib.sha256(tv).hexdigest(), core_sha256=hashlib.sha256(core).hexdigest(),
                differs_from_core=[r['name'] for r in planned if r['differs_from_core']])
            print('SCOPE: TV=%d MANAGED_ROUTES=%d CORE_DIFF=%s' % (
                len(entries), len(planned), ','.join(record['differs_from_core']) or 'none'), flush=True)
            for index, item in enumerate(planned, 1):
                if time.monotonic() >= deadline:
                    record.update(state='STOPPED', reason='time_budget')
                    break
                blocked = check_resources(config, record)
                if blocked:
                    record.update(state='STOPPED', reason=blocked)
                    break
                if item['scope_issue']:
                    row = dict(name=item['name'], channel_key=item['channel_key'],
                               status='UNKNOWN', error=item['scope_issue'])
                else:
                    print('TESTING: %d/%d %s' % (index, len(planned), item['name']), flush=True)
                    floor = probe.minimum_height(item['name'], config)
                    if re.search(r'4K', item['name'], re.I):
                        floor = max(2160, floor)
                    raw = probe.probe_route(item['name'], item['url'], floor=floor, config=config,
                        sample_limit=2 * 1024 * 1024, include_metadata=True)
                    row = public_result(raw, item)
                record['rows'].append(row)
                completed.add(item['route_id'])
                print('RESULT: ' + json.dumps(row, ensure_ascii=False), flush=True)
                probe.atomic_json(output, record)
            else:
                record['state'] = 'COMPLETE'
    except ScanStopped as exc:
        record.update(state='STOPPED', reason=str(exc))
    except Exception as exc:
        record.update(state='FAILED', reason=type(exc).__name__)
    finally:
        record['untested'] = [r['name'] for r in planned if r['route_id'] not in completed]
        record['temporary_rule_cleaned'] = None if transport is None else not transport.installed
        if transport:
            record['connections'] = dict(IPv4=transport.dialer.ipv4, IPv6=transport.dialer.ipv6)
            record['dns_queries'] = transport.dialer.resolver.diagnostics()
        record['finished_utc'] = probe.utc_text()
        record['counts'] = dict(collections.Counter(r['status'] for r in record['rows']))
        probe.atomic_json(output, record)
        print('SUMMARY: ' + json.dumps({k: record.get(k) for k in (
            'state', 'reason', 'counts', 'untested', 'connections', 'dns_queries',
            'temporary_rule_cleaned', 'last_resources')}, ensure_ascii=False), flush=True)
        print('MANUAL_ONLY: no production report or source replacement', flush=True)
    return record


def main():
    root = Path('/opt/var/lib/iptv-home-probe/manual')
    root.mkdir(parents=True, exist_ok=True)
    # Separate lock and output from formal run.sh / report queue.
    with (root / 'scan.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('ALREADY_RUNNING', flush=True)
            return 1
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
            signal.signal(sig, stop)
        os.nice(15)
        config = probe.load_json(Path('/opt/etc/iptv-home-probe.json'))
        output = root / ('tv-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()) + '.json')
        print('OUTPUT: ' + str(output), flush=True)
        signal.alarm(1200)
        try:
            result = scan(config, output)
        finally:
            signal.alarm(0)
        return 0 if result['state'] == 'COMPLETE' and result['temporary_rule_cleaned'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
