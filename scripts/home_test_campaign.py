"""Expiring household experiment using the production lease and budget ledger.

No results from this module are production reports or qualified backups. Keep
the manifest after expiry: removing the publisher freeze requires separate user
confirmation. This module does not enable or upgrade the runtime network guard.
"""
import copy
import hashlib
import ipaddress
import json
import re
from collections import Counter, defaultdict
from urllib.parse import parse_qsl, urlsplit, unquote

from router.ac86u.home_contract import make_candidate, candidate_id, SATELLITES
from router.ac86u.home_decision import candidate_result
from router.ac86u.thin_contract import (digest, epoch, timestamp, slot, KINDS,
    TASK_SCHEMA, ROUTE_CONTEXT, MAX_ENVELOPE, seal_task, validate_task,
    validate_observations)
from scripts.publish_home_decisions import unique_core_routes, sha256_bytes

SCHEMA = 'iptv-home-test-campaign/v1'
MANIFEST_PATH = 'config/home-test-campaign.json'
RESULT_SCHEMA = 'iptv-home-temporary-results/v1'
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_INPUTS = 50000
TOKEN_KEYS = re.compile(r'token|sign|auth|credential|expire|timestamp|secret|session|key', re.I)


def safe_url(value):
    """Text admission only; the separately verified runtime guards DNS/redirects."""
    try:
        if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\s\\\x00-\x1f]', value):
            return False
        p = urlsplit(value)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme not in ('http', 'https') or not host or p.username is not None or p.password is not None or p.fragment:
            return False
        if p.port is not None and not 1 <= p.port <= 65535:
            return False
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global or (getattr(address, 'ipv4_mapped', None) and not address.ipv4_mapped.is_global):
                return False
        except ValueError:
            if '.' not in host or host.endswith(('.localhost','.local','.internal','.home','.lan','.test','.invalid','.example')):
                return False
            if re.fullmatch(r'[0-9.]+', host) or host.startswith('0x'):
                return False
            if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in host.split('.')):
                return False
        if any(TOKEN_KEYS.search(k) for k, _ in parse_qsl(p.query, keep_blank_values=True)):
            return False
        return not re.search(r'(?i)(?:token|signature|auth|expires)[=/]', unquote(p.path))
    except (TypeError, ValueError):
        return False


def eligible_rows(rows, state, formal, feedback):
    """Recheck history, vetoes and cross-channel identities without altering them."""
    from scripts.home_thin_control import allowed
    current = unique_core_routes(formal)
    owners = defaultdict(set)
    history_urls = set()
    known = set(state['tested']) | set(state['queue']) | set(state['archive'])
    for key, route in current.items():
        owners[route.url].add(key)
        history_urls.add(route.url)
    known_rows = list(state['queue'].values()) + list(state['archive'].values())
    for record in state['tested'].values():
        known_rows += [record.get('candidate') or {}, record.get('result') or {}]
    # Renaming an experiment is not permission to retry its measured URLs.
    # Pending inputs and excluded-but-unmeasured rows remain distinct from history.
    for campaign in state.get('temporary_campaigns',{}).values():
        for identity, record in campaign.get('results',{}).items():
            known.add(identity)
            known_rows += [campaign.get('candidates',{}).get(identity,{}) or {},
                           record.get('result') or {}]
    for row in known_rows:
        if row.get('url'):
            history_urls.add(row['url'])
            if row.get('channel_key'):
                owners[row['url']].add(row['channel_key'])
    normalized = {}
    for raw in rows:
        row = make_candidate(raw)
        if raw.get('candidate_id', row['candidate_id']) != row['candidate_id']:
            raise ValueError('campaign candidate identity mismatch')
        normalized[row['candidate_id']] = row
        owners[row['url']].add(row['channel_key'])
    merged = dict(state, queue={**state['queue'], **normalized})
    check = allowed(merged, current, feedback)
    keys = set(current) | set(SATELLITES) | {'cctv'+str(n) for n in range(1,18)} | {'cctv4k','cctv5plus'}
    accepted, excluded = {}, {}
    for identity, row in normalized.items():
        reason = None
        if not safe_url(row['url']) or row['request_options']:
            reason = 'unsafe_address_or_request_options'
        elif row['url'] in history_urls or any(candidate_id(k,row['url'],row['request_options']) in known for k in keys):
            reason = 'formal_queue_archive_or_tested_history'
        elif len(owners[row['url']]) > 1:
            reason = 'conflicting_channel_identity'
        elif not check(row):
            reason = 'scope_or_manual_veto'
        if reason:
            excluded[identity] = reason
        else:
            accepted[identity] = row
    return accepted, excluded


