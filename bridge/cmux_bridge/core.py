"""Bridge core: discover / bind / observe / submit / wait / compact / release.

Pure logic over `CmuxCLI` (argument arrays) and `StateStore` (private files). No shell, no generic
RPC, no pane deletion, no process cleanup.

What this module does NOT promise: exactly-once delivery. The runtime cannot establish it. What it
does promise is narrower and checkable:

* at most one Enter per request, and only on an editor whose staged text is *exactly* this
  request's delivered line (single-line; trailing whitespace is not compared — see
  `staged_is_ours`). A multi-line or long payload is NEVER pasted raw: it is written to an
  immutable, bridge-owned task file and the delivered line is a short single-line reference to
  that file, so the thing Enter is pressed on is always exactly verifiable. A paste marker's line
  count is NOT payload identity;
* acceptance only on exact transcript correlation — a user message for this session whose
  sha256 equals the *delivered* line's, timestamped at/after the request started;
* deliveries on one binding are serialised by a per-binding lock: a concurrent retry observes the
  in-flight operation instead of delivering again;
* a non-zero CLI exit is NOT proof of non-delivery (the effect may have applied before the channel
  failed); only an explicitly simulated pre-send fault (zero bytes sent) is a resend basis. Every
  other outcome is retained as uncertainty (`uncertain`, `uncertain_foreign`) and is never
  resolved by resending.

Request statuses: delivering | staged_unverified | submitted_unconfirmed | uncertain (non-terminal)
and accepted | completed | uncertain_foreign (terminal; replayed verbatim on a duplicate call).
`send_result`: null | attempting | ok | cli_error:<kind> | uncertain | not_attempted_simulated.
Screen states: prompt_idle | staged | running | modal | not_claude | unknown.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .cmuxcli import CmuxCLI, CmuxError, SimulatedFault
from .model_policy import (
    DEFAULT_PURPOSE,
    PURPOSES,
    check_session_model,
    footer_model,
    model_family,
)
from .model_policy import argv_model as _argv_model
from .model_policy import transcript_model as _transcript_model
from .state import REQUEST_TERMINAL_STATUSES, StateError, StateStore

CONTRACT = "tandem 0.1.0"


def _same_model(a, b) -> bool:
    """Same family; a model that names no known family (only reachable with
    TANDEM_ALLOW_UNKNOWN_MODEL) must match as text."""
    fa = model_family(a)
    if fa != model_family(b):
        return False
    return fa is not None or str(a).strip().lower() == str(b).strip().lower()

COMPACT_DUE_PCT = 30.0
COMPACT_LIMIT_PCT = 40.0
MAX_TEXT_CHARS = 16000
MAX_LINES = 200
SINGLE_LINE_MAX = 160  # longer or multi-line payloads are delivered as an immutable task-file reference
MAX_EVENTS = 400
MAX_WAIT_S = 600.0
DEFAULT_WAIT_S = 25.0  # Codex yields host tool calls at ~30s; callers loop on non-terminal results
DEFAULT_LEASE_S = 1800
TRANSCRIPT_TAIL_BYTES = 2_000_000
COMPACT_COMMAND_PREFIX = "<command-name>/compact</command-name>"

TERMINAL_STATUSES = REQUEST_TERMINAL_STATUSES
REQUEST_STATUSES = (
    "delivering",
    "staged_unverified",
    "submitted_unconfirmed",
    "uncertain",
    "lease_lost",
) + TERMINAL_STATUSES
WAIT_UNTIL = (
    "accepted",
    "turn_complete",
    "idle",
    "task_complete",
    "compaction_complete",
)
SUBMIT_KINDS = (
    "task",
    "reply",
    "steer",
)  # "steer" = authorized scoped queued-input into a RUNNING turn
RESENDABLE = ("not_attempted_simulated",)

UUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
PROMPT_RE = re.compile(
    "^❯(?: (.*))?$"
)  # input line = ❯ + NBSP; dialog cursors are ' ❯ option' with a plain space
SPINNER_RE = re.compile(r"^\s*[·✻✽✶✳✢*]\s+\S[^\n]*…|esc to interrupt")
# "Press up to edit queued messages" is Claude UI chrome that fills the ENTIRE input line only
# while the agent is busy with queued input. Match the EXACT placeholder shape (the whole input
# equals it): a substring search would misread a real draft that merely mentions the phrase, e.g.
# "Explain the phrase Press up to edit queued messages" (codex-r2-classifier-neighbor-and-rollout-
# scope). The fix is observation ACCURACY — report the true busy/queued state instead of a
# misleading "staged"; existing staged handling already refuses writes/compaction
# (codex-live-queued-placeholder-review-r2).
QUEUED_PLACEHOLDER = "press up to edit queued messages"
# ccstatusline renders 'Ctx Used:' or, in newer builds, 'Ctx(u) Used:' (any letters in the parens).
# The real footer separates tokens with NBSP (\xa0), not ' ' (live v2.1.292: 'Ctx(u)\xa0Used:\xa00.0%'),
# so the gaps are \s (which matches NBSP in str patterns), never a literal space.
CTX_RE = re.compile(r"Ctx(?:\([A-Za-z]+\))?\s+Used:\s*([0-9]+(?:\.[0-9]+)?)%")
FOOTER_RE = re.compile(r"Model:\s.+\|.*Ctx(?:\([A-Za-z]+\))?\s+Used:")
# Claude Code's dimmed empty-prompt suggestion; plain-text read-screen loses the dim styling, so it
# looks like typed text. Matched on the WHOLE input line only (no continuation rows, no paste marker).
PLACEHOLDER_RE = re.compile(r'^Try "[^"\n]+"$')
PASTED_RE = re.compile(r"\[Pasted text #(\d+) \+(\d+) lines\]")
AGENTS_RE = re.compile(r"←\s*(\d+)\s+agents?\b")
SIDEBAR_RE = re.compile(
    r"claude_code (\S+).*?--tab=(\S+).*?--panel=(\S+).*?--pid=(\d+)"
)
MODAL_HINTS = (
    "Do you want to proceed",
    "Is this a project you created or one you trust",
    "Yes, I trust this folder",
    "Do you trust the files in this folder",
    "Yes, and don't ask again",
    "Enter to confirm",
    "esc to cancel",
    "Esc to cancel",
)


NO_TERMINAL_VIEW_INSTRUCTION = (
    "the cmux surface has no terminal view (created unfocused); select its workspace once "
    "(cmux select-workspace) or relaunch via executor_session.py launch, then retry"
)


def canonical_dir(path: str) -> str:
    """realpath, then (macOS, case-insensitive FS) the true on-disk spelling via F_GETPATH on an
    O_RDONLY fd of the directory, so differently-cased spellings of one directory compare equal."""
    real = os.path.realpath(path)
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is None:
        return real
    try:
        fd = os.open(real, os.O_RDONLY)
    except OSError:
        return real
    try:
        raw = fcntl.fcntl(fd, getpath, b"\0" * 1024)
        out = raw.split(b"\0", 1)[0].decode()
        return os.path.realpath(out) if out else real
    except (OSError, UnicodeDecodeError):
        return real
    finally:
        os.close(fd)


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, **self.detail}


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(s: str) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def clamp_timeout(
    value, default: float, lo: float = 0.5, hi: float = MAX_WAIT_S
) -> float:
    """Non-finite / non-numeric / non-positive timeouts collapse to a documented bound."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v) or v <= 0:
        return default if math.isnan(v) or not math.isfinite(v) else lo
    return max(lo, min(v, hi))


FEED_SESSION_PREFIX = "cmux-feed-v1:"


def canonical_session_id(raw):
    """Map cmux's feed session id 'cmux-feed-v1:<b64 agent>:<b64 id>' to the bridge form
    'claude-<uuid>' when the decoded agent is 'claude'. Padding-tolerant, standard or urlsafe
    base64. Anything else (other agents, legacy 'claude-<uuid>', garbage, non-str) is returned
    unchanged."""
    if not isinstance(raw, str) or not raw.startswith(FEED_SESSION_PREFIX):
        return raw
    parts = raw[len(FEED_SESSION_PREFIX) :].split(":")
    if len(parts) != 2:
        return raw

    def _dec(t: str) -> str:
        t = t.strip()
        return base64.b64decode(
            t.replace("-", "+").replace("_", "/") + "=" * (-len(t) % 4), validate=True
        ).decode("utf-8")

    try:
        agent, ident = _dec(parts[0]), _dec(parts[1])
    except (ValueError, UnicodeDecodeError):
        return raw
    if agent != "claude" or not ident:
        return raw
    return "claude-" + ident


def classify_screen(lines: list[str]) -> dict:
    """Classify the last rows of a surface. Screen is the primary evidence; callers add events."""
    tail = lines[-40:]
    footer = any(FOOTER_RE.search(l) for l in tail)
    prompt_idx = None
    for i in range(len(tail) - 1, -1, -1):
        if PROMPT_RE.match(tail[i]):
            prompt_idx = i
            break
    ctx = None
    agents = None
    for l in tail:
        m = CTX_RE.search(l)
        if m:
            try:
                ctx = float(m.group(1))
            except ValueError:
                pass
        a = AGENTS_RE.search(l)
        if a:
            agents = int(a.group(1))
    # `model` is the footer display name ("Opus 5", "Fable 5.1"); it is the live model the
    # session will answer with next and the evidence the purpose/model gate reads (2026-09-20).
    base = {"ctx_used_pct": ctx, "background_agents": agents, "model": (footer_model(tail) or "").replace("\xa0", " ") or None}
    if not tail or all(not l.strip() for l in tail):
        return {"state": "unknown", "reason": "empty screen", "claude": False, **base}
    # A modal replaces the input box, so its text must be in the LAST rows. `--lines N` implies
    # scrollback, so an earlier dialog (e.g. the trust prompt) can still sit higher in the window.
    region = [l for l in tail if l.strip()][-12:]
    if prompt_idx is None and any(h in l for l in region for h in MODAL_HINTS):
        return {
            "state": "modal",
            "reason": "dialog text visible without an input prompt",
            "claude": True,
            **base,
        }
    if not footer and prompt_idx is None:
        return {
            "state": "not_claude",
            "reason": "no Claude prompt or footer visible",
            "claude": False,
            **base,
        }
    above = tail[:prompt_idx] if prompt_idx is not None else tail
    if any(SPINNER_RE.match(l) for l in above[-12:]):
        # A steer types into the editor WHILE the spinner runs, so the running classification also
        # reports the input-line text (input_text). It is the ONLY way queued_is_ours can verify a
        # payload provably ours before pressing Enter to queue it — staged_is_ours needs state
        # 'staged', which a spinner suppresses. Extra key; no existing caller reads it.
        running_input = ""
        if prompt_idx is not None:
            running_input = (PROMPT_RE.match(tail[prompt_idx]).group(1) or "").strip()
        return {
            "state": "running",
            "reason": "spinner line visible",
            "claude": True,
            "input_text": running_input,
            **base,
        }
    if prompt_idx is None:
        return {
            "state": "unknown",
            "reason": "footer without input prompt",
            "claude": True,
            **base,
        }
    staged_text = (PROMPT_RE.match(tail[prompt_idx]).group(1) or "").strip()
    # Exact-shape match only: the placeholder fills the whole input line and appears solely while
    # the agent is busy with queued input (the spinner can sit above a short read). Reporting the
    # accurate busy/queued state fixes the observation, without over-matching a real draft that
    # merely contains the phrase (codex-r2-classifier-neighbor-and-rollout-scope).
    if staged_text.lower() == QUEUED_PLACEHOLDER:
        return {
            "state": "running",
            "reason": "queued-messages placeholder fills the input (agent busy with queued input; spinner above the read window)",
            "queued_messages": True,
            "claude": True,
            **base,
        }
    # Editor rows live strictly between the prompt row and the first border rule / footer row.
    # Rows AFTER the footer (background workflow/agent panels) are UI chrome, never editor text.
    end = len(tail)
    for j in range(prompt_idx + 1, len(tail)):
        if tail[j].startswith("─") or FOOTER_RE.search(tail[j]):
            end = j
            break
    continuation = [
        l
        for l in tail[prompt_idx + 1 : end]
        if l.strip()
        and not l.startswith("─")
        and not FOOTER_RE.search(l)
    ]
    pm = None
    for l in tail[prompt_idx:]:
        m = PASTED_RE.search(l)
        if m:
            pm = int(m.group(2))
    if PLACEHOLDER_RE.match(staged_text) and pm is None and not continuation:
        # Idle. `editor_text` keeps the RAW editor text so staged_is_ours/queued_is_ours can still
        # verify a payload that literally equals a 'Try "x"' string, independent of this rule.
        return {
            "state": "prompt_idle",
            "reason": "empty input prompt showing Claude Code's placeholder suggestion, no spinner",
            "placeholder": staged_text,
            "editor_text": staged_text,
            "claude": True,
            **base,
        }
    if staged_text or pm is not None or continuation:
        return {
            "state": "staged",
            "reason": "text present in input editor",
            "staged_text": staged_text,
            "staged_tail": staged_text[:200],
            "pasted_marker": pm is not None,
            "pasted_lines": pm,
            # item 3: nonempty continuation rows are returned, not discarded, so a caller can see
            # (and staged_is_ours can refuse on) editor content beyond the matched staged line —
            # a foreign row inserted after ours, or our own single line wrapping past the pane
            # width, are both ambiguous from here and must not be silently accepted.
            "continuation": [c[:200] for c in continuation],
            "continuation_lines": len(continuation),
            "claude": True,
            **base,
        }
    return {
        "state": "prompt_idle",
        "reason": "empty input prompt, no spinner",
        "claude": True,
        **base,
    }


def _placeholder_as_staged(screen: dict) -> dict:
    """A prompt_idle screen carrying the placeholder rule's raw `editor_text` is verified as a
    one-line staged editor, so payload identity is exact text equality and never depends on the
    placeholder heuristic. A placeholder can only be 'ours' if it equals the payload exactly; the
    one residual ambiguity (payload identical to the live placeholder, indistinguishable from an
    idle prompt) is closed in submit by refusing it (payload_matches_placeholder)."""
    if screen.get("state") == "prompt_idle" and screen.get("editor_text"):
        return {**screen, "state": "staged", "staged_text": screen["editor_text"], "continuation": []}
    return screen


