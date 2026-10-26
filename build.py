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
    require_4k: bool = False  # a 4K channel: only listed while a real-4K source plays

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


def _split_extinf(line: str) -> tuple[str, str]:
    """Split '#EXTINF:-1 attrs,Title' at the first comma outside quotes (attribute values may contain commas)."""
    quoted = False
    for i, c in enumerate(line):
        if c == '"':
            quoted = not quoted
        elif c == "," and not quoted:
            return line[:i], line[i + 1:]
    return line, ""


def parse_m3u(text: str) -> list[Entry]:
    entries, pending = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            head, title = _split_extinf(line)
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
    audio: str = ""
    container: str = ""
    bitrate: float = 0.0     # Mbps, from segment sizes and durations
    throughput: float = 0.0  # Mbps, measured download speed
    startup: float = 0.0     # seconds from first request until the first segment arrived
    segment: float = 0.0     # HLS target duration; players sit roughly 3x this behind live
    pinned: str = ""         # top-variant media playlist of a master, when its URL is stable

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


def _ffprobe(target: str, timeout: float) -> dict:
    """Video size/codec, first audio codec and container of a local segment or a stream URL."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "stream=codec_type,codec_name,width,height:format=format_name", "-of", "json"]
    if "://" in target:
        cmd += ["-rw_timeout", str(int(timeout * 1e6)), "-user_agent", USER_AGENT]
    try:
        out = subprocess.run(cmd + [target], capture_output=True, text=True, timeout=timeout * 2)
        info = json.loads(out.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    streams = info.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video" and s.get("height")]
    if not video:
        return {}
    v = max(video, key=lambda s: s["height"])
    audio = next((s.get("codec_name", "") for s in streams if s.get("codec_type") == "audio"), "")
    return {"width": v.get("width") or 0, "height": v["height"], "codec": v.get("codec_name") or "",
            "audio": audio, "container": (info.get("format") or {}).get("format_name", "")}


# What AVPlayer (APTV's 系统播放器 on tvOS) decodes in HLS. Notably not HEVC inside MPEG-TS
# (Apple only accepts HEVC in fMP4) and not MP2 audio, both common in Chinese IPTV feeds.
APPLE_AUDIO = {"", "aac", "ac3", "eac3", "mp3", "alac", "flac"}


def apple_compatible(codec: str, audio: str, container: str) -> bool:
    if codec not in ("h264", "hevc"):
        return False
    if codec == "hevc" and "mpegts" in container:
        return False
    return audio in APPLE_AUDIO


# Query parameters that usually mean a URL expires or is tied to one session.
_EPHEMERAL = re.compile(r"(?:^|&)(?:token|expires?|exp|sig|signature|hdnts|hdnea|auth|session|sid|st|e)=", re.I)


def _stable_variant(url: str, variant: str, timeout: float) -> str:
    """The variant URL if a fresh master fetch hands out the same one and it carries no session token."""
    try:
        final, body = fetch(url, timeout, MAX_PLAYLIST_BYTES)
        again = _best_variant(final, body.decode("utf-8", "replace"))
    except Exception:
        return ""
    return variant if again == variant and not _EPHEMERAL.search(urllib.parse.urlsplit(variant).query) else ""


def probe(url: str, timeout: float, pin_height: int = 0) -> Probe:
    """Join the stream like a player: playlist -> best variant -> newest segments, then inspect them.

    For a master playlist whose top variant is at least `pin_height` tall, also record that
    variant's URL in `pinned` if it is stable, so a playlist can lock players onto it.
    """
    p = Probe(url)
    t0 = time.monotonic()
    try:
        final, body = fetch(url, timeout, MAX_PLAYLIST_BYTES)
        text = body.decode("utf-8", "replace")
        if "#EXTM3U" not in text[:1024]:
            raise ProbeError("not an HLS playlist")
        variant = ""
        if "#EXT-X-STREAM-INF" in text:
            variant = _best_variant(final, text)
            final, body = fetch(variant, timeout, MAX_PLAYLIST_BYTES)
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
            info = _ffprobe(f.name, timeout)
        if not info:  # encrypted or unusual container: let ffprobe walk the playlist itself
            info = _ffprobe(url, timeout)
        if not info:
            raise ProbeError("no decodable video")
        for key, value in info.items():
            setattr(p, key, value)
        if variant and pin_height and p.height >= pin_height:
            p.pinned = _stable_variant(url, variant, timeout)
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
    audio: str = ""
    container: str = ""
    apple: bool = True       # plays in AVPlayer, APTV's system player
    pinned: str = ""         # stable 2160p variant URL, when the source is an adaptive master


def quality(s: Source, floors: dict[int, float]) -> int:
    """Effective resolution class (2160/1080/720/576): nominal height, demoted when the bitrate can't carry it.

    `floors` maps a class to the minimum HEVC-equivalent Mbps it needs; H.264 counts half.
    A "4K" stream at 1.5 Mbps HEVC carries less picture than an ordinary 1080p stream.
    """
    eq = s.bitrate * (1.0 if s.codec == "hevc" else 0.5)
    for height in sorted(floors, reverse=True):
        if s.height >= height * 0.9 and eq >= floors[height]:
            return height
    return min(s.height, 576)


def rank(sources: list[Source], min_headroom: float, good_headroom: float,
         floors: dict[int, float]) -> list[Source]:
    """Fresh, Apple-playable and reliably fast first; then real picture quality, broadcaster-run, zap speed.

    Reliability outranks quality so a channel's main entry just works; 4K sources that are
    marginal or need APTV's other player engine are still listed in the 4K group.
    """
    return sorted(sources, key=lambda s: (
        not s.stale,
        s.apple,
        (s.headroom >= good_headroom) + (s.headroom >= min_headroom),
        quality(s, floors),
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


def render_m3u(epg: str, rows: list[tuple[Channel, str, str, str, str]]) -> str:
    lines = [f'#EXTM3U x-tvg-url="{epg}"']
    for ch, logo, group, title, url in rows:
        lines += [extinf(ch, logo, group, title), url]
    return "\n".join(lines) + "\n"


def is_4k_channel(ch: Channel, uhd_group: str) -> bool:
    return ch.require_4k or ch.group == uhd_group


def floors_of(settings: dict) -> dict[int, float]:
    return {int(k): v for k, v in settings["quality_floors"].items()}


def _lines(sources: list[Source], pin: bool) -> list[str]:
    """Playable URLs in order. With `pin`, a source's locked 2160p variant comes first and its
    adaptive master right after, as the fallback line."""
    urls: list[str] = []
    for s in sources:
        for u in (s.pinned, s.url) if pin and s.pinned else (s.url,):
            if u not in urls:
                urls.append(u)
    return urls


def playlist_rows(results: list[tuple[Channel, str, list[Source]]], settings: dict,
                  pick) -> list[tuple[Channel, str, str, str, str]]:
    """Rows for one playlist; `pick(urls)` chooses which of a channel's ranked lines to emit.

    Sources whose real quality is 4K are also listed in the 4K group, locked to their 2160p
    variant when that is possible. Main entries keep adaptive masters for reliability.
    """
    uhd_group, floors = settings["uhd_group"], floors_of(settings)
    rows = []
    for ch, logo, sources in results:
        uhd = [s for s in sources if quality(s, floors) >= 2160]
        if is_4k_channel(ch, uhd_group):  # never list a 4K channel that would play HD
            if not uhd:
                continue
            group = uhd_group  # real-4K lines first; HD only as a fallback line behind them
            rest = [s for s in sources if s not in uhd]
            rows += [(ch, logo, group, ch.name, u) for u in pick(_lines(uhd, pin=True) + _lines(rest, pin=False))]
            continue
        if uhd:
            title = ch.name if "4K" in ch.name.upper() else f"{ch.name} 4K"
            rows += [(ch, logo, uhd_group, title, u) for u in pick(_lines(uhd, pin=True))]
        rows += [(ch, logo, ch.group, ch.name, u) for u in pick(_lines(sources, pin=False))]
    order = {g: i for i, g in enumerate(settings["group_order"])}
    return sorted(rows, key=lambda r: order.get(r[2], len(order)))  # stable: keeps catalog order


QUALITY_LABEL = {2160: "4K", 1080: "1080p", 720: "720p"}


def render_report(results, generated: str, counts: dict, floors: dict[int, float], uhd_group: str) -> str:
    out = [
        "# Channel status",
        "",
        f"Generated {generated} by `build.py` on a machine outside China. "
        f"Probed {counts['probed']} candidate URLs; {counts['ok']} played.",
        "",
        "Latency ≈ 3 × segment length behind live. Headroom = download speed ÷ stream bitrate "
        "(≥ 1.3 plays, ≥ 2 comfortably). *Official* = served by the broadcaster's own CDN. "
        "*Apple* = plays in APTV's 系统播放器 (AVPlayer); ✗ needs APTV's other player engine. "
        "*Quality* = resolution the bitrate can actually carry (H.264 needs about twice HEVC's bitrate); "
        "📌 = locked to the 2160p variant so the player can't drop to a lower one.",
        "",
        "| Channel | Group | Best source | Codec | Mbps | Quality | Apple | Headroom | Segment | Start | Source | Backups |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for ch, _, sources in results:
        if not sources:
            out.append(f"| {ch.name} | {ch.group} | ❌ nothing plays | | | | | | | | | |")
            continue
        s = sources[0]
        if is_4k_channel(ch, uhd_group) and not any(quality(u, floors) >= 2160 for u in sources):
            out.append(f"| {ch.name} | {ch.group} | ⚠️ no real 4K source (best: {s.width}×{s.height}, "
                       f"{s.bitrate:.1f} Mbps), left out | | | | | | | | | |")
            continue
        kind = "official" if s.official else "relay"
        if s.stale:
            kind += " (stale)"
        host = urllib.parse.urlsplit(s.url).hostname
        codec = f"{s.codec}/{s.audio}" if s.audio else s.codec
        q = QUALITY_LABEL.get(quality(s, floors), "SD")
        best4k = next((u for u in sources if quality(u, floors) >= 2160), None)
        if best4k and best4k is not s:  # the main entry is HD, but the 4K group has this one
            q += f" (4K {best4k.bitrate:.1f} Mbps{' 📌' if best4k.pinned else ''})"
        elif best4k and s.pinned:
            q += " 📌"
        out.append(f"| {ch.name} | {ch.group} | {s.width}×{s.height} | {codec} | {s.bitrate:.1f} | {q} "
                   f"| {'✓' if s.apple else '✗'} | {s.headroom:.1f}× "
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
    ap.add_argument("--force", action="store_true", help="write dist/ even if most channels failed")
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
            if not ch:
                continue
            if e.logo:  # even when the URL is one we pinned ourselves
                upstream_logo.setdefault(ch.name, e.logo)
            if usable_url(e.url) and e.url not in candidates[ch.name]:
                candidates[ch.name].append(e.url)
                matched += 1
        print(f"  upstream {up['name']}: {len(entries)} entries, {matched} new candidates", file=sys.stderr)
    for name in candidates:
        candidates[name] = candidates[name][: settings["max_candidates"]]

    # 2. probe everything once
    urls = sorted({u for us in candidates.values() for u in us})
    print(f"  probing {len(urls)} URLs with {args.workers} workers...", file=sys.stderr)
    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(args.workers) as ex:
        probes = dict(zip(urls, ex.map(lambda u: probe(u, timeout, settings["uhd_height"]), urls)))
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
                   startup=round(p.startup, 2), segment=p.segment, last_ok=now.isoformat(), audio=p.audio,
                   container=p.container, apple=apple_compatible(p.codec, p.audio, p.container),
                   pinned=p.pinned)
            for p in (probes[u] for u in candidates[ch.name]) if p.ok
        ]
        if not sources:
            sources = carry_over(previous.get(ch.name, []), now, settings["stale_days"])
        logo = logos.get(ch.name) or upstream_logo.get(ch.name, "")
        results.append((ch, logo, rank(sources, settings["min_headroom"], settings["good_headroom"],
                                       floors_of(settings))))

    generated = now.strftime("%Y-%m-%d %H:%M UTC")
    counts = {"probed": len(urls), "ok": ok_count}
    report = render_report(results, generated, counts, floors_of(settings), settings["uhd_group"])
    if args.only:
        print(report)
        return 0
    fresh = sum(any(not s.stale for s in srcs) for _, _, srcs in results)
    if fresh < len(results) * settings["min_fresh_share"] and not args.force:
        # Almost always the build machine's network, not the streams: keep the last good playlists.
        print(f"! only {fresh}/{len(results)} channels probed OK; leaving {args.out} untouched "
              "(--force to write anyway)", file=sys.stderr)
        return 1

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
