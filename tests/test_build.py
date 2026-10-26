import datetime as dt
import functools
import http.server
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build  # noqa: E402

SETTINGS = {"uhd_group": "4K 超高清", "uhd_height": 2000, "group_order": ["4K 超高清", "央视", "卫视"]}


def source(url="http://x/a.m3u8", *, height=1080, official=False, headroom=5.0, startup=1.0,
           stale=False, last_ok="2026-10-25T00:00:00+00:00"):
    return build.Source(url=url, official=official, width=height * 16 // 9, height=height, codec="h264",
                        bitrate=3.0, headroom=headroom, startup=startup, segment=10, last_ok=last_ok,
                        stale=stale)


class NormTest(unittest.TestCase):
    def test_spellings_of_the_same_channel_collapse(self):
        for name in ["CCTV-1", "CCTV1 HD", "cctv-1高清", "CCTV-1 (720p)", "CCTV1 [Geo-blocked]", "CCTV1频道"]:
            self.assertEqual(build.norm(name), "CCTV1", name)

    def test_distinct_channels_stay_distinct(self):
        self.assertNotEqual(build.norm("CCTV-1"), build.norm("CCTV-13"))
        self.assertEqual(build.norm("CCTV-4K"), "CCTV4K")
        self.assertEqual(build.norm("CCTV-5+"), "CCTV5+")
        self.assertEqual(build.norm("Zhejiang Satellite TV (1080p)"), "ZHEJIANGSATELLITETV")

    def test_english_names_become_aliases(self):
        ch = build.Channel(name="浙江卫视", tvg="浙江卫视", group="卫视", en="Zhejiang")
        self.assertTrue({"浙江卫视", "ZHEJIANGSATELLITETV", "ZHEJIANGTV"} <= ch.keys())


class ConfigTest(unittest.TestCase):
    def test_shipped_catalog_loads(self):
        settings, upstreams, channels = build.load_config(build.ROOT / "channels.toml")
        self.assertIn(settings["uhd_group"], settings["group_order"])
        self.assertTrue(upstreams)
        names = [c.name for c in channels]
        self.assertEqual(len(names), len(set(names)))
        for c in channels:
            self.assertTrue(all(build.usable_url(u) for u in c.urls), c.name)

    def test_alias_claimed_twice_is_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8") as f:
            f.write('[settings]\n[[channels]]\nname = "A"\ntvg = "X"\ngroup = "g"\n'
                    '[[channels]]\nname = "B"\ntvg = "Y"\ngroup = "g"\naliases = ["x"]\n')
        with self.assertRaises(SystemExit):
            build.load_config(Path(f.name))


class ParseTest(unittest.TestCase):
    def test_parse_m3u(self):
        text = ('#EXTM3U x-tvg-url="e.xml"\n'
                '#EXTINF:-1 tvg-id="CCTV1" tvg-logo="https://l/1.png" group-title="央视",CCTV-1 综合\n'
                '#EXTVLCOPT:http-user-agent=foo\n'
                'http://a/1.m3u8\n'
                '#EXTINF:-1 tvg-name="浙江卫视",\n'
                'http://a/2.m3u8\n'
                'http://orphan/without/extinf.m3u8\n')
        entries = build.parse_m3u(text)
        self.assertEqual([(e.name, e.url) for e in entries],
                         [("CCTV-1 综合", "http://a/1.m3u8"), ("浙江卫视", "http://a/2.m3u8")])
        self.assertEqual(entries[0].logo, "https://l/1.png")

    def test_usable_url(self):
        self.assertTrue(build.usable_url("https://a.com/x.m3u8"))
        self.assertTrue(build.usable_url("http://1.2.3.4:82/live/x.m3u8"))
        self.assertFalse(build.usable_url("http://[2409:8087::1]/x.m3u8"))
        self.assertFalse(build.usable_url("rtp://239.1.1.1:5000"))
        self.assertFalse(build.usable_url("p3p://whatever"))

    def test_is_official(self):
        hosts = ["cztv.com", "*cgtn*"]
        self.assertTrue(build.is_official("https://ali-m-l.cztv.com/x.m3u8", hosts))
        self.assertTrue(build.is_official("https://amg00405-rakutentv-cgtn-x.amagi.tv/m.m3u8", hosts))
        self.assertFalse(build.is_official("https://notcztv.com/x.m3u8", hosts))
        self.assertFalse(build.is_official("http://74.91.26.218:82/live/cctv1.m3u8", hosts))


