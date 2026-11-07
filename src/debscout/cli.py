"""Command-line interface for debscout."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from . import __version__
from . import mirrors as ml
from .apply import DebscoutError, apply_plan, build_plan, load_state, plan_diff, run_apt_update, undo
from .net import Sample
from .sources import MIRRORLIST_DEBIAN, MIRRORLIST_SECURITY, Classifier, debian_suites, read_sources

EPILOG = """\
examples:
  debscout                    test mirrors and show the ranking (no root needed)
  debscout -c IN,SG,HK        only test mirrors in these countries (plus the CDNs)
  debscout --apply --dry-run  preview the changes to your apt sources
  sudo debscout --apply       switch apt to the fastest mirrors
  sudo debscout --undo        put your original apt sources back
"""


class Reporter:
    """Progress messages on stderr, so stdout carries only results."""

    def __init__(self, quiet: bool = False, verbose: bool = False):
        self.quiet, self.verbose = quiet, verbose
        self.stream = sys.stderr
        self.tty = self.stream.isatty()
        self.live_shown = False

    def _clear(self) -> None:
        if self.live_shown:
            self.stream.write("\r\033[K")
            self.live_shown = False

    def info(self, message: str = "") -> None:
        if not self.quiet:
            self._clear()
            print(message, file=self.stream, flush=True)

    def live(self, message: str) -> None:
        """A status line that is overwritten in place (terminals only)."""
        if not self.quiet and self.tty:
            self.stream.write("\r\033[K" + message)
            self.stream.flush()
            self.live_shown = True

    def warn(self, message: str) -> None:
        self._clear()
        print(f"warning: {message}", file=self.stream, flush=True)


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="debscout",
        description="Find the fastest up-to-date Debian mirrors for your connection "
        "and configure apt to use them, with automatic fallback.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action = p.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="switch apt to the fastest mirrors (needs root)")
    action.add_argument("--undo", action="store_true", help="restore the apt sources debscout changed (needs root)")
    action.add_argument("--status", action="store_true", help="show what debscout has configured")

    t = p.add_argument_group("testing")
    t.add_argument("-c", "--country", metavar="CC,...", help="only test mirrors in these countries, by ISO code (e.g. IN,SG); the CDNs are always tested")
    t.add_argument("-t", "--test", type=positive_int, default=10, metavar="N", help="speed-test the N closest (lowest-latency) mirrors (default: 10)")
    t.add_argument("--rounds", type=positive_int, default=2, metavar="N", help="speed-test rounds per mirror (default: 2)")
    t.add_argument("--sample-mb", type=positive_float, default=8.0, metavar="MB", help="most MB to download per test (default: 8)")
    t.add_argument("--time-limit", type=positive_float, default=5.0, metavar="SEC", help="most seconds per test download (default: 5)")
    t.add_argument("--max-age", type=positive_float, default=12.0, metavar="HOURS", help="skip mirrors more than HOURS behind the newest (default: 12)")
    t.add_argument("--timeout", type=positive_float, default=4.0, metavar="SEC", help="response timeout when checking mirrors (default: 4)")
    t.add_argument("--suite", help="Debian suite to test with (default: from your apt sources)")
    t.add_argument("--arch", help="architecture to test with (default: from dpkg)")
    t.add_argument("--no-cdn", action="store_true", help="don't consider Debian's CDNs for the main archive")
    t.add_argument("--workers", type=positive_int, default=32, help=argparse.SUPPRESS)

    a = p.add_argument_group("applying")
    a.add_argument("-n", "--top", type=positive_int, default=3, metavar="N", help="put the N fastest mirrors in apt's list (default: 3); deb.debian.org is always added last as a fallback")
    a.add_argument("--dry-run", action="store_true", help="with --apply: show the changes without making them")
    a.add_argument("--no-update", action="store_true", help="don't run apt-get update after --apply or --undo")
    a.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    a.add_argument("--root", help=argparse.SUPPRESS)  # operate on another filesystem root (testing)

    o = p.add_argument_group("output")
    o.add_argument("--json", action="store_true", help="print the ranking as JSON")
    o.add_argument("-v", "--verbose", action="store_true", help="show every speed test")
    o.add_argument("-q", "--quiet", action="store_true", help="only print results and errors")
    p.add_argument("-V", "--version", action="version", version=f"debscout {__version__}")
    return p


# ---------------------------------------------------------------- formatting


def fmt_speed(bps: float) -> str:
    if bps >= 1e6:
        return f"{bps / 1e6:.1f} MB/s"
    return f"{bps / 1e3:.0f} kB/s"


def fmt_ms(seconds: Optional[float]) -> str:
    return "-" if seconds is None else f"{seconds * 1000:.0f} ms"


def fmt_age(hours: Optional[float]) -> str:
    if hours is None:
        return "-"
    if hours < 1:
        return "<1 h"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


def print_table(ranked: list[ml.Mirror]) -> None:
    rows = [("#", "Speed", "Latency", "Behind", "Where", "Mirror", "")]
    for i, m in enumerate(ranked, 1):
        notes = []
        if m.unstable:
            notes.append("unstable")
        if any(s.error for s in m.samples):
            notes.append("errors")
        rows.append((str(i), fmt_speed(m.speed), fmt_ms(m.latency), fmt_age(m.age_hours), m.location, m.url, ", ".join(notes)))
    widths = [max(len(r[c]) for r in rows) for c in range(6)]
    for r in rows:
        cells = [r[0].rjust(widths[0]), r[1].rjust(widths[1]), r[2].rjust(widths[2]), r[3].rjust(widths[3]), r[4].ljust(widths[4]), r[5].ljust(widths[5]), r[6]]
        print("  ".join(cells).rstrip())


def results_json(ranked, candidates, suite, arch, newest) -> dict:
    def mirror(m: ml.Mirror) -> dict:
        return {
            "url": m.url,
            "site": m.site,
            "country": m.country_code or None,
            "cdn": m.is_cdn,
            "speed_bytes_per_sec": round(m.speed),
            "latency_ms": round(m.latency * 1000) if m.latency is not None else None,
            "behind_hours": round(m.age_hours, 1) if m.age_hours is not None else None,
            "samples": [{"bytes": s.nbytes, "seconds": round(s.seconds, 3), "error": s.error} for s in m.samples],
        }

    return {
        "debscout": __version__,
        "suite": suite,
        "arch": arch,
        "newest_archive_date": newest.isoformat() if newest else None,
        "ranking": [dict(rank=i, **mirror(m)) for i, m in enumerate(ranked, 1)],
        "out_of_date": [mirror(m) for m in candidates if m.status == "stale"],
        "failed": [{"url": m.url, "error": m.error} for m in candidates if m.status == "failed"],
        "unreachable": sum(1 for m in candidates if m.status == "unreachable"),
    }


# ---------------------------------------------------------------- detection


def detect_arch() -> str:
    try:
        out = subprocess.run(["dpkg", "--print-architecture"], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        machine = platform.machine()
        return {"x86_64": "amd64", "aarch64": "arm64", "i686": "i386", "armv7l": "armhf"}.get(machine, machine)


def detect_codename(root: Path) -> Optional[str]:
    for rel in ("etc/os-release", "usr/lib/os-release"):
        try:
            for line in (root / rel).read_text().splitlines():
                if line.startswith("VERSION_CODENAME="):
                    value = line.split("=", 1)[1].strip().strip("\"'")
                    if value:
                        return value
        except OSError:
            continue
    try:  # testing and unstable: e.g. "forky/sid"
        value = (root / "etc/debian_version").read_text().strip()
        if value and not value[0].isdigit():
            return value.split("/")[0]
    except OSError:
        pass
    return None


def pick_test_suite(suites: list[str], root: Path) -> str:
    for suite in suites:
        if "-" not in suite:
            return suite
    if suites:
        return suites[0].split("-")[0]
    return detect_codename(root) or "stable"


def require_root(root: Path, what: str) -> None:
    if root == Path("/") and os.geteuid() != 0:
        raise DebscoutError(f"{what} needs root. Try: sudo debscout {what}")


def confirm(question: str) -> bool:
    try:
        return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------- commands


def cmd_scan(args, root: Path, rep: Reporter) -> int:
    if args.apply and not args.dry_run:
        require_root(root, "--apply")
        if not args.yes and not sys.stdin.isatty():
            raise DebscoutError("not running interactively; add --yes to apply without confirmation")

    arch = args.arch or detect_arch()
    text, how = ml.load_masterlist(ml.default_cache_dir())
    listed = ml.parse_masterlist(text, arch) if text else []
    if not listed:
        rep.warn(f"could not get Debian's mirror list ({how}); testing only Debian's CDNs")

    classifier = Classifier(root, {m.site: urlsplit(m.url).path for m in listed})
    files = read_sources(root)
    suites = debian_suites(files, classifier)
    suite = args.suite or pick_test_suite(suites, root)

    candidates = listed
    if args.country:
        wanted = {c.strip().upper() for c in args.country.split(",") if c.strip()}
        candidates = [m for m in listed if m.country_code in wanted]
        if not candidates:
            rep.warn(f"no listed mirrors in {', '.join(sorted(wanted))}")
    if not args.no_cdn:
        candidates = ml.builtin_cdns() + candidates
    if not candidates:
        raise DebscoutError("there are no mirrors to test")

    rep.info(f"debscout {__version__}: testing mirrors for Debian {suite} ({arch})")
    if listed:
        rep.info(f"Debian mirror list: {len(listed)} mirrors ({how})")

    started = time.monotonic()
    ml.check_mirrors(
        candidates,
        timeout=args.timeout,
        workers=args.workers,
        progress=lambda done, total: rep.live(f"Checking how up to date each mirror is: {done}/{total}"),
    )
    newest = ml.mark_stale(candidates, args.max_age)
    count = {s: sum(1 for m in candidates if m.status == s) for s in ("ok", "stale", "unreachable")}
    rep.info(
        f"Checked {len(candidates)} mirrors in {time.monotonic() - started:.0f}s: "
        f"{count['ok']} up to date, {count['stale']} out of date, {count['unreachable']} unreachable"
    )
    if not count["ok"]:
        raise DebscoutError("no up-to-date mirror responded; check your internet connection")

    to_test = ml.pick_for_speed_test(candidates, args.test)
    rep.info(f"Speed-testing {len(to_test)} mirrors, {args.rounds} round(s) each (up to {args.sample_mb:g} MB or {args.time_limit:g}s per test)...")
    step = [0]
    total = len(to_test) * args.rounds

    def on_sample(round_no: int, rounds: int, mirror: ml.Mirror, sample: Sample) -> None:
        step[0] += 1
        result = sample.error if sample.error and not sample.nbytes else fmt_speed(sample.speed)
        line = f"  round {round_no}/{rounds}  {mirror.site:<40} {result}"
        if rep.verbose:
            rep.info(line)
        else:
            rep.live(f"Speed test {step[0]}/{total}: {mirror.site} {result}")

    ranked = ml.speed_test(
        to_test,
        suite,
        arch,
        rounds=args.rounds,
        max_bytes=int(args.sample_mb * 1_000_000),
        max_seconds=args.time_limit,
        progress=on_sample,
    )
    rep.info("")
    if not ranked:
        raise DebscoutError("every speed test failed; check your internet connection")

    if args.json:
        print(json.dumps(results_json(ranked, candidates, suite, arch, newest), indent=2))
        return 0

    print_table(ranked)
    stale = sorted((m for m in candidates if m.status == "stale"), key=lambda m: m.latency or 0.0)
    if stale:
        print("\nSkipped because they are out of date (closest first):")
        for m in stale[:5]:
            print(f"  {m.url}  {fmt_age(m.age_hours)} behind")
    failed = [m for m in to_test if m.status == "failed"]
    if failed:
        print("\nFailed the speed test:")
        for m in failed:
            print(f"  {m.url}  {m.error}")

    if not args.apply:
        hint = "debscout --apply" if os.geteuid() == 0 else "sudo debscout --apply"
        print(f"\nTo switch apt to the top {args.top}: {hint}   (preview first with --apply --dry-run)")
        return 0
    return do_apply(args, root, rep, files, classifier, ranked, suites)


def do_apply(args, root, rep, files, classifier, ranked, suites) -> int:
    if not suites:
        raise DebscoutError("no enabled Debian archive entries found in your apt sources, so there is nothing to switch")
    rep.info(f"Checking the fastest mirrors carry: {' '.join(suites)}")
    chosen, debian_urls = ml.choose_archive_mirrors(ranked, args.top, suites, warn=rep.warn)
    if not chosen:
        raise DebscoutError("none of the tested mirrors carries all of your suites")
    security_urls = ml.security_order(ranked)
    plan = build_plan(root, files, classifier, debian_urls, security_urls)

    speeds = {m.url: m.speed for m in chosen}
    print("\nDebian archive mirrors for apt, in order:")
    for n, url in enumerate(debian_urls, 1):
        note = fmt_speed(speeds[url]) if url in speeds else "fallback"
        print(f"  {n}. {url}  ({note})")
    if plan.has_security:
        print("Security mirrors (Debian's official hosts only):")
        for n, url in enumerate(security_urls, 1):
            print(f"  {n}. {url}")
    print("Files:")
    for path, (old, _) in plan.changes.items():
        print(f"  {'create' if old is None else 'update'}  /{path.relative_to(root)}")
    if plan.disabled:
        print(f"  ({len(plan.disabled)} original Debian entr{'y' if len(plan.disabled) == 1 else 'ies'} switched off; backups kept for --undo)")

    if args.dry_run:
        print("\n" + plan_diff(plan) + "\nDry run: nothing was changed.")
        return 0
    if not args.yes and not confirm("\nApply these changes?"):
        print("Nothing was changed.")
        return 1
    apply_plan(plan, run_update=not args.no_update and root == Path("/"), log=rep.info)
    print("\nDone. apt now uses the fastest mirrors. Undo any time with: sudo debscout --undo")
    return 0


def cmd_undo(args, root: Path, rep: Reporter) -> int:
    require_root(root, "--undo")
    actions = undo(root)
    if not actions:
        print("debscout has not changed your apt sources, so there is nothing to undo.")
        return 0
    for action in actions:
        print(f"  {action}")
    if not args.no_update and root == Path("/"):
        rep.info("Running apt-get update...")
        run_apt_update()
    print("Your original apt sources are back.")
    return 0


def cmd_status(args, root: Path, rep: Reporter) -> int:
    state = load_state(root)
    if not state:
        print("debscout is not managing your apt sources.")
        return 0
    print(f"debscout has managed your apt sources since {state.get('created')} (last change {state.get('updated')}).")
    for title, rel in (("Debian archive", MIRRORLIST_DEBIAN), ("Security", MIRRORLIST_SECURITY)):
        path = root / rel
        if path.is_file():
            print(f"\n{title} mirrors (/{rel}):")
            for line in path.read_text().splitlines():
                if line.strip() and not line.startswith("#"):
                    print("  " + line.replace("\t", "  "))
    print("\nChanged files:")
    for record in state.get("files", []):
        print(f"  /{record['path']}  ({'backed up' if record['existed'] else 'created by debscout'})")
    print("\nRe-rank with 'sudo debscout --apply', or revert with 'sudo debscout --undo'.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.json and (args.apply or args.undo or args.status):
        parser.error("--json only works with the plain ranking")
    if args.dry_run and not args.apply:
        parser.error("--dry-run only works with --apply")
    rep = Reporter(quiet=args.quiet or args.json, verbose=args.verbose)
    root = Path(args.root).resolve() if args.root else Path("/")
    try:
        if args.undo:
            return cmd_undo(args, root, rep)
        if args.status:
            return cmd_status(args, root, rep)
        return cmd_scan(args, root, rep)
    except KeyboardInterrupt:
        rep.warn("interrupted")
        return 130
    except DebscoutError as exc:
        print(f"debscout: {exc}", file=sys.stderr)
        return 1