def register_campaign(state, root, now):
    path = root / MANIFEST_PATH
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('unsafe campaign manifest')
    manifest = json.loads(path.read_bytes())
    if manifest.get('schema') != SCHEMA or manifest.get('probe_id') != state['probe_id']:
        raise ValueError('wrong campaign schema or probe')
    identity = manifest.get('campaign_id', '')
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}',identity):
        raise ValueError('invalid campaign ID')
    created, start, end = (epoch(manifest[k]) for k in ('created_utc','not_before_utc','expires_utc'))
    if not created <= start < end or end-created > 30*86400 or created > now:
        raise ValueError('invalid campaign interval')
    rows = manifest.get('candidates')
    if not isinstance(rows,list) or not 1 <= len(rows) <= MAX_INPUTS:
        raise ValueError('invalid campaign candidate pool')
    binding = digest(manifest)
    campaigns = state.setdefault('temporary_campaigns', {})
    saved = campaigns.get(identity)
    if saved and saved['manifest_sha256'] != binding:
        raise ValueError('campaign ID cannot change pool, guard, or expiry')
    if manifest.get('enabled', True) is not True or manifest.get('runtime_public_guard_verified') is not True:
        return None
    formal = (root/'tv-core.m3u').read_bytes()
    feedback = root/'config/home-route-feedback.json'
    if manifest.get('formal_sha256') != sha256_bytes(formal) or manifest.get('feedback_sha256') != sha256_bytes(feedback.read_bytes()):
        raise ValueError('campaign formal or feedback binding changed')
    if not saved:
        accepted, excluded = eligible_rows(rows,state,formal,feedback)
        saved = dict(schema=RESULT_SCHEMA, campaign_id=identity, probe_id=state['probe_id'],
            manifest_sha256=binding, created_utc=manifest['created_utc'], not_before_utc=manifest['not_before_utc'],
            expires_utc=manifest['expires_utc'], formal_sha256=manifest['formal_sha256'],
            feedback_sha256=manifest['feedback_sha256'], registered_utc=timestamp(now),
            candidate_count=len(accepted), input_count=len(rows), candidates=accepted,
            excluded=excluded, results={}, batch_ids=[], collected_batches=[],
            cycle_id=digest(['temporary-home-test',identity,binding]),
            production_eligible=False, actionable=False, status='REGISTERED')
        campaigns[identity] = saved
    if now >= end:
        saved['status'] = 'EXPIRED'
        saved.pop('outstanding',None)
        return None
    if now < start:
        saved['status'] = 'WAITING_START'
        return None
    return saved


def collect_results(state, reports, now):
    """Ingest already settled observations into the isolated result tree only."""
    from scripts.home_thin_control import normalise
    for campaign in state.get('temporary_campaigns',{}).values():
        for batch_id in campaign['batch_ids']:
            if batch_id in campaign['collected_batches'] or batch_id not in state['processed']:
                continue
            task = state['batches'][batch_id]
            if task.get('temporary_campaign_id') != campaign['campaign_id'] or task['cycle_id'] != campaign['cycle_id']:
                raise ValueError('temporary task ownership mismatch')
            path = reports/'observations'/state['probe_id']/(batch_id+'.json')
            if path.is_symlink() or path.stat().st_size > MAX_ENVELOPE:
                raise ValueError('unsafe temporary observation')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != state['processed'][batch_id]:
                raise ValueError('temporary observation changed after settlement')
            value = validate_observations(json.loads(raw),task,now)
            tasks = {r['task_id']:r for r in task['tasks']}
            for observation in value['results']:
                row = tasks[observation['task_id']]
                result = normalise(observation['result'],row)
                outcome = candidate_result(row,result,purpose='daily-qualification',switch_reverified=False)
                campaign['results'].setdefault(row['candidate_id'],dict(candidate_id=row['candidate_id'],
                    batch_id=batch_id, task_id=row['task_id'], observed_utc=observation['finished_utc'],
                    qualification=outcome['qualification'], result=result,
                    production_eligible=False, actionable=False))
            campaign['collected_batches'].append(batch_id)
            if campaign.get('outstanding') == batch_id:
                campaign.pop('outstanding')
            campaign['last_stop_reason'] = value.get('stop_reason','')
            if not value['results'] and value.get('stop_reason'):
                campaign['retry_after_epoch'] = now+15*60
        campaign['measured_count'] = len(campaign['results'])
        campaign['outcomes'] = dict(Counter(r['qualification'] for r in campaign['results'].values()))


def normal_slot_complete(state, root, now):
    active = slot(now)
    cycle = state.get('cycle') or {}
    return bool(active and active[0] == KINDS[0]
        and active[1]+'-'+active[0] in state['completed_slots']
        and cycle.get('slot') == active[1]+'-'+active[0]
        and cycle.get('binding',{}).get('sha256') == sha256_bytes((root/'tv-core.m3u').read_bytes())
        and cycle.get('feedback_sha') == sha256_bytes((root/'config/home-route-feedback.json').read_bytes()))


