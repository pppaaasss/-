#!/usr/bin/env python3
"""IPv6-first reinforcement for mainland CCTV and satellite channels."""
from __future__ import annotations
import collections, ipaddress, json, re, urllib.parse, urllib.request
from dataclasses import dataclass, field
from pathlib import Path

UA = "Mozilla/5.0 (AppleTV; APTV core IPv6 reinforcement/1.1)"
TIMEOUT = 12.0
FEEDS = (
    ("fanmingming", "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u", 30),
    ("xjt1995", "https://raw.githubusercontent.com/xjt1995/iptv/main/cn.txt", 28),
    ("guovin", "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv6/result.m3u", 24),
    ("ftindy", "https://raw.githubusercontent.com/Ftindy/IPTV-URL/main/IPV6.m3u", 20),
    ("peterhchina", "https://raw.githubusercontent.com/peterHchina/iptv/main/CNTV-V6.m3u", 16),
)
SATELLITES = {
    "三沙卫视","东南卫视","东方卫视","云南卫视","兵团卫视","内蒙古卫视","农林卫视","北京卫视",
    "吉林卫视","四川卫视","天津卫视","宁夏卫视","安多卫视","安徽卫视","山东卫视","山西卫视",
    "广东卫视","广西卫视","延边卫视","新疆卫视","江苏卫视","江西卫视","河北卫视","河南卫视",
    "浙江卫视","海南卫视","海峡卫视","深圳卫视","湖北卫视","湖南卫视","甘肃卫视","西藏卫视",
    "贵州卫视","辽宁卫视","重庆卫视","陕西卫视","青海卫视","黑龙江卫视",
}
MARKERS = ("IPv6主线", "IPv6备用", "IPv4备用")

@dataclass
class Candidate:
    channel: str
    url: str
    sources: set[str] = field(default_factory=set)
    weight: int = 0
    @property
    def host(self): return (urllib.parse.urlsplit(self.url).hostname or "").lower()
    @property
    def score(self):
        low = self.url.lower()
        value = self.weight + max(0, len(self.sources)-1)*40
        value += 18 if self.host.startswith("2409:8087:") else 0
        value += 10 if any(x in low for x in ("/pltv/","/cms001/","/zte_cms/","/tvod/")) else 0
        value -= 24 if any(x in low for x in ("accountinfo=","securitykey=","timestamp=","auth_key=","token=")) else 0
        value += 4 if "love=freedom" in low else 0
        return value

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/plain,*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(8*1024*1024).decode("utf-8", "ignore")

def ipv6(url):
    try: return ipaddress.ip_address(urllib.parse.urlsplit(url).hostname or "").version == 6
    except ValueError: return False

