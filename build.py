#!/usr/bin/env python3
"""Build APTV playlists of Chinese TV channels that actually play from where this runs.

Reads channels.toml, gathers candidate stream URLs (pinned ones plus matches found in public
upstream playlists), probes every candidate the way a player would join a live HLS stream,
and writes the best working sources per channel to dist/. Needs Python 3.11+ and ffprobe.

    python3 build.py              # full build
    python3 build.py --only 浙江   # probe matching channels and print the table; writes nothing
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
# What AVPlayer on tvOS sends; some origins reject unknown agents.
USER_AGENT = "AppleCoreMedia/1.0.0.22J580 (Apple TV; U; CPU OS 18_0 like Mac OS X; en_us)"
MAX_PLAYLIST_BYTES = 4 << 20
MAX_SEGMENT_BYTES = 64 << 20


# ------------------------------------------------------------------------------------------ names

_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|（[^）]*）|【[^】]*】")
_SEPARATORS = re.compile(r"[\s\-_·.|]+")
_QUALITY_SUFFIX = re.compile(r"(?:HD|FHD|UHD|SD|H265|HEVC|IPV6|1080P|720P|高清|超清|标清|蓝光|频道)$")


def norm(name: str) -> str:
    """Normalize a channel name so 'CCTV-1 (720p)', 'CCTV1 HD' and 'cctv-1高清' all compare equal."""
    s = _SEPARATORS.sub("", _BRACKETS.sub("", name)).upper()
    while (t := _QUALITY_SUFFIX.sub("", s)) != s and t:
        s = t
    return s


@dataclass
class Channel:
    name: str
    tvg: str
    group: str
    urls: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    en: str = ""

    def keys(self) -> set[str]:
        names = [self.name, self.tvg, *self.aliases]
        if self.en:
            names += [f"{self.en} Satellite TV", f"{self.en} TV"]
        return {norm(n) for n in names}


def load_config(path: Path) -> tuple[dict, list[dict], list[Channel]]:
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    channels = [Channel(**c) for c in cfg["channels"]]
    seen: dict[str, str] = {}
    for ch in channels:
        for key in ch.keys():
            if key in seen and seen[key] != ch.name:
                raise SystemExit(f"alias {key!r} is claimed by both {seen[key]!r} and {ch.name!r}")
            seen[key] = ch.name
    return cfg["settings"], cfg.get("upstreams", []), channels


# ------------------------------------------------------------------------------------------ m3u in

@dataclass
class Entry:
    name: str
    url: str
    logo: str = ""


_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def parse_m3u(text: str) -> list[Entry]:
    entries, pending = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            head, _, title = line.partition(",")
            attrs = dict(_ATTR.findall(head))
            pending = Entry(title.strip() or attrs.get("tvg-name", ""), "", attrs.get("tvg-logo", ""))
        elif line and not line.startswith("#") and pending:
            pending.url = line
            entries.append(pending)
            pending = None
    return entries


def usable_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    # IPv6 literals in these lists are China Mobile/Telecom addresses only reachable inside China.
    return parts.scheme in ("http", "https") and bool(parts.hostname) and "[" not in parts.netloc


# ------------------------------------------------------------------------------------------ probing

class ProbeError(Exception):
    pass


def fetch(url: str, timeout: float, limit: int) -> tuple[str, bytes]:
    """GET url with an overall deadline; returns (final url after redirects, body)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    deadline = time.monotonic() + timeout * 2
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        chunks, size = [], 0
        while chunk := resp.read(256 << 10):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ProbeError("response too large")
            if time.monotonic() > deadline:
                raise ProbeError("download too slow")
        return resp.geturl(), b"".join(chunks)


@dataclass
class Probe:
    url: str
    ok: bool = False
    error: str = ""
    width: int = 0
    height: int = 0
    codec: str = ""
    bitrate: float = 0.0     # Mbps, from segment sizes and durations
    throughput: float = 0.0  # Mbps, measured download speed
    startup: float = 0.0     # seconds from first request until the first segment arrived
    segment: float = 0.0     # HLS target duration; players sit roughly 3x this behind live

    @property
    def headroom(self) -> float:
        return self.throughput / self.bitrate if self.bitrate else 0.0


