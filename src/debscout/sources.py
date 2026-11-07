"""Read apt sources (one-line .list and deb822 .sources) and generate debscout's own."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from . import __version__

# Paths relative to the filesystem root.
MANAGED_SOURCES = "etc/apt/sources.list.d/debscout.sources"
MIRRORLIST_DEBIAN = "etc/apt/mirrors/debscout-debian.list"
MIRRORLIST_SECURITY = "etc/apt/mirrors/debscout-debian-security.list"

ONE_LINE = re.compile(r"^(deb|deb-src)\s+(?:\[([^\]]*)\]\s+)?(\S+)\s+(\S+)(?:\s+(.*))?$")
OPTION = re.compile(r"^([A-Za-z0-9-]+?)([+-]?)=(.*)$")

# One-line option name -> deb822 field name (see sources.list(5)).
OPTION_NAMES = {
    "arch": "Architectures",
    "lang": "Languages",
    "target": "Targets",
    "pdiffs": "PDiffs",
    "by-hash": "By-Hash",
    "allow-insecure": "Allow-Insecure",
    "allow-weak": "Allow-Weak",
    "allow-downgrade-to-insecure": "Allow-Downgrade-To-Insecure",
    "trusted": "Trusted",
    "signed-by": "Signed-By",
    "check-valid-until": "Check-Valid-Until",
    "valid-until-min": "Valid-Until-Min",
    "valid-until-max": "Valid-Until-Max",
    "check-date": "Check-Date",
    "date-max-future": "Date-Max-Future",
    "inrelease-path": "InRelease-Path",
    "snapshot": "Snapshot",
}
CANONICAL = {name.lower(): name for name in OPTION_NAMES.values()}
LIST_FIELDS = {"Architectures", "Languages", "Targets"}
CORE_FIELDS = {"types", "uris", "suites", "components", "enabled"}

# Debian hosts whose content ordinary mirrors don't carry.
NOT_MIRRORED_HOSTS = {"archive.debian.org", "snapshot.debian.org", "incoming.debian.org"}


@dataclass
class Entry:
    path: Path
    fmt: str  # "list" or "sources"
    index: int  # line number (list) or paragraph number (sources), from 0
    types: list[str]
    uris: list[str]
    suites: list[str]
    components: list[str]
    options: dict[str, str] = field(default_factory=dict)  # deb822 field name -> value
    enabled: bool = True


@dataclass
class SourceFile:
    path: Path
    fmt: str
    text: str
    entries: list[Entry]


@dataclass
class Paragraph:
    start: int
    end: int  # last line of the paragraph, inclusive
    fields: dict[str, str] = field(default_factory=dict)  # lower-case name -> value
    names: dict[str, str] = field(default_factory=dict)  # lower-case name -> name as written
    enabled_line: Optional[int] = None


@dataclass
class Stanza:
    kind: str  # "debian" or "security"
    types: list[str]
    suites: list[str]
    components: list[str]
    options: list[tuple[str, str]]


def canonical_field(name: str) -> str:
    return CANONICAL.get(name.lower(), name)


def option_field(key: str, op: str) -> str:
    base = OPTION_NAMES.get(key.lower()) or "-".join(p.capitalize() for p in key.split("-"))
    return base + {"+": "-Add", "-": "-Remove"}.get(op, "")


def parse_one_line(path: Path, text: str) -> list[Entry]:
    entries = []
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"\s+#.*$", "", line)
        match = ONE_LINE.match(line)
        if not match:
            continue
        kind, opts, uri, suite, comps = match.groups()
        options: dict[str, str] = {}
        for token in (opts or "").split():
            om = OPTION.match(token)
            if not om:
                continue
            key, op, value = om.groups()
            name = option_field(key, op)
            if name.replace("-Add", "").replace("-Remove", "") in LIST_FIELDS:
                value = " ".join(v for v in value.split(",") if v)
            options[name] = value
        entries.append(Entry(path, "list", index, [kind], [uri], [suite], (comps or "").split(), options))
    return entries


def parse_paragraphs(text: str) -> list[Paragraph]:
    paragraphs: list[Paragraph] = []
    current: Optional[Paragraph] = None
    last: Optional[str] = None
    for index, raw in enumerate(text.splitlines()):
        if not raw.strip():
            if current:
                paragraphs.append(current)
            current, last = None, None
            continue
        if raw.lstrip().startswith("#"):
            if current:
                current.end = index
            continue
        if raw[0] in " \t":
            if current and last:
                current.fields[last] += "\n" + raw
                current.end = index
            continue
        key, sep, value = raw.partition(":")
        if not sep:
            continue
        if current is None:
            current = Paragraph(start=index, end=index)
        name = key.strip()
        last = name.lower()
        current.fields[last] = value.strip()
        current.names[last] = name
        current.end = index
        if last == "enabled":
            current.enabled_line = index
    if current:
        paragraphs.append(current)
    return paragraphs


def parse_deb822(path: Path, text: str) -> list[Entry]:
    entries = []
    for index, para in enumerate(parse_paragraphs(text)):
        f = para.fields
        if not {"types", "uris", "suites"} <= f.keys():
            continue
        enabled = f.get("enabled", "yes").strip().lower() not in ("no", "false", "0")
        options = {
            canonical_field(para.names[key]): value
            for key, value in f.items()
            if key not in CORE_FIELDS and not key.startswith("x-")
        }
        entries.append(
            Entry(
                path,
                "sources",
                index,
                f["types"].split(),
                f["uris"].split(),
                f["suites"].split(),
                f.get("components", "").split(),
                options,
                enabled,
            )
        )
    return entries


def source_files(root: Path) -> list[Path]:
    files = []
    main = root / "etc/apt/sources.list"
    if main.is_file():
        files.append(main)
    folder = root / "etc/apt/sources.list.d"
    if folder.is_dir():
        files += sorted(p for p in folder.iterdir() if p.is_file() and p.suffix in (".list", ".sources"))
    return files


def read_sources(root: Path) -> list[SourceFile]:
    result = []
    for path in source_files(root):
        text = path.read_text(errors="replace")
        if path.suffix == ".sources":
            result.append(SourceFile(path, "sources", text, parse_deb822(path, text)))
        else:
            result.append(SourceFile(path, "list", text, parse_one_line(path, text)))
    return result


class Classifier:
    """Decide whether an apt entry points at the Debian archive or Debian security."""

    def __init__(self, root: Path, known_sites: dict[str, str]):
        self.root = root
        self.known = {site.lower(): "/" + path.strip("/") for site, path in known_sites.items()}

    def _first_mirror(self, target: str) -> Optional[str]:
        path = self.root / target.lstrip("/")
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return None
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                return line.split()[0]
        return None

    def kind_of_uri(self, uri: str, follow: bool = True) -> Optional[str]:
        if uri.startswith("mirror+file:"):
            first = self._first_mirror(uri[len("mirror+file:"):]) if follow else None
            return self.kind_of_uri(first, follow=False) if first else None
        parts = urlsplit(uri)
        if parts.scheme not in ("http", "https"):
            return None
        host = (parts.hostname or "").lower()
        if not host or host in NOT_MIRRORED_HOSTS:
            return None
        if not (host in self.known or host.endswith(".debian.org") or host == "http.debian.net"):
            return None
        path = "/" + parts.path.strip("/")
        if host == "security.debian.org" or path.endswith("/debian-security"):
            return "security"
        if path.endswith("/debian") or self.known.get(host) == path:
            return "debian"
        return None

    def classify(self, entry: Entry) -> Optional[str]:
        kinds = {self.kind_of_uri(uri) for uri in entry.uris}
        if len(kinds) != 1 or None in kinds or not entry.components or "Snapshot" in entry.options:
            return None
        kind = kinds.pop()
        security_suites = [s for s in entry.suites if s.endswith("-security") or s.endswith("/updates")]
        if kind == "security" and len(security_suites) != len(entry.suites):
            return None
        if kind == "debian" and security_suites:
            return None
        return kind


def debian_suites(files: list[SourceFile], classifier: Classifier) -> list[str]:
    """Suites of enabled Debian archive entries (not security), in order."""
    suites: list[str] = []
    for sf in files:
        for entry in sf.entries:
            if entry.enabled and classifier.classify(entry) == "debian":
                suites += [s for s in entry.suites if s not in suites]
    return suites


def group_stanzas(selected: list[tuple[str, Entry]]) -> list[Stanza]:
    """Merge entries into as few deb822 stanzas as possible, keeping their meaning."""
    by_suite: dict[tuple, list[str]] = {}
    for kind, entry in selected:
        options = tuple(sorted(entry.options.items()))
        for suite in entry.suites:
            types = by_suite.setdefault((kind, suite, tuple(entry.components), options), [])
            types += [t for t in entry.types if t not in types]
    groups: dict[tuple, list[str]] = {}
    for (kind, suite, components, options), types in by_suite.items():
        key = (kind, tuple(sorted(types)), components, options)
        groups.setdefault(key, []).append(suite)
    stanzas = [
        Stanza(kind, list(types), suites, list(components), list(options))
        for (kind, types, components, options), suites in groups.items()
    ]
    return sorted(stanzas, key=lambda s: s.kind != "debian")


def render_sources(stanzas: list[Stanza], now: datetime) -> str:
    lines = [
        f"# Generated by debscout {__version__} on {now:%Y-%m-%d %H:%M} UTC.",
        "# Mirrors are listed fastest first in /etc/apt/mirrors/debscout-*.list.",
        "# Re-rank with 'sudo debscout --apply', or revert with 'sudo debscout --undo'.",
    ]
    for stanza in stanzas:
        mirrorlist = MIRRORLIST_DEBIAN if stanza.kind == "debian" else MIRRORLIST_SECURITY
        lines += [
            "",
            f"Types: {' '.join(stanza.types)}",
            f"URIs: mirror+file:/{mirrorlist}",
            f"Suites: {' '.join(stanza.suites)}",
            f"Components: {' '.join(stanza.components)}",
        ]
        lines += [f"{name}: {value}" for name, value in stanza.options]
    return "\n".join(lines) + "\n"


def render_mirrorlist(urls: list[str], title: str) -> str:
    lines = [
        f"# {title} mirrors chosen by debscout {__version__}.",
        "# apt tries the lowest priority number first and falls back down the list.",
    ]
    lines += [f"{url}\tpriority:{n}" for n, url in enumerate(urls, 1)]
    return "\n".join(lines) + "\n"


def disable_entries(sf: SourceFile, entries: list[Entry]) -> str:
    """Return the file's text with the given entries switched off."""
    lines = sf.text.splitlines(keepends=True)
    if sf.fmt == "list":
        for entry in entries:
            lines[entry.index] = "# Disabled by debscout: " + lines[entry.index]
        return "".join(lines)
    paragraphs = parse_paragraphs(sf.text)
    inserts = []
    for entry in entries:
        para = paragraphs[entry.index]
        if para.enabled_line is not None:
            newline = "\n" if lines[para.enabled_line].endswith("\n") else ""
            lines[para.enabled_line] = "Enabled: no" + newline
        else:
            inserts.append(para.end)
    for end in sorted(inserts, reverse=True):
        if not lines[end].endswith("\n"):
            lines[end] += "\n"
        lines[end + 1 : end + 1] = [
            "# Disabled by debscout; 'sudo debscout --undo' restores it.\n",
            "Enabled: no\n",
        ]
    return "".join(lines)
