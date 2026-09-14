"""Native wire format and offline media analysis. Runs only on GitHub/phone.

Media bytes travel in temporary draft-release assets, never in Git history.
Retained evidence includes measurements, media hashes and parser provenance.
"""
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile

from router.ac86u.thin_contract import (RESULT_SCHEMA, ROUTE_CONTEXT, timestamp,
    epoch, validate_observations)
from router.ac86u.home_probe import empty_result, parse_rate

MAX_NATIVE = 768 * 1024
PREFIX_BYTES = 128 * 1024
WIRE = 'IPTV_NATIVE_V1'


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def task_wire(task, release_id):
    if task.get('schema') != 'iptv-home-task/v1':
        return ('IDLE\t' + task['state'] + '\n').encode()
    if not isinstance(release_id, int) or release_id < 1:
        raise ValueError('native sample release is not ready')
    lines = ['\t'.join(map(str, [WIRE, task['probe_id'], task['batch_id'], task['cycle_id'],
        int(epoch(task['created_utc'])), int(epoch(task['expires_utc'])),
        task['limits']['seconds'], release_id]))]
    for row in task['tasks']:
        lines.append('\t'.join(['T', row['task_id'], b64(row['url'].encode()), str(row['sample_bytes'])]))
    raw = ('\n'.join(lines) + '\n').encode()
    return raw + ('SHA256\t' + hashlib.sha256(raw).hexdigest() + '\n').encode()


def bounded(value, lo, hi):
    n = float(value)
    if not math.isfinite(n) or not lo <= n <= hi:
        raise ValueError('native number outside bounds')
    return n


def decode(value, maximum):
    if value == '-':
        return b''
    if len(value) > (maximum + 2) // 3 * 4:
        raise ValueError('native field too large')
    raw = base64.b64decode(value, validate=True)
    if len(raw) > maximum:
        raise ValueError('native field too large')
    return raw


def metric(encoded, limit):
    if encoded == '-':
        return None
    fields = decode(encoded, 8192).decode().strip().split('\t')
    if len(fields) != 8:
        raise ValueError('invalid native metric')
    code, status, size, elapsed, total, complete, url, duration = fields
    count = int(bounded(size, 0, limit))
    elapsed = bounded(elapsed, .001, 30)
    total = int(bounded(total, 0, 2**40))
    duration = bounded(duration, 0, 3600)
    if complete not in ('0', '1') or not url.startswith(('http://', 'https://')):
        raise ValueError('invalid native transfer')
    return {'curl_code': int(bounded(code, 0, 1000)), 'http_status': int(bounded(status, 0, 599)),
        'downloaded_bytes': count, 'elapsed_s': elapsed, 'total_bytes': total,
        'complete': complete == '1', 'url': url, 'duration_s': duration,
        'download_mbps': count * 8 / elapsed / 1e6,
        'stream_mbps': total * 8 / duration / 1e6 if total and duration else 0}


def media_metadata(raw):
    if not raw:
        return {}
    with tempfile.TemporaryDirectory(prefix='home-native-') as folder:
        path = Path(folder) / 'household-sample.bin'
        path.write_bytes(raw)
        # Local captured bytes only: never make cloud-origin media requests.
        command = ['ffprobe', '-v', 'error', '-threads', '1',
            '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mpegts,flv',
            '-probesize', str(PREFIX_BYTES), '-analyzeduration', '1000000',
            '-select_streams', 'v:0', '-show_entries',
            'stream=width,height,codec_name,avg_frame_rate,r_frame_rate,bit_rate:format=bit_rate',
            '-of', 'json', str(path)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=8, check=True)
            if len(result.stdout) > 32768:
                return {}
            parsed = json.loads(result.stdout)
            video = parsed['streams'][0]
            fps = next((r for r in (parse_rate(video.get('avg_frame_rate')), parse_rate(video.get('r_frame_rate'))) if 0 < r <= 240), 0)
            if not fps or not 0 < int(video.get('width', 0)) <= 16384 or not 0 < int(video.get('height', 0)) <= 8640:
                return {}
            # Only intrinsic stream metadata is a bitrate fallback; truncated
            # local-file format estimates are not the live stream's bitrate.
            bitrate = float(video.get('bit_rate', 0) or 0) / 1e6
            return {'width': int(video['width']), 'height': int(video['height']),
                'codec': video['codec_name'], 'fps': fps,
                'bitrate_mbps': bitrate if math.isfinite(bitrate) and bitrate > 0 else 0,
                'deep_checked': True}
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError):
            return {}


