"""End-to-end apply / re-apply / undo against a fake filesystem root."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from debscout import apply as ap
from debscout.sources import MANAGED_SOURCES, MIRRORLIST_DEBIAN, MIRRORLIST_SECURITY, Classifier, debian_suites, read_sources

SOURCES_LIST = """\
deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware
deb http://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware
deb http://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
"""

DEBIAN_SOURCES = """\
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie-backports
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp
"""

GOOGLE = "deb [arch=amd64] https://dl.google.com/linux/chrome/deb/ stable main\n"
DEBIAN_URLS = ["https://cdn-aws.deb.debian.org/debian/", "http://mirror.sg.gs/debian/", "http://deb.debian.org/debian/"]
SECURITY_URLS = ["https://cdn-aws.deb.debian.org/debian-security/", "http://security.debian.org/debian-security/"]


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "etc/apt/sources.list.d").mkdir(parents=True)
        self.originals = {
            "etc/apt/sources.list": SOURCES_LIST,
            "etc/apt/sources.list.d/debian.sources": DEBIAN_SOURCES,
            "etc/apt/sources.list.d/google-chrome.list": GOOGLE,
        }
        for rel, text in self.originals.items():
            (self.root / rel).write_text(text)
        self.classifier = Classifier(self.root, {"mirror.sg.gs": "/debian/"})

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, urls=DEBIAN_URLS):
        return ap.build_plan(self.root, read_sources(self.root), self.classifier, urls, SECURITY_URLS)

    def read(self, rel):
        return (self.root / rel).read_text()

    def assert_original_state(self):
        for rel, text in self.originals.items():
            self.assertEqual(self.read(rel), text, rel)
        for rel in (MANAGED_SOURCES, MIRRORLIST_DEBIAN, MIRRORLIST_SECURITY, ap.STATE_DIR):
            self.assertFalse((self.root / rel).exists(), rel)

    def test_apply_then_undo(self):
        ap.apply_plan(self.plan(), run_update=False, log=lambda *_: None)

        managed = self.read(MANAGED_SOURCES)
        self.assertIn("Suites: trixie trixie-updates\n", managed)
        self.assertIn("Suites: trixie-backports\n", managed)
        self.assertIn("Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp", managed)
        self.assertIn("Suites: trixie-security\n", managed)
        self.assertIn("https://cdn-aws.deb.debian.org/debian/\tpriority:1", self.read(MIRRORLIST_DEBIAN))
        self.assertIn("http://security.debian.org/debian-security/\tpriority:2", self.read(MIRRORLIST_SECURITY))
        self.assertEqual(self.read("etc/apt/sources.list").count("# Disabled by debscout: "), 3)
        self.assertIn("Enabled: no", self.read("etc/apt/sources.list.d/debian.sources"))
        self.assertEqual(self.read("etc/apt/sources.list.d/google-chrome.list"), GOOGLE)

        # Only debscout's own file now provides the Debian suites.
        files = read_sources(self.root)
        self.assertEqual(debian_suites(files, self.classifier), ["trixie", "trixie-updates", "trixie-backports"])

        actions = ap.undo(self.root)
        self.assertTrue(any("restored /etc/apt/sources.list" in a for a in actions))
        self.assert_original_state()

    def test_reapply_keeps_first_backup(self):
        ap.apply_plan(self.plan(), run_update=False, log=lambda *_: None)
        second = self.plan(["http://mirror.sg.gs/debian/", "http://deb.debian.org/debian/"])
        self.assertEqual(second.disabled, [])  # originals are already switched off
        ap.apply_plan(second, run_update=False, log=lambda *_: None)
        self.assertTrue(self.read(MIRRORLIST_DEBIAN).splitlines()[-2].startswith("http://mirror.sg.gs/debian/"))
        self.assertIn("Suites: trixie-backports", self.read(MANAGED_SOURCES))
        ap.undo(self.root)
        self.assert_original_state()

    def test_undo_keeps_later_user_edits(self):
        ap.apply_plan(self.plan(), run_update=False, log=lambda *_: None)
        edited = self.read("etc/apt/sources.list") + "deb http://example.com/repo stable main\n"
        (self.root / "etc/apt/sources.list").write_text(edited)
        actions = ap.undo(self.root)
        self.assertTrue(any("kept your later edits" in a for a in actions))
        self.assertEqual(self.read("etc/apt/sources.list.debscout-saved"), edited)
        self.assertEqual(self.read("etc/apt/sources.list"), SOURCES_LIST)

    def test_failed_apt_update_rolls_back(self):
        with mock.patch.object(ap, "run_apt_update", return_value=100) as update:
            with self.assertRaises(ap.DebscoutError):
                ap.apply_plan(self.plan(), run_update=True, log=lambda *_: None)
        self.assertEqual(update.call_count, 2)  # the failed check, then a refresh after restoring
        self.assert_original_state()

    def test_no_debian_entries(self):
        (self.root / "etc/apt/sources.list").write_text("")
        (self.root / "etc/apt/sources.list.d/debian.sources").unlink()
        with self.assertRaises(ap.DebscoutError):
            self.plan()

    def test_dry_run_diff(self):
        diff = ap.plan_diff(self.plan())
        self.assertIn("+++ /etc/apt/sources.list.d/debscout.sources", diff)
        self.assertIn("--- /dev/null", diff)
        self.assertIn("-deb http://deb.debian.org/debian trixie main", diff)
        self.assert_original_state()  # building a plan changes nothing

    def test_undo_without_state(self):
        self.assertEqual(ap.undo(self.root), [])

    def test_state_is_world_readable(self):
        ap.apply_plan(self.plan(), run_update=False, log=lambda *_: None)
        mode = (self.root / ap.STATE_DIR / ap.STATE_FILE).stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)


if __name__ == "__main__":
    unittest.main()
