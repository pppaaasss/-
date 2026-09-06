"""Bounded CPU and memory sampling shared by router scans."""
from pathlib import Path
import time


def cpu_ticks():
    fields = Path('/proc/stat').read_text().splitlines()[0].split()
    if fields[0] != 'cpu' or len(fields) < 9:
        raise ValueError('invalid_cpu_counters')
    # guest/guest_nice are already included in user/nice.
    ticks = [int(v) for v in fields[1:9]]
    if min(ticks) < 0:
        raise ValueError('negative_cpu_counter')
    return ticks


def sample_resources(config, resources_reader):
    """Use a one-second CPU delta; load average remains diagnostic only."""
    resources = {}
    try:
        before = cpu_ticks()
        time.sleep(1)
        after = cpu_ticks()
        delta = [b - a for a, b in zip(before, after)]
        if any(v < 0 for i, v in enumerate(delta) if i != 4):
            raise ValueError('cpu_counter_regressed')
        # Linux documents that iowait can decrease; keep that uncertainty visible.
        resources['iowait_counter_regressed'] = delta[4] < 0
        delta[4] = max(0, delta[4])
        total = sum(delta)
        if total <= 0:
            raise ValueError('no_cpu_sample')
        busy = 100 * (total - delta[3] - delta[4]) / total
        wait = 100 * delta[4] / total
        resources.update(resources_reader())
        resources.update(cpu_busy_percent=round(busy, 1), iowait_percent=round(wait, 1),
                         sample_seconds=1)
        available = int(resources.get('mem_available_kib') or 0)
        minimum = max(40 * 1024, int(config.get('minimum_mem_available_kib') or 0))
        if available <= 0:
            reason = 'memory_sample_unavailable'
        elif available < minimum:
            reason = 'memory_below_' + str(minimum)
        elif busy >= 85:
            reason = 'sampled_cpu_busy_at_least_85_percent'
        elif wait >= 20:
            reason = 'sampled_iowait_at_least_20_percent'
        else:
            reason = ''
    except (OSError, ValueError, IndexError, TypeError) as exc:
        reason = 'resource_sample_unavailable_' + type(exc).__name__
    return reason, resources