def staged_is_ours(text: str, screen: dict) -> tuple[bool, dict]:
    """Staged evidence rule (review item 5, hardened per mcp-hardening-followup #1).

    Enter is pressed only on a *single-line* delivered payload whose captured editor text equals
    ours exactly. Trailing whitespace is NOT compared: a terminal render pads/trims the right edge
    and those bytes cannot be read back reliably. This is disclosed here and recorded as
    `trailing_ws_ignored`; if literal trailing bytes matter, the caller must deliver a task file.

    A multi-line payload is never verifiable from the screen — a paste marker only reports a *line
    count*, so foreign pasted text with the same count would match. It is refused here; submit
    converts it to an immutable task-file reference (a single line this rule can verify) first.
    """
    screen = _placeholder_as_staged(screen)
    if screen.get("state") != "staged":
        return False, {"rule": "screen_not_staged", "screen_state": screen.get("state")}
    if "\n" in text:
        return False, {
            "rule": "multiline_not_screen_verifiable",
            "detail": "a paste marker's line count is not payload identity; deliver a task-file reference instead",
        }
    continuation = screen.get("continuation") or []
    if continuation:
        # item 3: a nonempty continuation row after the matched line is ambiguous — it may be a
        # foreign row a paste/keystroke landed after ours, or our own line wrapping past the pane
        # width. Neither is provable from the screen, so Enter is refused rather than guessed at;
        # dropping paste-count matching alone does not close this, the row itself must be checked.
        return False, {
            "rule": "ambiguous_continuation_row",
            "detail": "one or more extra editor rows follow the staged line; not provably ours",
            "continuation_lines": len(continuation),
            "continuation_preview": continuation[:3],
        }
    got = (screen.get("staged_text") or "").rstrip()
    want = text.rstrip()
    return (got == want), {
        "rule": "exact_single_line",
        "trailing_ws_ignored": text != want,
        "observed_len": len(got),
        "expected_len": len(want),
    }


def queued_is_ours(text: str, screen: dict) -> tuple[bool, dict]:
    """Steer (queued-input) staging rule — the running-turn analogue of staged_is_ours.

    While the agent is running, typed input sits in the editor UNDER the spinner and the screen
    classifies as 'running' (never 'staged'), so staged_is_ours cannot verify it. This accepts a
    single-line payload that is provably ours in EITHER a running editor (input_text) or an
    idle/staged editor (staged_text, e.g. the turn ended between staging and this read) — with the
    SAME exactness staged_is_ours demands: one line, no continuation row, exact match after trailing
    whitespace is normalized (a terminal pads/trims the right edge unreadably). A multi-line payload
    is refused — a paste marker's line count is not identity; submit converts it to a single-line
    task-file reference first. Any other screen state is refused: a steer is never queued into a
    modal, an unknown surface, or a foreign staged draft.
    """
    if "\n" in text:
        return False, {
            "rule": "multiline_not_screen_verifiable",
            "detail": "a paste marker's line count is not payload identity; deliver a task-file reference instead",
        }
    screen = _placeholder_as_staged(screen)
    state = screen.get("state")
    if state == "running":
        got = (screen.get("input_text") or "").rstrip()
    elif state == "staged":
        continuation = screen.get("continuation") or []
        if continuation:
            return False, {
                "rule": "ambiguous_continuation_row",
                "detail": "one or more extra editor rows follow the staged line; not provably ours",
                "continuation_lines": len(continuation),
                "continuation_preview": continuation[:3],
            }
        got = (screen.get("staged_text") or "").rstrip()
    else:
        return False, {"rule": "not_steerable_state", "screen_state": state}
    want = text.rstrip()
    return (got == want), {
        "rule": "exact_single_line_queued",
        "screen_state": state,
        "trailing_ws_ignored": text != want,
        "observed_len": len(got),
        "expected_len": len(want),
    }


