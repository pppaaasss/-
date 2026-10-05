#!/usr/bin/env python3
"""Bounded GitHub playlist-text discovery; never fetch candidate media URLs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts import build_playlist, harvest_sources
from scripts.harvest_incremental import normalize_rows
from scripts.build_home_candidate_manifest import build_manifest, DEFAULT_FORMAL_URL
from scripts.home_test_campaign import eligible_rows, initial_host
from router.ac86u.home_contract import object_sha256
from router.ac86u.thin_contract import epoch, timestamp

LIMIT = 800
ZONE = ZoneInfo('Asia/Shanghai')
MAX_BYTES = 12 * 1024 * 1024


def target_day(now):
    local = datetime.fromtimestamp(now, ZONE)
    if local.hour >= 11:
        local += timedelta(days=1)
    return local.strftime('%Y%m%d')


def github_file(url):
    p = urlsplit(url)
    parts = p.path.lstrip('/').split('/', 3)
    if p.scheme != 'https' or p.hostname != 'raw.githubusercontent.com' or len(parts) != 4:
        raise ValueError('not_a_github_raw_file')
    owner, repo, ref, path = parts
    if not all(re.fullmatch(r'[\w.-]+', x) for x in (owner, repo, ref)) or p.query or p.fragment:
        raise ValueError('unsupported_github_reference')
    if not path.lower().endswith(('.m3u', '.m3u8', '.txt')) or 'readme' in path.lower():
        raise ValueError('not_a_playlist_file')
    path = unquote(path)
    if ref in ('.', '..') or any(x in ('', '.', '..') for x in path.split('/')) or any(c in path for c in '\\?#%\r\n'):
        raise ValueError('unsafe_github_file_path')
    return owner, repo, ref, path


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('source_redirect_rejected')


def read_url(url, api=False):
    p = urlsplit(url)
    expected = 'api.github.com' if api else 'raw.githubusercontent.com'
    if p.scheme != 'https' or p.netloc != expected or p.fragment:
        raise ValueError('untrusted_source_endpoint')
    headers = {'User-Agent': 'home-daily-text-intake', 'Accept': 'application/vnd.github+json' if api else 'text/plain'}
    if api and os.environ.get('GH_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['GH_TOKEN']
    with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=20) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('source_too_large')
    return raw


def verified_text(url, now, read=read_url):
    owner, repo, ref, path = github_file(url)
    base = f'https://api.github.com/repos/{owner}/{repo}'
    commits = json.loads(read(base + '/commits?' + urlencode({'sha': ref, 'path': path, 'per_page': 2}), True))
    if not isinstance(commits, list) or not commits:
        raise ValueError('no_file_history')
    latest = commits[0]
    age = now - epoch(latest['commit']['committer']['date'])
    if not 0 <= age <= 24 * 3600:
        raise ValueError('playlist_not_updated_within_24h')
    revision = latest['sha']
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('invalid_commit')
    raw_base = f'https://raw.githubusercontent.com/{owner}/{repo}/'
    suffix = '/' + quote(path, safe='/')
    data = read(raw_base + revision + suffix)
    # Compare the exact playlist with its previous path revision; mode-only or
    # unchanged-content commits are not evidence of a real playlist update.
    if len(commits) < 2:
        raise ValueError('insufficient_file_change_evidence')
    previous = commits[1]
    if not re.fullmatch('[0-9a-f]{40}', previous['sha']):
        raise ValueError('invalid_previous_commit')
    previous_data = read(raw_base + previous['sha'] + suffix)
    if data == previous_data:
        raise ValueError('playlist_content_unchanged')
    new_entries = normalize_rows(harvest_sources.parse_source(data.decode('utf-8-sig'), '大陆', url))
    old_entries = normalize_rows(harvest_sources.parse_source(previous_data.decode('utf-8-sig'), '大陆', url))
    if not new_entries or new_entries == old_entries:
        raise ValueError('playlist_entries_unchanged_or_empty')
    interval = epoch(latest['commit']['committer']['date']) - epoch(previous['commit']['committer']['date'])
    return data.decode('utf-8-sig'), dict(source=url, revision=revision,
        file_updated_utc=latest['commit']['committer']['date'], age_seconds=age,
        previous_file_revision=previous['sha'], content_sha256=hashlib.sha256(data).hexdigest(),
        previous_content_sha256=hashlib.sha256(previous_data).hexdigest(),
        observed_update_interval_seconds=interval,
        priority_refresh=age <= 8*3600 and 0 < interval <= 8*3600,
        evidence='path_commit_and_different_playlist_bytes')


def diverse_rows(rows, limit=LIMIT, priority_urls=()):
    pending = {r['candidate_id']: r for r in rows}
    hosts, channels = Counter(), Counter()
    host_of = {i:initial_host(r['url']) for i,r in pending.items()}
    priority_urls = set(priority_urls)
    out = []
    while pending and len(out) < limit:
        row = min(pending.values(), key=lambda r: (
            hosts[host_of[r['candidate_id']]], r['url'] not in priority_urls,
            channels[r['channel_key']], r['candidate_id']))
        out.append(row)
        hosts[host_of[row['candidate_id']]] += 1
        channels[row['channel_key']] += 1
        del pending[row['candidate_id']]
    return out


def build_daily(rows, state, formal, feedback, now, evidence):
    if state.get('migration_complete') is not True:
        raise ValueError('history_not_migrated')
    verified = {e['source'] for e in evidence}
    rows = [dict(r, sources=sorted(set(r['sources']) & verified)) for r in rows if set(r['sources']) & verified]
    accepted, excluded = {}, {}
    def select(normalized):
        nonlocal accepted, excluded
        # Existing pending IDs require today's evidence; measured/archived
        # history and every existing safety/conflict check remain authoritative.
        accepted, excluded = eligible_rows(normalized, dict(state, queue={}), formal, feedback)
        fast_sources = {e['source'] for e in evidence if e.get('priority_refresh')}
        fast_urls = {r['url'] for r in accepted.values() if set(r['sources']) & fast_sources}
        return diverse_rows(accepted.values(), priority_urls=fast_urls)
    manifest, summary = build_manifest(discovery_rows=rows, formal_bytes=formal,
        formal_url=DEFAULT_FORMAL_URL, source_revision='verified-github-file-content',
        generated_utc=timestamp(now), candidate_selector=select)
    selected = manifest['candidates']
    manifest.update(candidates=selected, candidate_count=len(selected), candidate_set_sha256=object_sha256(selected),
        daily_intake_day=target_day(now), daily_target=LIMIT,
        source_evidence_sha256=object_sha256(evidence))
    summary.update(eligible_count=len(accepted), selected_count=len(selected), target=LIMIT,
        shortfall=max(0, LIMIT-len(selected)), reason='ready' if len(selected)==LIMIT else 'insufficient_fresh_unmeasured_candidates',
        history_and_safety_exclusions=dict(Counter(excluded.values())))
    return manifest, summary


def collect_daily(sources, state, formal, feedback, now, read=None):
    from scripts.home_source_expansion import (ReadBudget, IntakeBudgetExceeded,
        discover_files, MAX_FILES, MAX_REQUESTS, MAX_SECONDS)
    budget = ReadBudget(read or read_url)
    raw_rows, evidence, failures, seen = [], [], [], set()
    audit = dict(attempted=False, queries=[], repositories_examined=0,
        rejected_repository_metadata=0, truncated_trees=0, errors=[],
        stop_reason='registry_sufficient', max_files=MAX_FILES,
        max_requests=MAX_REQUESTS, max_seconds=MAX_SECONDS)

    def collect(source, origin):
        url = source['url']
        if url in seen:
            return
        budget.check()
        seen.add(url)
        try:
            content, proof = verified_text(url, now, budget)
            parsed = harvest_sources.parse_source(content, source.get('group', '大陆'), url)
            raw_rows.extend(parsed)
            proof.update(entries=len(parsed), origin=origin)
            if origin == 'search':
                proof.update(discovery_repository=source['discovery_repository'],
                             discovery_query=source['discovery_query'])
            evidence.append(proof)
        except IntakeBudgetExceeded:
            raise
        except Exception as exc:
            failures.append(dict(source=url, origin=origin, error=type(exc).__name__,
                reason=str(exc)[:120] if isinstance(exc, ValueError) else 'source_fetch_or_api_failure'))

    def build():
        normalized = normalize_rows(raw_rows)
        manifest, summary = build_daily(normalized, state, formal, feedback, now, evidence)
        summary.update(parsed_rows=len(raw_rows), normalized_rows=len(normalized),
                       normalization_merged_or_invalid=len(raw_rows)-len(normalized))
        return manifest, summary

    try:
        for source in sources[:MAX_FILES]:
            collect(source, 'registry')
        manifest, summary = build()
        audit['registry_selected'] = manifest['candidate_count']
        if manifest['candidate_count'] < LIMIT:
            audit.update(stop_reason='bounded_search_results_exhausted')
            if len(seen) >= MAX_FILES:
                audit['stop_reason'] = 'file_budget_exhausted'
            else:
                audit['attempted'] = True
                for source in discover_files(now, budget, seen, audit):
                    collect(source, 'search')
                    manifest, summary = build()
                    if manifest['candidate_count'] >= LIMIT:
                        audit['stop_reason'] = 'target_reached'
                        break
                    if len(seen) >= MAX_FILES:
                        audit['stop_reason'] = 'file_budget_exhausted'
                        break
    except IntakeBudgetExceeded as exc:
        audit['stop_reason'] = str(exc)
    except Exception as exc:
        # A failed search never claims that all GitHub supply is exhausted.
        audit['stop_reason'] = 'search_failed'
        audit['errors'].append(dict(reason=type(exc).__name__))
    if audit['stop_reason'] == 'bounded_search_results_exhausted':
        if audit['errors'] or any(f.get('origin') == 'search' and f['reason'] == 'source_fetch_or_api_failure' for f in failures):
            audit['stop_reason'] = 'search_read_failures'
        elif (audit.get('search_incomplete') or audit['truncated_trees']
              or audit['rejected_repository_metadata'] or audit.get('repository_scan_limited')):
            audit['stop_reason'] = 'search_incomplete'
    manifest, summary = build()
    audit['expansion_net_selected_delta'] = manifest['candidate_count'] - audit.get('registry_selected', manifest['candidate_count'])
    audit.update(requests_used=budget.requests, files_attempted=len(seen),
                 selected_after_expansion=manifest['candidate_count'],
                 search_files_attempted=sum(x.get('origin')=='search' for x in evidence+failures))
    return manifest, summary, evidence, failures, audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--root', default='.')
    args = parser.parse_args()
    root = Path(args.root)
    state = json.loads(Path(args.state).read_bytes())  # Missing history fails closed.
    config = json.loads((root/'config/home-thin.json').read_bytes())
    if state.get('probe_id') != config['probe_id']:
        raise ValueError('wrong_history_probe')
    now = time.time()
    registry = root/'config/home-daily-sources.json'
    sources = json.loads(registry.read_bytes())['sources'] if registry.exists() else [
        {'url': url, 'group': group} for group, url, _ in build_playlist.SOURCES
        if url.startswith('https://raw.githubusercontent.com/')]
    sources = list({r['url']: r for r in sources}.values())[:48]
    manifest, summary, evidence, failures, expansion = collect_daily(
        sources, state, (root/'tv-core.m3u').read_bytes(),
        root/'config/home-route-feedback.json', now)
    report = dict(generated_utc=timestamp(now), day=manifest['daily_intake_day'], summary=summary,
        sources=evidence, failures=failures, expansion=expansion, stream_probe_performed=False, production_modified=False,
        policy={'home_probe_is_the_only_health_authority': True, 'hong_kong_evidence_consulted': False,
                'hong_kong_tombstones_consulted': False})
    for name, value in [('home-candidates.json', manifest), ('home-discovery-manifest.json', report)]:
        (root/'harvest'/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(summary, ensure_ascii=False))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as f:
            f.write(f"Daily target 800; selected {len(manifest['candidates'])}; shortfall {summary['shortfall']}. "
                    "Static admission only; household measurement determines qualification.\n")



def admitted_daily_ids(manifest, state, formal, feedback, now):
    day = datetime.fromtimestamp(now, ZONE).strftime('%Y%m%d')
    if manifest.get('daily_intake_day') != day or manifest.get('daily_target') != LIMIT:
        return []
    if not 0 <= now - epoch(manifest['generated_utc']) <= 24*3600:
        return []
    rows = manifest['candidates']
    if len(rows) > LIMIT or not re.fullmatch('[0-9a-f]{64}', manifest.get('source_evidence_sha256', '')):
        return []
    # Recheck latest user vetoes and *all* measured trial/normal history.
    accepted, _ = eligible_rows(rows, dict(state, queue={}), formal, feedback)
    return list(accepted)


def order_daily_candidates(state, identities, limit):
    from scripts.home_test_campaign import choose_candidates
    rows = {i:state['queue'][i] for i in identities if i in state['queue'] and i not in state['tested']}
    history = dict(state.get('temporary_campaigns', {}))
    history['_normal'] = {'results': state['tested']}
    return choose_candidates(rows, {'temporary_campaigns': history}, limit)


def daily_progress(state, task, now):
    day = datetime.fromtimestamp(now, ZONE).strftime('%Y%m%d')
    measured = [v for v in state['tested'].values() if v.get('last_checked_utc') and
                datetime.fromtimestamp(epoch(v['last_checked_utc']), ZONE).strftime('%Y%m%d') == day]
    batches = [b for b in state['batches'].values() if b.get('created_utc') and
               datetime.fromtimestamp(epoch(b['created_utc']), ZONE).strftime('%Y%m%d') == day]
    candidate_batches = [b for b in batches if not b.get('temporary_campaign_id') and any(r['role']=='candidate' for r in b['tasks'])]
    settled = [b for b in candidate_batches if b['batch_id'] in state['processed']]
    # Observed dispatch spacing includes workflow, household and upload overhead.
    starts = sorted(epoch(b['created_utc']) for b in candidate_batches)
    spacing = (starts[-1]-starts[0])/(len(starts)-1) if len(starts)>1 else None
    local = datetime.fromtimestamp(now, ZONE)
    remaining_s = max(0, (local.replace(hour=11, minute=0, second=0, microsecond=0)-local).total_seconds()) if local.hour>=2 else 9*3600
    transition_measured = sum(1 for c in state.get('temporary_campaigns',{}).values()
        for v in c.get('results',{}).values() if v.get('observed_utc') and
        datetime.fromtimestamp(epoch(v['observed_utc']), ZONE).strftime('%Y%m%d') == day)
    reserved = state.get('native_budget',{}).get('candidates',0) if state.get('native_budget',{}).get('day') == day else 0
    formal_batches = sum(all(r['role']!='candidate' for r in b['tasks']) for b in batches)
    capacity = min(max(0,LIMIT-reserved),int(remaining_s/spacing*4)) if spacing and spacing>0 else None
    from router.ac86u.home_decision import candidate_is_qualified
    return dict(day=day, target=LIMIT, measured=len(measured), shortfall=max(0,LIMIT-len(measured)),
        qualified=sum(candidate_is_qualified(v.get('result',{})) for v in measured),
        available_unmeasured=state.get('daily_intake',{}).get('available',0),
        transition_temporary_measured=transition_measured, reserved_candidate_count=reserved,
        candidate_batches=len(candidate_batches), settled_candidate_batches=len(settled),
        formal_or_backup_batches=formal_batches, observed_batch_spacing_seconds=spacing,
        estimated_additional_window_capacity=capacity, estimate_is_guarantee=False,
        reason=state.get('daily_stop_reason') or task.get('state') or 'WAITING_HOUSEHOLD_OBSERVATIONS',
        intake_reason=state.get('daily_intake',{}).get('reason','awaiting_fresh_intake'))


if __name__ == '__main__':
    main()
