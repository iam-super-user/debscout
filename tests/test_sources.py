import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from debscout.sources import (
    Classifier,
    SourceFile,
    debian_suites,
    disable_entries,
    group_stanzas,
    parse_deb822,
    parse_one_line,
    render_mirrorlist,
    render_sources,
)

KNOWN = {"mirror.sg.gs": "/debian/", "ftp.example.edu": "/pub/linux/debian/"}

ONE_LINE = """\
# Debian GNU/Linux 13 (trixie)
deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware
deb-src http://deb.debian.org/debian trixie main contrib non-free non-free-firmware
deb [arch=amd64,i386 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://deb.debian.org/debian trixie-updates main # comment
deb http://security.debian.org/debian-security trixie-security main
#deb http://deb.debian.org/debian trixie-backports main
deb https://dl.google.com/linux/chrome/deb/ stable main
"""

DEB822 = """\
Types: deb deb-src
URIs: https://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp

Types: deb
URIs: https://security.debian.org/debian-security
Suites: trixie-security
Components: main non-free-firmware
Enabled: yes
Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp

Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie-backports
Components: main
Enabled: no
"""

EMBEDDED_KEY = """\
X-Repolib-Name: Debian
Types: deb
URIs: http://deb.debian.org/debian
Suites: trixie
Components: main
Signed-By:
 -----BEGIN PGP PUBLIC KEY BLOCK-----
 .
 mQINBGPL0BUBEADmW5NdOOHwPIJlgPu6JDcKw/NZJPR8lsD9K7UgrQVGhTrgUf0n
 -----END PGP PUBLIC KEY BLOCK-----
"""


class ParsingTests(unittest.TestCase):
    def test_one_line(self):
        entries = parse_one_line(Path("sources.list"), ONE_LINE)
        self.assertEqual(len(entries), 5)
        self.assertEqual([e.index for e in entries], [1, 2, 3, 4, 6])
        updates = entries[2]
        self.assertEqual(updates.suites, ["trixie-updates"])
        self.assertEqual(updates.components, ["main"])  # trailing comment dropped
        self.assertEqual(
            updates.options,
            {"Architectures": "amd64 i386", "Signed-By": "/usr/share/keyrings/debian-archive-keyring.gpg"},
        )
        self.assertEqual(entries[1].types, ["deb-src"])

    def test_one_line_add_remove_options(self):
        entries = parse_one_line(Path("x.list"), "deb [arch+=i386 lang-=de] http://deb.debian.org/debian trixie main\n")
        self.assertEqual(entries[0].options, {"Architectures-Add": "i386", "Languages-Remove": "de"})

    def test_deb822(self):
        entries = parse_deb822(Path("debian.sources"), DEB822)
        self.assertEqual(len(entries), 3)
        first = entries[0]
        self.assertEqual(first.types, ["deb", "deb-src"])
        self.assertEqual(first.suites, ["trixie", "trixie-updates"])
        self.assertEqual(first.options, {"Signed-By": "/usr/share/keyrings/debian-archive-keyring.pgp"})
        self.assertTrue(entries[1].enabled)
        self.assertFalse(entries[2].enabled)

    def test_deb822_embedded_key_kept_and_x_fields_dropped(self):
        (entry,) = parse_deb822(Path("debian.sources"), EMBEDDED_KEY)
        self.assertNotIn("X-Repolib-Name", entry.options)
        key = entry.options["Signed-By"]
        self.assertTrue(key.startswith("\n -----BEGIN PGP PUBLIC KEY BLOCK-----"))
        self.assertTrue(key.endswith(" -----END PGP PUBLIC KEY BLOCK-----"))


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.c = Classifier(self.root, KNOWN)

    def tearDown(self):
        self.tmp.cleanup()

    def test_uris(self):
        cases = {
            "http://deb.debian.org/debian": "debian",
            "https://deb.debian.org/debian/": "debian",
            "http://ftp.us.debian.org/debian/": "debian",
            "https://cdn-aws.deb.debian.org/debian": "debian",
            "http://mirror.sg.gs/debian/": "debian",
            "http://ftp.example.edu/pub/linux/debian": "debian",
            "http://security.debian.org/debian-security": "security",
            "http://security.debian.org/": "security",
            "http://deb.debian.org/debian-security/": "security",
            "http://deb.debian.org/debian-debug": None,
            "http://archive.debian.org/debian": None,
            "https://snapshot.debian.org/archive/debian/20250101T000000Z": None,
            "https://dl.google.com/linux/chrome/deb/": None,
            "http://mirror.unknown.example/debian": None,
            "file:/srv/debian": None,
            "mirror+https://example.com/mirrors.txt": None,
        }
        for uri, expected in cases.items():
            with self.subTest(uri=uri):
                self.assertEqual(self.c.kind_of_uri(uri), expected)

    def test_mirror_file_is_followed(self):
        mirrors = self.root / "etc/apt/mirrors"
        mirrors.mkdir(parents=True)
        (mirrors / "debian.list").write_text("# comment\nhttps://cdn-aws.deb.debian.org/debian/\tpriority:1\n")
        self.assertEqual(self.c.kind_of_uri("mirror+file:/etc/apt/mirrors/debian.list"), "debian")
        self.assertIsNone(self.c.kind_of_uri("mirror+file:/etc/apt/mirrors/missing.list"))

    def test_entries(self):
        entries = parse_one_line(Path("sources.list"), ONE_LINE)
        kinds = [self.c.classify(e) for e in entries]
        self.assertEqual(kinds, ["debian", "debian", "debian", "security", None])
        bad = parse_one_line(Path("x.list"), "deb http://deb.debian.org/debian trixie-security main\n")
        self.assertIsNone(self.c.classify(bad[0]))

    def test_debian_suites(self):
        files = [SourceFile(Path("a.list"), "list", ONE_LINE, parse_one_line(Path("a.list"), ONE_LINE))]
        self.assertEqual(debian_suites(files, self.c), ["trixie", "trixie-updates"])


