"""Find Debian mirrors, check that they are up to date, and measure their speed."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Optional

from .net import FetchError, Sample, connect_time, fetch_text, sample_download, url_exists

TRACE_PATH = "project/trace/master"

MASTERLIST_URL = "https://mirror-master.debian.org/status/Mirrors.masterlist"
MASTERLIST_CACHE_SECONDS = 24 * 3600

# Masterlist entries of these types are not ordinary archive mirrors.
# deb.debian.org is one of them (GeoDNS); it is added separately as a CDN.
NON_MIRROR_TYPES = {"geodns", "origin", "roundrobindns"}

CDN_AWS = "cdn-aws.deb.debian.org"
CDN_FASTLY = "deb.debian.org"
FALLBACK_URL = "http://deb.debian.org/debian/"

# Security updates only come from Debian's own hosts, never from ordinary mirrors.
SECURITY_URLS = {
    CDN_AWS: "https://cdn-aws.deb.debian.org/debian-security/",
    "security.debian.org": "http://security.debian.org/debian-security/",
    CDN_FASTLY: "http://deb.debian.org/debian-security/",
}


@dataclass
class Mirror:
    url: str  # base URL of the archive, ending in "/"
    site: str
    country_code: str = ""
    country: str = ""
    is_cdn: bool = False
    latency: Optional[float] = None  # best TCP connect time, in seconds
    trace_date: Optional[datetime] = None
    age_hours: Optional[float] = None  # how far behind the newest mirror
    samples: list[Sample] = field(default_factory=list)
    status: str = "new"  # new, ok, stale, unreachable, failed, tested
    error: Optional[str] = None

    @property
    def speed(self) -> float:
        """Average speed over all samples, in bytes per second.

        Total bytes divided by total time, so one slow sample drags the
        average down the way it would slow down a real apt run.
        """
        seconds = sum(s.seconds for s in self.samples)
        return sum(s.nbytes for s in self.samples) / seconds if seconds > 0 else 0.0

    @property
    def unstable(self) -> bool:
        """True if speeds varied by more than 4x between rounds."""
        speeds = [s.speed for s in self.samples if s.nbytes]
        return len(speeds) > 1 and max(speeds) > 4 * min(speeds)

    @property
    def location(self) -> str:
        return "CDN" if self.is_cdn else (self.country_code or "?")


def builtin_cdns() -> list[Mirror]:
    """Debian's two CDNs. Neither is an ordinary entry in the masterlist."""
    return [
        Mirror(f"https://{CDN_AWS}/debian/", CDN_AWS, country="Amazon CloudFront", is_cdn=True),
        Mirror(FALLBACK_URL, CDN_FASTLY, country="Fastly", is_cdn=True),
    ]


def parse_control(text: str) -> list[dict[str, list[str]]]:
    """Parse deb822 control-style text into stanzas. Repeated fields keep every value."""
    stanzas: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] = {}
    last: Optional[str] = None
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
            current, last = {}, None
            continue
        if line.startswith("#"):
            continue
        if line[0] in " \t":
            if last:
                current[last][-1] += "\n" + line.strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        last = key.strip()
        current.setdefault(last, []).append(value.strip())
    if current:
        stanzas.append(current)
    return stanzas


def parse_masterlist(text: str, arch: Optional[str] = None) -> list[Mirror]:
    """Archive mirrors from Debian's Mirrors.masterlist that carry `arch`."""
    mirrors: list[Mirror] = []
    seen: set[str] = set()
    for stanza in parse_control(text):
        site = stanza.get("Site", [""])[0].strip().lower()
        path = stanza.get("Archive-http", [""])[0].strip()
        kind = stanza.get("Type", [""])[0].strip().lower()
        if not site or not path or kind in NON_MIRROR_TYPES or site in seen:
            continue
        archs = stanza.get("Archive-architecture", [""])[0].split()
        if arch and archs and arch not in archs:
            continue
        code, _, name = stanza.get("Country", [""])[0].partition(" ")
        path = "/" + path.strip("/") + "/" if path.strip("/") else "/"
        mirrors.append(Mirror(f"http://{site}{path}", site, code.upper(), name.strip()))
        seen.add(site)
    return mirrors


def default_cache_dir() -> Path:
    if os.geteuid() == 0:
        return Path("/var/cache/debscout")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "debscout"


def load_masterlist(cache_dir: Optional[Path], timeout: float = 20.0) -> tuple[Optional[str], str]:
    """Get the masterlist, from a cache under a day old if possible.

    Returns (text or None, how it was obtained or why it failed).
    """
    cache = cache_dir / "Mirrors.masterlist" if cache_dir else None
    try:
        if cache and cache.is_file() and time.time() - cache.stat().st_mtime < MASTERLIST_CACHE_SECONDS:
            return cache.read_text(errors="replace"), "cached"
    except OSError:
        pass
    try:
        text, _ = fetch_text(MASTERLIST_URL, timeout=timeout, limit=10_000_000)  # (text, final URL)
        if "Site:" not in text:
            raise FetchError("unexpected content")
    except FetchError as exc:
        if cache and cache.is_file():
            return cache.read_text(errors="replace"), f"old cached copy ({exc})"
        return None, str(exc)
    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text)
        except OSError:
            pass
    return text, "downloaded"


