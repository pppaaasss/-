#!/opt/bin/python3
"""Print only non-secret installation and pairing facts for household setup."""
import json
from pathlib import Path


def main():
    config = json.loads(Path('/opt/etc/iptv-home-probe.json').read_text())
    for key in ('probe_id', 'daily_worker_enabled', 'route_context', 'runtime_transport',
                'lan_dns_server', 'actionable', 'github_push_enabled', 'protected_publishing_ready',
                'minimum_mem_available_kib', 'minimum_h264_stream_mbps', 'minimum_headroom_ratio'):
        print(key + ': ' + str(config.get(key)), flush=True)
    root = Path(config.get('output_dir') or '/opt/var/lib/iptv-home-probe')
    for name in ('daily-status.json', 'daily-runtime-error.json'):
        if (root / name).exists():
            print(name + ': ' + (root / name).read_text()[:2000])
    print('trial_results_preserved: ' + str((root / 'pipeline-trial/state.json').exists()))
    public = Path('/opt/etc/iptv-home-probe/github_report_ed25519.pub')
    print('GITHUB_DEPLOY_PUBLIC_KEY:')
    print(public.read_text().strip() if public.exists() else 'MISSING')
    print('PRIVATE_KEY_NOT_PRINTED')


if __name__ == '__main__':
    main()
