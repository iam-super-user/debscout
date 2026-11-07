"""Freshness checks and speed tests against a local HTTP server."""

import functools
import os
import socket
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from debscout import mirrors as ml
from debscout.net import connect_time

FRESH = "Date: Sat, 07 Nov 2026 02:30:39 +0000\n"
STALE = "Date: Tue, 09 Jun 2026 08:18:39 +0000\n"


class QuietHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/moved/"):  # a mirror that redirects elsewhere
            self.send_response(301)
            self.send_header("Location", f"http://{self.headers['Host']}/fresh/{self.path[len('/moved/'):]}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *args):
        pass


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # the speed test hangs up early on purpose


def make_archive(base: Path, trace: str, with_packages: bool = True) -> None:
    (base / "project/trace").mkdir(parents=True)
    (base / "project/trace/master").write_text(trace)
    dists = base / "dists/trixie"
    (dists / "main/binary-amd64").mkdir(parents=True)
    (dists / "InRelease").write_text("Origin: Debian\n")
    if with_packages:
        (dists / "main/binary-amd64/Packages.xz").write_bytes(os.urandom(300_000))


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        make_archive(root / "fresh", FRESH)
        make_archive(root / "fresh2", FRESH)
        make_archive(root / "stale", STALE)
        make_archive(root / "nopackages", FRESH, with_packages=False)
        handler = functools.partial(QuietHandler, directory=str(root))
        cls.server = QuietServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        with socket.socket() as s:  # a port with nothing listening
            s.bind(("127.0.0.1", 0))
            cls.closed_port = s.getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def mirror(self, name):
        return ml.Mirror(f"{self.base}/{name}/", name)

    def test_full_pipeline(self):
        mirrors = [self.mirror(n) for n in ("fresh", "fresh2", "stale", "nopackages", "absent")]
        mirrors.append(ml.Mirror(f"http://127.0.0.1:{self.closed_port}/debian/", "closed"))
        ml.check_mirrors(mirrors, timeout=2, workers=4)
        ml.mark_stale(mirrors, 12)
        status = {m.site: m.status for m in mirrors}
        self.assertEqual(status, {"fresh": "ok", "fresh2": "ok", "stale": "stale", "nopackages": "ok", "absent": "unreachable", "closed": "unreachable"})
        self.assertEqual(next(m for m in mirrors if m.site == "absent").error, "HTTP 404")

        to_test = ml.pick_for_speed_test(mirrors, 10)
        ranked = ml.speed_test(to_test, "trixie", "amd64", rounds=2, max_bytes=200_000, max_seconds=5)
        self.assertEqual(sorted(m.site for m in ranked), ["fresh", "fresh2"])
        for m in ranked:
            self.assertEqual([s.nbytes for s in m.samples], [200_000, 200_000])
            self.assertGreater(m.speed, 0)
        failed = next(m for m in mirrors if m.site == "nopackages")
        self.assertEqual((failed.status, failed.error, len(failed.samples)), ("failed", "HTTP 404", 1))

        chosen, urls = ml.choose_archive_mirrors(ranked, 3, ["trixie"])
        self.assertEqual(len(chosen), 2)
        self.assertEqual(urls[-1], ml.FALLBACK_URL)
        none, _ = ml.choose_archive_mirrors(ranked, 3, ["trixie-updates"], warn=lambda _: None)
        self.assertEqual(none, [])

    def test_redirect_is_followed_and_recorded(self):
        moved = self.mirror("moved")
        ml.check_mirrors([moved], timeout=2)
        self.assertEqual(moved.status, "ok")
        self.assertEqual(moved.url, f"{self.base}/fresh/")

    def test_connect_time(self):
        self.assertIsInstance(connect_time(self.base + "/"), float)
        self.assertIsNone(connect_time(f"http://127.0.0.1:{self.closed_port}/", timeout=1))
        self.assertIsNone(connect_time("http://no-such-host.invalid/", timeout=1))


if __name__ == "__main__":
    unittest.main()
