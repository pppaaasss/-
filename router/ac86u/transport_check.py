#!/opt/bin/python3
"""Manual one-channel check. Does not activate the probe or write reports."""
import json
import re
import signal
from pathlib import Path

import home_probe


def stop(_signum, _frame):
    raise InterruptedError("manual transport check interrupted")


def main():
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop)
    config = home_probe.load_json(Path("/opt/etc/iptv-home-probe.json"))
    # Local copy only: these values are never written back to the config.
    config.update(runtime_transport="merlinclash-marked", lan_dns_server="192.168.50.1")
    print("Checking LAN DNS + marked IPv4 / direct IPv6 + ffprobe", flush=True)
    for proc in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = proc.read_bytes().decode('utf-8', 'replace').split('\0')
            if Path(args[0]).name != 'clash' or '-f' not in args:
                continue
            text = Path(args[args.index('-f') + 1]).read_text()
            lines = [x for x in text.splitlines() if x.lstrip().startswith('-')]
            source_rules = sum(bool(re.search(r'(?:^|[,\s(])(?:SRC-[A-Z-]+|PROCESS-[A-Z-]+|IN-[A-Z-]+|UID|DSCP),', x.lstrip(' -\"\''))) for x in lines)
            providers = sum('RULE-SET,' in x for x in lines)
            print('CLASH_RULES: source_sensitive=%d rule_set_refs=%d' % (source_rules, providers), flush=True)
            break
        except (OSError, IndexError):
            continue
    try:
        with home_probe.transport_context(config) as transport:
            addresses = transport.dialer.resolver.resolve("raw.githubusercontent.com")
            print("LAN_DNS: OK, A=%d AAAA=%d" % (
                sum(":" not in x for x in addresses), sum(":" in x for x in addresses)), flush=True)
            data, _, _ = home_probe.fetch_playlist("https://raw.githubusercontent.com/pppaaasss/-/master/tv.m3u")
            entries = home_probe.parse_playlist(data)
            print("TV_PLAYLIST: %d channels" % len(entries), flush=True)
            channel = next((n, u) for n, u in entries if home_probe.station_key(n) == "cctv1")
            print("TESTING: " + channel[0], flush=True)
            result = home_probe.probe_route(
                *channel, floor=1080, config=config, sample_limit=2097152, include_metadata=True)
            print("CHANNEL_RESULT: " + json.dumps({k: result[k] for k in (
                "name", "status", "sample_count", "height", "codec", "deep_checked", "min_download_mbps", "error"
            )}, ensure_ascii=False), flush=True)
            print("CONNECTIONS: IPv4=%d IPv6=%d" % (transport.dialer.ipv4, transport.dialer.ipv6), flush=True)
        print("TEMP_RULE: REMOVED", flush=True)
        print("Manual check only; production and route verification remain unchanged.")
        return 0
    except Exception as exc:
        print("CHECK_FAILED: " + str(exc), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
