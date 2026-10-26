# aptv

Playlists of Chinese TV (CCTV, 浙江卫视, 江苏卫视, 湖南卫视 4K, …) that **actually play in
[APTV](https://apps.apple.com/app/id1630403500) on an Apple TV outside mainland China**. They are
re-tested twice a day from a US machine, and only working streams are kept.

- [`dist/aptv.m3u`](dist/aptv.m3u): best working source per channel (start here)
- [`dist/aptv-backup.m3u`](dist/aptv-backup.m3u): second-best source per channel, for APTV failover
- [`dist/aptv-official.m3u`](dist/aptv-official.m3u): only streams served by the broadcasters' own CDNs
- [`dist/README.md`](dist/README.md): per-channel status (resolution, codec, headroom, latency, origin)

## Why your old APTV setup didn't play

1. **The popular Chinese playlists are China-only.** Lists like `fanmingming/live` (the one APTV
   users usually add) are mostly IPv6 addresses inside China Mobile and China Telecom IPTV
   networks. They only work on a mainland Chinese home connection with IPv6. Your network has no
   IPv6, and those addresses aren't reachable from abroad anyway.
2. **The official CCTV streams are geo-blocked.** From a US address, CCTV's own live API
   (`vdn.live.cntv.cn`) returns a static "copyright" image and an audio-only stream for
   CCTV-1/4/13 and even CCTV-4 America/Europe. 央视频 restricts live TV by IP in the same way,
   and its TV app no longer has live channels.

## What works from the US (tested 2026-10-25, US West Coast)

| Channel | Works? | Best source | Notes |
|---|---|---|---|
| **湖南卫视** | ✅ **4K** HEVC | official (芒果TV CDN) | The best 4K channel available abroad |
| **浙江卫视** + 8 Zhejiang channels | ✅ 1080p | official (中国蓝 CDN) | All of cztv's channels are open overseas |
| **东方卫视** | ✅ 1080p | official (BesTV) | The "东方卫视4K" feed arrives as 1080p; thin headroom (~1.4×), so it may buffer |
| **河北卫视** | ✅ **4K** H.264 | official | About 7 Mbps with thin headroom; may stutter |
| **苏州 4K** | ✅ **4K** | official | Plain http only (the https certificate has expired) |
| **江苏卫视** | ✅ 1080p | third-party relay | No official stream plays outside China |
| **CCTV-1 … CCTV-17, 5+** | ✅ 720p–1080p | third-party relays | Official CCTV is blocked abroad (see above) |
| Most other 卫视 (北京, 广东, 深圳, 山东, 安徽, …) | ✅ 1080p | relays | |
| CGTN, CGTN 纪录 | ✅ | official / relay | |
| CCTV-4K, CCTV-8K | ❌ / 1080p | — | True 4K/8K CCTV exists only inside China (operator IPTV, 央视频) |

The live table, regenerated on every run, is in [`dist/README.md`](dist/README.md).

**About the relays.** CCTV and most 卫视 come from a handful of US data-center hosts that
re-stream Chinese feeds. They appear in public lists such as iptv-org and Guovin/iptv-api. They
are fast from the US (about 1 s to start, 10–20× bandwidth headroom), but nobody knows who runs
them or whether they are licensed, and they come and go. That is why the build re-discovers
sources automatically. If you would rather use only broadcaster-run streams, subscribe to
`aptv-official.m3u` instead.

### 4K, honestly

- Outside China you can get real 4K on **湖南卫视**, **河北卫视** and **苏州 4K**. They are listed
  together in the playlist's `4K 超高清` group.
- **CCTV-4K/8K, 北京卫视4K, 广东/深圳 4K and 东方卫视4K at full resolution** are only delivered
  inside China, through operator IPTV multicast or 央视频 with a mainland IP.
- You need an Apple TV 4K. The Apple TV HD tops out at 1080p and has no hardware HEVC decoding.

### Latency, honestly

Every internet source here is HLS. A player joins about 3 segments behind the live edge, so you
are roughly **3 × segment length** behind the broadcast: about 25 s for 浙江卫视 (8 s
segments), 30–40 s for the relays (10–14 s segments) and about 12 s for 东方卫视 (4 s
segments). That doesn't matter for dramas or variety shows; for live sports you'll be about
half a minute behind. True low latency (1–3 s) needs the operator's multicast feed inside China
(option 5 below). What this playlist does optimize is **zapping speed**: sources that start
faster rank higher when everything else is equal.

## Using it in APTV (Apple TV)

1. **配置 → + → 订阅配置**, then paste the playlist URL:
   `https://raw.githubusercontent.com/realzza/aptv/main/dist/aptv.m3u`.
   This only works if the repo is public; see "Hosting" below if it is private. Turn on
   **自动刷新配置** so APTV picks up the twice-daily refreshes.
2. The program guide (EPG) loads automatically through `x-tvg-url`
   (`https://live.fanmingming.cn/e.xml`). You can also enter it as the 节目单地址.
3. *Optional: automatic failover.* Add `aptv-backup.m3u` as a second 订阅配置, then create a
   **聚合配置** with `aptv` first and `aptv-backup` second. APTV merges channels with the same
   name and switches to the backup if a stream doesn't start within the timeout (default 5 s,
   under 设置 → 高级设置 → 播放超时时间).