class Bridge:
    def __init__(
        self,
        cli: CmuxCLI | None = None,
        store: StateStore | None = None,
        clock=time.time,
        sleep=time.sleep,
    ):
        self.cli = cli or CmuxCLI()
        self.store = store or StateStore()
        self.clock = clock
        self.sleep = sleep

    # ------------------------------------------------------------------ time / budget
    def _now(self) -> str:
        return _iso(self.clock())

    def _budget(self, deadline: float | None, default: float) -> float:
        """Every CLI call inside a bounded operation gets min(default, remaining)."""
        if deadline is None:
            return default
        return max(0.5, min(default, deadline - self.clock()))

    # ------------------------------------------------------------------ transport helpers
    def _events(
        self, after: int, limit: int, timeout: float
    ) -> tuple[dict | None, list[dict]]:
        res = self.cli.run(
            "events",
            "--after",
            str(after),
            "--limit",
            str(limit),
            "--no-heartbeat",
            timeout=timeout,
        )
        if res.error_kind not in (None, "timeout"):
            raise CmuxError(
                res.error_kind, (res.stderr or res.stdout).strip()[:300], res
            )
        ack, evs = None, []
        for line in res.stdout.splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("type") == "ack":
                ack = j
            elif j.get("type") == "event":
                evs.append(j)
        return ack, evs

    @staticmethod
    def _ack_gap(ack: dict | None, boot_id: str | None) -> dict:
        """Every events read keeps its ack. A gap or a changed boot_id is recorded, never ignored."""
        if not ack:
            return {"gap": False, "boot_id": None, "boot_changed": False, "ack": False}
        boot = ack.get("boot_id")
        return {
            "gap": bool(ack.get("resume", {}).get("gap")),
            "boot_id": boot,
            "boot_changed": bool(boot_id and boot and boot != boot_id),
            "ack": True,
            "latest_seq": ack.get("resume", {}).get("latest_seq"),
        }

    def _latest(self, timeout: float = 10.0) -> tuple[str, int]:
        ack, _ = self._events(0, 1, timeout)
        if not ack:
            raise BridgeError("transport", "no ack frame from events stream")
        return ack.get("boot_id", ""), int(ack["resume"]["latest_seq"])

    def _tail(
        self, n: int = MAX_EVENTS, timeout: float = 25.0
    ) -> tuple[str, int, list[dict]]:
        boot, latest = self._latest(min(10.0, timeout))
        after = max(0, latest - n)
        if latest - after <= 0:
            return boot, latest, []
        _, evs = self._events(after, latest - after, timeout)
        return boot, latest, evs

    def _read_screen(
        self, surface: str, lines: int, timeout: float | None = None
    ) -> list[str]:
        lines = max(1, min(int(lines), MAX_LINES))
        try:
            out = self.cli.run_ok(
                "read-screen", "--surface", surface, "--lines", str(lines), timeout=timeout
            )
        except CmuxError as e:
            if e.kind == "no_terminal_view":
                raise BridgeError(
                    "no_terminal_view", NO_TERMINAL_VIEW_INSTRUCTION, surface_uuid=surface
                ) from e
            raise
        return out.split("\n")

    def _screen(
        self, b: dict, lines: int = 40, deadline: float | None = None
    ) -> tuple[dict, list[str]]:
        rows = self._read_screen(b["surface_uuid"], lines, self._budget(deadline, 15.0))
        return classify_screen(rows), rows

    def _workspaces(self, timeout: float | None = None) -> set[str]:
        out = self.cli.run_ok("list-workspaces", "--id-format", "both", timeout=timeout)
        return {
            m.group(0).upper()
            for m in (UUID_RE.search(l) for l in out.splitlines())
            if m
        }

    def _surfaces(self, workspace: str, timeout: float | None = None) -> dict[str, str]:
        out = self.cli.run_ok(
            "list-pane-surfaces",
            "--workspace",
            workspace,
            "--id-format",
            "both",
            timeout=timeout,
        )
        found = {}
        for l in out.splitlines():
            m = UUID_RE.search(l)
            if m:
                found[m.group(0).upper()] = l.strip()
        return found

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except ProcessLookupError:
            return False
        except (PermissionError, OverflowError, ValueError):
            return True

    @staticmethod
    def _proc_start(pid: int) -> str | None:
        """`ps -o lstart= -p <pid>` -> 'Tue Sep  8 11:47:38 2026'. Process start identity: it changes
        when a pid is reused, so it detects the replacement `_pid_alive` cannot. Static so tests and
        the fake can monkeypatch it."""
        try:
            out = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except Exception:
            return None
        return out.strip() or None

    @staticmethod
    def _event_at_or_after_start(event_at, proc_start) -> bool | None:
        """Is the agent hook event fresh — emitted at/after this process's start — so it belongs to
        THIS incarnation rather than a prior one that reused the pid?

        Returns True (fresh), False (stale: clearly predates the start), or None (undecidable —
        either timestamp missing/unparseable). Callers in the identity-critical reconciliation path
        must FAIL CLOSED on anything but True. `event_at` is the hook's ISO-8601 `occurred_at`
        (parsed via `_parse_iso`, so Z / aware / naive-as-UTC all normalise); `proc_start` is
        `ps -o lstart=` local-clock text ('Tue Sep  8 11:47:38 2026'), made aware in the system
        timezone. `lstart` is already truncated DOWN to the whole second, so the parsed start is at
        or before the real start; a genuine post-start event is therefore always >= it and no
        negative grace is applied (any grace would admit a retained event from just before this
        process began — exactly the stale case (c) exists to reject)."""
        if not event_at or not proc_start:
            return None
        try:
            ev = _parse_iso(event_at)
        except ValueError:
            return None
        try:
            ps = datetime.strptime(str(proc_start).strip(), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            return None
        ps = ps.astimezone()  # naive lstart is system-local wall time -> aware
        return ev >= ps

    @staticmethod
    def _pid_cwd(pid: int) -> str | None:
        try:
            out = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except Exception:
            return None
        for l in out.splitlines():
            if l.startswith("n/"):
                return l[1:]
        return None

    @staticmethod
    def _proc_cmux_ids(pid: int) -> dict | None:
        """Surface/workspace UUIDs from the process environment (cmux exports CMUX_SURFACE_ID and
        CMUX_WORKSPACE_ID into every surface's shell). Only those two variables are extracted; the
        rest of the environment (which holds tokens) is discarded and never logged."""
        try:
            out = subprocess.run(
                ["ps", "-Eww", "-o", "command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except Exception:
            return None
        ws = re.search(r"CMUX_WORKSPACE_ID=([0-9A-Fa-f-]{36})", out)
        sf = re.search(r"CMUX_SURFACE_ID=([0-9A-Fa-f-]{36})", out)
        if not (ws and sf):
            return None
        return {
            "workspace_uuid": ws.group(1).upper(),
            "surface_uuid": sf.group(1).upper(),
        }

    @staticmethod
    def _proc_prompt_suggestions(pid: int) -> str:
        """`disabled` when the process environment carries CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false,
        else `enabled_or_unknown`. Same discipline as _proc_cmux_ids: only that one variable is
        extracted; the rest of the environment (tokens) is discarded and never stored or logged."""
        try:
            out = subprocess.run(
                ["ps", "-Eww", "-o", "command=", "-p", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except Exception:
            return "enabled_or_unknown"
        m = re.search(r"(?:^|\s)CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=(\S+)", out)
        return (
            "disabled"
            if m and m.group(1).strip().lower() in ("false", "0", "no", "off")
            else "enabled_or_unknown"
        )

    @staticmethod
    def _suggestion_hint(b: dict, screen: dict) -> str | None:
        """Instruction when a 'staged' editor with no request of ours in flight may merely be a Claude
        Code prompt suggestion (dim ghost text, indistinguishable from typed text in plain-text
        read-screen). None otherwise. Never auto-clears the editor."""
        if (
            screen.get("state") == "staged"
            and not b.get("inflight")
            and b.get("prompt_suggestions") != "disabled"
        ):
            return (
                "the editor may hold a Claude Code prompt suggestion (ghost text); launch sessions via "
                "executor_session.py (sets CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false) or set "
                "promptSuggestionEnabled:false"
            )
        return None

    @staticmethod
    def _proc_argv(pid: int) -> str | None:
        """`ps -ww -o command= -p <pid>` -> the Claude process's own command line (argv only; the
        environment is NOT requested, so no token can leak into a binding or a log). It shows what
        was REQUESTED at launch (`--model opus`), independently of the footer, which shows what the
        session will use next. Static so tests and the fake can monkeypatch it."""
        try:
            out = subprocess.run(
                ["ps", "-ww", "-o", "command=", "-p", str(int(pid))],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except Exception:
            return None
        return out.strip() or None

    def _model_evidence(
        self, claude_pid: int, claude_session_id: str, worktree_realpath: str, screen: dict | None
    ) -> dict:
        """The three independent model readings for a session (each None when unavailable): the live
        footer (what answers next), the launch argv (what was requested), the transcript tail (what
        actually answered). Read-only; no cmux mutation."""
        argv = self._proc_argv(claude_pid) if self._pid_alive(claude_pid) else None
        tr = None
        if claude_session_id and worktree_realpath:
            tr = _transcript_model(
                self._transcript(
                    {
                        "claude_session_id": claude_session_id,
                        "worktree_realpath": worktree_realpath,
                    }
                )
            )
        return {
            "footer": (screen or {}).get("model"),
            "argv": _argv_model(argv),
            "argv_present": argv is not None,
            "transcript": tr,
        }

    @staticmethod
    def _sidebar_index(evs: list[dict]) -> dict[str, dict]:
        idx = {}
        for e in evs:
            if e.get("name") != "sidebar.metadata.updated":
                continue
            m = SIDEBAR_RE.search(e.get("payload", {}).get("args", ""))
            if m:
                idx[m.group(3).upper()] = {
                    "status": m.group(1),
                    "workspace_uuid": m.group(2).upper(),
                    "surface_uuid": m.group(3).upper(),
                    "pid": int(m.group(4)),
                    "seq": e["seq"],
                    "at": e.get("occurred_at"),
                }
        return idx

    @staticmethod
    def _agent_index(evs: list[dict]) -> dict[str, dict]:
        idx = {}
        for e in evs:
            if not e.get("name", "").startswith("agent.hook."):
                continue
            p = e.get("payload", {})
            sid = canonical_session_id(p.get("session_id"))
            if sid:
                idx[sid] = {
                    "session_id": sid,
                    "pid": p.get("_ppid"),
                    "cwd": p.get("cwd"),
                    "workspace_uuid": (
                        p.get("workspace_id") or e.get("workspace_id") or ""
                    ).upper(),
                    "last_event": p.get("hook_event_name"),
                    "seq": e["seq"],
                    "at": e.get("occurred_at"),
                }
        return idx

    @staticmethod
    def _session_events(
        evs: list[dict], session_id: str, pid: int, name: str, after_seq: int
    ) -> list[dict]:
        out = []
        for e in evs:
            if e.get("name") != name or e.get("seq", 0) <= after_seq:
                continue
            p = e.get("payload", {})
            if canonical_session_id(p.get("session_id")) == session_id and int(p.get("_ppid") or -1) == int(
                pid
            ):
                out.append(e)
        return out

    # ------------------------------------------------------------------ transcript (durable truth)
    @staticmethod
    def transcript_path(session_id: str, worktree_realpath: str) -> Path:
        root = Path(
            os.environ.get("CMUX_BRIDGE_CLAUDE_PROJECTS")
            or (Path.home() / ".claude/projects")
        )
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(worktree_realpath))
        return root / slug / f"{str(session_id).removeprefix('claude-')}.jsonl"

    def _transcript(self, b: dict) -> list[dict]:
        """Tail-bounded read of the durable Claude Code transcript for this session."""
        p = self.transcript_path(b["claude_session_id"], b["worktree_realpath"])
        try:
            size = p.stat().st_size
            with open(p, "rb") as f:
                if size > TRANSCRIPT_TAIL_BYTES:
                    f.seek(size - TRANSCRIPT_TAIL_BYTES)
                    f.readline()  # drop the partial line
                raw = f.read()
        except OSError:
            return []
        out = []
        for line in raw.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    @staticmethod
    def _entry_text(entry: dict) -> str:
        c = (entry.get("message") or {}).get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "".join(
                x.get("text", "")
                for x in c
                if isinstance(x, dict) and x.get("type") == "text"
            )
        return ""

    def _user_messages(self, b: dict, entries: list[dict] | None = None) -> list[dict]:
        out = []
        for e in entries if entries is not None else self._transcript(b):
            if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary"):
                continue
            if e.get("sessionId") and e["sessionId"] != str(
                b["claude_session_id"]
            ).removeprefix("claude-"):
                continue
            text = self._entry_text(e)
            out.append(
                {
                    "text": text,
                    "sha256": sha256_text(text),
                    "at": e.get("timestamp"),
                    "uuid": e.get("uuid"),
                }
            )
        return out

    @staticmethod
    def _systems(entries: list[dict], subtype: str) -> list[dict]:
        return [
            e
            for e in entries
            if e.get("type") == "system" and e.get("subtype") == subtype
        ]

    def _find_user_message(
        self, b: dict, since_iso: str, matcher, entries: list[dict] | None = None
    ) -> tuple[dict | None, dict | None]:
        """Exact correlation. Returns (ours, foreign) among user messages at/after `since_iso`."""
        since = _parse_iso(since_iso)
        mine = foreign = None
        for m in self._user_messages(b, entries):
            if not m.get("at"):
                continue
            try:
                ts = _parse_iso(m["at"])
            except ValueError:
                continue
            if ts < since:
                continue
            if matcher(m):
                mine = mine or m
            else:
                foreign = foreign or m
        return mine, foreign

    def _transcript_summary(self, b: dict) -> dict:
        entries = self._transcript(b)
        users = self._user_messages(b, entries)
        turns = self._systems(entries, "turn_duration")
        comps = self._systems(entries, "compact_boundary")
        p = self.transcript_path(b["claude_session_id"], b["worktree_realpath"])
        return {
            "path_exists": p.is_file(),
            "user_messages": len(users),
            "turn_ends": len(turns),
            "compact_boundaries": len(comps),
            "last_user_at": users[-1]["at"] if users else None,
            "last_turn_end_at": turns[-1].get("timestamp") if turns else None,
            "last_compact_at": comps[-1].get("timestamp") if comps else None,
        }

    # ------------------------------------------------------------------ discover
    def discover(
        self, workspace_uuid: str | None = None, max_targets: int = 20
    ) -> dict:
        max_targets = max(1, min(int(max_targets), 50))
        ping = self.cli.run("ping", timeout=10)
        if not ping.ok:
            req = {
                "access_denied": "cmux socket is in cmuxOnly mode and this process is not descended from the cmux app. Options: run the controller inside a cmux terminal, or set automation.socketControlMode=password in cmux Settings > Automation and provide the password via CMUX_BRIDGE_PASSWORD_FILE (owner-readable only). Never allowAll; no TCP.",
                "socket_missing": "cmux socket path not found; is cmux running? (CMUX_BRIDGE_SOCKET overrides the path)",
                "connection_refused": "socket file exists but nothing accepts connections (stale socket or cmux restarting)",
                "cli_missing": "cmux CLI not found; set CMUX_BRIDGE_CLI",
            }.get(ping.error_kind or "other", "cmux did not answer ping")
            return {
                "contract": CONTRACT,
                "transport": {
                    "ok": False,
                    "error_kind": ping.error_kind,
                    "detail": (ping.stderr or ping.stdout).strip()[:300],
                    "requirement": req,
                },
                "targets": [],
            }
        caps = self.cli.run("capabilities", timeout=10)
        access_mode, version = None, None
        try:
            cj = json.loads(caps.stdout)
            access_mode = cj.get("access_mode")
            version = cj.get("version") or cj.get("app_version")
        except ValueError:
            pass
        ident = {}
        try:
            ident = json.loads(self.cli.run_ok("identify", "--json"))
        except (CmuxError, ValueError):
            pass
        boot, latest, evs = self._tail()
        sidebar = self._sidebar_index(evs)
        agents = self._agent_index(evs)
        by_pid = {a["pid"]: a for a in agents.values() if a.get("pid")}
        targets, seen_pids = [], set()
        for surf, s in sidebar.items():
            if workspace_uuid and s["workspace_uuid"] != workspace_uuid.upper():
                continue
            a = by_pid.get(s["pid"], {})
            seen_pids.add(s["pid"])
            targets.append(
                {
                    **s,
                    "link": "sidebar_event",
                    "alive": self._pid_alive(s["pid"]),
                    "claude_session_id": a.get("session_id"),
                    "cwd": a.get("cwd"),
                    "last_agent_event": a.get("last_event"),
                    "last_agent_seq": a.get("seq"),
                    "model": self._target_model(s["pid"], a.get("session_id"), a.get("cwd")),
                }
            )
        for (
            pid,
            a,
        ) in by_pid.items():  # sessions with hook events but no sidebar status yet
            if pid in seen_pids or not self._pid_alive(pid):
                continue
            ids = self._proc_cmux_ids(pid)
            if not ids or (
                workspace_uuid and ids["workspace_uuid"] != workspace_uuid.upper()
            ):
                continue
            targets.append(
                {
                    "status": "unknown",
                    **ids,
                    "pid": pid,
                    "seq": a.get("seq"),
                    "at": a.get("at"),
                    "link": "process_env",
                    "alive": True,
                    "claude_session_id": a.get("session_id"),
                    "cwd": a.get("cwd"),
                    "last_agent_event": a.get("last_event"),
                    "last_agent_seq": a.get("seq"),
                    "model": self._target_model(pid, a.get("session_id"), a.get("cwd")),
                }
            )
        targets.sort(key=lambda t: -t["seq"])
        return {
            "contract": CONTRACT,
            "transport": {
                "ok": True,
                "access_mode": access_mode,
                "cmux_version": version,
                "socket_path": ident.get("socket_path"),
                "boot_id": boot,
                "latest_seq": latest,
            },
            "caller": ident.get("caller"),
            "focused": ident.get("focused"),
            "targets": targets[:max_targets],
            "targets_truncated": len(targets) > max_targets,
            "note": "targets come from sidebar status events in the retained window; a session with no recent status change may be absent until its next event",
        }

    def _target_model(self, pid, session_id, cwd) -> dict:
        """Off-screen model evidence for a discover target (no surface read): the launch argv and
        the transcript tail. `fable_evidence` marks a planning session that an implementation bind
        will refuse. The live footer is read at bind, not here."""
        ev = self._model_evidence(int(pid), session_id, cwd, None)
        argv_fam, tr_fam = model_family(ev["argv"]), model_family(ev["transcript"])
        return {
            "argv": ev["argv"],
            "argv_present": ev["argv_present"],
            "argv_family": argv_fam,
            "transcript": ev["transcript"],
            "transcript_family": tr_fam,
            "fable_evidence": "fable" in (argv_fam, tr_fam),
        }

    # ------------------------------------------------------------------ leases
    def _lease_live(self, rel: str) -> dict | None:
        l = self.store.read(rel)
        if not l or _parse_iso(l["expires_at"]).timestamp() <= self.clock():
            return None
        return l

    @staticmethod
    def _wt_key(real: str) -> str:
        return hashlib.sha1(real.encode()).hexdigest()[:16]

    def bind(
        self,
        workspace_uuid: str,
        surface_uuid: str,
        claude_session_id: str,
        claude_pid: int,
        worktree_realpath: str,
        controller_id: str,
        role: str = "writer",
        lease_ttl_s: int = DEFAULT_LEASE_S,
        purpose: str = DEFAULT_PURPOSE,
        expected_model: str | None = None,
    ) -> dict:
        if role not in ("writer", "monitor"):
            raise BridgeError("bad_request", "role must be writer or monitor")
        if (
            not isinstance(controller_id, str)
            or not controller_id.strip()
            or len(controller_id) > 200
        ):
            raise BridgeError(
                "bad_request",
                "controller_id must be a non-empty, non-whitespace string of at most 200 chars",
            )
        if purpose not in PURPOSES:
            raise BridgeError(
                "bad_request", f"purpose must be one of {list(PURPOSES)}", purpose=purpose
            )
        for name, v in (
            ("workspace_uuid", workspace_uuid),
            ("surface_uuid", surface_uuid),
        ):
            if not UUID_RE.fullmatch(v or ""):
                raise BridgeError(
                    "bad_request",
                    f"{name} must be a UUID (refs like surface:N are not identities)",
                )
        workspace_uuid, surface_uuid = workspace_uuid.upper(), surface_uuid.upper()
        claude_session_id = canonical_session_id(claude_session_id)
        if not str(claude_session_id).startswith("claude-"):
            claude_session_id = "claude-" + str(claude_session_id)
        lease_ttl_s = max(60, min(int(lease_ttl_s), 24 * 3600))
        claude_pid = int(claude_pid)
        real = canonical_dir(worktree_realpath)

        # 1. the workspace itself exists, and the surface is a member of it
        try:
            if workspace_uuid not in self._workspaces():
                raise BridgeError(
                    "identity",
                    "workspace UUID is not present in list-workspaces",
                    workspace=workspace_uuid,
                )
            surfaces = self._surfaces(workspace_uuid)
        except CmuxError as e:
            raise BridgeError(
                "identity", f"workspace lookup failed: {e.kind}", detail=e.message
            )
        if surface_uuid not in surfaces:
            raise BridgeError(
                "identity",
                "surface UUID not present in workspace",
                surfaces=list(surfaces),
            )
        # 2. the process is alive, and we record its start identity
        if not self._pid_alive(claude_pid):
            raise BridgeError("identity", f"pid {claude_pid} is not alive")
        proc_start = self._proc_start(claude_pid)
        # 3. surface <-> pid link
        boot, latest, evs = self._tail()
        sb = self._sidebar_index(evs).get(surface_uuid)
        link_source = None
        if sb and sb["pid"] == claude_pid:
            link_source = "sidebar_event"
        else:
            env_ids = self._proc_cmux_ids(claude_pid)
            if (
                env_ids
                and env_ids["surface_uuid"] == surface_uuid
                and env_ids["workspace_uuid"] == workspace_uuid
            ):
                link_source = "process_env"
        if not link_source:
            raise BridgeError(
                "bind_evidence_missing",
                "neither a sidebar status event nor the process environment links this surface to that pid",
                sidebar=sb,
            )
        # 4. session <-> pid <-> cwd. A writer REQUIRES an agent hook event; a monitor may be partial.
        ag = self._agent_index(evs).get(claude_session_id)
        if ag and int(ag.get("pid") or -1) != claude_pid:
            raise BridgeError(
                "identity", "session id belongs to a different pid", agent=ag
            )
        # The hook event's self-reported workspace_id (ag["workspace_uuid"]) is stamped by cmux and
        # has been observed to CONFLICT with the requested workspace even when the session is
        # genuinely in it (retained preflight1: the process environment AND list-surfaces both
        # confirmed the requested workspace/surface while the hook event carried a different one;
        # the upstream cause and the actual hook subprocess environment remain UNPROVEN). Do not
        # silently drop the conflict and do not silently trust it: on a differing event workspace,
        # RE-VERIFY LIVE from the current process before admitting the bind, and refuse anything the
        # live process cannot confirm. Three independent live signals must all hold, else identity:
        #   (a) a FRESH _proc_cmux_ids read (not the cached step-3 link) shows this pid in the
        #       requested workspace AND surface;
        #   (b) the process-start identity is STABLE across the reconciliation re-read — a pid that
        #       was replaced mid-bind shows a different lstart;
        #   (c) the retained agent event is FRESH (occurred at/after this process's start), so it
        #       belongs to THIS incarnation, not an older event a reused pid inherited. A reused pid
        #       can match live workspace/surface/cwd while an older retained event names a prior
        #       session; (c) is what separates those.
        # Surface membership (step 1), session<->pid<->cwd and the recorded proc_start are preserved
        # unchanged; this narrow gate only reconciles the proven-unreliable event workspace field.
        hook_ws = (ag or {}).get("workspace_uuid") or None
        hook_ws_conflict = bool(hook_ws) and hook_ws != workspace_uuid
        hook_ws_reconciled = False
        recon_ids = None
        recon_proc_start = None
        event_fresh = None
        if hook_ws_conflict:
            recon_ids = self._proc_cmux_ids(claude_pid)
            recon_proc_start = self._proc_start(claude_pid)
            event_fresh = self._event_at_or_after_start(
                (ag or {}).get("at"), proc_start
            )
            if not (
                recon_ids
                and recon_ids.get("workspace_uuid") == workspace_uuid
                and recon_ids.get("surface_uuid") == surface_uuid
                and proc_start
                and recon_proc_start
                and recon_proc_start == proc_start
                and event_fresh is True
            ):
                raise BridgeError(
                    "identity",
                    "agent hook event reports a different workspace; reconciling it requires the "
                    "live process environment to confirm the requested workspace and surface, a "
                    "stable process-start identity, and a fresh (post-start) event, and at least "
                    "one of these is unconfirmed",
                    hook_event_workspace=hook_ws,
                    requested_workspace=workspace_uuid,
                    requested_surface=surface_uuid,
                    live_process_ids=recon_ids,
                    proc_start=proc_start,
                    reconcile_proc_start=recon_proc_start,
                    event_fresh=event_fresh,
                    event_at=(ag or {}).get("at"),
                )
            hook_ws_reconciled = True
        agent_ok = (
            bool(ag) and canonical_dir(ag.get("cwd") or "/nonexistent") == real
        )
        if (
            ag
            and role == "writer"
            and canonical_dir(ag.get("cwd") or "/nonexistent") != real
        ):
            raise BridgeError(
                "identity",
                "worktree realpath does not match the session's cwd from its agent event",
                session_cwd=ag.get("cwd"),
                requested=real,
                agent=ag,
            )
        if role == "writer" and not agent_ok:
            raise BridgeError(
                "bind_evidence_missing",
                "no agent.hook event for this session/pid in the retained window with a matching cwd; a writer binding requires that evidence",
                agent=ag,
                worktree=real,
                instruction="bind with role=monitor for bounded observation, or let the session emit a hook event (any prompt/tool use) and bind again",
            )
        identity_evidence = "full" if agent_ok else "partial"
        cwd = (ag or {}).get("cwd") or self._pid_cwd(claude_pid)
        if cwd is None:
            raise BridgeError(
                "bind_evidence_missing",
                "could not establish the session's cwd from events or the process",
            )
        if canonical_dir(cwd) != real:
            raise BridgeError(
                "identity",
                "worktree realpath does not match the session's cwd",
                session_cwd=cwd,
                requested=real,
            )
        # 5. the surface shows Claude
        try:
            screen, _ = self._screen({"surface_uuid": surface_uuid}, 40)
        except CmuxError as e:
            raise BridgeError(
                "unknown_state", f"cannot read surface: {e.kind}", detail=e.message
            )
        if not screen.get("claude"):
            raise BridgeError(
                "not_claude",
                "surface does not show a Claude Code prompt/footer",
                screen=screen,
            )
        # 5b. purpose / model gate (model-role policy). A WRITER binding
        # is the authority to dispatch work into this session, so for purpose=implementation the
        # session must be on an implementation model NOW (footer) and must not be a Fable planning
        # session being reused (argv / transcript). An unreadable footer never passes. A monitor
        # only observes, so its verdict is recorded but not enforced. Window/focus play no part.
        model_ev = self._model_evidence(claude_pid, claude_session_id, real, screen)
        model_check = check_session_model(
            purpose,
            footer=model_ev["footer"],
            argv=model_ev["argv"],
            transcript=model_ev["transcript"],
            expected=expected_model,
        )
        if role == "writer" and not model_check["ok"]:
            raise BridgeError(
                model_check["code"],
                model_check["reason"],
                purpose=purpose,
                model=model_ev,
                instruction=(
                    "launch a dedicated executor with an explicit --model (scripts/executor_session.py "
                    "launch) or bind a planning session with purpose=planning; do not implement in a "
                    "planning-only session (see TANDEM_PLANNING_ONLY_MODELS)"
                    if model_check["code"] != "model_policy_config"
                    else "TANDEM_PLANNING_ONLY_MODELS is malformed; set it to a comma-separated list "
                    "of model families (fable, opus, sonnet, haiku) or unset it, then bind again"
                ),
            )
        # 6. leases + binding: one transaction, no CLI call inside
        now = self.clock()
        binding_id = f"b-{secrets.token_hex(6)}"
        expires = _iso(now + lease_ttl_s)
        wt_key = self._wt_key(real)
        surf_rel, wt_rel = (
            f"leases/surface-{surface_uuid}.json",
            f"leases/worktree-{wt_key}.json",
        )
        b = {
            "contract": CONTRACT,
            "binding_id": binding_id,
            "controller_id": controller_id,
            "role": role,
            "workspace_uuid": workspace_uuid,
            "surface_uuid": surface_uuid,
            "claude_pid": claude_pid,
            "claude_session_id": claude_session_id,
            "proc_start": proc_start,
            "worktree_realpath": real,
            "worktree_key": wt_key,
            "boot_id": boot,
            "cursor_seq": latest,
            "revision": 1,
            "inflight": None,
            "lease_expires_at": expires,
            "bound_at": self._now(),
            "released": False,
            "superseded": False,
            "identity_evidence": identity_evidence,
            "purpose": purpose,
            "prompt_suggestions": self._proc_prompt_suggestions(claude_pid),
            "model": {
                **model_ev,
                "family": model_check["family"],
                "expected": expected_model,
                "verified": bool(model_check["ok"]),
                "code": model_check["code"],
                "verified_at": self._now(),
            },
            "hook_event_workspace": hook_ws,
            "hook_event_workspace_conflict": hook_ws_conflict,
            "hook_event_workspace_reconciled": hook_ws_reconciled,
            "workspace_reconciliation": (
                {
                    "conflicting_event_workspace": hook_ws,
                    "authoritative_workspace": workspace_uuid,
                    "authoritative_surface": surface_uuid,
                    "live_process_ids": recon_ids,
                    "reconcile_proc_start": recon_proc_start,
                    "event_at": (ag or {}).get("at"),
                    "event_fresh": event_fresh,
                    "sources": [
                        "surface_membership",
                        "process_env_live",
                        "proc_start_stable",
                        "event_post_start",
                        "session_pid_cwd",
                    ],
                }
                if hook_ws_reconciled
                else None
            ),
            "evidence": {
                "surface_pid_link": link_source,
                "sidebar_seq": (sb or {}).get("seq"),
                "agent_seq": (ag or {}).get("seq"),
                "agent_event": (ag or {}).get("last_event"),
                "cwd_source": "agent_event" if ag and ag.get("cwd") else "process",
                "proc_start": proc_start,
                "screen": screen,
            },
        }
        with self.store.lock():
            surf_lease = self._lease_live(surf_rel)
            wt_lease = self._lease_live(wt_rel)
            if role == "writer":
                for l in (surf_lease, wt_lease):
                    if (
                        l
                        and l.get("role") == "writer"
                        and l.get("controller_id") != controller_id
                    ):
                        raise BridgeError(
                            "lease_conflict",
                            "another controller holds the writer lease",
                            holder=l,
                        )
                superseded = {
                    l["binding_id"]
                    for l in (surf_lease, wt_lease)
                    if l and l.get("binding_id") and l["binding_id"] != binding_id
                }
                # one Claude per controller: any OTHER live writer binding this controller holds
                # (another surface/worktree) is superseded in the same transaction.
                extra = set()
                for p in self.store.list("bindings"):
                    ob = self.store.read(f"bindings/{p.stem}.json")
                    if (
                        ob
                        and ob.get("binding_id") not in (None, binding_id)
                        and ob.get("role") == "writer"
                        and ob.get("controller_id") == controller_id
                        and not ob.get("released")
                        and not ob.get("superseded")
                        and _parse_iso(ob["lease_expires_at"]).timestamp() > now
                    ):
                        extra.add(ob["binding_id"])
                extra -= superseded
                superseded |= extra
                # a pause survives a rebind of the same surface, even after release or lease
                # expiry: the most recent writer binding on this surface decides (a resume on it
                # clears the pause for good; an older paused one is never resurrected).
                prior = [
                    ob
                    for p in self.store.list("bindings")
                    for ob in [self.store.read(f"bindings/{p.stem}.json")]
                    if ob
                    and ob.get("binding_id") != binding_id
                    and ob.get("role") == "writer"
                    and ob.get("surface_uuid") == surface_uuid
                ]
                if prior:
                    last = max(prior, key=lambda o: (o.get("bound_at", ""), o["binding_id"]))
                    if last.get("paused"):
                        b["paused"] = last["paused"]
                self.store.write(
                    surf_rel,
                    {
                        "binding_id": binding_id,
                        "controller_id": controller_id,
                        "role": role,
                        "expires_at": expires,
                        "surface_uuid": surface_uuid,
                    },
                )
                self.store.write(
                    wt_rel,
                    {
                        "binding_id": binding_id,
                        "controller_id": controller_id,
                        "role": role,
                        "expires_at": expires,
                        "surface_uuid": surface_uuid,
                        "worktree_realpath": real,
                    },
                )
                self.store.write(f"bindings/{binding_id}.json", b)
                for old in superseded:
                    ob = self.store.read(f"bindings/{old}.json")
                    if ob:
                        for lrel in (
                            f"leases/surface-{ob['surface_uuid']}.json",
                            f"leases/worktree-{ob['worktree_key']}.json",
                        ):
                            l = self.store.read(lrel)
                            if l and l.get("binding_id") == old:
                                self.store.delete(lrel)
                        ob["released"] = True
                        ob["released_at"] = self._now()
                        ob["superseded"] = True
                        ob["superseded_by"] = binding_id
                        ob["superseded_at"] = self._now()
                        self.store.write(f"bindings/{old}.json", ob)
                b["superseded_bindings"] = sorted(superseded)
            else:
                self.store.write(f"bindings/{binding_id}.json", b)
        self.store.append_receipt(
            "bind",
            {
                "binding_id": binding_id,
                "controller_id": controller_id,
                "role": role,
                "surface_uuid": surface_uuid,
                "claude_pid": claude_pid,
                "claude_session_id": claude_session_id,
                "identity_evidence": identity_evidence,
                "purpose": purpose,
                "model_family": model_check["family"],
                "model_verified": bool(model_check["ok"]),
            },
        )
        return b

    # ------------------------------------------------------------------ binding load / repair
    def _load(
        self, binding_id: str, controller_id: str, need_writer: bool = False
    ) -> dict:
        with self.store.lock():
            b = self.store.read(f"bindings/{binding_id}.json")
            if not b:
                raise BridgeError("unknown_binding", f"no binding {binding_id}")
            self._require_owner(b, controller_id)
            if need_writer and b.get("superseded"):
                raise BridgeError(
                    "lease_lost",
                    "this binding was replaced by a newer one",
                    superseded_by=b.get("superseded_by"),
                )
            if b.get("released"):
                raise BridgeError("released", "binding was released; bind again")
            if _parse_iso(b["lease_expires_at"]).timestamp() <= self.clock():
                raise BridgeError(
                    "lease_expired",
                    "lease expired; bind again",
                    expired_at=b["lease_expires_at"],
                )
            if need_writer:
                if b["role"] != "writer":
                    raise BridgeError(
                        "not_writer",
                        "this binding is a monitor; it cannot submit or compact",
                    )
                if b.get("superseded"):
                    raise BridgeError(
                        "lease_lost",
                        "this binding was replaced by a newer one",
                        superseded_by=b.get("superseded_by"),
                    )
                for rel in (
                    f"leases/surface-{b['surface_uuid']}.json",
                    f"leases/worktree-{b['worktree_key']}.json",
                ):
                    l = self._lease_live(rel)
                    if not l or l.get("binding_id") != binding_id:
                        raise BridgeError(
                            "lease_lost",
                            "the writer lease is no longer held by this binding",
                            lease=rel,
                            holder=l,
                        )
                b = self._repair(b)
            return b

    @staticmethod
    def _require_owner(b: dict, controller_id: str) -> None:
        if not controller_id or b.get("controller_id") != controller_id:
            raise BridgeError(
                "not_owner",
                "only the controller that owns this binding may use it",
            )

    def _repair(self, b: dict) -> dict:
        """Crash between the request record and the binding bump: the record is written first with
        `revision_after`, so a higher accepted/completed inflight record repairs the binding."""
        rid = b.get("inflight")
        if not rid:
            return b
        rec = self.store.read(f"requests/{b['binding_id']}/{rid}.json")
        if not rec:
            return b
        if rec.get("status") in ("accepted", "completed") and int(
            rec.get("revision_after") or 0
        ) > int(b["revision"]):
            b["revision"] = int(rec["revision_after"])
            b["inflight"] = None
            b["repaired_from"] = rid
            self.store.write(f"bindings/{b['binding_id']}.json", b)
        elif rec.get("status") in TERMINAL_STATUSES:
            b["inflight"] = None
            self.store.write(f"bindings/{b['binding_id']}.json", b)
        return b

    def _validate(self, b: dict, deadline: float | None = None) -> dict:
        """Identity re-check: surface membership, pid alive AND same process start, boot_id, and
        (from the retained events) no SessionEnd and no newer agent event for this pid under a
        different session id."""
        problems = []
        try:
            if b["surface_uuid"] not in self._surfaces(
                b["workspace_uuid"], self._budget(deadline, 15.0)
            ):
                problems.append("surface_missing")
        except CmuxError as e:
            problems.append(f"transport:{e.kind}")
        if not self._pid_alive(b["claude_pid"]):
            problems.append("pid_dead")
        else:
            now_start = self._proc_start(b["claude_pid"])
            if b.get("proc_start") and now_start and now_start != b["proc_start"]:
                problems.append("proc_start_changed")
        latest = b["cursor_seq"]
        try:
            boot, latest, evs = self._tail(MAX_EVENTS, self._budget(deadline, 25.0))
            if boot != b["boot_id"]:
                problems.append("boot_id_changed")
            if self._session_events(
                evs,
                b["claude_session_id"],
                b["claude_pid"],
                "agent.hook.SessionEnd",
                b["cursor_seq"] - 1,
            ):
                problems.append("session_ended")
            for e in evs:
                p = e.get("payload", {})
                if (
                    str(e.get("name", "")).startswith("agent.hook.")
                    and int(p.get("_ppid") or -1) == int(b["claude_pid"])
                    and p.get("session_id")
                    and canonical_session_id(p["session_id"]) != b["claude_session_id"]
                    and e.get("seq", 0) > b["cursor_seq"]
                ):
                    problems.append("session_replaced")
                    break
        except (CmuxError, BridgeError) as e:
            problems.append(f"events:{getattr(e, 'kind', getattr(e, 'code', 'error'))}")
        return {
            "ok": not problems,
            "problems": sorted(set(problems)),
            "latest_seq": latest,
        }

    # ------------------------------------------------------------------ observe
    def observe(
        self,
        binding_id: str,
        controller_id: str,
        lines: int = 30,
        max_events: int = 200,
        after_seq: int | None = None,
    ) -> dict:
        """Raw observations. Never mutates the binding; the cursor is caller-owned (review item 10)."""
        b = self._load(binding_id, controller_id)
        v = self._validate(b)
        after = int(after_seq) if after_seq is not None else int(b["cursor_seq"])
        if not v["ok"]:
            return {
                "binding_id": binding_id,
                "state": "unknown",
                "reason": "identity check failed",
                "identity": v,
                "revision": b["revision"],
                "after_seq": after,
                "next_after_seq": after,
            }
        read_error = None
        try:
            screen, rows = self._screen(b, lines)
        except BridgeError as e:
            if e.code != "no_terminal_view":
                raise
            read_error = {"error_kind": e.code, "instruction": e.message}
            rows, screen = (
                [],
                {
                    "state": "unknown",
                    "reason": f"read failed: {e.code}",
                    "ctx_used_pct": None,
                    "background_agents": None,
                    "claude": None,
                },
            )
        except CmuxError as e:
            rows, screen = (
                [],
                {
                    "state": "unknown",
                    "reason": f"read failed: {e.kind} {e.message}",
                    "ctx_used_pct": None,
                    "background_agents": None,
                    "claude": None,
                },
            )
        max_events = max(1, min(int(max_events), MAX_EVENTS))
        gap = {"gap": False, "boot_changed": False, "ack": False}
        evs = []
        if v["latest_seq"] > after:
            ack, evs = self._events(after, min(max_events, v["latest_seq"] - after), 20)
            gap = self._ack_gap(ack, b["boot_id"])
        sess = [
            e
            for e in evs
            if canonical_session_id(e.get("payload", {}).get("session_id"))
            == b["claude_session_id"]
        ]
        pct = screen.get("ctx_used_pct")
        return {
            **(read_error or {}),
            "binding_id": binding_id,
            "revision": b["revision"],
            "state": screen["state"],
            "reason": screen["reason"],
            "ctx_used_pct": pct,
            "background_agents": screen.get("background_agents"),
            "staged_tail": screen.get("staged_tail"),
            **(
                {"instruction": h}
                if (h := self._suggestion_hint(b, screen))
                else {}
            ),
            "screen_tail": rows[-8:],
            "events": {
                "new": len(evs),
                "session": len(sess),
                "last_stop_seq": max(
                    [e["seq"] for e in sess if e["name"] == "agent.hook.Stop"],
                    default=None,
                ),
                "last_pretooluse_seq": max(
                    [e["seq"] for e in sess if e["name"] == "agent.hook.PreToolUse"],
                    default=None,
                ),
                "last_userpromptsubmit_seq": max(
                    [
                        e["seq"]
                        for e in sess
                        if e["name"] == "agent.hook.UserPromptSubmit"
                    ],
                    default=None,
                ),
                **gap,
            },
            "after_seq": after,
            "next_after_seq": max([e["seq"] for e in evs], default=after),
            "boot_id": b["boot_id"],
            "identity": v,
            "inflight_request": b.get("inflight"),
            "paused": b.get("paused"),
            "transcript": self._transcript_summary(b),
            "compaction": {
                "due": pct is not None and pct >= COMPACT_DUE_PCT,
                "overdue": pct is not None and pct >= COMPACT_LIMIT_PCT,
                "policy": f"due at {COMPACT_DUE_PCT:.0f}% used, before {COMPACT_LIMIT_PCT:.0f}% used",
            },
            "note": "raw observation; the cursor is not persisted, pass next_after_seq back yourself",
        }

    # ------------------------------------------------------------------ submit
    @staticmethod
    def _check_text(text: str, kind: str) -> None:
        if not text or not text.strip():
            raise BridgeError("bad_request", "text is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise BridgeError(
                "bad_request",
                f"text exceeds {MAX_TEXT_CHARS} chars; deliver a file path plus a short instruction instead",
            )
        if [c for c in text if ord(c) < 32 and c not in "\n\t"]:
            raise BridgeError(
                "bad_request", "text contains control characters other than newline/tab"
            )
        if kind == "task" and text.lstrip().startswith("/"):
            raise BridgeError(
                "bad_request",
                "slash commands are not tasks; use bridge_compact for /compact",
            )

    def _mark(self, rec: dict, rel: str, status: str | None = None, **kw) -> dict:
        if status:
            rec["status"] = status
        rec.update(kw)
        rec.setdefault("history", []).append(
            {
                "at": self._now(),
                "status": rec["status"],
                **{k: v for k, v in kw.items() if k != "history"},
            }
        )
        with self.store.lock():
            self.store.write(rel, rec)
        return rec

    def _deliver(self, b: dict, text: str, deadline: float | None) -> None:
        """Deliver one *single-line* payload with `send`. Multi-line/long payloads never reach
        here: submit converts them to an immutable task-file reference (a single line) first,
        because a raw multi-line paste cannot be proved ours from the screen and, on this Claude
        Code build (2.1.263), a raw `\x1b[200~...\x1b[201~` sequence auto-submits fragments during
        `send` itself (captured 2026-09-08 under checks/paste-probe-*). Nothing is submitted here:
        the caller verifies the staging before any Enter."""
        if "\n" in text:
            raise BridgeError(
                "bad_delivery",
                "multi-line text must be delivered as a task-file reference; reaching _deliver with a newline is a bug",
            )
        self.cli.run_ok(
            "send",
            "--surface",
            b["surface_uuid"],
            "--",
            text,
            timeout=self._budget(deadline, 15.0),
        )

    def _receipt(self, b: dict, rec: dict, duplicate: bool) -> dict:
        return {
            "status": rec["status"],
            "request_id": rec["request_id"],
            "revision": b["revision"],
            "evidence": rec.get("acceptance"),
            "send_result": rec.get("send_result"),
            "event_gap": rec.get("event_gap"),
            # receipt hash distinction (finding 3 addendum): the ORIGINAL payload hash vs. the hash
            # of the short single-line reference actually staged and matched in the transcript.
            # They differ for task_file deliveries; acceptance proves acceptance of the reference,
            # not that the full brief was read -- the executor's own result is that evidence.
            "text_sha256": rec.get("text_sha256"),
            "delivered_text_sha256": rec.get("delivered_text_sha256"),
            "delivery_mode": rec.get("delivery_mode"),
            "duplicate_call": duplicate,
            "replayed_from": "request_record" if duplicate else None,
            "note": "terminal request record replayed verbatim; nothing was sent"
            if duplicate
            else None,
        }

    def submit(
        self,
        binding_id: str,
        controller_id: str,
        request_id: str,
        text: str,
        expected_revision: int,
        kind: str = "task",
        accept_timeout_s: float = 12.0,
        pre_enter_gate=None,
    ) -> dict:
        """Stage a payload, press Enter at most once on a *verified* staging, then correlate.

        A single-line payload is delivered inline; a multi-line or long payload is written to an
        immutable, bridge-owned task file and the delivered line is a short reference to it, so the
        editor never holds unverifiable pasted text. Deliveries on one binding are serialised (a
        per-binding lock): a concurrent call observes the in-flight operation instead of delivering
        a second time. A duplicate call after completion replays the persisted receipt. Nothing is
        resent after an ambiguous outcome; only a simulated pre-send fault is a resend basis.

        ``pre_enter_gate`` (optional): a caller callback invoked with no args immediately BEFORE the
        real Enter keystroke — i.e. AFTER all staging/waits — at every Enter site (fresh delivery and
        reconcile resend). Raising from it withholds the Enter and nothing is sent (the staged text is
        left for inspection/reconcile, exactly like the writer-lease pre-Enter re-check). Independently of
        this gate, the bridge re-reads the binding from disk right before Enter and withholds it if the
        binding was paused (bridge_pause) during this submit's staging. Default None adds no extra veto.
        """
        if kind not in SUBMIT_KINDS:
            raise BridgeError(
                "bad_request", f"kind must be one of {list(SUBMIT_KINDS)}"
            )
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", request_id or ""):
            raise BridgeError(
                "bad_request", "request_id must be 1-80 chars of [A-Za-z0-9._:-]"
            )
        accept_timeout_s = clamp_timeout(accept_timeout_s, 12.0, 1.0, 60.0)
        deadline = self.clock() + accept_timeout_s + 60.0
        b = self._load(binding_id, controller_id, need_writer=True)
        self._assert_not_paused(binding_id)
        rel = f"requests/{binding_id}/{request_id}.json"
        try:
            with self.store.lock(f"delivery-{binding_id}", timeout_s=3.0):
                return self._submit_locked(
                    b,
                    binding_id,
                    request_id,
                    text,
                    int(expected_revision),
                    kind,
                    rel,
                    accept_timeout_s,
                    deadline,
                    pre_enter_gate,
                )
        except StateError as e:
            if getattr(e, "code", "") != "lock_timeout":
                raise
        # another call holds the per-binding delivery lock: observe, never deliver a second time
        with self.store.lock():
            rec = self.store.read(rel)
        if rec and rec.get("status") in TERMINAL_STATUSES:
            return self._receipt(b, rec, duplicate=True)
        return {
            "status": (rec or {}).get("status", "in_progress"),
            "request_id": request_id,
            "binding_id": binding_id,
            "revision": b["revision"],
            "duplicate_call": True,
            "outcome": "in_progress",
            "note": "another call is delivering on this binding; observed, nothing re-sent. Call again with the same request_id to reconcile once it completes.",
        }

    def _submit_locked(
        self,
        b,
        binding_id,
        request_id,
        text,
        expected_revision,
        kind,
        rel,
        accept_timeout_s,
        deadline,
        pre_enter_gate=None,
    ) -> dict:
        h = sha256_text(text)
        with self.store.lock():
            rec = self.store.read(rel)
        if rec:
            if rec.get("text_sha256") != h:
                raise BridgeError(
                    "request_conflict",
                    "request_id already used with different text",
                    existing=rec.get("text_sha256"),
                )
            if rec["status"] in TERMINAL_STATUSES:
                return self._receipt(b, rec, duplicate=True)
            return self._reconcile(
                b, rec, rel, accept_timeout_s, deadline, pre_enter_gate=pre_enter_gate
            )
        # a NEW mutation: a stale revision blocks it up front (the reservation re-checks atomically)
        self._check_text(text, kind)
        if int(expected_revision) != int(b["revision"]):
            raise BridgeError(
                "stale_revision",
                f"expected_revision {expected_revision} != current {b['revision']}",
                current_revision=b["revision"],
            )
        delivered_text, mode, payload = self._prepare_delivery(b, request_id, text)
        v = self._validate(b, deadline)
        if not v["ok"]:
            raise BridgeError(
                "identity_lost", "identity check failed; re-bind", identity=v
            )
        screen, _ = self._screen(b, 40, deadline)
        self._require_writable(screen, kind, b)
        if screen.get("placeholder") and delivered_text.strip() == screen["placeholder"]:
            # Typed, this payload is pixel-identical to the idle placeholder, so staging could never
            # be told apart from "nothing typed" and Enter would be unverifiable. Refuse; harmless
            # to reword. Checked before anything is typed.
            raise BridgeError(
                "payload_matches_placeholder",
                "payload equals Claude Code's current placeholder suggestion; reword it",
                screen=screen,
            )
        self._require_model_current(b, screen)
        # every refusal gate has passed: only now is the task file (PHI-bearing) written
        task_rel = f"tasks/{binding_id}/{request_id}.txt"
        created_task = bool(payload) and not self.store.path(task_rel).exists()
        self._write_task_file(b, request_id, text, payload)
        # atomic reservation: re-check the authoritative binding under the state lock, then reserve.
        # No CLI runs inside this lock; reconcile/deliver happen after it is released.
        reserved = None
        try:
            reserved = self._reserve_submit(
                b, binding_id, rel, request_id, kind, h, text, delivered_text, mode,
                payload, v, expected_revision,
            )
        except BaseException:
            if created_task:  # a refused submit must not leave the PHI-bearing brief behind
                self._discard_task_file(task_rel)
            raise
        kind_r, obj = reserved
        if kind_r == "existing":
            if obj["status"] in TERMINAL_STATUSES:
                return self._receipt(b, obj, duplicate=True)
            return self._reconcile(
                b, obj, rel, accept_timeout_s, deadline, pre_enter_gate=pre_enter_gate
            )
        return self._deliver_and_confirm(
            b, obj, rel, accept_timeout_s, deadline, pre_enter_gate=pre_enter_gate
        )

    def _discard_task_file(self, rel: str) -> None:
        p = self.store.path(rel)
        try:
            os.chmod(p, 0o600)
            p.unlink()
        except OSError:
            pass

    def _reserve_submit(
        self, b, binding_id, rel, request_id, kind, h, text, delivered_text, mode, payload, v,
        expected_revision,
    ):
        reserved = None
        with self.store.lock():
            cur = self.store.read(f"bindings/{binding_id}.json") or b
            self._assert_writer_current(cur, binding_id)
            if int(expected_revision) != int(cur["revision"]):
                raise BridgeError(
                    "stale_revision",
                    f"expected_revision {expected_revision} != current {cur['revision']}",
                    current_revision=cur["revision"],
                )
            again = self.store.read(rel)
            if again:
                reserved = ("existing", again)
            elif cur.get("inflight") not in (None, request_id):
                raise BridgeError(
                    "busy",
                    "another request is in flight on this binding",
                    inflight=cur.get("inflight"),
                )
            else:
                rec = {
                    "request_id": request_id,
                    "binding_id": binding_id,
                    "kind": kind,
                    "text_sha256": h,
                    "delivered_text": delivered_text,
                    "delivered_text_sha256": sha256_text(delivered_text),
                    "delivery_mode": mode,
                    "payload": payload,
                    "text_len": len(text),
                    "text_lines": text.count("\n") + 1,
                    "first_line": (text.splitlines() or [""])[0][:120],
                    "status": "delivering",
                    "send_result": None,
                    "cursor_before": v["latest_seq"],
                    "boot_id": b["boot_id"],
                    "started_at": self._now(),
                    "history": [],
                }
                self.store.write(rel, rec)
                cur["inflight"] = request_id
                self.store.write(f"bindings/{binding_id}.json", cur)
                b.update({"revision": cur["revision"], "inflight": request_id})
                reserved = ("deliver", rec)
        return reserved

    def _prepare_delivery(
        self, b: dict, request_id: str, text: str
    ) -> tuple[str, str, dict | None]:
        """Decide how a payload is delivered. Short single-line text goes inline; multi-line or long
        text is written to an immutable (0444) bridge-owned task file and delivered as a one-line
        reference, so the editor only ever holds text this bridge can verify exactly (item 1)."""
        if "\n" not in text and len(text) <= SINGLE_LINE_MAX:
            return text, "inline", None
        rel = f"tasks/{b['binding_id']}/{request_id}.txt"
        # item 3 addendum: the bridge never learns the real terminal width (a state root under a
        # deep tmp/CMUX_BRIDGE_STATE_DIR path is common and not itself unsafe), so this does NOT
        # guess a column bound here. Instead the reference is verified against the REAL rendered
        # editor: staged_is_ours refuses Enter (rule ambiguous_continuation_row) if the reference
        # wraps into a continuation row on screen, exactly like a foreign row would.
        p = self.store.path(rel)
        delivered_text = f"Task brief: {p}"
        payload = {
            "file": str(p),
            "payload_sha256": sha256_text(text),
            "mode": "0444",
            "lines": text.count("\n") + 1,
            "bytes": len(text.encode()),
        }
        return delivered_text, "task_file", payload

    def _write_task_file(self, b: dict, request_id: str, text: str, payload: dict | None) -> None:
        """Materialise the task file decided by _prepare_delivery (a no-op for inline delivery).
        Called only after every refusal gate has passed, so a refused submit writes nothing."""
        if payload:
            self.store.write_task_file(f"tasks/{b['binding_id']}/{request_id}.txt", text)

    def _assert_writer_current(self, b: dict, binding_id: str) -> None:
        """Under a held state lock, re-check this binding still owns the writer lease (closes a
        rebind/release-during-preflight race, item 2)."""
        cur = self.store.read(f"bindings/{binding_id}.json") or b
        if cur.get("superseded"):
            raise BridgeError(
                "lease_lost",
                "this binding was replaced by a newer one",
                superseded_by=cur.get("superseded_by"),
            )
        if cur.get("released"):
            raise BridgeError("released", "binding was released; bind again")
        if _parse_iso(cur["lease_expires_at"]).timestamp() <= self.clock():
            raise BridgeError(
                "lease_expired",
                "lease expired; bind again",
                expired_at=cur["lease_expires_at"],
            )
        for lrel in (
            f"leases/surface-{cur['surface_uuid']}.json",
            f"leases/worktree-{cur['worktree_key']}.json",
        ):
            l = self._lease_live(lrel)
            if not l or l.get("binding_id") != binding_id:
                raise BridgeError(
                    "lease_lost",
                    "the writer lease is no longer held by this binding",
                    lease=lrel,
                    holder=l,
                )

    @staticmethod
    def _require_writable(screen: dict, kind: str = "task", b: dict | None = None) -> None:
        state = screen["state"]
        if state == "prompt_idle":
            return
        # An authorized scoped steer may queue input into a RUNNING turn (the agent is busy) — that is
        # the ONLY non-idle state a steer may write into. Every other busy/ambiguous state (modal,
        # unknown, a foreign staged draft) is still refused, and a non-steer submit still needs an idle
        # prompt. Identity/lease/pre-Enter gates downstream are unchanged, so authorization is not
        # relaxed here, only the idle precondition for the one queued-steer case.
        if kind == "steer" and state == "running":
            return
        hint = Bridge._suggestion_hint(b, screen) if b is not None else None
        raise BridgeError(
            "busy",
            f"surface state is {state}: {screen['reason']}",
            screen=screen,
            **({"instruction": hint} if hint else {}),
        )
        # `background_agents` is the footer's "← N agent" hint, reported raw. A brand-new session shows
        # "← 1 agent" (live run 2026-09-08T18:12Z), so it is NOT evidence of owned jobs and never gates
        # a write; job state stays unknown unless the caller's explicit drained checkpoint proves it.

    @staticmethod
    def _require_model_current(b: dict, screen: dict) -> None:
        """Re-read the live footer model at submit time and refuse when the session is no longer the
        one the binding verified: a `/model` switch to Fable is `model_policy`; a switch to any other
        family is `model_changed` (re-bind to re-verify); an unreadable footer is `model_unverified`.
        Bindings made before this gate existed carry no `model` record and are held to the purpose
        default (implementation) on the live footer alone. Nothing is typed; nothing is resent."""
        purpose = b.get("purpose") or DEFAULT_PURPOSE
        bound = (b.get("model") or {}).get("footer")
        live = screen.get("model")
        # policy first (a switch to Fable is named as such), then drift from the bound model
        v = check_session_model(purpose, footer=live)
        if v["ok"] and (not bound or _same_model(bound, live)):
            return
        code, reason = v["code"], v["reason"]
        if v["ok"]:
            code = "model_changed"
            reason = f"the session's model changed from {bound!r} to {live!r} since the bind"
        raise BridgeError(
            code,
            reason,
            purpose=purpose,
            bound_model=bound,
            live_model=screen.get("model"),
            instruction="re-bind (bridge_bind) to re-verify the session's model and purpose before dispatching",
        )

    def _assert_writer_before_enter(
        self, b: dict, rec: dict, rel: str, compact: bool = False
    ) -> None:
        """Atomically re-check the writer lease AND the pause flag, under ONE state-lock hold,
        immediately before an Enter press (item 2). The reservation-time preflight is not enough: a
        release, a rebind or a pause landing after the payload is staged must still block the press.
        A lost lease marks the request `lease_lost`; a pause marks it `uncertain` and refuses with
        code `paused` and an instruction (the payload is staged, Enter was NOT pressed; bridge_resume,
        then call again with the SAME request_id to reconcile). The staged text is left in the editor,
        unsubmitted; nothing is cleared or resent here.

        Residual race: the check and the subsequent cmux send-key are not atomic (the lock is
        released before the keystroke goes out), so a pause or release landing in that last gap is
        not caught."""
        with self.store.lock():
            try:
                self._assert_writer_current(b, b["binding_id"])
            except BridgeError as e:
                self._mark(rec, rel, "lease_lost", lease_check=e.to_dict())
                raise BridgeError(
                    "lease_lost",
                    "the writer lease was released or replaced after staging; Enter was never pressed",
                    detail=e.to_dict(),
                ) from e
            cur = self.store.read(f"bindings/{b['binding_id']}.json") or {}
            if cur.get("paused"):
                err = BridgeError(
                    "paused",
                    "the binding was paused after staging: the payload is staged in the prompt box and "
                    "Enter was NOT pressed",
                    paused=cur["paused"],
                    instruction=(
                        "call bridge_resume, clear the staged /compact from the prompt box, then call "
                        "bridge_compact with a NEW request_id (this request is not re-Entered)"
                        if compact
                        else "call bridge_resume, then call again with the SAME request_id to "
                        "reconcile (the staged payload is verified and Enter pressed then)"
                    ),
                )
                self._mark(rec, rel, "uncertain", pre_enter_gate=err.to_dict())
                raise err

    def _assert_not_paused(self, binding_id: str) -> None:
        """Refuse early when the binding is paused (re-read from disk under the state lock)."""
        with self.store.lock():
            cur = self.store.read(f"bindings/{binding_id}.json") or {}
        if cur.get("paused"):
            raise BridgeError(
                "paused",
                "this binding is paused; call bridge_resume before submitting or compacting",
                paused=cur["paused"],
            )

    def _run_pre_enter_gate(
        self, pre_enter_gate, rec: dict, rel: str, binding_id: str | None = None
    ) -> None:
        """Invoke the optional caller gate immediately before an Enter press (review R1). The gate
        signals a refusal by RAISING; the staged text is then left in the editor unsubmitted (nothing
        is cleared or resent), exactly like the writer-lease pre-Enter re-check. A BridgeError
        propagates as-is; any other exception is wrapped so nothing is pressed. The pause check lives in
        _assert_writer_before_enter (one lock hold with the lease check)."""
        if pre_enter_gate is None:
            return
        try:
            pre_enter_gate()
        except BridgeError as e:
            self._mark(rec, rel, "uncertain", pre_enter_gate=e.to_dict())
            raise
        except (
            Exception
        ) as e:  # pragma: no cover - defensive: an unexpected gate error never sends
            self._mark(
                rec,
                rel,
                "uncertain",
                pre_enter_gate={"error": f"{type(e).__name__}: {e}"},
            )
            raise BridgeError(
                "pre_enter_gate_error",
                "the pre-Enter gate raised; the Enter was NOT pressed",
                detail=f"{type(e).__name__}: {e}",
            ) from e

    def _deliver_and_confirm(
        self,
        b: dict,
        rec: dict,
        rel: str,
        accept_timeout_s: float,
        deadline: float,
        resend_basis: str | None = None,
        pre_enter_gate=None,
    ) -> dict:
        text = rec["delivered_text"]
        if resend_basis:
            self._mark(rec, rel, "delivering", resend_basis=resend_basis)
        self._mark(rec, rel, "delivering", send_result="attempting")
        try:
            self._deliver(b, text, deadline)
        except SimulatedFault as e:
            if getattr(e, "phase", "unknown") == "before":
                self._mark(
                    rec,
                    rel,
                    "delivering",
                    send_result="not_attempted_simulated",
                    simulated=True,
                    send_error=e.kind,
                )
                raise BridgeError(
                    "send_not_attempted",
                    "a simulated pre-send fault proves zero bytes were sent",
                    simulated=True,
                    kind=e.kind,
                )
            # the send DID run and only its reply was lost: bytes may be staged, so this is never resendable
            self._mark(
                rec,
                rel,
                "delivering",
                send_result="ok",
                response_lost_simulated=True,
                simulated=True,
                send_error=e.kind,
            )
            raise BridgeError(
                "send_response_lost",
                "the send command ran but its reply was lost (simulated); call again with the same request_id to reconcile",
                simulated=True,
                kind=e.kind,
            )
        except CmuxError as e:
            # a non-zero CLI exit is NOT proof of non-delivery: the effect may have applied before
            # the channel failed. Keep it non-terminal and NON-resendable (item 3) — reconcile will
            # correlate the transcript and retain uncertainty rather than resend.
            sr = "uncertain" if e.kind == "timeout" else f"cli_error:{e.kind}"
            self._mark(
                rec,
                rel,
                "uncertain" if sr == "uncertain" else "delivering",
                send_result=sr,
                send_error=e.kind,
            )
            raise BridgeError(
                "send_failed",
                f"the send command failed: {e.kind}",
                send_result=sr,
                detail=e.message,
            )
        self._mark(rec, rel, "delivering", send_result="ok")
        # A steer queues its payload into a RUNNING turn, where the editor classifies 'running' (the
        # spinner suppresses 'staged'); an ordinary submit stages into an idle prompt. Pick the
        # matching verifier off the request's own kind so reconcile stays consistent.
        is_steer = rec.get("kind") == "steer"
        screen = {}
        for _ in range(5):  # a long paste can take a moment to render
            self.sleep(0.5)
            screen, _ = self._screen(b, 40, deadline)
            if screen["state"] == "staged":
                break
            if (
                is_steer
                and screen["state"] == "running"
                and (screen.get("input_text") or "").strip()
            ):
                break
        ok, why = (
            queued_is_ours(text, screen) if is_steer else staged_is_ours(text, screen)
        )
        if not ok:
            self._mark(rec, rel, "staged_unverified", staged_check=why, screen=screen)
            raise BridgeError(
                "staged_unverified",
                "the editor does not provably hold this request's payload; nothing was submitted",
                staged_check=why,
                screen=screen,
                instruction="inspect the surface; clear the editor and call again with the same request_id",
            )
        # item 2: re-check the writer lease atomically, immediately before this Enter press — a
        # release or a rebind that lands after staging must still block it, not just the earlier
        # reservation-time preflight.
        self._assert_writer_before_enter(b, rec, rel)
        # a pause (re-read from disk) or caller gate re-checked AFTER staging, immediately before the real
        # Enter — one that landed during this submit's own staging/waits withholds the keystroke.
        self._run_pre_enter_gate(
            pre_enter_gate, rec, rel, b["binding_id"]
        )
        self._mark(
            rec,
            rel,
            "submitted_unconfirmed",
            staged_check=why,
            submitted_at=self._now(),
        )
        self.cli.run_ok(
            "send-key",
            "--surface",
            b["surface_uuid"],
            "Enter",
            timeout=self._budget(deadline, 15.0),
        )
        return self._confirm(b, rec, rel, accept_timeout_s, deadline)

    def _await_acceptance(
        self, b: dict, rec: dict, timeout_s: float, deadline: float
    ) -> dict:
        """Poll the durable transcript; events are corroboration only, never proof."""
        end = min(self.clock() + timeout_s, deadline)
        matcher = self._matcher(rec)
        event_seen, gap = False, {"gap": False, "boot_changed": False}
        cursor = int(rec["cursor_before"])
        while True:
            try:
                ack, evs = self._events(cursor, 100, self._budget(end + 1.0, 6.0))
                g = self._ack_gap(ack, b["boot_id"])
                gap = {
                    "gap": gap["gap"] or g["gap"],
                    "boot_changed": gap["boot_changed"] or g["boot_changed"],
                }
                if evs:
                    cursor = max(e["seq"] for e in evs)
                if self._session_events(
                    evs,
                    b["claude_session_id"],
                    b["claude_pid"],
                    "agent.hook.UserPromptSubmit",
                    int(rec["cursor_before"]),
                ):
                    event_seen = True
            except CmuxError:
                pass
            mine, foreign = self._find_user_message(b, rec["started_at"], matcher)
            if mine:
                return {
                    "status": "accepted",
                    "message": mine,
                    "event_seen": event_seen,
                    "event_gap": gap,
                }
            if foreign:
                return {
                    "status": "uncertain_foreign",
                    "message": foreign,
                    "event_seen": event_seen,
                    "event_gap": gap,
                }
            if self.clock() >= end:
                return {
                    "status": "uncertain",
                    "message": None,
                    "event_seen": event_seen,
                    "event_gap": gap,
                }

    @staticmethod
    def _matcher(rec: dict):
        if rec.get("kind") == "command":
            return lambda m: m["text"].startswith(COMPACT_COMMAND_PREFIX)
        # correlate on the *delivered* line's hash (a task-file reference differs from the payload)
        want = rec.get("delivered_text_sha256") or rec["text_sha256"]
        return lambda m: m["sha256"] == want

    def _confirm(
        self,
        b: dict,
        rec: dict,
        rel: str,
        accept_timeout_s: float,
        deadline: float,
        reconciled: bool = False,
    ) -> dict:
        ac = self._await_acceptance(b, rec, accept_timeout_s, deadline)
        extra = {"reconciled": True} if reconciled else {}
        if ac["status"] == "accepted":
            return {**self._commit_accepted(b, rec, rel, ac), **extra}
        if ac["status"] == "uncertain_foreign":
            self._mark(
                rec,
                rel,
                "uncertain_foreign",
                acceptance={
                    "foreign_message_uuid": ac["message"].get("uuid"),
                    "foreign_at": ac["message"].get("at"),
                    "transcript": "foreign",
                },
                event_gap=ac["event_gap"],
            )
            return {
                **self._receipt(b, rec, duplicate=False),
                "instruction": "a different user message reached this session after the request started; the bridge will never resend or press Enter. Inspect the transcript and decide by hand.",
                **extra,
            }
        self._mark(
            rec,
            rel,
            "uncertain",
            acceptance={"event_seen": ac["event_seen"], "transcript": "missing"},
            event_gap=ac["event_gap"],
        )
        return {
            **self._receipt(b, rec, duplicate=False),
            "instruction": "no exact transcript correlation within the window; call bridge_submit again with the same request_id (it reconciles, it does not resend blindly) or bridge_wait until=accepted",
            "note": "a UserPromptSubmit event alone is corroboration, not acceptance",
            **extra,
        }

    def _commit_accepted(self, b: dict, rec: dict, rel: str, ac: dict) -> dict:
        """Record first (with revision_after), then bump the binding — both under one lock.

        Monotonic/idempotent under overlap (item 1): re-reads the AUTHORITATIVE on-disk request
        record inside this same lock. If it is already terminal — a concurrent caller (e.g. a
        submit() reconcile racing this one, or one observed through a stale wait() snapshot before
        the persist-milestones fix) committed it first — this replays that record instead of
        bumping the binding revision a second time for one actual send.
        """
        already = False
        with self.store.lock():
            cur = self.store.read(f"bindings/{b['binding_id']}.json") or b
            authoritative = self.store.read(rel)
            if (
                authoritative is not None
                and authoritative.get("status") in TERMINAL_STATUSES
            ):
                already = True
                rec.clear()
                rec.update(authoritative)
            else:
                rec["status"] = "accepted"
                rec["accepted_at"] = ac["message"].get("at")
                rec["acceptance"] = {
                    "transcript_uuid": ac["message"].get("uuid"),
                    "transcript_at": ac["message"].get("at"),
                    # receipt hash distinction (finding 3 addendum): the ORIGINAL payload hash vs.
                    # the hash of the short reference actually matched in the transcript. They
                    # differ for task_file deliveries -- this proves acceptance of the reference,
                    # not that the full brief was read.
                    "payload_text_sha256": rec["text_sha256"],
                    "matched_text_sha256": rec.get("delivered_text_sha256")
                    or rec["text_sha256"],
                    "event_seen": ac["event_seen"],
                    "rule": "exact transcript correlation",
                }
                rec["event_gap"] = ac.get("event_gap")
                rec["revision_after"] = int(cur["revision"]) + 1
                rec.setdefault("history", []).append(
                    {"at": self._now(), "status": "accepted"}
                )
                self.store.write(rel, rec)
                cur["revision"] = rec["revision_after"]
                cur["inflight"] = None
                self.store.write(f"bindings/{cur['binding_id']}.json", cur)
        b.update({"revision": cur["revision"], "inflight": cur.get("inflight")})
        if already:
            return self._receipt(b, rec, duplicate=True)
        self.store.append_receipt(
            "submit",
            {
                "binding_id": b["binding_id"],
                "request_id": rec["request_id"],
                "text_sha256": rec["text_sha256"],
                "delivered_text_sha256": rec.get("delivered_text_sha256"),
                "delivery_mode": rec.get("delivery_mode"),
                "acceptance": rec["acceptance"],
                "revision": b["revision"],
            },
        )
        return self._receipt(b, rec, duplicate=False)

    def _reconcile(
        self,
        b: dict,
        rec: dict,
        rel: str,
        accept_timeout_s: float,
        deadline: float,
        pre_enter_gate=None,
    ) -> dict:
        """Non-terminal record. Correlate first; re-deliver ONLY on a simulated pre-send fault (the
        one proven zero-bytes-sent case). A non-zero CLI exit is never a resend basis (item 3), and
        identity is re-validated before any further Enter/send."""
        text = rec["delivered_text"]
        matcher = self._matcher(rec)
        mine, foreign = self._find_user_message(b, rec["started_at"], matcher)
        if mine:
            return {
                **self._commit_accepted(
                    b,
                    rec,
                    rel,
                    {
                        "message": mine,
                        "event_seen": bool(rec.get("acceptance", {}).get("event_seen")),
                    },
                ),
                "reconciled": True,
            }
        if foreign:
            self._mark(
                rec,
                rel,
                "uncertain_foreign",
                acceptance={
                    "foreign_message_uuid": foreign.get("uuid"),
                    "foreign_at": foreign.get("at"),
                    "transcript": "foreign",
                },
            )
            return {
                **self._receipt(b, rec, duplicate=False),
                "reconciled": True,
                "instruction": "a foreign user message landed after this request started; never resent, never Enter",
            }
        # any further Enter/send re-checks identity first (item 3)
        v = self._validate(b, deadline)
        if not v["ok"]:
            raise BridgeError(
                "identity_lost",
                "identity check failed during reconcile; re-bind",
                identity=v,
            )
        screen, _ = self._screen(b, 40, deadline)
        sr = rec.get("send_result")
        # ONLY a simulated pre-send fault (zero bytes sent) is a resend basis. A `cli_error:*` exit
        # is NOT proof of non-delivery: the effect may have applied before the channel failed.
        basis = "simulated_pre_send_fault" if sr == "not_attempted_simulated" else None
        if basis and screen["state"] == "prompt_idle":
            self._assert_not_paused(b["binding_id"])
            return self._deliver_and_confirm(
                b,
                rec,
                rel,
                accept_timeout_s,
                deadline,
                resend_basis=basis,
                pre_enter_gate=pre_enter_gate,
            )
        if screen["state"] == "staged":
            ok, why = staged_is_ours(text, screen)
            if ok:
                # item 2: same atomic re-check immediately before the RECONCILE Enter press — a
                # release/rebind landing during this later window must block it too, not only the
                # already-covered initial Enter and reservation-time preflight.
                self._assert_writer_before_enter(b, rec, rel)
                # the same post-staging pause/gate check covers the reconcile Enter too.
                self._run_pre_enter_gate(
                    pre_enter_gate, rec, rel, b["binding_id"]
                )
                self._mark(
                    rec,
                    rel,
                    "submitted_unconfirmed",
                    staged_check=why,
                    reconciled_enter_at=self._now(),
                )
                self.cli.run_ok(
                    "send-key",
                    "--surface",
                    b["surface_uuid"],
                    "Enter",
                    timeout=self._budget(deadline, 15.0),
                )
                return self._confirm(
                    b, rec, rel, accept_timeout_s, deadline, reconciled=True
                )
            raise BridgeError(
                "staged_unverified",
                "the editor holds text that is not provably this request; nothing was submitted",
                staged_check=why,
                screen=screen,
            )
        return {
            **self._confirm(
                b, rec, rel, min(accept_timeout_s, 2.0), deadline, reconciled=True
            ),
            "screen": screen,
        }

    # ------------------------------------------------------------------ wait
    def wait(
        self,
        binding_id: str,
        controller_id: str,
        until: str,
        timeout_s: float = DEFAULT_WAIT_S,
        request_id: str | None = None,
        evidence: dict | None = None,
        after_seq: int | None = None,
        poll_s: float = 5.0,
    ) -> dict:
        """Bounded wait on raw evidence. Persists per-request milestones so completion that happened
        before the call stays discoverable; never sends anything."""
        if until not in WAIT_UNTIL:
            raise BridgeError("bad_request", f"until must be one of {list(WAIT_UNTIL)}")
        timeout_s = clamp_timeout(timeout_s, DEFAULT_WAIT_S, 1.0, MAX_WAIT_S)
        poll_s = clamp_timeout(poll_s, 5.0, 0.5, 60.0)
        b = self._load(binding_id, controller_id)
        rec, rel = None, None
        if request_id:
            rel = f"requests/{binding_id}/{request_id}.json"
            rec = self.store.read(rel)
            if not rec:
                raise BridgeError(
                    "unknown_request", f"no request {request_id} on this binding"
                )
        if until in ("accepted", "task_complete") and not rec:
            raise BridgeError(
                "bad_request",
                f"until={until} needs request_id (it is scoped to one request)",
            )
        if until == "task_complete":
            self._check_evidence(evidence, "evidence")
        cursor = (
            int(after_seq)
            if after_seq is not None
            else int(rec["cursor_before"] if rec else b["cursor_seq"])
        )
        start = self.clock()
        deadline = start + timeout_s
        stop_seq = rec.get("stop_seq") if rec else None
        accepted_seq = rec.get("accepted_seq") if rec else None
        session_start_seqs = []
        last_identity = start
        while True:
            ack, evs = self._events(
                cursor, 200, min(poll_s, max(0.5, deadline - self.clock()))
            )
            g = self._ack_gap(ack, b["boot_id"])
            if g["boot_changed"] or g["gap"]:
                self._persist_milestones(rel, rec, {"event_gap": g})
                return {
                    "outcome": "reconnect_gap",
                    "until": until,
                    "boot_id_before": b["boot_id"],
                    "boot_id_now": g["boot_id"],
                    "gap": g["gap"],
                    "cursor_seq": cursor,
                    "instruction": "cmux restarted or events were lost; nothing was sent. Reconcile from the transcript before any further input",
                }
            if evs:
                cursor = max(e["seq"] for e in evs)
            ups = self._session_events(
                evs,
                b["claude_session_id"],
                b["claude_pid"],
                "agent.hook.UserPromptSubmit",
                -1,
            )
            if ups and accepted_seq is None:
                accepted_seq = ups[0]["seq"]
            stops = self._session_events(
                evs,
                b["claude_session_id"],
                b["claude_pid"],
                "agent.hook.Stop",
                accepted_seq if accepted_seq is not None else -1,
            )
            if stops:
                stop_seq = stops[-1]["seq"]
            session_start_seqs += [
                e["seq"]
                for e in self._session_events(
                    evs,
                    b["claude_session_id"],
                    b["claude_pid"],
                    "agent.hook.SessionStart",
                    -1,
                )
            ]
            self._persist_milestones(
                rel, rec, {"accepted_seq": accepted_seq, "stop_seq": stop_seq}
            )
            satisfied = self._evaluate(
                b, until, rec, evidence, stop_seq, accepted_seq, session_start_seqs
            )
            if satisfied is not None and satisfied.get("blocked"):
                return {
                    "outcome": "blocked_modal",
                    "until": until,
                    "screen": satisfied["screen"],
                    "cursor_seq": cursor,
                    "instruction": "a dialog is waiting for a human decision; the bridge will not answer it",
                }
            if satisfied:
                return {
                    "outcome": "satisfied",
                    "until": until,
                    "elapsed_s": round(self.clock() - start, 1),
                    "evidence": satisfied,
                    "cursor_seq": cursor,
                }
            if self.clock() >= deadline:
                return {
                    "outcome": "timeout",
                    "until": until,
                    "elapsed_s": round(self.clock() - start, 1),
                    "cursor_seq": cursor,
                    "note": "finite wait; nothing was sent",
                }
            if self.clock() - last_identity > 15:
                last_identity = self.clock()
                v = self._validate(b, deadline)
                if not v["ok"]:
                    return {
                        "outcome": "identity_lost",
                        "until": until,
                        "identity": v,
                        "cursor_seq": cursor,
                    }

    def _persist_milestones(self, rel: str | None, rec: dict | None, kw: dict) -> None:
        """Merge milestone fields (accepted_seq, stop_seq, event_gap, ...) into the AUTHORITATIVE
        on-disk record under the state lock (item 1). `rec` is a snapshot the caller (wait()) may
        have held across a poll loop while a concurrent submit() advanced the real record past it
        -- writing that stale snapshot back would regress a durable accepted/completed status and
        (via a later replay) double-bump the binding revision. So this reads the current record
        fresh, merges ONLY the given milestone fields into it, writes that, and then refreshes the
        caller's `rec` in place from the merged result so wait()'s own subsequent checks (e.g. an
        already-accepted status) see it too."""
        if not (rel and rec):
            return
        with self.store.lock():
            cur = self.store.read(rel)
            if cur is None:
                return
            changed = {k: v for k, v in kw.items() if v is not None and cur.get(k) != v}
            if changed:
                cur.update(changed)
                self.store.write(rel, cur)
            rec.clear()
            rec.update(cur)

    @staticmethod
    def _check_evidence(ev: dict | None, name: str) -> None:
        if not isinstance(ev, dict) or not ev.get("file"):
            raise BridgeError(
                "bad_request" if name == "evidence" else "checkpoint_required",
                f"{name} must be {{'file': path, 'contains': str}} or {{'file': path, 'sha256': hex}}",
            )
        if not (ev.get("contains") or ev.get("sha256")):
            raise BridgeError(
                "bad_request" if name == "evidence" else "checkpoint_required",
                f"{name} needs 'contains' or 'sha256': existence is not evidence",
            )

    @staticmethod
    def _file_evidence(ev: dict, not_before: float | None) -> dict:
        p = Path(ev["file"])
        out = {
            "file": str(p),
            "exists": p.is_file(),
            "content_match": False,
            "fresh": False,
        }
        if not out["exists"]:
            return out
        st = p.stat()
        out["mtime"] = st.st_mtime
        out["fresh"] = not_before is None or st.st_mtime >= not_before
        try:
            data = p.read_bytes()
        except OSError:
            return out
        if ev.get("sha256"):
            out["content_match"] = (
                hashlib.sha256(data).hexdigest() == str(ev["sha256"]).lower()
            )
            out["rule"] = "sha256"
        else:
            out["content_match"] = str(ev["contains"]) in data.decode("utf-8", "ignore")
            out["rule"] = "contains"
        return out

    def _turn_complete(
        self, b: dict, rec: dict | None, stop_seq: int | None, accepted_seq: int | None
    ) -> dict | None:
        if rec and rec.get("accepted_at"):
            since = _parse_iso(rec["accepted_at"])
            for t in self._systems(self._transcript(b), "turn_duration"):
                if t.get("timestamp") and _parse_iso(t["timestamp"]) >= since:
                    return {
                        "transcript_turn_end_at": t["timestamp"],
                        "rule": "transcript turn_duration after the accepted message",
                    }
        if stop_seq is not None and (accepted_seq is None or stop_seq > accepted_seq):
            return {"stop_seq": stop_seq, "rule": "Stop event after acceptance"}
        return None

    def _evaluate(
        self,
        b: dict,
        until: str,
        rec: dict | None,
        evidence: dict | None,
        stop_seq,
        accepted_seq,
        session_starts,
    ) -> dict | None:
        if until == "accepted":
            if rec and rec.get("status") == "accepted":
                return {
                    "accepted": rec.get("acceptance"),
                    "accepted_at": rec.get("accepted_at"),
                }
            mine, _ = self._find_user_message(b, rec["started_at"], self._matcher(rec))
            return (
                {"accepted_at": mine.get("at"), "transcript_uuid": mine.get("uuid")}
                if mine
                else None
            )
        if until == "turn_complete":
            return self._turn_complete(b, rec, stop_seq, accepted_seq)
        if until == "compaction_complete":
            if not rec:
                return None
            since = _parse_iso(rec.get("submitted_at") or rec["started_at"])
            for cb in self._systems(self._transcript(b), "compact_boundary"):
                if cb.get("timestamp") and _parse_iso(cb["timestamp"]) > since:
                    return {
                        "compact_boundary_at": cb["timestamp"],
                        "trigger": (cb.get("compactMetadata") or {}).get("trigger"),
                        "session_start_seqs": session_starts,
                        "rule": "transcript compact_boundary after this request's submit",
                    }
            return None
        screen, _ = self._screen(b, 40)
        if screen["state"] == "modal":
            return {"blocked": True, "screen": screen}
        if until == "idle":
            return {"screen": screen} if screen["state"] == "prompt_idle" else None
        # task_complete: acceptance + turn completion + fresh matching artifact + a quiet screen
        if not (rec and rec.get("status") == "accepted"):
            return None
        turn = self._turn_complete(b, rec, stop_seq, accepted_seq)
        if not turn:
            return None
        not_before = (
            _parse_iso(rec["accepted_at"]).timestamp()
            if rec.get("accepted_at")
            else None
        )
        fe = self._file_evidence(evidence, not_before)
        if not (fe["exists"] and fe["fresh"] and fe["content_match"]):
            return None
        if screen["state"] != "prompt_idle":
            return None
        # honest job-state basis (item 4): background job drain is NOT proven from the footer hint,
        # and task_complete is a completion PREDICATE, not a raw observation — so it is satisfied
        # only with a CURRENT, evidenced controller attestation for this bound task/revision, never
        # on a bare {'drained': true}. Without one, job state stays honestly unknown and this
        # returns None (not satisfied); a caller that wants the raw acceptance/turn/artifact
        # observation without claiming completion should wait until='turn_complete' or 'idle'
        # instead, which are not task-complete predicates.
        att = self._drain_ok(
            evidence.get("drained_attestation") if isinstance(evidence, dict) else None,
            not_before,
        )
        if att is None:
            return None
        return {
            "turn": turn,
            "executor_evidence": fe,
            "screen": screen,
            "background_agents_hint": screen.get("background_agents"),
            "job_state": "controller_attested",
            "drained_attestation": att,
            "note": "acceptance + turn + fresh artifact + idle prompt + a current, evidenced drained attestation. The footer agent hint is not a job count and is never a completion basis by itself.",
        }

    # ------------------------------------------------------------------ compact
    def compact(
        self,
        binding_id: str,
        controller_id: str,
        request_id: str,
        expected_revision: int,
        checkpoint: dict,
        ctx_used_pct: float | None = None,
        reason: str | None = None,
        drained_attestation: dict | None = None,
        force: bool = False,
        timeout_s: float = 120.0,
    ) -> dict:
        """Apply the 30/40 policy at a safe boundary. Idempotent by request_id; deliveries on one
        binding are serialised (a per-binding lock). Requires a current drained-checkpoint artifact
        AND an explicit controller drained attestation — the footer '← N agent' hint is not job
        tracking (item 4). Never resends beyond a simulated pre-send fault."""
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", request_id or ""):
            raise BridgeError(
                "bad_request", "request_id must be 1-80 chars of [A-Za-z0-9._:-]"
            )
        timeout_s = clamp_timeout(timeout_s, 120.0, 1.0, MAX_WAIT_S)
        deadline = self.clock() + timeout_s
        b = self._load(binding_id, controller_id, need_writer=True)
        self._assert_not_paused(binding_id)
        rel = f"requests/{binding_id}/{request_id}.json"
        try:
            with self.store.lock(f"delivery-{binding_id}", timeout_s=3.0):
                return self._compact_locked(
                    b,
                    binding_id,
                    request_id,
                    int(expected_revision),
                    checkpoint,
                    ctx_used_pct,
                    reason,
                    drained_attestation,
                    force,
                    deadline,
                )
        except StateError as e:
            if getattr(e, "code", "") != "lock_timeout":
                raise
        with self.store.lock():
            rec = self.store.read(rel)
        if rec and rec["status"] in TERMINAL_STATUSES:
            return {
                **self._receipt(b, rec, duplicate=True),
                "outcome": rec["status"],
                "policy": rec.get("policy"),
            }
        return {
            "outcome": "in_progress",
            "request_id": request_id,
            "binding_id": binding_id,
            "revision": b["revision"],
            "duplicate_call": True,
            "note": "another call is compacting on this binding; observed, nothing re-sent. Call again with the same request_id to re-check.",
        }

    def _compact_locked(
        self,
        b,
        binding_id,
        request_id,
        expected_revision,
        checkpoint,
        ctx_used_pct,
        reason,
        drained_attestation,
        force,
        deadline,
    ) -> dict:
        rel = f"requests/{binding_id}/{request_id}.json"
        with self.store.lock():
            rec = self.store.read(rel)
        if rec:
            if rec["status"] in TERMINAL_STATUSES:
                return {
                    **self._receipt(b, rec, duplicate=True),
                    "outcome": rec["status"],
                    "policy": rec.get("policy"),
                }
            return self._compact_recheck(b, rec, rel, deadline)
        if int(expected_revision) != int(b["revision"]):
            raise BridgeError(
                "stale_revision",
                f"expected_revision {expected_revision} != current {b['revision']}",
                current_revision=b["revision"],
            )
        self._check_evidence(checkpoint, "checkpoint")
        v = self._validate(b, deadline)
        if not v["ok"]:
            raise BridgeError(
                "identity_lost", "identity check failed; re-bind", identity=v
            )
        screen, _ = self._screen(b, 40, deadline)
        pct_screen = screen.get("ctx_used_pct")
        pct = float(ctx_used_pct) if ctx_used_pct is not None else pct_screen
        src = (
            "controller"
            if ctx_used_pct is not None
            else ("screen" if pct_screen is not None else "unknown")
        )
        policy = {
            "due_pct": COMPACT_DUE_PCT,
            "limit_pct": COMPACT_LIMIT_PCT,
            "ctx_used_pct": pct,
            "ctx_source": src,
            "due": pct is not None and pct >= COMPACT_DUE_PCT,
            "overdue": pct is not None and pct >= COMPACT_LIMIT_PCT,
        }
        if not policy["due"] and not force:
            return {
                "outcome": "not_due",
                "policy": policy,
                "revision": b["revision"],
                "request_id": request_id,
                "note": "no action; pass force=true only for a test or an explicit operator instruction",
            }
        # the footer agent hint is reported raw (see submit); the drained-checkpoint evidence below is
        # the only job-state claim this bridge accepts
        if screen["state"] != "prompt_idle":
            if not reason:
                raise BridgeError(
                    "deferral_needs_reason",
                    f"surface state is {screen['state']}; compaction is deferred and a reason is required",
                    screen=screen,
                    policy=policy,
                )
            self.store.append_receipt(
                "compact_deferred",
                {
                    "binding_id": binding_id,
                    "request_id": request_id,
                    "reason": reason,
                    "state": screen["state"],
                    "policy": policy,
                },
            )
            return {
                "outcome": "deferred",
                "request_id": request_id,
                "reason": reason,
                "state": screen["state"],
                "policy": policy,
                "next_safe_boundary": "bridge_wait until=turn_complete, then call bridge_compact again with the same request_id",
                "revision": b["revision"],
            }
        cp = self._file_evidence(checkpoint, self._last_accepted_at(b))
        if not (cp["exists"] and cp["fresh"] and cp["content_match"]):
            raise BridgeError(
                "checkpoint_stale",
                "the checkpoint artifact is missing, older than the last accepted request, or does not match",
                checkpoint=cp,
                instruction="write a current checkpoint (objective, accepted scope, done + validation, active jobs, next action) and call again",
            )
        # a fresh checkpoint + an idle surface is NOT proof jobs are drained; require the controller
        # to attest it (item 4). The footer '← N agent' hint is reported raw, never treated as a job
        # count (a confirmed-drained session has shown '← 1 agent').
        att = self._drain_ok(drained_attestation, self._last_accepted_at(b))
        if not att and not force:
            return {
                "outcome": "needs_drained_attestation",
                "request_id": request_id,
                "policy": policy,
                "revision": b["revision"],
                "checkpoint": cp,
                "background_agents_hint": screen.get("background_agents"),
                "note": "the checkpoint is fresh and the surface is idle, but the bridge cannot natively verify that owned jobs are drained (the footer '← N agent' hint is not job tracking). Pass drained_attestation={'drained': true, 'attested_by': <controller_id>, 'evidence': <text>} to proceed, or force=true for a test.",
            }
        rec = {
            "request_id": request_id,
            "binding_id": binding_id,
            "kind": "command",
            "text_sha256": sha256_text("/compact"),
            "status": "delivering",
            "send_result": None,
            "cursor_before": v["latest_seq"],
            "boot_id": b["boot_id"],
            "policy": policy,
            "ctx_before": pct_screen,
            "checkpoint": cp,
            "job_state": "controller_attested" if att else "forced",
            "drained_attestation": att,
            "started_at": self._now(),
            "history": [],
        }
        with self.store.lock():
            cur = self.store.read(f"bindings/{binding_id}.json") or b
            self._assert_writer_current(cur, binding_id)
            if int(expected_revision) != int(cur["revision"]):
                raise BridgeError(
                    "stale_revision",
                    f"expected_revision {expected_revision} != current {cur['revision']}",
                    current_revision=cur["revision"],
                )
            if cur.get("inflight") not in (None, request_id):
                raise BridgeError(
                    "busy",
                    "another request is in flight on this binding",
                    inflight=cur.get("inflight"),
                )
            self.store.write(rel, rec)
            cur["inflight"] = request_id
            self.store.write(f"bindings/{binding_id}.json", cur)
            b.update({"revision": cur["revision"], "inflight": request_id})
        self._mark(rec, rel, "delivering", send_result="attempting")
        try:
            self.cli.run_ok(
                "send",
                "--surface",
                b["surface_uuid"],
                "--",
                "/compact",
                timeout=self._budget(deadline, 15.0),
            )
        except SimulatedFault as e:
            if getattr(e, "phase", "unknown") != "before":
                self._mark(
                    rec,
                    rel,
                    "delivering",
                    send_result="ok",
                    response_lost_simulated=True,
                    simulated=True,
                )
                raise BridgeError(
                    "send_response_lost",
                    "the /compact send ran but its reply was lost (simulated); call again with the same request_id to reconcile",
                    simulated=True,
                )
            self._mark(
                rec,
                rel,
                "delivering",
                send_result="not_attempted_simulated",
                simulated=True,
            )
            raise BridgeError(
                "send_not_attempted",
                "a simulated pre-send fault proves zero bytes were sent",
                simulated=True,
                kind=e.kind,
            )
        except CmuxError as e:
            self._mark(rec, rel, "delivering", send_result=f"cli_error:{e.kind}")
            raise BridgeError(
                "send_failed", f"the send command failed: {e.kind}", detail=e.message
            )
        self._mark(rec, rel, "delivering", send_result="ok")
        self.sleep(0.5)
        st, _ = self._screen(b, 40, deadline)
        ok, why = staged_is_ours("/compact", st)
        if not ok:
            self._mark(rec, rel, "staged_unverified", staged_check=why, screen=st)
            raise BridgeError(
                "staged_unverified",
                "/compact is not provably staged in the input editor; nothing submitted",
                staged_check=why,
                screen=st,
            )
        # lease + pause re-checked under one lock hold immediately before the Enter
        self._assert_writer_before_enter(b, rec, rel, compact=True)
        self._mark(
            rec,
            rel,
            "submitted_unconfirmed",
            submitted_at=self._now(),
            staged_check=why,
        )
        self.cli.run_ok(
            "send-key",
            "--surface",
            b["surface_uuid"],
            "Enter",
            timeout=self._budget(deadline, 15.0),
        )
        return self._compact_recheck(b, rec, rel, deadline)

    @staticmethod
    def _drain_ok(att: dict | None, not_before: float | None = None) -> dict | None:
        """Normalise a controller drained attestation. Requires a CURRENT attestation for the
        bound task/revision, naming its attester and carrying nonempty evidence (item 4): a bare
        {'drained': true} with a null/missing attester, evidence or timestamp is refused outright,
        and a well-formed but stale attestation (timestamped before `not_before`, e.g. from a
        request prior to the one this checkpoint is scoped to) does not count either. Recorded as
        *attested* — never as native job tracking."""
        if not isinstance(att, dict) or att.get("drained") is not True:
            return None
        attested_by, evidence, at = (
            att.get("attested_by"),
            att.get("evidence"),
            att.get("at"),
        )
        if not attested_by or not evidence or not at:
            return None
        try:
            at_ts = _parse_iso(at).timestamp()
        except (ValueError, TypeError):
            return None
        if not_before is not None and at_ts < not_before:
            return None
        return {
            "drained": True,
            "attested_by": attested_by,
            "evidence": evidence,
            "at": at,
            "source": "controller_attested",
        }

    def _last_accepted_at(self, b: dict) -> float:
        """Freshness floor for a checkpoint: the last accepted request on this binding, else bind."""
        best = _parse_iso(b["bound_at"]).timestamp()
        for p in self.store.list(f"requests/{b['binding_id']}"):
            try:
                r = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if r.get("status") in ("accepted", "completed") and r.get("accepted_at"):
                best = max(best, _parse_iso(r["accepted_at"]).timestamp())
        return best

    def _compact_recheck(self, b: dict, rec: dict, rel: str, deadline: float) -> dict:
        """Re-observe a compaction without ever resending /compact."""
        since = rec.get("submitted_at") or rec["started_at"]
        w = self.wait(
            b["binding_id"],
            b["controller_id"],
            "compaction_complete",
            timeout_s=max(1.0, deadline - self.clock()),
            request_id=rec["request_id"],
            poll_s=5.0,
        )
        after, _ = self._screen(b, 40)
        cmd, _ = self._find_user_message(
            b, since, lambda m: m["text"].startswith(COMPACT_COMMAND_PREFIX)
        )
        out = {
            "request_id": rec["request_id"],
            "policy": rec.get("policy"),
            "ctx_before": rec.get("ctx_before"),
            "ctx_after_screen": after.get("ctx_used_pct"),
            "screen_state_after": after["state"],
            "wait": w,
            "accepted": bool(cmd),
            "accepted_at": (cmd or {}).get("at"),
            "revision": b["revision"],
        }
        if w["outcome"] == "satisfied":
            with self.store.lock():
                cur = self.store.read(f"bindings/{b['binding_id']}.json") or b
                rec["status"] = "completed"
                rec["acceptance"] = {
                    "command_message_uuid": (cmd or {}).get("uuid"),
                    "compact_boundary": w["evidence"],
                    "rule": "transcript compact_boundary after this request's submit",
                }
                rec["accepted_at"] = (cmd or {}).get("at")
                rec["revision_after"] = int(cur["revision"]) + 1
                self.store.write(rel, rec)
                cur["revision"] = rec["revision_after"]
                cur["inflight"] = None
                self.store.write(f"bindings/{cur['binding_id']}.json", cur)
            b.update({"revision": cur["revision"], "inflight": None})
            stale = (
                rec.get("ctx_before") is not None
                and after.get("ctx_used_pct") is not None
                and after["ctx_used_pct"] >= rec["ctx_before"] - 0.05
            )
            out.update(
                {
                    "outcome": "completed",
                    "revision": b["revision"],
                    "meter_stale": stale,
                    "evidence": rec["acceptance"],
                    "note": "completion evidence = a transcript compact_boundary after this request's submit; a footer percentage that has not moved yet is stale, not a failure",
                }
            )
            self.store.append_receipt(
                "compact",
                {
                    "binding_id": b["binding_id"],
                    "request_id": rec["request_id"],
                    "evidence": rec["acceptance"],
                    "meter_stale": stale,
                    "revision": b["revision"],
                    "job_state": rec.get("job_state"),
                    "drained_attestation": rec.get("drained_attestation"),
                },
            )
            return out
        self._mark(rec, rel, "uncertain", wait=w)
        out.update(
            {
                "outcome": "uncertain",
                "instruction": "no compact_boundary in the transcript within the window; do not resend /compact. Call bridge_compact again with the SAME request_id to re-check, or bridge_wait until=compaction_complete",
            }
        )
        return out

    # ------------------------------------------------------------------ pause / resume
    def _set_paused(self, binding_id: str, controller_id: str, paused: dict | None) -> dict:
        self._load(binding_id, controller_id)
        with self.store.lock():
            b = self.store.read(f"bindings/{binding_id}.json")
            if not b:
                raise BridgeError("unknown_binding", f"no binding {binding_id}")
            if b.get("controller_id") != controller_id:
                raise BridgeError(
                    "not_owner",
                    "only the controller that owns this binding may pause or resume it",
                )
            if paused is None:
                b.pop("paused", None)
            else:
                b["paused"] = paused
            self.store.write(f"bindings/{binding_id}.json", b)
        return {"binding_id": binding_id, "paused": paused}

    def pause(self, binding_id: str, controller_id: str, reason: str) -> dict:
        """Bridge-local pause: submit and compact refuse (code 'paused') and a pause landing during
        staging withholds the Enter. Owner-only; cleared by resume. release does not clear it: it makes
        the binding unusable. A same-surface rebind carries the pause over to the new binding."""
        reason = (reason or "").strip()[:500]
        if not reason:
            raise BridgeError("bad_request", "reason is required")
        out = self._set_paused(
            binding_id, controller_id, {"reason": reason, "at": self._now()}
        )
        return {**out, "note": "paused; nothing already staged or accepted is touched"}

    def resume(self, binding_id: str, controller_id: str) -> dict:
        return self._set_paused(binding_id, controller_id, None)

    # ------------------------------------------------------------------ release
    def release(self, binding_id: str, controller_id: str) -> dict:
        with self.store.lock():
            b = self.store.read(f"bindings/{binding_id}.json")
            if not b:
                raise BridgeError("unknown_binding", f"no binding {binding_id}")
            self._require_owner(b, controller_id)
            removed = []
            for rel in (
                f"leases/surface-{b['surface_uuid']}.json",
                f"leases/worktree-{b['worktree_key']}.json",
            ):
                l = self.store.read(rel)
                if l and l.get("binding_id") == binding_id and self.store.delete(rel):
                    removed.append(rel)
            b["released"] = True
            b["released_at"] = self._now()
            self.store.write(f"bindings/{binding_id}.json", b)
            # PHI minimisation: drop the task briefs unless a request is still in flight (a
            # non-terminal record may still need its file for reconcile).
            rid = b.get("inflight")
            inflight = self.store.read(f"requests/{binding_id}/{rid}.json") if rid else None
            if not (inflight and inflight.get("status") not in TERMINAL_STATUSES):
                self.store.delete_tree(f"tasks/{binding_id}")
        self.store.append_receipt(
            "release", {"binding_id": binding_id, "leases_removed": removed}
        )
        return {
            "binding_id": binding_id,
            "released": True,
            "leases_removed": removed,
            "note": "the surface and the Claude process are left untouched",
        }