def parse_trace_date(text: str) -> Optional[datetime]:
    """Read the archive date from a mirror's project/trace/master file."""
    lines = text.splitlines()
    for line in lines:
        if line.startswith("Date:"):
            try:
                date = parsedate_to_datetime(line[5:].strip())
            except (TypeError, ValueError):
                date = None
            if date is not None:
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return date.astimezone(timezone.utc)
            break
    if lines:  # older trace files only have a date on the first line
        try:
            return datetime.strptime(lines[0].strip(), "%a %b %d %H:%M:%S %Z %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def check_mirrors(
    mirrors: list[Mirror],
    timeout: float = 4.0,
    workers: int = 32,
    progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """Check every mirror in parallel: archive date, redirects and network distance."""
    future_limit = datetime.now(timezone.utc) + timedelta(hours=1)

    def check(mirror: Mirror) -> None:
        try:
            text, final_url = fetch_text(mirror.url + TRACE_PATH, timeout=timeout, limit=16_384)
        except FetchError as exc:
            mirror.status, mirror.error = "unreachable", str(exc)
            return
        date = parse_trace_date(text)
        if date is None or date > future_limit:
            mirror.status, mirror.error = "unreachable", "unreadable trace file"
            return
        if final_url != mirror.url + TRACE_PATH and final_url.endswith("/" + TRACE_PATH):
            # Use where the mirror redirects to (often http -> https), so apt
            # doesn't pay for a redirect on every request.
            mirror.url = final_url[: -len(TRACE_PATH)]
        latency = connect_time(mirror.url, timeout=timeout)
        if latency is None:
            mirror.status, mirror.error = "unreachable", "connection failed"
            return
        mirror.latency, mirror.trace_date, mirror.status = latency, date, "ok"

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(check, m) for m in mirrors]
        for done, future in enumerate(futures, 1):
            future.result()
            if progress:
                progress(done, len(futures))


def mark_stale(mirrors: list[Mirror], max_age_hours: float) -> Optional[datetime]:
    """Mark mirrors more than max_age_hours behind the newest one as stale.

    Returns the newest archive date seen.
    """
    dated = [m for m in mirrors if m.status == "ok" and m.trace_date]
    if not dated:
        return None
    newest = max(m.trace_date for m in dated)
    for m in dated:
        m.age_hours = (newest - m.trace_date).total_seconds() / 3600
        if m.age_hours > max_age_hours:
            m.status = "stale"
    return newest


def pick_for_speed_test(mirrors: list[Mirror], count: int) -> list[Mirror]:
    """The CDNs plus the `count` closest (lowest-latency) up-to-date mirrors."""
    fresh = [m for m in mirrors if m.status == "ok"]
    cdns = [m for m in fresh if m.is_cdn]
    others = sorted((m for m in fresh if not m.is_cdn), key=lambda m: m.latency or 0.0)
    return cdns + others[:count]


def speed_test(
    mirrors: list[Mirror],
    suite: str,
    arch: str,
    rounds: int = 2,
    max_bytes: int = 4_000_000,
    max_seconds: float = 5.0,
    progress: Optional[Callable[[int, int, Mirror, Sample], None]] = None,
) -> list[Mirror]:
    """Download part of the suite's Packages.xz from each mirror, `rounds` times.

    Tests run one at a time so they don't compete for bandwidth, and rounds
    are interleaved so a brief network hiccup can't sink a single mirror.
    Returns the mirrors that passed, fastest first.
    """
    path = f"dists/{suite}/main/binary-{arch}/Packages.xz"
    active = list(mirrors)
    for round_no in range(1, rounds + 1):
        for mirror in list(active):
            sample = sample_download(mirror.url + path, max_bytes, max_seconds)
            mirror.samples.append(sample)
            if sample.error and sample.nbytes == 0:
                mirror.status, mirror.error = "failed", sample.error
                active.remove(mirror)
            if progress:
                progress(round_no, rounds, mirror, sample)
    for mirror in active:
        mirror.status = "tested"
    return sorted(active, key=lambda m: m.speed, reverse=True)


def choose_archive_mirrors(
    ranked: list[Mirror],
    count: int,
    suites: list[str],
    warn: Optional[Callable[[str], None]] = None,
) -> tuple[list[Mirror], list[str]]:
    """Pick the `count` fastest mirrors that carry every suite in use.

    Returns (chosen mirrors, URLs for apt's mirror list). deb.debian.org is
    always the final fallback.
    """
    chosen: list[Mirror] = []
    for mirror in ranked:
        if len(chosen) >= count:
            break
        missing = [s for s in suites if not url_exists(f"{mirror.url}dists/{s}/InRelease")]
        if missing:
            if warn:
                warn(f"skipping {mirror.site}: it does not carry {', '.join(missing)}")
            continue
        chosen.append(mirror)
    urls = [m.url for m in chosen]
    if FALLBACK_URL not in urls:
        urls.append(FALLBACK_URL)
    return chosen, urls


def security_order(ranked: list[Mirror]) -> list[str]:
    """Order Debian's official security hosts for this connection.

    security.debian.org and deb.debian.org both use Fastly, so if the
    CloudFront CDN beat deb.debian.org in the archive tests it goes first.
    """
    speeds = {m.site: m.speed for m in ranked}
    if speeds.get(CDN_AWS, 0.0) > speeds.get(CDN_FASTLY, 0.0):
        order = [CDN_AWS, "security.debian.org", CDN_FASTLY]
    else:
        order = ["security.debian.org", CDN_AWS, CDN_FASTLY]
    return [SECURITY_URLS[site] for site in order]