class RankTest(unittest.TestCase):
    def test_order(self):
        relay_1080 = source("relay1080", startup=0.5)
        official_1080 = source("official1080", official=True, startup=3.0)
        uhd = source("uhd", height=2160)
        choppy_uhd = source("choppy", height=2160, headroom=0.8)
        stale = source("stale", height=2160, official=True, stale=True)
        ranked = build.rank([stale, choppy_uhd, relay_1080, official_1080, uhd], min_headroom=1.3)
        self.assertEqual([s.url for s in ranked], ["uhd", "official1080", "relay1080", "choppy", "stale"])

    def test_faster_start_breaks_ties(self):
        ranked = build.rank([source("slow", startup=4), source("fast", startup=0.7)], min_headroom=1.3)
        self.assertEqual(ranked[0].url, "fast")

    def test_carry_over_expires(self):
        now = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)
        fresh = vars(source("fresh", last_ok="2026-10-25T00:00:00+00:00"))
        old = vars(source("old", last_ok="2026-10-01T00:00:00+00:00"))
        kept = build.carry_over([fresh, old], now, stale_days=3)
        self.assertEqual([s.url for s in kept], ["fresh"])
        self.assertTrue(kept[0].stale)


class PlaylistTest(unittest.TestCase):
    def test_uhd_sources_also_listed_in_4k_group_first(self):
        cctv1 = build.Channel(name="CCTV-1 综合", tvg="CCTV1", group="央视")
        hunan = build.Channel(name="湖南卫视", tvg="湖南卫视", group="卫视")
        results = [
            (cctv1, "", [source("cctv1-a"), source("cctv1-b")]),
            (hunan, "logo.png", [source("hunan-4k", height=2160), source("hunan-hd")]),
        ]
        rows = build.playlist_rows(results, SETTINGS, lambda s: s[:1])
        self.assertEqual([(r[2], r[3], r[4].url) for r in rows], [
            ("4K 超高清", "湖南卫视 4K", "hunan-4k"),
            ("央视", "CCTV-1 综合", "cctv1-a"),
            ("卫视", "湖南卫视", "hunan-4k"),
        ])
        backup = build.playlist_rows(results, SETTINGS, lambda s: s[1:2])
        self.assertEqual([r[4].url for r in backup], ["cctv1-b", "hunan-hd"])

    def test_render_m3u(self):
        ch = build.Channel(name="浙江卫视", tvg="浙江卫视", group="卫视")
        text = build.render_m3u("https://epg/e.xml", [(ch, "https://l/z.png", "卫视", "浙江卫视", source("http://z"))])
        self.assertEqual(text, '#EXTM3U x-tvg-url="https://epg/e.xml"\n'
                               '#EXTINF:-1 tvg-id="浙江卫视" tvg-name="浙江卫视" tvg-logo="https://l/z.png" '
                               'group-title="卫视",浙江卫视\nhttp://z\n')


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class ProbeTest(unittest.TestCase):
    """Serve a real (tiny) HLS stream from localhost and probe it."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = Path(cls.dir.name)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                        "-t", "6", "-c:v", "libx264", "-g", "50", "-f", "hls", "-hls_time", "2",
                        "-hls_list_size", "0", str(root / "vod.m3u8")], check=True)
        live = (root / "vod.m3u8").read_text().replace("#EXT-X-ENDLIST\n", "")
        (root / "live.m3u8").write_text(live)
        (root / "master.m3u8").write_text("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nmissing.m3u8\n"
                                          "#EXT-X-STREAM-INF:BANDWIDTH=900000\nlive.m3u8\n")
        (root / "notes.txt").write_text("hello")
        handler = functools.partial(QuietHandler, directory=str(root))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.dir.cleanup()

    def test_live_stream_through_master_playlist(self):
        p = build.probe(f"{self.base}/master.m3u8", timeout=10)
        self.assertTrue(p.ok, p.error)
        self.assertEqual((p.width, p.height, p.codec), (640, 360, "h264"))
        self.assertEqual(p.segment, 2)
        self.assertGreater(p.headroom, 1)

    def test_rejects_vod_missing_and_non_hls(self):
        self.assertIn("VOD", build.probe(f"{self.base}/vod.m3u8", timeout=5).error)
        self.assertIn("404", build.probe(f"{self.base}/nope.m3u8", timeout=5).error)
        self.assertIn("not an HLS", build.probe(f"{self.base}/notes.txt", timeout=5).error)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    unittest.main()