**Hosting.** APTV needs a URL it can fetch without logging in. If this repo stays private, you
can either:

- Make the repo public (the raw URL above then just works), or
- Use a **本地配置**: AirDrop or iCloud the `.m3u` to APTV on your iPhone or Mac. It syncs to the
  Apple TV through iCloud (requires APTV 高级版), but you lose the automatic refreshes.

## All the options I looked at

| # | Option | Cost | CCTV-1 etc. | 浙江/江苏 | 4K | Latency | Verdict |
|---|---|---|---|---|---|---|---|
| 1 | **This playlist in APTV** | free | ✅ via relays | ✅ (浙江 official) | 湖南/河北/苏州 | 12–40 s | **Best overall from the US today** |
| 2 | **iTalkBB TV** (licensed, uses CCTV's Great Wall platform) | $5.99/mo app, or $9.99/mo plus a $249 box | ✅ CCTV, licensed | international versions only | ❌ | HLS | The legal choice for CCTV. US/Canada only, and the app is iPhone/iPad only (no tvOS app, so AirPlay or their box) |
| 3 | Official free streams | free | ❌ (CGTN only) | 浙江 ✅, 湖南 ✅ | 湖南 | HLS | Already in `aptv-official.m3u` |
| 4 | "回国" VPN or router with a mainland exit | ~$5–15/mo | ✅ official, with tokens | ✅ | 1080p mostly | VPN adds delay | Clunky on tvOS: no official CCTV/卫视 tvOS apps, signed URLs expire, and it breaks the platforms' terms |
| 5 | Relay a relative's China operator IPTV (udpxy/msd_lite plus Tailscale/WireGuard) | their IPTV plan | ✅ multicast quality | ✅ | ✅ CCTV-4K and provincial 4K | lowest | Only real path to true CCTV-4K. Cross-border upload (4K needs 25–40 Mbps sustained) is unreliable at China's evening peak, and operator terms usually forbid relaying |

Apps like 华人TV and similar "overseas Chinese TV" boxes re-stream channels with unclear
licensing. That puts them in the same position as the relays, often with worse apps, so I didn't
consider them further.

## How the playlists are built

`build.py` (Python 3.11+ stdlib, plus `ffprobe`) does the following on each run:

1. Reads [`channels.toml`](channels.toml): 72 channels, each with names, aliases and pinned
   official URLs.
2. Pulls candidate URLs from the upstream lists (iptv-org China and Chinese-language, Guovin/iptv-api,
   vbskycn/iptv) whose normalized names match a channel. Mainland-only IPv6 sources are dropped.
3. Probes every candidate the way a player joins a live stream: master playlist → highest
   variant → the two newest segments. It measures startup time, bitrate, download headroom and
   segment length, and checks the real resolution and codec with `ffprobe`. VOD loops,
   placeholders without video, expired TLS certificates and anything that doesn't decode are
   dropped.
4. Ranks each channel's sources: smooth (headroom ≥ 1.3×) → resolution → official over relay →
   faster start. If a channel finds no working source in a run, its sources from the last 3 days
   are kept.
   If more than half the channels fail at once (usually the build machine's network), the run
   stops and the previous playlists stay as they are.
5. Writes `dist/`. [`refresh.yml`](.github/workflows/refresh.yml) runs this on a US GitHub
   runner at about 7am and 7pm Pacific and commits any changes.

Results depend on where the probe runs. To check from your own network:

```bash
python3 build.py                 # rebuild dist/
python3 build.py --only 卫视      # probe matching channels, print the table, write nothing
python3 -m unittest discover -s tests
```

To add a channel, append a `[[channels]]` block to `channels.toml`. Pinned `urls` are optional,
since upstream matches are found by name.

## Sources

- iptv-org: <https://github.com/iptv-org/iptv>. Guovin/iptv-api: <https://github.com/Guovin/iptv-api>.
  vbskycn/iptv: <https://github.com/vbskycn/iptv>. fanmingming/live (EPG and logos):
  <https://github.com/fanmingming/live>
- Official interfaces, including the note that the 央视频 provincial API is mainland-only:
  <https://github.com/ShaoWentao/official-tv-sources>
- APTV docs: <https://docs.aptvapp.com/llms.txt> (订阅配置, 聚合配置, IPv6 FAQ)
- iTalkBB TV app (US/Canada only, $5.99/mo VIP): <https://apps.apple.com/us/app/id1463739440>.
  Plans: <https://www.italkbb.com/us/en/chinese-tv-plans>
- Geo-restriction on 央视频 and CCTV5 overseas: <https://m.tech.china.com/jujiao/2026/0710/1914353.html>.
  央视频's TV app dropping live channels: <https://sspai.com/post/85192>
- Provincial 4K launches (北京, 广东, 深圳): <https://www.ithome.com/0/886/494.htm>