def _best_variant(base: str, text: str) -> str:
    best, best_bw = None, -1
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(m[1]) if m else 0
            uri = next((l.strip() for l in lines[i + 1:] if l.strip() and not l.startswith("#")), None)
            if uri and bw > best_bw:
                best, best_bw = uri, bw
    if not best:
        raise ProbeError("master playlist without variants")
    return urllib.parse.urljoin(base, best)


def _segments(base: str, text: str) -> list[tuple[str, float]]:
    segs, duration = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(re.match(r"#EXTINF:([\d.]+)", line)[1])
        elif line and not line.startswith("#") and duration is not None:
            segs.append((urllib.parse.urljoin(base, line), duration))
            duration = None
    return segs


def _ffprobe(target: str, timeout: float) -> tuple[int, int, str]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
           "stream=codec_name,width,height", "-of", "json"]
    if "://" in target:
        cmd += ["-rw_timeout", str(int(timeout * 1e6)), "-user_agent", USER_AGENT]
    try:
        out = subprocess.run(cmd + [target], capture_output=True, text=True, timeout=timeout * 2)
    except subprocess.TimeoutExpired:
        return 0, 0, ""
    streams = json.loads(out.stdout or "{}").get("streams") or []
    if not streams:
        return 0, 0, ""
    s = max(streams, key=lambda s: s.get("height") or 0)
    return s.get("width") or 0, s.get("height") or 0, s.get("codec_name") or ""


def probe(url: str, timeout: float) -> Probe:
    """Join the stream like a player: playlist -> best variant -> newest segments, then inspect them."""
    p = Probe(url)
    t0 = time.monotonic()
    try:
        final, body = fetch(url, timeout, MAX_PLAYLIST_BYTES)
        text = body.decode("utf-8", "replace")
        if "#EXTM3U" not in text[:1024]:
            raise ProbeError("not an HLS playlist")
        if "#EXT-X-STREAM-INF" in text:
            final, body = fetch(_best_variant(final, text), timeout, MAX_PLAYLIST_BYTES)
            text = body.decode("utf-8", "replace")
        if "#EXT-X-ENDLIST" in text:
            raise ProbeError("VOD playlist, not a live stream")
        segs = _segments(final, text)
        if not segs:
            raise ProbeError("playlist has no segments")
        m = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
        p.segment = float(m[1]) if m else max(d for _, d in segs)

        init = b""
        if m := re.search(r'#EXT-X-MAP:.*?URI="([^"]+)"', text):
            init = fetch(urllib.parse.urljoin(final, m[1]), timeout, MAX_SEGMENT_BYTES)[1]
        t1 = time.monotonic()
        total, data = 0, b""
        for i, (seg_url, _) in enumerate(segs[-2:]):
            data = fetch(seg_url, timeout, MAX_SEGMENT_BYTES)[1]
            total += len(data)
            if i == 0:
                p.startup = time.monotonic() - t0
        elapsed = max(time.monotonic() - t1, 1e-3)
        seconds = sum(d for _, d in segs[-2:]) or 1.0
        p.bitrate = total * 8 / 1e6 / seconds
        p.throughput = total * 8 / 1e6 / elapsed

        with tempfile.NamedTemporaryFile(suffix=".seg") as f:
            f.write(init + data)
            f.flush()
            p.width, p.height, p.codec = _ffprobe(f.name, timeout)
        if not p.height:  # encrypted or unusual container: let ffprobe walk the playlist itself
            p.width, p.height, p.codec = _ffprobe(url, timeout)
        if not p.height:
            raise ProbeError("no decodable video")
        p.ok = True
    except ProbeError as e:
        p.error = str(e)
    except Exception as e:  # network errors, bad TLS, HTTP 4xx/5xx...
        p.error = f"{type(e).__name__}: {e}"[:160]
    return p


# ------------------------------------------------------------------------------------------ ranking

def is_official(url: str, official_hosts: list[str]) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for pattern in official_hosts:
        if "*" in pattern:
            if fnmatch.fnmatch(host, pattern):
                return True
        elif host == pattern or host.endswith("." + pattern):
            return True
    return False


@dataclass
class Source:
    url: str
    official: bool
    width: int
    height: int
    codec: str
    bitrate: float
    headroom: float
    startup: float
    segment: float
    last_ok: str
    stale: bool = False