def clean_name(name):
    name = name.strip().replace("－","-").replace("＋","+")
    name = re.sub(r"[「【\[(（]\s*IPV?6\s*[」】\])）]", "", name, flags=re.I)
    name = re.sub(r"\b(?:2160p|1080p|1080i|720p|576p|540p|uhd|fhd|hd)\b", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -_")

def canonical(name):
    raw = clean_name(name); compact = re.sub(r"[\s_-]+", "", raw).lower()
    if "cctv4k" in compact: return "CCTV-4K"
    if re.search(r"cctv5\+|cctv5plus", compact, re.I): return "CCTV-5+"
    m = re.search(r"cctv(1[0-7]|[1-9])(?:\D|$)", compact, re.I)
    if m:
        n = int(m.group(1)); return "CCTV-5" if n == 5 else f"CCTV-{n}"
    return next((s for s in SATELLITES if s in raw), None)

def parse_source(text):
    out=[]; pending=None
    for raw in text.splitlines():
        line=raw.strip()
        if not line: continue
        if line.startswith("#EXTINF"):
            pending=line.rsplit(",",1)[-1].strip() if "," in line else ""; continue
        if pending and line.startswith(("http://","https://")):
            out.append((pending,line)); pending=None; continue
        if line.startswith("#"): continue
        if "," in line:
            name,url=line.split(",",1); url=url.strip()
            if url.startswith(("http://","https://")): out.append((name.strip(),url))
    return out

def candidates():
    seen={}; status={}
    for source,url,weight in FEEDS:
        try:
            accepted=0
            for name,stream in parse_source(fetch(url)):
                key=canonical(name)
                if not key or key == "CCTV-5" or not ipv6(stream): continue
                item=seen.setdefault((key,stream), Candidate(key,stream))
                item.sources.add(source); item.weight=max(item.weight,weight); accepted+=1
            status[source]=f"ok:{accepted}"
        except Exception as e:
            status[source]=f"fail:{type(e).__name__}:{str(e)[:80]}"
    grouped=collections.defaultdict(list)
    for item in seen.values(): grouped[item.channel].append(item)
    for items in grouped.values(): items.sort(key=lambda x:(x.score,len(x.sources)), reverse=True)
    return dict(grouped),status

def parse_playlist(text):
    header=[]; entries=[]; pending=None
    for raw in text.splitlines():
        line=raw.strip()
        if line.startswith("#EXTINF"): pending=line
        elif pending and line.startswith(("http://","https://")):
            entries.append((pending,line)); pending=None
        elif pending is None: header.append(raw)
    return header,entries

def visible(extinf): return extinf.rsplit(",",1)[-1].strip() if "," in extinf else ""
def renamed(extinf,name): return extinf.rsplit(",",1)[0]+","+name if "," in extinf else extinf+","+name

def count_header(header,count):
    out=[]; done=False
    for line in header:
        if line.startswith("# channels="): out.append(f"# channels={count}"); done=True
        else: out.append(line)
    if not done: out.append(f"# channels={count}")
    return out

def reinforce(path, pool):
    if not path.exists(): return False,0
    original=path.read_text(encoding="utf-8",errors="ignore")
    header,entries=parse_playlist(original)
    entries=[e for e in entries if not any(m in visible(e[0]) for m in MARKERS)]
    first={}
    for i,(meta,_) in enumerate(entries):
        key=canonical(visible(meta))
        if key and key not in first: first[key]=i
    out=[]; promoted=0
    for i,(meta,url) in enumerate(entries):
        key=canonical(visible(meta))
        if not key or key == "CCTV-5" or first.get(key) != i:
            out.append((meta,url)); continue
        items=pool.get(key,[])
        coverage={s for item in items for s in item.sources}
        if len(coverage) < 2 or not items:
            out.append((meta,url)); continue
        primary=items[0]
        out.append((renamed(meta,f"{key} IPv6主线"),primary.url))
        if url != primary.url:
            out.append((renamed(meta,f"{key} {'IPv6' if ipv6(url) else 'IPv4'}备用"),url))
        promoted+=1
    header=count_header(header,len(out))
    rendered="\n".join(header+[part for entry in out for part in entry])+"\n"
    if rendered != original: path.write_text(rendered,encoding="utf-8",newline="\n")
    return rendered != original,promoted

def core_playlist(source,target):
    if not source.exists(): return 0
    _,entries=parse_playlist(source.read_text(encoding="utf-8",errors="ignore"))
    core=[e for e in entries if canonical(visible(e[0]))]
    header=['#EXTM3U x-tvg-url="https://live.fanmingming.cn/e.xml"','# 央视 + 中国大陆卫视：IPv6主线 / 实测备用',f'# channels={len(core)}']
    target.write_text("\n".join(header+[part for entry in core for part in entry])+"\n",encoding="utf-8",newline="\n")
    return len(core)

def report(status,promoted,count):
    path=Path("build-report.txt")
    if not path.exists(): return
    text=re.sub(r"^core_ipv6_.*\n?","",path.read_text(encoding="utf-8",errors="ignore"),flags=re.M)
    lines=[f"core_ipv6_promoted={promoted}",f"core_ipv6_playlist_channels={count}","core_ipv6_feed_status="+json.dumps(status,ensure_ascii=False,sort_keys=True)]
    path.write_text(text.rstrip()+"\n"+"\n".join(lines)+"\n",encoding="utf-8",newline="\n")

def main():
    pool,status=candidates(); total=0
    for path in (Path("tv.m3u"),Path("tv-all.m3u")):
        changed,n=reinforce(path,pool); total+=n; print(f"{path}: changed={changed} promoted={n}")
    count=core_playlist(Path("tv.m3u"),Path("tv-core.m3u")); report(status,total,count)
    print("feeds="+json.dumps(status,ensure_ascii=False,sort_keys=True)); print(f"core_playlist_channels={count}")
    return 0
if __name__ == "__main__": raise SystemExit(main())
