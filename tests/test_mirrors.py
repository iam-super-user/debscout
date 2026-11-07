import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from debscout import mirrors as ml
from debscout.net import Sample

MASTERLIST = """\
Site: deb.debian.org
Type: GeoDNS
Archive-architecture: amd64 arm64
Archive-http: /debian/

Site: mirror.sg.gs
Country: SG Singapore
Archive-architecture: amd64 arm64 i386
Archive-http: /debian/

Site: ftp.example.edu
Country: IN India
Location: Kolkata
Archive-architecture: amd64
Archive-http: pub/linux/debian

Site: armonly.example.org
Country: JP Japan
Archive-architecture: arm64
Archive-http: /debian/

Site: cdimage.example.org
Country: SE Sweden
CDImage-http: /debian-cd/
"""

TRACE = """\
Sat Nov  7 02:30:39 UTC 2026
Creator: dak g9aa37e37
Archive serial: 2026110701
Date: Sat, 07 Nov 2026 02:30:39 +0000
"""


def mirror(site, latency=0.1, hours_old=0.0, cdn=False, status="ok"):
    now = datetime(2026, 11, 7, 2, 30, tzinfo=timezone.utc)
    return ml.Mirror(f"http://{site}/debian/", site, is_cdn=cdn, latency=latency, trace_date=now - timedelta(hours=hours_old), status=status)


class MasterlistTests(unittest.TestCase):
    def test_parse(self):
        mirrors = ml.parse_masterlist(MASTERLIST, "amd64")
        self.assertEqual([m.site for m in mirrors], ["mirror.sg.gs", "ftp.example.edu"])
        self.assertEqual(mirrors[1].url, "http://ftp.example.edu/pub/linux/debian/")
        self.assertEqual((mirrors[1].country_code, mirrors[1].country), ("IN", "India"))

    def test_arch_filter(self):
        self.assertEqual([m.site for m in ml.parse_masterlist(MASTERLIST, "arm64")], ["mirror.sg.gs", "armonly.example.org"])


class TraceTests(unittest.TestCase):
    def test_date_line(self):
        self.assertEqual(ml.parse_trace_date(TRACE), datetime(2026, 11, 7, 2, 30, 39, tzinfo=timezone.utc))

    def test_old_first_line_format(self):
        self.assertEqual(ml.parse_trace_date("Tue Jun  9 08:18:39 UTC 2026\n"), datetime(2026, 6, 9, 8, 18, 39, tzinfo=timezone.utc))

    def test_garbage(self):
        self.assertIsNone(ml.parse_trace_date("<html>not found</html>"))
        self.assertIsNone(ml.parse_trace_date(""))


class SelectionTests(unittest.TestCase):
    def test_mark_stale(self):
        ms = [mirror("a"), mirror("b", hours_old=6), mirror("c", hours_old=24 * 151), mirror("d", status="unreachable")]
        newest = ml.mark_stale(ms, 12)
        self.assertEqual(newest, ms[0].trace_date)
        self.assertEqual([m.status for m in ms], ["ok", "ok", "stale", "unreachable"])
        self.assertAlmostEqual(ms[1].age_hours, 6)

    def test_pick_for_speed_test(self):
        ms = [mirror("slow", 0.5), mirror("fast", 0.05), mirror("mid", 0.2), mirror("cdn", 0.9, cdn=True), mirror("old", 0.01, status="stale")]
        picked = ml.pick_for_speed_test(ms, 2)
        self.assertEqual([m.site for m in picked], ["cdn", "fast", "mid"])

    def test_speed_is_total_bytes_over_total_time(self):
        m = mirror("a")
        m.samples = [Sample(1_000_000, 0.1), Sample(100_000, 1.0)]  # 10 MB/s, then 100 kB/s
        self.assertAlmostEqual(m.speed, 1_100_000 / 1.1)
        self.assertTrue(m.unstable)
        m.samples = [Sample(1_000_000, 0.1), Sample(900_000, 0.1)]
        self.assertFalse(m.unstable)

    def test_security_order(self):
        aws, fastly = mirror(ml.CDN_AWS, cdn=True), mirror(ml.CDN_FASTLY, cdn=True)
        aws.samples, fastly.samples = [Sample(10, 1)], [Sample(1, 1)]
        self.assertEqual(ml.security_order([aws, fastly])[0], ml.SECURITY_URLS[ml.CDN_AWS])
        aws.samples, fastly.samples = [Sample(1, 1)], [Sample(10, 1)]
        self.assertEqual(ml.security_order([fastly, aws])[0], ml.SECURITY_URLS["security.debian.org"])
        self.assertEqual(len(ml.security_order([])), 3)

    def test_choose_archive_mirrors(self):
        ranked = [mirror("a"), mirror("lacks-updates"), mirror("b"), mirror("c")]
        exists = lambda url: "lacks-updates" not in url or "trixie-updates" not in url
        warnings = []
        with mock.patch.object(ml, "url_exists", side_effect=exists):
            chosen, urls = ml.choose_archive_mirrors(ranked, 2, ["trixie", "trixie-updates"], warn=warnings.append)
        self.assertEqual([m.site for m in chosen], ["a", "b"])
        self.assertEqual(urls, ["http://a/debian/", "http://b/debian/", ml.FALLBACK_URL])
        self.assertEqual(len(warnings), 1)

    def test_fallback_not_duplicated(self):
        cdn = ml.Mirror(ml.FALLBACK_URL, ml.CDN_FASTLY, is_cdn=True)
        with mock.patch.object(ml, "url_exists", return_value=True):
            _, urls = ml.choose_archive_mirrors([cdn], 3, ["trixie"])
        self.assertEqual(urls, [ml.FALLBACK_URL])


if __name__ == "__main__":
    unittest.main()