def rank(sources: list[Source], min_headroom: float) -> list[Source]:
    """Smooth playback first, then resolution (4K wins), then broadcaster-run over relays, then zap speed."""
    return sorted(sources, key=lambda s: (
        not s.stale,
        s.headroom >= min_headroom,
        min(s.height, 2160),
        s.official,
        -s.startup,
    ), reverse=True)


def carry_over(previous: list[dict], now: dt.datetime, stale_days: float) -> list[Source]:
    """Last run's sources for a channel that probed nothing this time, if they worked recently."""
    out = []
    for s in previous:
        last_ok = dt.datetime.fromisoformat(s["last_ok"])
        if now - last_ok <= dt.timedelta(days=stale_days):
            out.append(Source(**{**s, "stale": True}))
    return out


# ------------------------------------------------------------------------------------------ output

def extinf(ch: Channel, logo: str, group: str, title: str) -> str:
    attrs = [f'tvg-id="{ch.tvg}"', f'tvg-name="{ch.tvg}"']
    if logo:
        attrs.append(f'tvg-logo="{logo}"')
    attrs.append(f'group-title="{group}"')
    return f"#EXTINF:-1 {' '.join(attrs)},{title}"


def render_m3u(epg: str, rows: list[tuple[Channel, str, str, str, Source]]) -> str:
    lines = [f'#EXTM3U x-tvg-url="{epg}"']
    for ch, logo, group, title, src in rows:
        lines += [extinf(ch, logo, group, title), src.url]
    return "\n".join(lines) + "\n"


def playlist_rows(results: list[tuple[Channel, str, list[Source]]], settings: dict,
                  pick) -> list[tuple[Channel, str, str, str, Source]]:
    """Rows for one playlist; `pick(sources)` chooses which of a channel's ranked sources to emit."""
    uhd_group, uhd_height = settings["uhd_group"], settings["uhd_height"]
    rows = []
    for ch, logo, sources in results:
        uhd = [s for s in sources if s.height >= uhd_height]
        if uhd and ch.group != uhd_group:
            title = ch.name if "4K" in ch.name.upper() else f"{ch.name} 4K"
            rows += [(ch, logo, uhd_group, title, s) for s in pick(uhd)]
        rows += [(ch, logo, ch.group, ch.name, s) for s in pick(sources)]
    order = {g: i for i, g in enumerate(settings["group_order"])}
    return sorted(rows, key=lambda r: order.get(r[2], len(order)))  # stable: keeps catalog order