def read_native(raw, task, now):
    if len(raw) > MAX_NATIVE or not raw.endswith(b'\n'):
        raise ValueError('native envelope truncated or oversized')
    lines = raw.decode('ascii').splitlines()
    header = lines[0].split('\t')
    if len(header) != 5 or header[:4] != [WIRE, task['probe_id'], task['batch_id'], task['cycle_id']]:
        raise ValueError('native identity mismatch')
    started = bounded(header[4], epoch(task['created_utc']), epoch(task['expires_utc']))
    tail = lines[-1].split('\t')
    if len(tail) != 3 or tail[0] != 'END' or tail[2] not in ('completed', 'interrupted_batch', 'window_closed'):
        raise ValueError('native completion missing')
    finished = bounded(tail[1], started, min(now + 60, epoch(task['expires_utc'])))
    requested = {r['task_id']: r for r in task['tasks']}
    observations, hashes, total_bytes = [], {}, 0
    for line in lines[1:-1]:
        f = line.split('\t')
        if len(f) != 11 or f[0] != 'R' or f[1] not in requested or f[1] in hashes:
            raise ValueError('invalid native route record')
        row = requested[f[1]]
        url = decode(f[2], 2048).decode('utf-8')
        if url != row['url']:
            raise ValueError('native measured a different URL')
        begin = bounded(f[3], started, finished)
        end = bounded(f[4], begin, finished)
        manifest = metric(f[6], 98304)
        samples = [m for m in (metric(f[9], row['sample_bytes']), metric(f[10], row['sample_bytes'])) if m]
        extra_bytes = int(bounded(f[5], 0, 4 * 98304))
        total_bytes += sum(m['downloaded_bytes'] for m in samples) + extra_bytes
        result = empty_result(row['name'], url, row['min_height'])
        result['channel_key'] = row['channel_key']
        result['segment_samples'] = samples
        result['sample_count'] = sum(m['curl_code'] == 0 and 200 <= m['http_status'] < 300 and m['downloaded_bytes'] >= 65536 for m in samples)
        result['probe_runtime_s'] = end - begin
        code = f[7]
        if code not in ('measured', 'unsupported', 'transfer_error'):
            raise ValueError('invalid native route outcome')
        transfers = ([manifest] if manifest else []) + samples
        failed = [m for m in transfers if m['curl_code'] or not 200 <= m['http_status'] < 300]
        if failed:
            explicit = any(m['curl_code'] in (7, 28) or m['http_status'] in (404, 410, 500, 502, 503, 504) for m in failed)
            result.update(observed_status='UNAVAILABLE' if explicit else 'UNKNOWN', error='native_transfer_error')
        elif code != 'measured' or result['sample_count'] != 2:
            result.update(observed_status='UNKNOWN', error='native_format_or_sample_unknown')
        else:
            result['startup_s'] = (manifest['elapsed_s'] if manifest else 0) + samples[0]['elapsed_s']
            result['min_download_mbps'] = min(m['download_mbps'] for m in samples)
            result['avg_download_mbps'] = sum(m['download_mbps'] for m in samples) / 2
            streams = [m['stream_mbps'] for m in samples if m['stream_mbps'] > 0]
            result['stream_mbps'] = sum(streams) / len(streams) if streams else 0
            result['observed_status'] = 'GOOD'
        media = decode(f[8], PREFIX_BYTES)
        if len(media) > (samples[0]['downloaded_bytes'] if samples else 0):
            raise ValueError('media bytes lack a household sample')
        if result['sample_count'] == 2 and result['observed_status'] == 'GOOD':
            result.update(media_metadata(media))
        hashes[f[1]] = {'sha256': hashlib.sha256(media).hexdigest(), 'bytes': len(media)}
        observations.append({'task_id': f[1], 'started_utc': timestamp(begin),
            'finished_utc': timestamp(end), 'result': result})
    value = {'schema': RESULT_SCHEMA, 'probe_id': task['probe_id'], 'batch_id': task['batch_id'],
        'cycle_id': task['cycle_id'], 'route_context': ROUTE_CONTEXT,
        'started_utc': timestamp(started), 'finished_utc': timestamp(finished), 'results': observations,
        'usage': {'downloaded_bytes': total_bytes, 'runtime_s': finished-started},
        'stop_reason': '' if tail[2] == 'completed' else tail[2],
        'native_evidence': {'wire_sha256': hashlib.sha256(raw).hexdigest(), 'media': hashes,
            'metadata_origin': 'offline_ffprobe_of_household_bytes', 'cloud_media_requests': False}}
    return validate_observations(value, task, now)


def prepare_assets(state, inbox, reports, now):
    """Convert known assets before the existing immutable observation ingestion."""
    done = []
    for path in sorted(Path(inbox).glob('*.native')):
        match = re.fullmatch(r'([0-9a-f]{64})-([0-9a-f]{64})\.native', path.name)
        if not match or match[1] not in state['batches']:
            continue
        if path.is_symlink() or path.stat().st_size > MAX_NATIVE:
            raise ValueError('unsafe native asset')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != match[2]:
            raise ValueError('native asset hash mismatch')
        target = Path(reports) / 'observations' / state['probe_id'] / (match[1]+'.json')
        if target.exists():
            prior = json.loads(target.read_bytes())
            if prior.get('native_evidence', {}).get('wire_sha256') != match[2]:
                raise ValueError('native observation already has different evidence')
        else:
            value = read_native(raw, state['batches'][match[1]], now)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value, sort_keys=True, separators=(',', ':'))+'\n')
        done.append(path.name)
    return done
