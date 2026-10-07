"""Private, owner-only persistent state for the bridge (leases, bindings, requests, receipts).

Root: `$CMUX_BRIDGE_STATE_DIR` or `~/.local/state/tandem`, mode 0700; every file is
written atomically (temp + rename) with mode 0600. Nothing here is ever a repository file.

Safety contract (review item 2): every managed directory and file is *preflighted* before any
mkdir/chmod/write. If the root, a managed subdirectory or a target file is a symlink, or exists as
something other than the expected kind, the store refuses with `StateError("unsafe_state_path")`
and mutates nothing — in particular nothing outside the root, whose mode is left untouched.

Cross-process serialisation (review item 3): `lock(name)` is an advisory `flock(LOCK_EX)` on
`locks/<name>.lock`. It is re-entrant within a process/thread and never held across a CLI call,
a sleep or a wait.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REQUEST_TERMINAL_STATUSES = ("accepted", "completed", "uncertain_foreign")


def scrub_terminal_request(rel: str, obj: dict) -> None:
    """PHI minimisation: once a request record is terminal its prompt text is no longer needed for
    replay (receipts replay hashes + metadata only), so keep only the sha256s."""
    if rel.startswith("requests/") and isinstance(obj, dict) and obj.get("status") in REQUEST_TERMINAL_STATUSES:
        # a task_file delivered_text is only the "Task brief: <path>" reference (no prompt content)
        if obj.get("delivery_mode") != "task_file" and obj.get("delivered_text") is not None:
            obj["delivered_text"] = None
        if obj.get("first_line"):
            obj["first_line"] = None
        obj["text_scrubbed"] = True


MANAGED_SUBDIRS = ("leases", "bindings", "requests", "receipts", "locks", "tasks")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class StateError(RuntimeError):
    def __init__(self, code: str, path: str = "", message: str = ""):
        super().__init__(f"{code}: {message or path}")
        self.code = code
        self.path = str(path)
        self.message = message or str(path)


class StateStore:
    def __init__(self, root: str | os.PathLike | None = None):
        root = (
            root
            or os.environ.get("CMUX_BRIDGE_STATE_DIR")
            or (Path.home() / ".local/state/tandem")
        )
        self.root = Path(root)
        self._local = threading.local()
        self._preflight()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        for sub in MANAGED_SUBDIRS:
            d = self.root / sub
            d.mkdir(exist_ok=True)
            os.chmod(d, 0o700)

    # ------------------------------------------------------------------ safety
    def _preflight(self) -> None:
        """Refuse linked/unsafe managed paths BEFORE creating or chmod-ing anything."""
        for p in (self.root, *(self.root / s for s in MANAGED_SUBDIRS)):
            if p.is_symlink():
                raise StateError(
                    "unsafe_state_path",
                    str(p),
                    f"{p} is a symlink; managed state paths must be real directories",
                )
            if p.exists() and not p.is_dir():
                raise StateError(
                    "unsafe_state_path", str(p), f"{p} exists and is not a directory"
                )

    def path(self, rel: str) -> Path:
        """Containment + symlink check for one managed path, without following links."""
        rel = str(rel)
        if os.path.isabs(rel) or ".." in Path(rel).parts:
            raise StateError(
                "unsafe_state_path", rel, "relative paths only, no parent traversal"
            )
        base = Path(os.path.normpath(str(self.root)))
        p = Path(os.path.normpath(str(base / rel)))
        if p != base and base not in p.parents:
            raise StateError("unsafe_state_path", rel, "path escapes the state root")
        cur = base
        for part in p.relative_to(base).parts:
            cur = cur / part
            if cur.is_symlink():
                raise StateError("unsafe_state_path", str(cur), f"{cur} is a symlink")
        return p

    # ------------------------------------------------------------------ lock
    @contextmanager
    def lock(self, name: str = "state", timeout_s: float = 5.0):
        """Cross-process exclusive lock on `locks/<name>.lock`. Re-entrant per thread."""
        held = getattr(self._local, "held", None)
        if held is None:
            held = self._local.held = {}
        if held.get(name):
            held[name] += 1
            try:
                yield
            finally:
                held[name] -= 1
            return
        p = self.path(f"locks/{name}.lock")
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise StateError(
                            "lock_timeout",
                            str(p),
                            f"could not acquire {name} lock in {timeout_s}s",
                        )
                    time.sleep(0.01)
            held[name] = 1
            try:
                yield
            finally:
                held[name] = 0
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ io
    def read(self, rel: str) -> dict | None:
        p = self.path(rel)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text())
        except ValueError:
            return None

    def write(self, rel: str, obj: dict) -> Path:
        p = self.path(rel)
        scrub_terminal_request(rel, obj)
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        return p

    def write_task_file(self, rel: str, data: str) -> Path:
        """Write a raw-text task-brief payload atomically as read-only (0444) inside the bridge-owned
        `tasks/` tree. The same containment + symlink preflight as `write` applies. Read-only + a
        0700 root make it an immutable, owner-private brief; a given request_id is written once."""
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(p.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, 0o444)
        os.replace(tmp, p)
        os.chmod(p, 0o444)
        return p

    def delete(self, rel: str) -> bool:
        p = self.path(rel)
        if p.is_file():
            p.unlink()
            return True
        return False

    def delete_tree(self, rel: str) -> bool:
        """Remove a bridge-owned directory (e.g. tasks/<binding_id>/), making 0444 files writable
        first. Containment/symlink preflight as for every other path; symlinks are never followed."""
        p = self.path(rel)
        if p.is_symlink() or not p.is_dir():
            return False
        for child in p.iterdir():
            if child.is_symlink() or not child.is_file():
                continue
            os.chmod(child, 0o600)
            child.unlink()
        try:
            p.rmdir()
        except OSError:
            return False
        return True

    def list(self, rel: str) -> list[Path]:
        p = self.path(rel)
        return sorted(x for x in p.glob("*.json") if x.is_file()) if p.is_dir() else []

    def append_receipt(self, kind: str, record: dict) -> Path:
        """O_APPEND under the receipts lock, through the same containment/symlink check."""
        rec = {"at": utcnow(), "kind": kind, **record}
        with self.lock("receipts"):
            p = self.path("receipts/receipts.jsonl")
            fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode())
            finally:
                os.close(fd)
            os.chmod(p, 0o600)
        return p
