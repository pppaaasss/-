"""Persist URL-specific evening failures independently of off-peak results."""
from datetime import datetime, timezone, timedelta


def in_peak(epoch):
    hour = datetime.fromtimestamp(epoch, timezone(timedelta(hours=8))).hour
    return 20 <= hour < 23


def apply_peak_policy(current, previous, *, run_kind, now_epoch, circuit_open=False):
    failures = dict(previous or {})
    output = []
    peak = run_kind == 'peak-2000' and in_peak(now_epoch)
    for value in current:
        row = dict(value)
        key = row['channel_key']
        old = failures.get(key)
        if peak and not circuit_open:
            if row['status'] == 'BAD' and row.get('failure_confirmed'):
                failures[key] = dict(row, peak_failed_epoch=now_epoch)
            elif row['status'] == 'GOOD' and old and old['url'] == row['url']:
                failures.pop(key, None)
        elif old and old['url'] == row['url']:
            # Explicitly label retained evidence; never call this a new failure.
            row = dict(old, peak_failure_retained=True,
                       latest_observed_status=value['status'])
        output.append(row)
    return output, failures