def dispatch(state, campaign, config, root, now):
    from scripts.home_thin_control import task_row
    active = slot(now)
    if not active or active[0] != KINDS[0] or not normal_slot_complete(state,root,now):
        return None
    if not (state.get('last_report') or {}).get('baseline',{}).get('home_network_ok'):
        campaign['status'] = 'WAITING_HEALTHY_BASELINE'
        return None
    if now < campaign.get('retry_after_epoch',0):
        campaign['status'] = 'WAITING_HOME_RESOURCES'
        return None
    deadline = min(active[2],epoch(campaign['expires_utc']))
    if deadline-now < 60:
        campaign['status'] = 'WINDOW_ENDING'
        return None
    pending = [r for i,r in campaign['candidates'].items() if i not in campaign['results'] and i not in campaign['excluded']]
    accepted, excluded = eligible_rows(pending,state,(root/'tv-core.m3u').read_bytes(),root/'config/home-route-feedback.json')
    campaign['excluded'].update(excluded)
    if not accepted:
        campaign['status'] = 'COMPLETE'
        return None
    # Fair channel order over the whole campaign, without an arbitrary nightly cap.
    tally = Counter(campaign['candidates'][i]['channel_key'] for i in campaign['results'])
    chosen=[]
    for _ in range(min(4,config['routes_per_batch'],len(accepted))):
        row = min(accepted.values(),key=lambda r:(tally[r['channel_key']],r['channel_key'],r['candidate_id']))
        chosen.append(row); tally[row['channel_key']]+=1; del accepted[row['candidate_id']]
    amount, seconds = min(config['batch_bytes'],64*1024*1024), min(config['batch_seconds'],240)
    budget = state.get('native_budget',{})
    if config.get('router_runtime') != 'native-v1' or budget.get('day') != active[1]:
        campaign['status'] = 'WAITING_SHARED_NATIVE_BUDGET'
        return None
    channels = len(unique_core_routes((root/'tv-core.m3u').read_bytes()))
    future = 4*((channels+config['routes_per_batch']-1)//config['routes_per_batch'])
    if (budget['bytes']+(future+1)*amount > min(config['daily_bytes'],8*1024**3)
            or budget['seconds']+(future+1)*seconds > config['daily_seconds']
            or budget['discovery_bytes']+amount > min(config['discovery_bytes'],6*1024**3)
            or budget['discovery_seconds']+seconds > config['discovery_seconds']
            or budget['candidates']+len(chosen) > config['daily_candidates']):
        campaign['status'] = 'WAITING_SHARED_BUDGET_WITH_FORMAL_RESERVE'
        return None
    temp_cycle = dict(id=campaign['cycle_id'],quality_policy=state.get('quality_policy',{}))
    task = seal_task(dict(schema=TASK_SCHEMA,probe_id=state['probe_id'],cycle_id=campaign['cycle_id'],
        temporary_campaign_id=campaign['campaign_id'],candidate_mode='daily',route_context=ROUTE_CONTEXT,
        run_kind=KINDS[0],created_utc=timestamp(now),expires_utc=timestamp(min(deadline,now+600)),
        formal_playlist=copy.deepcopy(state['cycle']['binding']),feedback_sha256=campaign['feedback_sha256'],
        tasks=[task_row(temp_cycle,r['channel_key'],r['url'],'candidate') for r in chosen],
        limits=dict(seconds=seconds,bytes=amount)))
    validate_task(task,state['probe_id'],now)
    budget['bytes']+=amount; budget['seconds']+=seconds; budget['candidates']+=len(chosen)
    budget['discovery_bytes']+=amount; budget['discovery_seconds']+=seconds
    budget['reservations'][task['batch_id']]=dict(bytes=amount,seconds=seconds,candidates=len(chosen))
    state['batches'][task['batch_id']]=task
    campaign['batch_ids'].append(task['batch_id']); campaign['outstanding']=task['batch_id']
    campaign['status']='WAITING_HOUSEHOLD_OBSERVATIONS'
    return task


def campaign_step(state, config, root, reports, now, production_step):
    from scripts.home_thin_control import ingest
    # No alternate task writer: this is the sole production dispatcher wrapper.
    state=copy.deepcopy(state)
    if config.get('enabled') is not True or state.get('migration_complete') is not True:
        return production_step(state,config,root,reports,now)
    ingest(state,reports,now)
    collect_results(state,reports,now)
    campaign=None
    try:
        campaign=register_campaign(state,root,now)
        state.pop('temporary_campaign_error',None)
    except (ValueError,KeyError,TypeError) as exc:
        state['temporary_campaign_error']=str(exc)
    if campaign and normal_slot_complete(state,root,now):
        outstanding=state['batches'].get(campaign.get('outstanding'))
        if outstanding and now < epoch(outstanding['expires_utc']):
            return state,outstanding,None
        campaign.pop('outstanding',None)
    live_config=dict(config,_skip_regular_candidates=True) if campaign else config
    state,task,report=production_step(state,live_config,root,reports,now)
    # production_step copies state; never mutate the pre-copy campaign reference.
    if campaign and task.get('state')=='SLOT_COMPLETE':
        campaign=state['temporary_campaigns'][campaign['campaign_id']]
        issued=dispatch(state,campaign,config,root,now)
        if issued:
            return state,issued,report
    return state,task,report