class EditingTests(unittest.TestCase):
    def test_disable_one_line(self):
        sf = SourceFile(Path("sources.list"), "list", ONE_LINE, parse_one_line(Path("sources.list"), ONE_LINE))
        new = disable_entries(sf, [sf.entries[0], sf.entries[3]])
        old_lines, new_lines = ONE_LINE.splitlines(), new.splitlines()
        self.assertEqual(new_lines[1], "# Disabled by debscout: " + old_lines[1])
        self.assertEqual(new_lines[4], "# Disabled by debscout: " + old_lines[4])
        for i in (0, 2, 3, 5, 6):
            self.assertEqual(new_lines[i], old_lines[i])
        self.assertEqual(len(parse_one_line(Path("x"), new)), 3)

    def test_disable_deb822(self):
        sf = SourceFile(Path("d.sources"), "sources", DEB822, parse_deb822(Path("d.sources"), DEB822))
        new = disable_entries(sf, sf.entries[:2])
        entries = parse_deb822(Path("d.sources"), new)
        self.assertEqual([e.enabled for e in entries], [False, False, False])
        self.assertEqual(new.count("Enabled: no"), 3)
        self.assertNotIn("Enabled: yes", new)

    def test_disable_deb822_without_trailing_newline(self):
        text = "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie\nComponents: main"
        sf = SourceFile(Path("d.sources"), "sources", text, parse_deb822(Path("d.sources"), text))
        entries = parse_deb822(Path("d.sources"), disable_entries(sf, sf.entries))
        self.assertFalse(entries[0].enabled)

    def test_group_and_render(self):
        entries = parse_one_line(Path("sources.list"), ONE_LINE)
        c = Classifier(Path("/nonexistent"), KNOWN)
        selected = [(c.classify(e), e) for e in entries if c.classify(e)]
        stanzas = group_stanzas(selected)
        self.assertEqual(len(stanzas), 3)
        self.assertEqual(stanzas[0].types, ["deb", "deb-src"])
        self.assertEqual(stanzas[0].suites, ["trixie"])
        self.assertEqual(stanzas[-1].kind, "security")
        text = render_sources(stanzas, datetime(2026, 11, 7, tzinfo=timezone.utc))
        self.assertIn("URIs: mirror+file:/etc/apt/mirrors/debscout-debian.list", text)
        self.assertIn("URIs: mirror+file:/etc/apt/mirrors/debscout-debian-security.list", text)
        self.assertIn("Architectures: amd64 i386", text)
        reparsed = parse_deb822(Path("debscout.sources"), text)
        self.assertEqual(len(reparsed), 3)

    def test_same_suite_merges(self):
        text = (
            "deb http://deb.debian.org/debian trixie main\n"
            "deb http://deb.debian.org/debian trixie-updates main\n"
            "deb-src http://deb.debian.org/debian trixie main\n"
        )
        entries = parse_one_line(Path("x.list"), text)
        stanzas = group_stanzas([("debian", e) for e in entries])
        self.assertEqual([(s.types, s.suites) for s in stanzas], [(["deb", "deb-src"], ["trixie"]), (["deb"], ["trixie-updates"])])

    def test_render_mirrorlist(self):
        text = render_mirrorlist(["https://a/debian/", "http://b/debian/"], "Debian archive")
        lines = [l for l in text.splitlines() if not l.startswith("#")]
        self.assertEqual(lines, ["https://a/debian/\tpriority:1", "http://b/debian/\tpriority:2"])


if __name__ == "__main__":
    unittest.main()
