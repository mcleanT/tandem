"""Argument-array runner around the installed cmux CLI.

Every call is an argv list handed to subprocess (no shell). Errors are classified from the CLI's
own messages so callers can distinguish access denial, a missing or stale socket, an unknown
target, an unreadable surface, and a timeout. A `FaultInjector` lets tests simulate transport
faults; every simulated fault is labelled `simulated=True` in the call log so real-runtime evidence
is never confused with a simulated one.

The socket password, if any, is read from the file named by `CMUX_BRIDGE_PASSWORD_FILE` (readable only by the user)
and passed to the CLI through the `CMUX_SOCKET_PASSWORD` environment variable, never on argv.
"""

from __future__ import annotations

import os
import collections
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

DEFAULT_CLI = "/Applications/cmux.app/Contents/Resources/bin/cmux"

ERROR_KINDS = (
    "access_denied",
    "socket_missing",
    "connection_refused",
    "not_found",
    "internal_error",
    "no_terminal_view",
    "timeout",
    "cli_missing",
    "other",
)


# Commands that only read cmux state. A transient socket-setup failure on one of these is retried
# once; anything that types or changes state (send, send-key, new-*, close-*) is NEVER retried, so
# the at-most-one-Enter guarantee is untouched.
READ_ONLY_COMMANDS = frozenset(
    {
        "ping", "capabilities", "identify", "events", "list-workspaces", "list-pane-surfaces",
        "read-screen", "capture-pane", "tree", "surface-health", "version",
    }
)
# Seen live on cmux 0.64.25 when two bridge calls connect at the same instant: the CLI fails before
# sending anything ("Failed to configure socket receive timeout (Invalid argument, errno 22)").
TRANSIENT_SOCKET_RE = re.compile(
    r"Failed to configure socket|errno 22|Connection reset by peer", re.I
)