def render_report(results, generated: str, counts: dict) -> str:
    out = [
        "# Channel status",
        "",
        f"Generated {generated} by `build.py` on a machine outside China. "
        f"Probed {counts['probed']} candidate URLs; {counts['ok']} played.",
        "",
        "Latency ≈ 3 × segment length behind live. Headroom = download speed ÷ stream bitrate "
        "(≥ 1.3 plays smoothly). *Official* = served by the broadcaster's own CDN.",
        "",
        "| Channel | Group | Best source | Codec | Headroom | Segment | Start | Source | Backups |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for ch, _, sources in results:
        if not sources:
            out.append(f"| {ch.name} | {ch.group} | ❌ nothing plays | | | | | | |")
            continue
        s = sources[0]
        kind = "official" if s.official else "relay"
        if s.stale:
            kind += " (stale)"
        host = urllib.parse.urlsplit(s.url).hostname
        out.append(f"| {ch.name} | {ch.group} | {s.width}×{s.height} | {s.codec} | {s.headroom:.1f}× "
                   f"| {s.segment:.0f}s | {s.startup:.1f}s | {kind} `{host}` | {len(sources) - 1} |")
    return "\n".join(out) + "\n"


def check_logos(channels: list[Channel], base: str, timeout: float) -> dict[str, str]:
    def head(ch):
        url = base + urllib.parse.quote(ch.tvg) + ".png"
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return ch.name, url if r.status == 200 else ""
        except Exception:
            return ch.name, ""
    with cf.ThreadPoolExecutor(16) as ex:
        return dict(ex.map(head, channels))


# ------------------------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=ROOT / "channels.toml")
    ap.add_argument("--out", type=Path, default=DIST)
    ap.add_argument("--only", help="probe only channels whose name contains this text; print, don't write")
    ap.add_argument("--workers", type=int, default=32)
    args = ap.parse_args(argv)

    settings, upstreams, channels = load_config(args.config)
    if args.only:
        channels = [c for c in channels if args.only.lower() in c.name.lower()]
    timeout = settings["probe_timeout"]
    by_key = {key: ch for ch in channels for key in ch.keys()}

    # 1. candidates: pinned URLs first, then upstream matches in upstream order
    candidates = {ch.name: list(dict.fromkeys(ch.urls)) for ch in channels}
    upstream_logo: dict[str, str] = {}
    for up in upstreams:
        try:
            text = fetch(up["url"], 30, 32 << 20)[1].decode("utf-8", "replace")
        except Exception as e:
            print(f"! upstream {up['name']}: {e}", file=sys.stderr)
            continue
        entries = parse_m3u(text)
        matched = 0
        for e in entries:
            ch = by_key.get(norm(e.name))
            if ch and usable_url(e.url) and e.url not in candidates[ch.name]:
                candidates[ch.name].append(e.url)
                if e.logo:
                    upstream_logo.setdefault(ch.name, e.logo)
                matched += 1
        print(f"  upstream {up['name']}: {len(entries)} entries, {matched} new candidates", file=sys.stderr)
    for name in candidates:
        candidates[name] = candidates[name][: settings["max_candidates"]]

    # 2. probe everything once
    urls = sorted({u for us in candidates.values() for u in us})
    print(f"  probing {len(urls)} URLs with {args.workers} workers...", file=sys.stderr)
    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(args.workers) as ex:
        probes = dict(zip(urls, ex.map(lambda u: probe(u, timeout), urls)))
    ok_count = sum(p.ok for p in probes.values())
    print(f"  {ok_count}/{len(urls)} played ({time.monotonic() - t0:.0f}s)", file=sys.stderr)

    # 3. rank per channel, carrying over recent sources for channels that went dark this run
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    state_path = args.out / "channels.json"
    previous = {}
    if state_path.exists():
        previous = {c["name"]: c["sources"] for c in json.loads(state_path.read_text())["channels"]}
    logos = check_logos(channels, settings["logo_base"], timeout)
    results: list[tuple[Channel, str, list[Source]]] = []
    for ch in channels:
        sources = [
            Source(url=p.url, official=is_official(p.url, settings["official_hosts"]), width=p.width,
                   height=p.height, codec=p.codec, bitrate=round(p.bitrate, 2), headroom=round(p.headroom, 1),
                   startup=round(p.startup, 2), segment=p.segment, last_ok=now.isoformat())
            for p in (probes[u] for u in candidates[ch.name]) if p.ok
        ]
        if not sources:
            sources = carry_over(previous.get(ch.name, []), now, settings["stale_days"])
        logo = logos.get(ch.name) or upstream_logo.get(ch.name, "")
        results.append((ch, logo, rank(sources, settings["min_headroom"])))

    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    counts = {"probed": len(urls), "ok": ok_count}
    report = render_report(results, generated, counts)
    if args.only:
        print(report)
        return 0

    # 4. write playlists, report and state
    args.out.mkdir(parents=True, exist_ok=True)
    epg = settings["epg"]
    official_only = [(ch, logo, [s for s in srcs if s.official]) for ch, logo, srcs in results]
    outputs = {
        "aptv.m3u": playlist_rows(results, settings, lambda s: s[:1]),
        "aptv-backup.m3u": playlist_rows(results, settings, lambda s: s[1:2]),
        "aptv-official.m3u": playlist_rows(official_only, settings, lambda s: s[:1]),
    }
    for filename, rows in outputs.items():
        (args.out / filename).write_text(render_m3u(epg, rows), encoding="utf-8")
    (args.out / "README.md").write_text(report, encoding="utf-8")
    state = {
        "generated": now.isoformat(),
        "channels": [{"name": ch.name, "sources": [asdict(s) for s in srcs]} for ch, _, srcs in results],
    }
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    playing = sum(bool(s) for _, _, s in results)
    print(f"  {playing}/{len(results)} channels have a working source -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
