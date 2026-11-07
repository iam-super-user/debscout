"""Turn speed-test results into apt configuration, with backup, rollback and undo."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .sources import (
    MANAGED_SOURCES,
    MIRRORLIST_DEBIAN,
    MIRRORLIST_SECURITY,
    Classifier,
    Entry,
    SourceFile,
    disable_entries,
    group_stanzas,
    render_mirrorlist,
    render_sources,
)

STATE_DIR = "var/lib/debscout"
STATE_FILE = "state.json"
BACKUP_DIR = "backup"


class DebscoutError(Exception):
    """An error to report to the user without a traceback."""


@dataclass
class Plan:
    root: Path
    changes: dict[Path, tuple[Optional[str], str]]  # path -> (old text or None, new text)
    disabled: list[Entry]
    debian_urls: list[str]
    security_urls: list[str]
    has_security: bool = False


def display(root: Path, path: Path) -> str:
    return "/" + str(path.relative_to(root))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_plan(
    root: Path,
    files: list[SourceFile],
    classifier: Classifier,
    debian_urls: list[str],
    security_urls: list[str],
    now: Optional[datetime] = None,
) -> Plan:
    now = now or datetime.now(timezone.utc)
    managed = root / MANAGED_SOURCES
    selected: list[tuple[str, Entry]] = []
    to_disable: dict[Path, list[Entry]] = {}
    for sf in files:
        for entry in sf.entries:
            kind = classifier.classify(entry) if entry.enabled else None
            if not kind:
                continue
            selected.append((kind, entry))
            if sf.path != managed:
                to_disable.setdefault(sf.path, []).append(entry)
    if not any(kind == "debian" for kind, _ in selected):
        raise DebscoutError("no enabled Debian archive entries found in your apt sources, so there is nothing to switch")

    changes: dict[Path, tuple[Optional[str], str]] = {}

    def put(path: Path, new: str) -> None:
        old = path.read_text(errors="replace") if path.is_file() else None
        if old != new:
            changes[path] = (old, new)

    put(managed, render_sources(group_stanzas(selected), now))
    put(root / MIRRORLIST_DEBIAN, render_mirrorlist(debian_urls, "Debian archive"))
    has_security = any(kind == "security" for kind, _ in selected)
    if has_security:
        put(root / MIRRORLIST_SECURITY, render_mirrorlist(security_urls, "Debian security (official hosts only)"))
    for sf in files:
        if sf.path in to_disable:
            put(sf.path, disable_entries(sf, to_disable[sf.path]))
    disabled = [e for entries in to_disable.values() for e in entries]
    return Plan(root, changes, disabled, debian_urls, security_urls, has_security)


def plan_diff(plan: Plan) -> str:
    chunks = []
    for path, (old, new) in plan.changes.items():
        name = display(plan.root, path)
        chunks.extend(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=name if old is not None else "/dev/null",
                tofile=name,
            )
        )
    return "".join(chunks)


def load_state(root: Path) -> Optional[dict]:
    path = root / STATE_DIR / STATE_FILE
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise DebscoutError(f"cannot read {display(root, path)} (permission denied); try again with sudo") from exc
    except (OSError, ValueError) as exc:
        raise DebscoutError(f"cannot read {display(root, path)}: {exc}") from exc


def save_state(root: Path, state: dict) -> None:
    # Only file paths and checksums: world-readable, so 'debscout --status' works without root.
    write_atomic(root / STATE_DIR / STATE_FILE, json.dumps(state, indent=2) + "\n", mode=0o644)


def write_atomic(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Write via a temporary file and rename, so apt never sees a half-written file.

    The temporary name has no .list/.sources suffix, so apt ignores it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = path.stat().st_mode & 0o7777 if path.exists() else 0o644
    tmp = path.parent / f".{path.name}.debscout-tmp"
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run_apt_update() -> int:
    return subprocess.call([shutil.which("apt-get") or "apt-get", "update"])


def apply_plan(plan: Plan, run_update: bool = True, log: Callable[[str], None] = print) -> None:
    """Back up, write the new files, then check them with apt-get update.

    If apt-get update fails, everything is restored automatically.
    """
    root = plan.root
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_state(root) or {"version": 1, "created": now, "files": []}
    tracked = {record["path"]: record for record in state["files"]}
    backup_root = root / STATE_DIR / BACKUP_DIR

    # Back up each file the first time debscout touches it, and record that
    # before writing anything, so an interrupted run can still be undone.
    for path in plan.changes:
        rel = str(path.relative_to(root))
        if rel in tracked:
            continue
        record = {"path": rel, "existed": path.exists()}
        if path.exists():
            dest = backup_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        tracked[rel] = record
    state["files"] = list(tracked.values())
    state["updated"] = now
    save_state(root, state)

    for path, (_, new) in plan.changes.items():
        write_atomic(path, new)
        tracked[str(path.relative_to(root))]["sha256"] = sha256(new)
    save_state(root, state)

    if run_update:
        log("Running apt-get update to check the new configuration...")
        if run_apt_update() != 0:
            log("apt-get update failed, so debscout is restoring your previous configuration.")
            for action in undo(root):
                log(f"  {action}")
            run_apt_update()
            raise DebscoutError("the new mirrors did not work with apt; your original sources were restored")


def undo(root: Path) -> list[str]:
    """Restore every file debscout changed. Returns what was done."""
    state = load_state(root)
    if not state:
        return []
    backup_root = root / STATE_DIR / BACKUP_DIR
    actions = []
    for record in state["files"]:
        path = root / record["path"]
        name = display(root, path)
        if path.is_file() and record.get("sha256"):
            if sha256(path.read_text(errors="replace")) != record["sha256"]:
                saved = path.with_name(path.name + ".debscout-saved")
                shutil.copy2(path, saved)
                actions.append(f"kept your later edits of {name} in {display(root, saved)}")
        if record["existed"]:
            shutil.copy2(backup_root / record["path"], path)
            actions.append(f"restored {name}")
        elif path.exists():
            path.unlink()
            actions.append(f"removed {name}")
    shutil.rmtree(root / STATE_DIR)
    return actions
