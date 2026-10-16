"""Select historical routes for the shared daily 500-route measurement budget."""
from collections import Counter

from router.ac86u.home_contract import make_candidate
from scripts.home_test_campaign import safe_url, initial_host


def historical_candidates(state, current, feedback, now, limit):
    from scripts.home_thin_control import allowed, datetime_day
    if limit <= 0:
        return []
    day = datetime_day(now)
    pool, owners = {}, {}
    for record in state.get('tested', {}).values():
        raw = record.get('candidate') or record.get('result') or {}
        if raw.get('url') and raw.get('channel_key'):
            owners.setdefault(raw['url'], set()).add(raw['channel_key'])
            pool[(raw['channel_key'], raw['url'])] = dict(raw,
                last_checked_utc=record.get('last_checked_utc', ''))
    for row in state.get('archive', {}).values():
        owners.setdefault(row['url'], set()).add(row['channel_key'])
        key = (row['channel_key'], row['url'])
        pool[key] = {**row, 'last_checked_utc': max(
            pool.get(key, {}).get('last_checked_utc', ''), row.get('last_verified_utc', ''))}
    check = allowed(state, current, feedback)
    measured_today = {row['result']['url'] for row in state.get('tested', {}).values()
        if row.get('last_checked_utc') and datetime_day_from_text(row['last_checked_utc']) == day}
    # Include measurements/reservations from every cycle today, so a manual
    # route/feedback change cannot cause a duplicate historical retry.
    reserved = {t['url'] for b in state.get('batches', {}).values()
        if b.get('created_utc') and datetime_day_from_text(b['created_utc']) == day
        for t in b['tasks']}
    candidates = []
    for row in pool.values():
        if (row['url'] in measured_today | reserved or now < row.get('retry_after_epoch', 0)
                or len(owners[row['url']]) != 1
                or row.get('request_options') or not safe_url(row['url']) or not check(row)):
            continue
        candidate = make_candidate(dict(name=row.get('name') or row['channel_key'],
            channel_key=row['channel_key'], url=row['url'], sources=['historical-household-record']))
        candidates.append((row.get('last_checked_utc', ''), candidate))
    # Oldest observations first, with host rotation inside each dispatch batch.
    candidates.sort(key=lambda item: (item[0], item[1]['candidate_id']))
    hosts, selected = Counter(), []
    while candidates and len(selected) < limit:
        index = min(range(len(candidates)), key=lambda i: (
            hosts[initial_host(candidates[i][1]['url'])], i))
        _, row = candidates.pop(index)
        selected.append(row)
        hosts[initial_host(row['url'])] += 1
    return selected


def datetime_day_from_text(value):
    from router.ac86u.thin_contract import epoch
    from scripts.home_thin_control import datetime_day
    return datetime_day(epoch(value))
