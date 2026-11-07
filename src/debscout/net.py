"""Minimal HTTP helpers on top of urllib (standard library only)."""

from __future__ import annotations

import http.client
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import __version__

USER_AGENT = f"debscout/{__version__} (+https://github.com/iam-super-user/debscout)"
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError)
CHUNK = 64 * 1024


class FetchError(Exception):
    """A download failed. The message is short and human readable."""


@dataclass
class Sample:
    """One timed download."""

    nbytes: int
    seconds: float
    error: str | None = None

    @property
    def speed(self) -> float:
        return self.nbytes / self.seconds if self.seconds > 0 else 0.0


def describe(exc: BaseException) -> str:
    """Turn a network exception into a short message."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "timed out"
        return str(reason) if reason else "connection failed"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timed out"
    return str(exc) or exc.__class__.__name__


def _open(url: str, timeout: float, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_text(url: str, timeout: float = 10.0, limit: int = 1_000_000) -> tuple[str, str]:
    """Download a small text file. Returns (text, final URL after any redirects)."""
    try:
        with _open(url, timeout) as response:
            data = response.read(limit)
            final_url = response.geturl()
    except NETWORK_ERRORS as exc:
        raise FetchError(describe(exc)) from exc
    return data.decode("utf-8", "replace"), final_url


def connect_time(url: str, timeout: float = 4.0, tries: int = 2) -> float | None:
    """Best TCP connect time to the URL's host, in seconds.

    DNS is resolved once beforehand and the fastest of `tries` connects is
    kept, so the result reflects network distance rather than DNS delays or
    momentary congestion. Returns None if no connection succeeded.
    """
    parts = urllib.parse.urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    best = None
    for _ in range(tries):
        for family, kind, proto, _, address in addresses:
            with socket.socket(family, kind, proto) as sock:
                sock.settimeout(timeout)
                start = time.monotonic()
                try:
                    sock.connect(address)
                except OSError:
                    continue
                elapsed = time.monotonic() - start
            best = elapsed if best is None else min(best, elapsed)
            break
    return best


def url_exists(url: str, timeout: float = 8.0) -> bool:
    """True if the URL answers with a 2xx status. Fetches at most one byte."""
    try:
        with _open(url, timeout, {"Range": "bytes=0-0"}) as response:
            response.read(1)
            return 200 <= response.status < 300
    except NETWORK_ERRORS:
        return False


def sample_download(url: str, max_bytes: int, max_seconds: float, timeout: float = 5.0) -> Sample:
    """Download up to max_bytes (or for up to max_seconds) and time it.

    The time includes connecting and waiting for the first byte, because apt
    pays those costs too. A failure part-way through still returns the bytes
    received so far, with the error recorded.
    """
    start = time.monotonic()
    got = 0
    try:
        with _open(url, timeout, {"Range": f"bytes=0-{max_bytes - 1}"}) as response:
            read = getattr(response, "read1", response.read)
            while got < max_bytes and time.monotonic() - start < max_seconds:
                chunk = read(min(CHUNK, max_bytes - got))
                if not chunk:
                    break
                got += len(chunk)
    except NETWORK_ERRORS as exc:
        return Sample(got, time.monotonic() - start, describe(exc))
    return Sample(got, time.monotonic() - start)