class CmuxError(RuntimeError):
    def __init__(self, kind: str, message: str, result: "CmuxResult | None" = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.result = result


class SimulatedFault(CmuxError):
    """Raised by a FaultInjector. Always labelled simulated in the call log.

    `phase` is set by `CmuxCLI.run`: "before" = the command was never executed (zero bytes sent);
    "after" = the command ran and only its reply was lost. Callers must treat the two differently.
    """

    phase: str = "unknown"


@dataclass
class CmuxResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    error_kind: str | None = None
    simulated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.error_kind is None


def classify(returncode: int, stdout: str, stderr: str) -> str | None:
    """Classify a *failed* CLI invocation. rc == 0 is never an error (review item 1).

    The real cmux CLI (0.64.17) exits 1 and prints `Error: <kind>: <message>` on stderr for every
    failure. A successful command's stdout is arbitrary terminal text that may legitimately quote
    "Access denied", "internal_error" or "not_found" — classifying that as a transport failure
    turns a normal observation into a fake outage, so rc == 0 always returns None.
    """
    if returncode == 0:
        return None
    for text in (stderr, stdout):
        low = (text or "").lower()
        if "access denied" in low or "broken pipe" in low:
            return "access_denied"
        if "socket not found" in low:
            return "socket_missing"
        if "connection refused" in low:
            return "connection_refused"
        if "not_found" in low or "not found" in low:
            return "not_found"
        if "failed to read terminal text" in low:
            # cmux 0.64.x: a terminal created unfocused has no view yet, so read-screen fails (and
            # its shell has not started). Not a transport fault: the surface is addressable.
            return "no_terminal_view"
        if "internal_error" in low:
            return "internal_error"
    return "other"


@dataclass
class FaultInjector:
    """Simulated transport faults for tests.

    `before(args)` may raise SimulatedFault to model a lost request before delivery.
    `after(args, result)` may raise SimulatedFault to model a lost response after delivery
    (the command DID run). `rewrite(args, result)` may return a replacement result (e.g. a
    fabricated access-denied reply or a changed boot_id).
    """

    before: Callable[[list[str]], None] | None = None
    after: Callable[[list[str], CmuxResult], None] | None = None
    rewrite: Callable[[list[str], CmuxResult], CmuxResult | None] | None = None


class CallLog(list):
    """Call log: a real list (json-serialisable, sliceable). With `maxlen` set, old entries are
    trimmed in place; `total` stays monotonic so `mark()`/`since()` remain correct past the cap.
    Use `maxlen=None` (unbounded) where exact history matters, e.g. the live acceptance runner."""

    def __init__(self, iterable=(), maxlen: int | None = 500):
        super().__init__(iterable)
        self.maxlen = maxlen
        self.total = len(self)
        self._trim()

    def _trim(self) -> None:
        if self.maxlen is not None and len(self) > self.maxlen:
            del self[: len(self) - self.maxlen]

    def append(self, item) -> None:
        super().append(item)
        self.total += 1
        self._trim()

    def mark(self) -> int:
        return self.total

    def since(self, mark: int) -> list:
        """Entries appended after `mark` (a prior `mark()`); only those still retained."""
        n = self.total - mark
        return list(self[-n:]) if n > 0 and len(self) else []


@dataclass
class CmuxCLI:
    cli_path: str = field(default_factory=lambda: os.environ.get("CMUX_BRIDGE_CLI", DEFAULT_CLI))
    socket: str | None = field(default_factory=lambda: os.environ.get("CMUX_BRIDGE_SOCKET"))
    timeout_s: float = 15.0
    fault: FaultInjector | None = None
    log: CallLog = field(default_factory=CallLog)

    def _env(self) -> dict:
        env = dict(os.environ)
        env["CMUX_QUIET"] = "1"
        pw_file = env.get("CMUX_BRIDGE_PASSWORD_FILE")
        if pw_file:
            try:
                p = Path(pw_file)
                if p.is_file() and (p.stat().st_mode & 0o077) == 0:
                    env["CMUX_SOCKET_PASSWORD"] = p.read_text().strip()
            except OSError:
                pass
        return env

    def run(self, *args: str, timeout: float | None = None) -> CmuxResult:
        argv = [self.cli_path]
        if self.socket:
            argv += ["--socket", self.socket]
        argv += list(args)
        t0 = time.monotonic()
        logged = list(args)
        if logged and logged[0] == "send" and "--" in logged:
            # PHI minimisation: the call log never retains delivered text; keep args[0] and flags
            i = logged.index("--")
            logged = logged[: i + 1] + [
                a if a == "/compact" else f"<redacted {len(a)} chars>" for a in logged[i + 1 :]
            ]
        entry = {"args": logged, "simulated": False}
        if self.fault and self.fault.before:
            try:
                self.fault.before(list(args))
            except SimulatedFault as e:
                e.phase = "before"  # the command was never executed: zero bytes reached cmux
                entry.update({"simulated": True, "fault": "before", "kind": e.kind})
                self.log.append(entry)
                raise
        res = self._execute(argv, list(args), timeout or self.timeout_s, t0)
        if (
            res.error_kind == "other"
            and args
            and args[0] in READ_ONLY_COMMANDS
            and TRANSIENT_SOCKET_RE.search(res.stderr or "")
        ):
            entry["retried_transient"] = (res.stderr or "").strip()[:120]
            time.sleep(0.2)
            res = self._execute(argv, list(args), timeout or self.timeout_s, time.monotonic())
        if self.fault and self.fault.rewrite:
            alt = self.fault.rewrite(list(args), res)
            if alt is not None:
                alt.simulated = True
                res = alt
        entry.update({"rc": res.returncode, "error_kind": res.error_kind, "duration_s": round(res.duration_s, 3), "simulated": res.simulated})
        self.log.append(entry)
        if self.fault and self.fault.after:
            try:
                self.fault.after(list(args), res)
            except SimulatedFault as e:
                e.phase = "after"  # the command DID run; only its reply was lost
                entry.update({"simulated": True, "fault": "after", "kind": e.kind})
                raise
        return res

    def _execute(self, argv: list[str], args: list[str], timeout: float, t0: float) -> CmuxResult:
        """Run the real CLI. Subclasses (test fakes) override this and nothing else."""
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self._env())
            res = CmuxResult(args, p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
            res.error_kind = classify(p.returncode, p.stdout, p.stderr)
            return res
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return CmuxResult(args, -1, out, "TIMEOUT", time.monotonic() - t0, "timeout")
        except FileNotFoundError:
            return CmuxResult(args, -1, "", f"cli not found: {self.cli_path}", 0.0, "cli_missing")

    def run_ok(self, *args: str, timeout: float | None = None) -> str:
        res = self.run(*args, timeout=timeout)
        if not res.ok:
            raise CmuxError(res.error_kind or "other", (res.stderr or res.stdout).strip()[:400], res)
        return res.stdout
