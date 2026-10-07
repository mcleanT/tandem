#!/usr/bin/env python3
"""REAL-runtime acceptance for the tandem bridge.

Run only from a cmux-descended process, and only by the human/lead operator: it creates a
disposable Claude Code session in a scratch git worktree on a NEW, unfocused terminal surface,
drives it through `cmux_bridge.core.Bridge` (observe/bind/submit/wait/compact/release) and writes
receipts.

Safety invariants enforced here, not merely documented:

* only surfaces this run created are ever written to or closed. Every created surface UUID is
  resolved EXACTLY from the `surface:N` ref returned by `new-surface` (never a set difference) and
  appended to <out>/owned-surfaces.json before anything else can fail;
* the source under test must be frozen (`git status --porcelain` empty) unless ALLOW_DIRTY=1;
* a case that could not run is recorded with pass=false and note "not run: <why>". Nothing
  unsupported, skipped or errored is ever recorded as a pass.

Every case is labelled `real`, `real+injected` (real transport, injected/simulated fault) or
`state-manipulated`.

Usage:
  python3 bridge/live_acceptance.py --workspace <WORKSPACE-UUID> --scratch <dir> --out <dir> \
      [--model sonnet] [--state-dir <dir>]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
import uuid as uuidmod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cmux_bridge.cmuxcli import (
    CallLog,
    CmuxCLI,
    CmuxError,
    CmuxResult,
    FaultInjector,
    SimulatedFault,
)  # noqa: E402
from cmux_bridge.core import (  # noqa: E402
    CONTRACT,
    Bridge,
    BridgeError,
    canonical_session_id,
    classify_screen,
    staged_is_ours,
)
from cmux_bridge.state import StateStore, utcnow  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = Path(__file__).resolve().parent

UUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
SURFACE_ROW_RE = re.compile(r"^\s*(?:\*\s*)?(surface:\d+)\s+(" + UUID_RE.pattern + r")\b")

PAYLOAD = 'line one: \'single\' "double" `backtick` $HOME ${X} \\n-literal\nline two: ✓ ünïcödé — 日本語 🚀 "$(id)" && ; | > <\nline three: tab\there and trailing spaces   \n'


def _r3_copy_command(taskfile: str, recv: str) -> str:
    """The R3 deterministic byte-copy command (rev2 amendment seq221).

    Extracts the PAYLOAD verbatim from between the anchored ``^<<<BEGIN>>>$`` / ``^<<<END>>>$``
    marker lines of the executor's OWN delivered task file into ``recv`` with a single allowlisted
    ``sed`` invocation — so a byte-sensitive payload round-trips exactly without the model retyping
    it (which drops trailing whitespace, seq221). The sed script is a fixed literal; only the two
    path operands are interpolated, each ``shlex.quote``-d so a scratch/state path containing a
    space or an apostrophe still resolves to one argv token (path-quoting review seq225). No pipe,
    process substitution, background, or interpreter ``-c``; ``sed`` is in the disposable session's
    inspection allowlist. The exact-169B sha256 oracle in ``case_r3`` is unchanged.
    """
    return (
        "sed -n '/^<<<BEGIN>>>$/,/^<<<END>>>$/{/^<<<BEGIN>>>$/d;/^<<<END>>>$/d;p;}' "
        f"{shlex.quote(taskfile)} > {shlex.quote(recv)}"
    )


# Ordered case ids. Any id never recorded by the run is emitted as pass=false / "not run".
CASE_IDS = [
    "R0-modal",
    "R0-setup",
    "R1-discover",
    "R2a-bad-ref",
    "R2b-wrong-uuid",
    "R2c-wrong-pid",
    "R2d-wrong-worktree",
    "R2e-bind",
    "R2f-monitor-partial",
    "R3-literal",
    # Completion/drain gate recorded apart from the literal-byte pass (seq185). It is in the padded
    # registry (seq188) so an early abort (e.g. a bind failure before R3) marks it explicitly
    # not_run rather than silently omitting it — the canonical total is fixed by this registry.
    "R3-completion-gate",
    "R3b-replay-original-revision",
    "R3c-diagnostic-terms",
    "R4a-second-writer",
    "R4b-monitor",
    "R4c-monitor-submit",
    "R4d-rebind-supersedes",
    "R4e-simultaneous-writers",
    "R5-stale-revision",
    "R6-busy",
    "R6-turn-complete",
    "R7-non-claude",
    "R8-finite-wait",
    "R8b-observe-then-wait",
    "R9-lost-before",
    "R10-lost-after",
    "R10b-gap-no-resend",
    "R10c-foreign-input",
    "R11a-checkpoint-task",
    "R11b-not-due",
    "R11c0-needs-attestation",
    "R11c-compact",
    "R11d-compact-replay",
    "R11e-checkpoint-refusals",
    "R12-expired-lease",
    "R13-auth-external",
    "R13b-missing-socket",
    "R15-release",
    "R16-teardown",
]

RACE_SCRIPT = """import json, os, sys, time
state_dir, bridge_dir, ws, surf, sid, pid, wt, ctl, barrier, outp = sys.argv[1:11]
os.environ["CMUX_BRIDGE_STATE_DIR"] = state_dir
sys.path.insert(0, bridge_dir)
from cmux_bridge.core import Bridge, BridgeError
from cmux_bridge.cmuxcli import CmuxCLI
from cmux_bridge.state import StateStore
b = Bridge(cli=CmuxCLI(timeout_s=25), store=StateStore(state_dir))
while not os.path.exists(barrier):
    time.sleep(0.01)
try:
    r = b.bind(ws, surf, sid, int(pid), wt, ctl)
    out = {"ok": True, "controller": ctl, "binding_id": r["binding_id"], "revision": r["revision"]}
except BridgeError as e:
    out = {"ok": False, "controller": ctl, "code": e.code, "message": e.message}
except Exception as e:
    out = {"ok": False, "controller": ctl, "code": "exception", "message": "%s: %s" % (type(e).__name__, e)}
open(outp, "w").write(json.dumps(out))
"""


# --------------------------------------------------------------------------- pure helpers
class SurfaceResolutionError(RuntimeError):
    """A `surface:N` ref could not be resolved to exactly one UUID."""


def parse_surface_listing(text: str) -> dict:
    """Map `surface:N` -> UUID from `list-pane-surfaces --id-format both` rows.

    Rows look like `  surface:52 6C1F...-...  some title`. Only the leading ref + UUID pair is
    trusted; anything in the title is ignored. A ref that appears twice with different UUIDs is an
    ambiguity, never a silent last-one-wins.
    """
    found: dict = {}
    for line in (text or "").splitlines():
        m = SURFACE_ROW_RE.match(line)
        if not m:
            continue
        ref, u = m.group(1), m.group(2).upper()
        if ref in found and found[ref] != u:
            raise SurfaceResolutionError(
                f"ref {ref} maps to two UUIDs: {found[ref]} and {u}"
            )
        found[ref] = u
    return found


def resolve_surface_ref(ref: str, listing_text: str) -> str:
    """Resolve exactly the ref `new-surface` returned. Never a set difference over the listing."""
    if not ref:
        raise SurfaceResolutionError("new-surface returned no surface ref")
    mapping = parse_surface_listing(listing_text)
    if ref not in mapping:
        raise SurfaceResolutionError(
            f"ref {ref} not present in list-pane-surfaces ({len(mapping)} rows parsed)"
        )
    return mapping[ref]


def surface_ref_from_json(stdout: str) -> str:
    """`new-surface --json` returns refs only, e.g. {"surface_ref": "surface:52", ...}."""
    try:
        j = json.loads(stdout)
    except ValueError as e:
        raise SurfaceResolutionError(
            f"new-surface did not return JSON: {stdout[:200]!r}"
        ) from e
    for k in ("surface_ref", "surface", "ref", "surfaceRef"):
        v = j.get(k) if isinstance(j, dict) else None
        if isinstance(v, str) and v.startswith("surface:"):
            return v
    raise SurfaceResolutionError(f"no surface ref in new-surface JSON: {j!r}")


class OwnedSurfaces:
    """Bookkeeping for surfaces THIS run created. The only closable set."""

    def __init__(self, path):
        self.path = Path(path)
        self.uuids: list = []
        self.unresolved: list = []
        self.closed: list = []
        self._flush()

    def add(self, surface_uuid: str) -> str:
        u = str(surface_uuid).upper()
        if not UUID_RE.fullmatch(u):
            raise SurfaceResolutionError(
                f"refusing to own a non-UUID surface id: {surface_uuid!r}"
            )
        if u not in self.uuids:
            self.uuids.append(u)
        self._flush()
        return u

    def add_unresolved(self, ref: str) -> None:
        if ref and ref not in self.unresolved:
            self.unresolved.append(ref)
        self._flush()

    def owns(self, surface_uuid: str) -> bool:
        return str(surface_uuid or "").upper() in self.uuids

    def mark_closed(self, surface_uuid: str) -> None:
        u = str(surface_uuid).upper()
        if u not in self.closed:
            self.closed.append(u)
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "owned": self.uuids,
                    "unresolved_refs": self.unresolved,
                    "closed": self.closed,
                    "at": utcnow(),
                },
                indent=2,
            )
        )


class CaseRecorder:
    """Case ledger. `supported=False` (or `not_run`) can never produce pass=true."""

    KINDS = ("real", "real+injected", "state-manipulated")

    def __init__(self, out_path):
        self.path = Path(out_path)
        self.cases: list = []

    def record(
        self, cid, label, kind, ok, evidence=None, note="", supported=True
    ) -> dict:
        if kind not in self.KINDS:
            raise ValueError(f"unknown case kind {kind!r}")
        passed = bool(ok) and bool(supported)
        if not supported:
            note = note if str(note).startswith("not run") else f"not run: {note}"
        rec = {
            "id": cid,
            "label": label,
            "kind": kind,
            "pass": passed,
            "evidence": evidence if evidence is not None else {},
            "note": note,
            "at": utcnow(),
        }
        self.cases.append(rec)
        print(f"[{'PASS' if passed else 'FAIL'}] {cid} {label} ({kind})", flush=True)
        self._flush()
        return rec

    def not_run(self, cid, label, why, kind="real") -> dict:
        return self.record(cid, label, kind, True, {}, note=str(why), supported=False)

    def has(self, cid) -> bool:
        return any(c["id"] == cid for c in self.cases)

    def failed(self) -> list:
        return [c["id"] for c in self.cases if not c["pass"]]

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.cases, indent=2, default=str))


def count_calls(log, name, simulated=None, contains=None) -> int:
    """Count CmuxCLI call-log entries for `name`, optionally filtered by the simulated label."""
    n = 0
    for e in log or []:
        args = e.get("args") or []
        if args[:1] != [name]:
            continue
        if simulated is not None and bool(e.get("simulated")) != bool(simulated):
            continue
        if contains is not None and not any(contains == a for a in args[1:]):
            continue
        n += 1
    return n


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def user_record_text(content):
    """Return the plain SUBMITTED text of a Claude-Code transcript ``user`` record, or None if the
    record is not a plain-text prompt submission.

    A string ``content`` is the submitted text verbatim (this is how the bridge's one-line delivery —
    inline text or a ``Task brief: <file>`` reference — lands in the transcript). A list ``content``
    is a plain prompt only when every block is a ``text`` block (joined); a list carrying a
    ``tool_result`` (or any non-text block) is a tool-result injection, not a prompt, so it returns
    None and is never counted."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "text":
                return None
            parts.append(b.get("text", ""))
        return "".join(parts) if parts else None
    return None


def count_delivered_prompts(records, delivered_text, delivered_text_sha256=None):
    """Authoritative acceptance count: DISTINCT-UUID native ``user`` transcript records whose
    submitted prompt text is EXACTLY the payload the bridge delivered (item-7 exact payload
    correlation).

    The raw ``agent.hook.UserPromptSubmit`` event stream over-counts (seq169 fact 2): a single
    correct delivery can be accompanied by internal skill/command-message turns (``isMeta`` records,
    e.g. a ``workflow-authoring`` skill invocation) that each also fire UserPromptSubmit. Those are
    NOT delivered prompts, so they are excluded here, as are tool-result injections. A GENUINE
    duplicate delivery of the same payload appears as a second distinct UUID with identical text and
    is therefore still detected (the count becomes 2), preserving duplicate detection.

    Matching is exact: full-string equality against ``delivered_text``, or, as a fallback, sha256
    equality against ``delivered_text_sha256``. Distinct ``uuid`` values are counted so a re-read of
    the same record can never inflate the count."""
    if not delivered_text and not delivered_text_sha256:
        return 0
    seen = set()
    for r in records or []:
        if not isinstance(r, dict) or r.get("type") != "user":
            continue
        if r.get(
            "isMeta"
        ):  # internal skill / command-message turn, not a delivered prompt
            continue
        if (
            r.get("toolUseResult") is not None
        ):  # tool-result injection, not a prompt submission
            continue
        text = user_record_text((r.get("message") or {}).get("content"))
        if text is None:
            continue
        hit = (delivered_text is not None and text == delivered_text) or (
            delivered_text_sha256 is not None
            and sha256_bytes(text.encode()) == delivered_text_sha256
        )
        if hit:
            uid = r.get("uuid")
            seen.add(uid if uid is not None else id(r))
    return len(seen)


def assistant_reply_after_delivered(
    records, delivered_text, delivered_text_sha256=None
):
    """Return ``(assistant_text, found)`` — the child's PUBLIC assistant output emitted in reply to
    the delivered prompt whose submitted text is EXACTLY ``delivered_text`` (or matches its sha256).

    This correlates the R3c transport-vocabulary terms to the child's OWN assistant output for the
    exact accepted request, so that an ECHO of the user prompt (which always lands as a ``user``
    record, never an ``assistant`` one), a REFUSAL (assistant text that does not carry the terms), or
    a STALE earlier turn cannot satisfy the case. Only ``assistant`` records' ``text`` blocks between
    the matched delivered ``user`` record and the NEXT delivered user prompt are joined; interleaved
    ``tool_result`` user records (not prompt submissions) do not close the reply window. The LAST
    matching delivery wins, so a duplicate re-delivery reads its own reply rather than an earlier one.

    ``found`` is False when the delivered prompt is not present in ``records`` (transcript unavailable
    or not yet flushed), so a missing/unflushed transcript is never mistaken for an empty reply."""
    if not records:
        return "", False
    start = None
    for i, r in enumerate(records):
        if not isinstance(r, dict) or r.get("type") != "user" or r.get("isMeta"):
            continue
        if (
            r.get("toolUseResult") is not None
        ):  # tool-result injection, not a prompt submission
            continue
        text = user_record_text((r.get("message") or {}).get("content"))
        if text is None:
            continue
        hit = (delivered_text is not None and text == delivered_text) or (
            delivered_text_sha256 is not None
            and sha256_bytes(text.encode()) == delivered_text_sha256
        )
        if hit:
            start = i
    if start is None:
        return "", False
    parts = []
    for r in records[start + 1 :]:
        if not isinstance(r, dict):
            continue
        typ = r.get("type")
        if typ == "user" and not r.get("isMeta") and r.get("toolUseResult") is None:
            if user_record_text((r.get("message") or {}).get("content")) is not None:
                break  # the next delivered prompt closes this reply window
            continue
        if typ != "assistant":
            continue
        content = (r.get("message") or {}).get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text", ""))
    return "".join(parts), True


def split_transport_errors(log_slice):
    """Partition CLI-log entries carrying an ``error_kind`` into
    ``(expected_events_poll_timeouts, real_transport_errors)`` (R3c cause 3, seq231/233).

    A finite ``events --no-heartbeat`` poll timeout is EXPECTED and explicitly tolerated by
    ``core._events`` (core.py:328) — it is normal cursor-poll behaviour, not a transport failure. Any
    OTHER errored CLI call — a ``read-screen``/``send``/``send-key``/ping failure — is a REAL transport
    error. Both classes are returned (never blanket-ignored) so evidence retains the tolerated
    timeouts while only a real transport error fails the case."""
    errs = [e for e in (log_slice or []) if e.get("error_kind")]
    expected = [
        e
        for e in errs
        if (e.get("args") or [""])[0] == "events" and e.get("error_kind") == "timeout"
    ]
    real = [e for e in errs if e not in expected]
    return expected, real


def parse_hook_evidence(text):
    """Recover complete JSON hook records from the disposable session's evidence stream, tolerant of a
    record serialized across more than one physical line, and RETAIN parse-failure evidence so
    malformed-but-nonempty data can never be silently read as 'no hooks fired' (R11c, seq240).

    The executor-side ``evidence.sh`` appends one hook object per PreCompact/SessionStart/Stop/
    UserPromptSubmit event. A per-line ``json.loads`` drops any record whose braces straddle a newline
    (the historical writer closed the outer object on a second line), which silently produced
    ``kinds=[]`` and read a real compaction as a missing dispatch. A ``JSONDecoder.raw_decode`` walk
    over the CONCATENATED stream recovers every complete object regardless of interior whitespace.
    Returns ``(kinds, records, stats)`` where ``kinds`` is the ordered list of ``event`` names,
    ``records`` is the ordered list of the decoded objects (so a caller can correlate a hook to THIS
    fixture, e.g. a SessionStart whose ``payload.source == 'compact'``), and ``stats`` records what was
    and was not consumed so a decode failure stays visible, never a silent zero."""
    kinds, records = [], []
    stats = {
        "objects": 0,
        "bytes_total": len(text or ""),
        "decode_errors": 0,
        "remainder_len": 0,
        "recovered_all": True,
    }
    if not text:
        return kinds, records, stats
    dec = json.JSONDecoder()
    i, n, errors = 0, len(text), 0
    while i < n:
        while i < n and text[i] in " \t\r\n":  # inter/intra-record whitespace
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            errors += 1
            nxt = text.find("{", i + 1)  # resync to the next plausible record start
            if nxt == -1:
                break
            i = nxt
            continue
        if isinstance(obj, dict) and "event" in obj:
            kinds.append(obj.get("event"))
            records.append(obj)
        i = end
    stats["objects"] = len(kinds)
    stats["decode_errors"] = errors
    stats["remainder_len"] = len(text[i:].strip())
    stats["recovered_all"] = errors == 0 and stats["remainder_len"] == 0
    return kinds, records, stats


def precompact_valid_native(records):
    """True iff ``records`` (already scoped to ONE compaction) contains a PreCompact hook carrying a
    real native payload — a non-empty dict WITHOUT a ``_raw`` key (seq246).

    The evidence writer wraps a malformed hook stdin as a VALID outer JSON object ``{"_raw": ...}``, so
    a decoder's ``recovered_all`` stays True even on a garbage payload; this predicate rejects such
    ``_raw``/empty payloads as compaction-dispatch evidence. A genuine native PreCompact carries fields
    like ``session_id`` / ``transcript_path`` / ``trigger``, so requiring a non-empty non-``_raw`` dict
    means an echoed, empty, or malformed payload can never be read as 'this compaction dispatched'."""
    return any(
        r.get("event") == "PreCompact"
        and isinstance(r.get("payload"), dict)
        and r["payload"]
        and "_raw" not in r["payload"]
        for r in records
    )


def r9_redelivered_exactly_once(first, rec1, r9, accepts, real_sends_delta):
    """The R9 receipt predicate: a request LOST before delivery (a simulated zero-byte pre-send fault)
    is re-delivered on the retry and accepted EXACTLY once (seq239).

    The substantive property is the one-send / one-delivery evidence: the first send was a simulated
    zero-byte loss (``not_attempted_simulated``), the retry was accepted, exactly one delivered prompt
    for this request id is in the transcript (``accepts == 1``), and exactly one REAL send was added by
    the retry. The core's ``reconciled`` flag is an OPTIONAL receipt marker its simulated-pre-send
    branch does not emit, so it is recorded as a diagnostic elsewhere but is NOT part of this
    predicate — the assertion matches the actual receipt contract, not an over-specified flag."""
    return (
        isinstance(first, str)
        and first.startswith(("SimulatedFault", "BridgeError send_not_attempted"))
        and (rec1 or {}).get("send_result") == "not_attempted_simulated"
        and (r9 or {}).get("status") == "accepted"
        and accepts == 1
        and real_sends_delta == 1
    )


def r6_completion_ok(wait_outcome, running, released, bytes_copied):
    """R6-turn-complete passes only when the busy fixture actually executed (seq243): the executor
    genuinely reached a running turn, the harness producer released it, the bounded finite stream copy
    produced output, AND the turn then completed. This refuses a false pass where `turn_complete` was
    'satisfied' by a tool that never ran (e.g. the executor errored out without running the copy)."""
    return bool(
        wait_outcome == "satisfied" and running and released and (bytes_copied or 0) > 0
    )


# R6-busy fixture bounds (module-level so a focused regression can shrink them without a real wait).
R6_RUNNING_DEADLINE_S = (
    75.0  # wall-clock budget for the executor to reach a running turn
)
R6_POLL_S = 1.5  # observe interval while waiting for the running state


def _write_all(fd, data):
    """Write EVERY byte of ``data`` to ``fd``. A pipe write of <= PIPE_BUF is atomic, but loop so a
    finite payload is fully delivered even if a short write occurs."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


@contextlib.contextmanager
def r6_stream_fixture(fifo_path, out_path, payload):
    """Controller-held finite-stream FIFO fixture for R6-busy (seq246).

    The controller opens the FIFO ``O_RDWR | O_NONBLOCK`` and holds the fd, so it is simultaneously the
    durable reader AND writer: ``os.write`` never blocks and buffered bytes survive a ``head`` that
    opens late (a producer-first copy), and there is no separate producer thread whose deadline could
    race the reader (the seq243 design waited the whole deadline, then its ``while elapsed < deadline``
    was already false and never fed a blocked reader). On EVERY exit path — including an exception
    raised by the caller's observe/submit/expect/wait — the ``finally`` writes the finite ``payload``
    (unblocking a ``head`` still reading), unlinks BOTH the FIFO and the out file (ENOENT-safe if the
    child never opened them), and closes the fd. So no foreground ``head`` can block past cleanup and no
    FIFO is ever left behind — closing the seq243 gap where the wait and both unlinks sat OUTSIDE
    ``finally`` and leaked on any exception. Yields a handle whose ``.release()`` feeds exactly
    ``payload`` once and whose ``.released`` records whether it did."""
    fifo_path = Path(fifo_path)
    out_path = Path(out_path)
    for p in (fifo_path, out_path):
        with contextlib.suppress(OSError):
            p.unlink()
    os.mkfifo(fifo_path)
    fd = os.open(str(fifo_path), os.O_RDWR | os.O_NONBLOCK)

    class _Stream:
        def __init__(self):
            self.released = False

        def release(self):
            if not self.released:
                _write_all(fd, payload)
                self.released = True

    stream = _Stream()
    try:
        yield stream
    finally:
        if not stream.released:
            with contextlib.suppress(OSError):
                _write_all(fd, payload)
        for p in (fifo_path, out_path):
            with contextlib.suppress(OSError):
                p.unlink()
        with contextlib.suppress(OSError):
            os.close(fd)


# interpreter binaries whose `-c` argument is an arbitrary program string we cannot bound
_INTERP_C = re.compile(
    r"\b(?:sh|bash|zsh|dash|ksh|fish|python[0-9.]*|perl|ruby|node|env)\b[^|&;]*\s-c(?:\s|$)"
)


def _bash_drain_status(cmd):
    """Classify a Bash command string for drain purposes (seq185/186). Returns one of:

      * ``background`` — a job-control ``&`` operator launches work that keeps running
        (``sleep 100 &``, ``sleep 100 & echo launched``, ``sleep 100 & # still running``);
      * ``unknown`` — the command contains an executable command/process substitution
        (``$(...)``, backticks, ``<(...)``, ``>(...)``) or an interpreter ``-c`` script string
        (``sh -c '...'``), which can hide backgrounding INSIDE it (e.g.
        ``echo "$(sleep 100 & echo launched)"``); this fixture check does not parse those and
        refuses them rather than fail open;
      * ``foreground`` — a plain synchronous command; ordinary quoted literal data
        (``echo "a & b"``, ``grep '&'``) and the known synchronous inspection utilities (including
        ``wc -c`` — the ``-c`` interpreter match is scoped to interpreter binaries) are supported.

    Only ``foreground`` is drain-eligible; ``background`` and ``unknown`` are NOT drained. This is a
    bounded honest check, not a shell parser: it never claims an arbitrary program terminated, it
    only decides whether the command form is one whose synchronous completion it can vouch for."""
    if not isinstance(cmd, str):
        return "unknown"
    # single quotes are literal in POSIX shells: a `$(`/backtick/`&` inside them is data. Strip them
    # first, but KEEP double-quoted spans for the substitution scan (they still expand inside "...").
    no_sq = re.sub(r"'[^']*'", " ", cmd)
    if re.search(r"\$\(|`|<\(|>\(", no_sq) or _INTERP_C.search(no_sq):
        return "unknown"
    # for the job-control '&' scan, also drop double-quoted literals (a bare '&' there is data);
    # any executable `$(...)` inside "..." was already caught as unknown above.
    no_q = re.sub(r'"[^"]*"', " ", no_sq)
    if re.search(r"(?<![&>])&(?![&>])", no_q):
        return "background"
    return "foreground"


def drain_check_transcript(records):
    """Ground 'no unknown active work' in a child Claude-Code transcript (seq178, item 4).

    task_complete's drained attestation must be EVIDENCED, never asserted. This computes the
    evidence from the durable transcript by pairing every ``tool_use`` block (assistant content
    items) with its ``tool_result`` (user content items, matched by ``tool_use_id``) AND inspecting
    BOTH the tool input and the tool-result metadata — matched foreground results ALONE never prove
    drain (a backgrounded task returns a result immediately while its work continues).

    FAIL-CLOSED. A session counts as drained ONLY when:
      * every tool_use has a matching tool_result — an unmatched tool_use is a call still in flight
        (e.g. one stuck on an approval modal, exactly the live3 R3 cascade); and
      * no tool_use is backgrounded or async in a way whose completion the transcript cannot prove.
        A tool is treated as pending/unknown when ANY of these hold (conservative):
          - its result carries ``toolUseResult.backgroundTaskId`` (Claude Code marks a backgrounded
            shell there, e.g. ``bjgkkj4wu`` — the authoritative signal, present even when the input
            had no explicit flag);
          - ``input.run_in_background is True`` (Bash sent to background, or a backgrounded Agent);
          - it is a ``Bash`` whose command trails a single ``&`` (shell-level backgrounding, which
            leaves NO backgroundTaskId, so it is caught here);
          - it is a ``Task`` or ``Agent`` subagent spawn (async; a subagent's completion is not
            observable from this transcript, and Agents run in the background by default).
        The footer agent hint and an Agent/Workflow-disallowed launch flag are NOT sufficient
        (item 4). Upstream job state is honestly unknown, so it fails closed.

    A transcript with no tool calls is vacuously drained here; the Runner separately requires the
    accepted request to be PRESENT in a readable transcript before attesting, so an unavailable or
    empty transcript is never mistaken for a proven reply-only completion.

    Returns the tallies it is based on so the attestation records its own grounding rather than a
    bare ``drained: true``."""
    tool_use = []  # (id, name, input)
    result_ids = set()
    result_bg_ids = set()  # tool_use_ids whose RESULT metadata marks them backgrounded
    for r in records or []:
        if not isinstance(r, dict):
            continue
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        if r.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_use.append((b.get("id"), b.get("name"), b.get("input") or {}))
        elif r.get("type") == "user":
            tur = r.get("toolUseResult")
            bg = isinstance(tur, dict) and bool(tur.get("backgroundTaskId"))
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid:
                        result_ids.add(tid)
                        if bg:
                            result_bg_ids.add(tid)
    unmatched = [tid for (tid, _n, _i) in tool_use if tid and tid not in result_ids]
    background = []
    for tid, name, inp in tool_use:
        inp = inp if isinstance(inp, dict) else {}
        reason = None
        if tid in result_bg_ids:
            reason = "result_backgroundTaskId"
        elif inp.get("run_in_background") is True:
            reason = "input_run_in_background"
        elif name == "Bash":
            st = _bash_drain_status(inp.get("command"))
            if st == "background":
                reason = "shell_background_operator"
            elif st == "unknown":
                reason = "shell_unknown_form"
        elif name in ("Task", "Agent"):
            reason = "subagent_spawn"
        if reason:
            background.append({"id": tid, "name": name, "reason": reason})
    return {
        "drained": (not unmatched) and (not background),
        "tool_use": len(tool_use),
        "tool_result": len(result_ids),
        "unmatched_tool_use": unmatched,
        "background_or_async": background,
    }


def pkg_version(name):
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return None


# --------------------------------------------------------------------------- runner
class Runner:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "fixtures").mkdir(exist_ok=True)
        self.rec = CaseRecorder(self.out / "cases.json")
        self.owned = OwnedSurfaces(self.out / "owned-surfaces.json")
        self.state_dir = Path(a.state_dir) if a.state_dir else (self.out / "state")
        os.environ["CMUX_BRIDGE_STATE_DIR"] = str(self.state_dir)
        self.cli = CmuxCLI(timeout_s=25, log=CallLog(maxlen=None))  # unbounded: zero-send deltas must be exact
        self.bridge = Bridge(cli=self.cli, store=StateStore(self.state_dir))
        self.ws = a.workspace.upper()
        self.bid = None
        self.ctl = "ctrl-A"  # controller_id of self.bid
        self.mon_bid = None
        self.surf = None
        self.session_id = None
        self.pid = None
        self.scratch = Path(a.scratch)
        self.evfile = None
        self.focus_before = None
        self.surface_focus = None
        self.meta = {}
        self.started_at = utcnow()

    # ------------------------------------------------------------- small utilities
    @contextlib.contextmanager
    def guard(self, *cids, label="case group"):
        """A failure inside one group records the group's unrecorded ids as 'not run'."""
        try:
            yield
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            detail = getattr(e, "detail", None)
            if detail:
                why += f" {json.dumps(detail, default=str)[:800]}"
            print(f"  ! group failed: {why}", flush=True)
            for cid in cids:
                if not self.rec.has(cid):
                    self.rec.not_run(cid, label, why)

    def cm(self, *args, timeout=25):
        return self.cli.run(*args, timeout=timeout)

    def screen(self, surf, n=40):
        return self.cli.run_ok(
            "read-screen", "--surface", surf, "--lines", str(n)
        ).split("\n")

    def state(self, surf=None):
        return classify_screen(self.screen(surf or self.surf))

    def fixture(self, cid, extra=None):
        """Save the screen tail for parser reuse whenever a delivery/acceptance case fails."""
        try:
            rows = self.screen(self.surf, 60)
        except Exception as e:
            rows = [f"<read-screen failed: {e}>"]
        body = "\n".join(rows)
        if extra:
            body += "\n\n--- extra ---\n" + json.dumps(extra, indent=2, default=str)
        (self.out / "fixtures" / f"{cid}.txt").write_text(body)

    def rev(self):
        return self.bridge.observe(self.bid, self.ctl, lines=5)["revision"]

    def latest_seq(self):
        return self.bridge._latest()[1]

    def ups_count(self, after_seq):
        """DIAGNOSTIC ONLY (seq169 fact 2): raw count of agent.hook.UserPromptSubmit events for this
        child session after `after_seq`. This over-counts internal skill/command-message turns, so it
        is no longer the acceptance predicate — use delivered_prompt_count() for that. Retained as a
        recorded diagnostic to expose the raw-vs-delivered gap."""
        _, evs = self.bridge._events(after_seq, 300, 20)
        return len(
            self.bridge._session_events(
                evs, self.session_id, self.pid, "agent.hook.UserPromptSubmit", after_seq
            )
        )

    def _read_transcript_records(self):
        """Read the child session's durable Claude-Code transcript as a list of JSON records (the
        same file the bridge reconciles acceptance against). Returns [] if the path is unset/missing;
        malformed lines are skipped."""
        p = Path(self.meta.get("transcript_path") or "/nonexistent")
        try:
            raw = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return []
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def delivered_prompt_count(self, B, req_id, min_expected=1, tries=6, delay=0.5):
        """Authoritative acceptance count for `req_id`: DISTINCT-UUID native user transcript records
        whose prompt text is EXACTLY the payload the bridge delivered (delivered_text / its sha256,
        read from the request record). Replaces the raw UserPromptSubmit event count in acceptance
        predicates (seq169 fact 2); internal skill turns and tool-result injections are excluded, and
        a genuine duplicate of the same payload is still detected (count 2).

        The transcript is written by the child asynchronously, so this polls up to tries*delay for
        the record to flush (submit already confirmed delivery, so a duplicate — if any — is already
        present on the first successful read and is never masked). Returns (count, delivered_text,
        records_scanned)."""
        rec = B.store.read(f"requests/{self.bid}/{req_id}.json") or {}
        delivered_text = rec.get("delivered_text")
        delivered_sha = rec.get("delivered_text_sha256")
        n, records = 0, []
        for i in range(max(1, tries)):
            records = self._read_transcript_records()
            n = count_delivered_prompts(records, delivered_text, delivered_sha)
            if n >= min_expected:
                break
            if i < tries - 1:
                time.sleep(delay)
        return n, delivered_text, len(records)

    def _fixture_drain_attestation(self, B, req_id, tries=4, delay=0.5):
        """Ground a drained attestation for `req_id` in the ACTUAL child transcript + a settled
        idle prompt (seq178, item 4). This is the fixture's OWN honest attestation, not a bare
        assertion: it is produced ONLY when the request was accepted, `drain_check_transcript`
        confirms every foreground tool call completed with no pending backgrounded/async work, and
        the bridge itself confirms an idle prompt. Otherwise it returns None (honestly NOT drained),
        so the task_complete predicate stays unsatisfied rather than claiming completion on unknown
        job state. The transcript flushes asynchronously, so an unmatched tool call is re-polled a
        few times to distinguish flush-lag from a genuinely in-flight (e.g. modal-stuck) call.

        An unavailable or empty transcript is NOT a drained session (item 4 hole): before trusting
        the vacuous no-tools drain, the accepted request itself must be PRESENT in a readable
        transcript (its delivered payload found by exact match). That distinguishes 'transcript not
        readable / not flushed' from a genuinely reply-only completed request."""
        rec = B.store.read(f"requests/{self.bid}/{req_id}.json") or {}
        accepted_at = rec.get("accepted_at")
        if not accepted_at:
            return None
        # The accepted request must be present in a readable transcript before we attest anything.
        # delivered_prompt_count polls for the transcript to flush and returns records_scanned==0
        # when the transcript is unavailable/empty; a request not found (accepts<1) is not groundable.
        accepts, _delivered_seen, records_scanned = self.delivered_prompt_count(
            B, req_id
        )
        if records_scanned == 0 or accepts < 1:
            return None
        check = None
        for i in range(max(1, tries)):
            check = drain_check_transcript(self._read_transcript_records())
            if check["drained"]:
                break
            if i < tries - 1:
                time.sleep(delay)
        if not check or not check["drained"]:
            return None
        idle = B.wait(self.bid, self.ctl, "idle", timeout_s=30)
        if idle.get("outcome") != "satisfied":
            return None
        return {
            "drained": True,
            "attested_by": "wfi-live-acceptance-fixture",
            "evidence": {
                "grounded_in": "child_transcript+idle_prompt",
                "request_id": req_id,
                "session_id": self.meta.get("session_id") or self.bid,
                "delivered_prompt_present": accepts,
                "transcript_records_scanned": records_scanned,
                "tool_use": check["tool_use"],
                "tool_result": check["tool_result"],
                "unmatched_tool_use": check["unmatched_tool_use"],
                "background_or_async": check["background_or_async"],
                "idle": idle.get("outcome"),
            },
            "at": utcnow(),
        }

    def expect(self, cid, label, fn, code, kind="real", note=""):
        """Record a refusal case: pass only when the exact error code is raised."""
        try:
            r = fn()
        except BridgeError as e:
            return self.rec.record(
                cid,
                label,
                kind,
                e.code == code,
                {
                    "code": e.code,
                    "expected_code": code,
                    "message": e.message,
                    "detail": e.detail,
                },
                note,
            )
        except Exception as e:  # transport / environment problem: never a pass, never a "failure of the bridge"
            return self.rec.not_run(cid, label, f"{type(e).__name__}: {e}", kind)
        return self.rec.record(
            cid,
            label,
            kind,
            False,
            {"unexpected_success": r, "expected_code": code},
            note,
        )

    def wait_event(self, name, pred, after_seq, secs=60):
        t = time.time()
        cursor = after_seq
        while time.time() - t < secs:
            try:
                _, evs = self.bridge._events(cursor, 200, 6)
            except CmuxError:
                continue
            for e in evs:
                if e.get("name") == name and pred(e):
                    return e
            if evs:
                cursor = max(e["seq"] for e in evs)
        return None

    # ------------------------------------------------------------- surfaces
    def new_surface(self, cwd) -> str:
        """Create a surface and resolve its UUID EXACTLY from the ref new-surface returned.

        cmux 0.64.25: a terminal created with `--focus false` has no terminal view (read-screen
        fails, the shell never starts). The runner already requires the target workspace to be the
        selected one, so the surface is created `--focus true` (the view exists at once), and the
        previously focused surface is then re-focused with `cmux focus-panel --panel <prev>`.
        The WORKSPACE selection is never changed. Outcome is kept in `self.surface_focus`."""
        prev = None
        try:
            # cmux 0.64.25 `identify --json` carries REFS only; the UUIDs appear as `workspace_id` /
            # `surface_id` and only with `--id-format both` (live: the old `*_uuid` read never matched,
            # prev stayed None and focus was never restored).
            f = (
                json.loads(self.cli.run_ok("identify", "--json", "--id-format", "both")).get("focused")
                or {}
            )
            f_ws = f.get("workspace_id") or f.get("workspace_uuid")
            f_sf = f.get("surface_id") or f.get("surface_uuid")
            if str(f_ws or "").upper() == self.ws and f_sf:
                prev = str(f_sf).upper()
        except (CmuxError, ValueError):
            prev = None
        out = self.cli.run_ok(
            "new-surface",
            "--type",
            "terminal",
            "--workspace",
            self.ws,
            "--working-directory",
            str(cwd),
            "--focus",
            "true",
            "--json",
        )
        ref = surface_ref_from_json(out)
        self.owned.add_unresolved(ref)
        last = None
        for _ in range(20):
            time.sleep(0.5)
            listing = self.cli.run_ok(
                "list-pane-surfaces", "--workspace", self.ws, "--id-format", "both"
            )
            try:
                u = resolve_surface_ref(ref, listing)
            except SurfaceResolutionError as e:
                last = str(e)
                continue
            self.owned.add(u)  # owned BEFORE anything else can fail
            realized = False
            for _ in range(20):  # the view must exist before focus moves off the new tab
                if self.cm("read-screen", "--surface", u, "--lines", "3").ok:
                    realized = True
                    break
                time.sleep(0.5)
            restored = False
            if prev and prev != u:
                restored = self.cm("focus-panel", "--panel", prev, "--workspace", self.ws).ok
            self.surface_focus = {
                "view_realized": realized,
                "previous_surface": prev,
                "focus_restored": restored,
                "command": "focus-panel",
            }
            time.sleep(1.5)
            return u
        raise SurfaceResolutionError(f"could not resolve {ref} to a UUID: {last}")

    def close_owned(self, surface_uuid) -> bool:
        if not self.owned.owns(surface_uuid):
            print(
                f"  ! refusing to close {surface_uuid}: not created by this run",
                flush=True,
            )
            return False
        res = self.cm(
            "close-surface", "--surface", surface_uuid, "--workspace", self.ws
        )
        self.owned.mark_closed(surface_uuid)
        return res.ok

    def wait_shell(self, surf, secs=60) -> bool:
        t = time.time()
        while time.time() - t < secs:
            res = self.cm("read-screen", "--surface", surf, "--lines", "8")
            if res.ok:  # a just-created surface answers internal_error until its terminal view exists
                lines = [l for l in res.stdout.split("\n") if l.strip()]
                if any(re.search(r"[%$#] ?$", l) for l in lines[-3:]):
                    return True
            time.sleep(0.5)
        return False

    # ------------------------------------------------------------- preflight / setup
    def preflight(self):
        ping = self.cm("ping", timeout=10)
        if not ping.ok:
            raise RuntimeError(
                f"cmux ping -> {ping.error_kind} {ping.stderr.strip()[:200]} "
                "(run from a cmux-descended process)"
            )
        d = self.bridge.discover(self.ws)
        self.meta = {
            "contract": CONTRACT,
            "head": subprocess.run(
                ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "git_status_porcelain": subprocess.run(
                ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "python": sys.version.split()[0],
            "fastmcp": pkg_version("fastmcp"),
            "mcp": pkg_version("mcp"),
            "cmux_version": (d.get("transport") or {}).get("cmux_version"),
            "cmux_access_mode": (d.get("transport") or {}).get("access_mode"),
            "host": os.uname().nodename,
            "started_at": self.started_at,
            "model": self.a.model,
            "workspace": self.ws,
            "state_dir": str(self.state_dir),
        }
        (self.out / "environment.json").write_text(json.dumps(self.meta, indent=2))
        return d

    def setup(self):
        ident = json.loads(self.cli.run_ok("identify", "--json"))
        self.focus_before = ident.get("focused")
        scratch = self.scratch
        scratch.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(scratch)], check=True)
        (scratch / "README.md").write_text(
            "scratch worktree for bridge acceptance; disposable\n"
        )
        subprocess.run(["git", "-C", str(scratch), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(scratch),
                "-c",
                "user.email=bridge@local",
                "-c",
                "user.name=bridge",
                "commit",
                "-q",
                "-m",
                "init",
            ],
            check=True,
        )
        ev_dir = scratch / ".bridge"
        ev_dir.mkdir(exist_ok=True)
        hook = ev_dir / "evidence.sh"
        # One COMPLETE, COMPACT JSON record per line (seq240/243). The old writer closed the outer
        # object on a second physical line, splitting every record across two invalid-JSON lines; a
        # naive single-printf still fails when the hook stdin is pretty-printed (interior newlines
        # survive). So the payload is PARSED and re-serialized to COMPACT single-line JSON with a
        # single append: json.dumps escapes any embedded newline INSIDE a string value (payload values
        # are preserved, not stripped), and a stdin that is not valid JSON is retained verbatim under
        # `_raw` rather than dropped, so a malformed hook can never silently vanish. python3 is the
        # bridge's own runtime and is present wherever the executor runs.
        hook.write_text(
            "#!/bin/bash\n"
            "# executor-side evidence hook for the disposable session only;"
            " one compact JSON record per line\n"
            'at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"\n'
            "python3 -c '"
            "import sys, json\n"
            "event, at = sys.argv[1], sys.argv[2]\n"
            "raw = sys.stdin.read()\n"
            "try:\n"
            "    payload = json.loads(raw) if raw.strip() else None\n"
            "except Exception:\n"
            '    payload = {"_raw": raw}\n'
            'rec = {"event": event, "at": at, "payload": payload}\n'
            'sys.stdout.write(json.dumps(rec, separators=(",", ":")) + "\\n")\n'
            '\' "$1" "$at" >> "$2"\n'
            "exit 0\n"
        )
        hook.chmod(0o755)
        evfile = ev_dir / "evidence.jsonl"
        hooks = {
            ev: [
                {
                    "hooks": [
                        {"type": "command", "command": f"bash {hook} {ev} {evfile}"}
                    ]
                }
            ]
            for ev in ("PreCompact", "SessionStart", "Stop", "UserPromptSubmit")
        }
        # Permit the executor to READ its own bridge-delivered task briefs (task-file references
        # under <state-dir>/tasks/**) without a permission modal, scoped to EXACTLY that owned
        # subtree. Filesystem-absolute permission rules require a DOUBLE leading slash — a single
        # slash anchors to the settings source, not the filesystem root (official permission docs,
        # seq169) — and macOS resolves /tmp to /private/tmp, so both aliases are granted. This is a
        # reads-only, path-scoped grant: Bash stays the narrow sleep/cat/od allowlist and approval
        # mode stays on, so an unexpected op still hits the modal and R0-modal's unknown-modal
        # refusal is preserved (seq168). A Read()-tool rule does NOT govern a Bash command's
        # file access, so the executor's OWN sed byte-extraction of its delivered brief (R3),
        # which lives under <state-dir>/tasks/** OUTSIDE the worktree, still hit the working-
        # directory boundary and raised an unanswerable "Do you want to proceed?" modal
        # (live-20260910T083422Z; dialog captured on surface:131). A NARROW run-owned
        # `--add-dir <state-dir>/tasks` is therefore added at launch (below) so those Bash
        # reads proceed without a modal (fixture-fix-decision). Still no BROAD --add-dir /
        # Read(*) / Bash(*) / trust-off, and approval mode stays on so an UNLISTED command
        # still hits the modal.
        read_allow = [
            f"Read(/{d}/tasks/**)"
            for d in dict.fromkeys(
                [str(self.state_dir), os.path.realpath(self.state_dir)]
            )
        ]
        (ev_dir / "settings.json").write_text(
            json.dumps({"hooks": hooks, "permissions": {"allow": read_allow}}, indent=2)
        )
        self.evfile = evfile
        # Runtime fact (observed on cmux 0.64.17): a new surface receives a terminal view only when
        # its workspace is the selected one in its window; in an unselected workspace read-screen
        # answers internal_error indefinitely and no shell ever starts. The runner never changes the
        # user's selection, so it refuses up front instead of waiting 60 s.
        try:
            ident = json.loads(self.cm("identify", "--json").stdout)
        except Exception:
            ident = {}
        sel = (ident.get("focused") or {}).get("workspace_ref")
        my_ref = None
        for row in self.cm(
            "list-workspaces", "--id-format", "both"
        ).stdout.splitlines():
            if self.ws in row.upper():
                m = re.search(r"workspace:\d+", row)
                my_ref = m.group(0) if m else None
        if sel and my_ref and sel != my_ref:
            raise RuntimeError(
                f"target workspace {my_ref} ({self.ws}) is not the selected workspace ({sel}); a new "
                "surface there would have no terminal view. Run while that workspace is selected; the "
                "runner does not change the user's selection"
            )
        self.surf = self.new_surface(str(scratch))
        if not self.wait_shell(self.surf):
            last = self.cm("read-screen", "--surface", self.surf, "--lines", "8")
            health = self.cm("surface-health", "--workspace", self.ws)
            self.rec.record(
                "R0-shell",
                "shell prompt never appeared on the disposable surface",
                "real",
                False,
                {
                    "last_read": {
                        "rc": last.returncode,
                        "error_kind": last.error_kind,
                        "stdout_tail": last.stdout.split("\n")[-4:],
                        "stderr": last.stderr.strip()[:200],
                    },
                    "surface_health": [
                        l
                        for l in health.stdout.splitlines()
                        if self.surf[:8] in l or "surface:" in l
                    ][-6:],
                    "selected_workspace": sel,
                },
            )
            raise RuntimeError("shell prompt never appeared on the disposable surface")
        self.session_uuid = str(uuidmod.uuid4())
        _, latest = self.bridge._latest()
        # allowedTools is a NARROW, curated set (never a blanket Bash(*)): sleep for the busy/wait
        # cases, plus a FIXED disposable-session INSPECTION-UTILITY allowlist. A byte-exact task (R3)
        # legitimately inspects the raw bytes of its OWN delivered task-file, because trailing
        # spaces, a literal tab and unicode are invisible in normal display — the executor hexdumps
        # to reproduce them verbatim. Live runs have used `cat -A`/`od -c`, and also reached for
        # sed/wc/xxd/tail, which stalled on the Bash modal under a cat/od-only set.
        # Enumerating the common inspection helpers —
        # scoped to THIS disposable throwaway session only — lets that own-file inspection proceed
        # without a modal.
        #
        # IMPORTANT: a command-NAME allowlist is NOT a read-only enforcement boundary. An
        # allowlisted tool used with a write flag (`sed -i`, `xxd -r`) or a shell redirect can still
        # WRITE without a modal, so this does not, and must not be described as, keeping every
        # write/destructive op behind the modal. What it IS: an inspection-
        # utility convenience for a throwaway session that is meant to be mutated anyway. The
        # property actually preserved is the BRIDGE's unknown-modal refusal — the bridge refuses to
        # send input while the surface shows a modal — and a genuinely UNLISTED command name (rm,
        # curl, git, python3, …) still raises that modal. Read access to delivered briefs stays via
        # the disposable --settings permissions.allow rules in setup(), NOT a broad --add-dir grant.
        # Effect confirmed by a bounded R3 preflight (reuse Runner) before the full run.
        inspect_tools = (
            "cat",
            "od",
            "sed",
            "wc",
            "xxd",
            "hexdump",
            "head",
            "tail",
            "cut",
            "strings",
            "diff",
            "cmp",
        )
        bash_allow = "Bash(sleep *) " + " ".join(f"Bash({t} *)" for t in inspect_tools)
        # Grant EXACTLY this run's owned task subtree as an additional trusted directory so the
        # executor's own allowlisted Bash reads of its delivered brief do not hit the working-
        # directory modal (see the read_allow note above). Narrow + run-owned; both /tmp and
        # /private/tmp aliases so the path resolves regardless of the macOS symlink.
        tasks_dir = self.state_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        add_dir_flags = " ".join(
            f"--add-dir {d}"
            for d in dict.fromkeys([str(tasks_dir), os.path.realpath(tasks_dir)])
        )
        perm = "acceptEdits"
        allow_flag = f"--allowedTools '{bash_allow}' "
        cmd = (
            # shell env prefix: the shell execs claude directly with the var, no extra process/argv change
            f"CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false claude --model {self.a.model} --permission-mode {perm} "
            f"{allow_flag}{add_dir_flags} "
            f"--disallowedTools Agent Workflow --session-id {self.session_uuid} "
            f"--settings {ev_dir / 'settings.json'}\n"
        )
        self.cli.run_ok("send", "--surface", self.surf, "--", cmd)
        # The workspace-trust dialog (a modal) can appear BEFORE SessionStart fires, so interleave
        # screen checks with event polling. The session is matched by cwd; id and pid come from the
        # event payload.
        real_scratch = os.path.realpath(str(scratch))
        t = time.time()
        modal_seen = False
        ev = None
        cursor = latest
        last_screen = []
        while time.time() - t < 150 and not ev:
            res = self.cm("read-screen", "--surface", self.surf, "--lines", "40")
            if res.ok:
                last_screen = res.stdout.split("\n")
                st = classify_screen(last_screen)
                if st["state"] == "modal" and not modal_seen:
                    modal_seen = True
                    self.rec.record(
                        "R0-modal",
                        "trust dialog classified as modal (the bridge refuses input); operator selects "
                        "'Yes, I trust this folder' on the disposable session only",
                        "real",
                        True,
                        {"screen": st, "tail": last_screen[-12:]},
                    )
                    if any("Yes, I trust this folder" in l for l in last_screen):
                        self.cli.run_ok("send-key", "--surface", self.surf, "Down")
                        time.sleep(0.3)
                    self.cli.run_ok("send-key", "--surface", self.surf, "Enter")
                    time.sleep(1.5)
            try:
                _, evs = self.bridge._events(cursor, 200, 4)
            except CmuxError:
                evs = []
            for e in evs:
                if (
                    e.get("name") == "agent.hook.SessionStart"
                    and os.path.realpath(e["payload"].get("cwd") or "") == real_scratch
                ):
                    ev = e
                    break
            if evs:
                cursor = max(e["seq"] for e in evs)
        if not ev:
            self.fixture("R0-setup", {"screen_tail": last_screen[-15:]})
            raise RuntimeError(
                f"no SessionStart for the disposable session; screen tail: {last_screen[-15:]}"
            )
        self.session_id = canonical_session_id(ev["payload"]["session_id"])
        self.pid = int(ev["payload"]["_ppid"])
        t = time.time()
        while time.time() - t < 60:
            st = self.state()
            if st["state"] == "modal":
                if not modal_seen:
                    modal_seen = True
                    self.rec.record(
                        "R0-modal",
                        "trust/permission dialog classified as modal (bridge would refuse input)",
                        "real",
                        True,
                        {"screen": st},
                    )
                self.cli.run_ok("send-key", "--surface", self.surf, "Enter")
                time.sleep(1.5)
            elif st["state"] == "prompt_idle":
                break
            else:
                time.sleep(1.0)
        st = self.state()
        if st["state"] != "prompt_idle":
            self.fixture("R0-setup", {"screen": st})
            raise RuntimeError(f"disposable session not idle: {st}")
        if not self.rec.has("R0-modal"):
            self.rec.not_run(
                "R0-modal",
                "trust dialog classified as modal",
                "no modal appeared (folder already trusted for this scratch path)",
            )
        ids = self.bridge._proc_cmux_ids(self.pid)
        self.meta["transcript_path"] = str(
            Bridge.transcript_path(self.session_id, os.path.realpath(str(scratch)))
        )
        self.rec.record(
            "R0-setup",
            "disposable Claude session started on an unfocused surface with executor evidence hooks; "
            "the process environment links pid to surface",
            "real",
            bool(ids) and ids["surface_uuid"] == self.surf,
            {
                "surface": self.surf,
                "pid": self.pid,
                "session_id": self.session_id,
                "process_env_ids": ids,
                "modal_seen": modal_seen,
                "focus_before": self.focus_before,
                "owned_surfaces": self.owned.uuids,
                "transcript_path": self.meta["transcript_path"],
            },
        )

    # ------------------------------------------------------------- cases
    def run(self):
        B = self.bridge
        C = self.ctl
        with self.guard("R1-discover"):
            d = B.discover(self.ws)
            mine = [t for t in d["targets"] if t["surface_uuid"] == self.surf]
            ok = bool(
                d["transport"]["ok"]
                and mine
                and mine[0].get("claude_session_id") == self.session_id
                and os.path.realpath(mine[0].get("cwd") or "")
                == os.path.realpath(str(self.scratch))
            )
            self.rec.record(
                "R1-discover",
                "discover reports transport/access mode and the disposable target with "
                "pid, session id and cwd",
                "real",
                ok,
                {
                    "transport": d["transport"],
                    "caller": d.get("caller"),
                    "focused": d.get("focused"),
                    "target": mine[:1],
                },
            )

        with self.guard(
            "R2a-bad-ref",
            "R2b-wrong-uuid",
            "R2c-wrong-pid",
            "R2d-wrong-worktree",
            "R2e-bind",
            "R2f-monitor-partial",
            label="bind identity",
        ):
            self.expect(
                "R2a-bad-ref",
                "bind refuses surface:N refs (refs are not identities)",
                lambda: B.bind(
                    self.ws,
                    "surface:47",
                    self.session_id,
                    self.pid,
                    str(self.scratch),
                    C,
                ),
                "bad_request",
            )
            self.expect(
                "R2b-wrong-uuid",
                "bind refuses an unknown surface UUID",
                lambda: B.bind(
                    self.ws,
                    str(uuidmod.uuid4()).upper(),
                    self.session_id,
                    self.pid,
                    str(self.scratch),
                    C,
                ),
                "identity",
            )
            self.expect(
                "R2c-wrong-pid",
                "bind refuses a pid the sidebar/process env does not link to the surface",
                lambda: B.bind(
                    self.ws,
                    self.surf,
                    self.session_id,
                    os.getpid(),
                    str(self.scratch),
                    C,
                ),
                "bind_evidence_missing",
            )
            self.expect(
                "R2d-wrong-worktree",
                "bind refuses a worktree path that is not the session cwd",
                lambda: B.bind(
                    self.ws, self.surf, self.session_id, self.pid, "/tmp", C
                ),
                "identity",
            )
            b = B.bind(
                self.ws, self.surf, self.session_id, self.pid, str(self.scratch), C
            )
            self.bid = b["binding_id"]
            focus_now = json.loads(self.cli.run_ok("identify", "--json")).get("focused")
            self.rec.record(
                "R2e-bind",
                "bind succeeds on UUID + session + pid + worktree; focus unchanged",
                "real",
                b["revision"] == 1 and focus_now == self.focus_before,
                {
                    "binding": {
                        k: b.get(k)
                        for k in (
                            "binding_id",
                            "revision",
                            "boot_id",
                            "cursor_seq",
                            "identity_evidence",
                            "evidence",
                        )
                    },
                    "focused_now": focus_now,
                },
            )
            # A well-formed but fabricated session id for the same pid: the core must either refuse,
            # or bind ONLY as a partial-evidence monitor. A full-evidence binding would be a bug.
            fake_sid = "claude-" + str(uuidmod.uuid4())
            try:
                mb = B.bind(
                    self.ws,
                    self.surf,
                    fake_sid,
                    self.pid,
                    str(self.scratch),
                    "ctrl-M",
                    role="monitor",
                )
            except BridgeError as e:
                self.rec.record(
                    "R2f-monitor-partial",
                    "monitor bind with a fabricated session id for the same pid is refused",
                    "real",
                    e.code in ("identity", "bind_evidence_missing"),
                    {
                        "outcome": "refused",
                        "code": e.code,
                        "message": e.message,
                        "detail": e.detail,
                    },
                    note=f"core refuses with code={e.code}",
                )
            else:
                partial = (
                    mb.get("identity_evidence") == "partial"
                    and mb.get("role") == "monitor"
                )
                self.rec.record(
                    "R2f-monitor-partial",
                    "monitor bind with a fabricated session id for the same pid is admitted only as a "
                    "partial-evidence monitor (never full evidence, never a writer)",
                    "real",
                    partial,
                    {
                        "outcome": "bound",
                        "identity_evidence": mb.get("identity_evidence"),
                        "role": mb.get("role"),
                        "binding_id": mb.get("binding_id"),
                    },
                    note="core admits partial monitor bindings; released immediately",
                )
                with contextlib.suppress(BridgeError):
                    B.release(mb["binding_id"], "ctrl-M")

        with self.guard(
            "R3-literal",
            "R3-completion-gate",
            "R3b-replay-original-revision",
            "R3c-diagnostic-terms",
            label="literal delivery",
        ):
            self.case_r3(B)

        if os.environ.get("LIVE_PREFLIGHT_R3") == "1":
            # Bounded R3 preflight (seq174): exercise the REAL run path — preflight, setup, discover,
            # bind, then the R3 literal-delivery group — and stop before the rest of the suite. This
            # reuses the Runner end-to-end (identical delivery/acceptance code); it is not a new
            # framework. Used to confirm the R3 tool-contract fix clears the Bash-inspection modal
            # cheaply, since a repeated early R3 failure otherwise cascades through the whole suite.
            return

        with self.guard(
            "R4a-second-writer", "R4b-monitor", "R4c-monitor-submit", label="leases"
        ):
            B2 = Bridge(cli=self.cli, store=StateStore(self.state_dir))
            self.b2 = B2
            self.expect(
                "R4a-second-writer",
                "a second controller cannot take the writer lease",
                lambda: B2.bind(
                    self.ws,
                    self.surf,
                    self.session_id,
                    self.pid,
                    str(self.scratch),
                    "ctrl-B",
                ),
                "lease_conflict",
            )
            mon = B2.bind(
                self.ws,
                self.surf,
                self.session_id,
                self.pid,
                str(self.scratch),
                "ctrl-B",
                role="monitor",
            )
            self.mon_bid = mon["binding_id"]
            o = B2.observe(self.mon_bid, "ctrl-B", lines=20)
            self.rec.record(
                "R4b-monitor",
                "a harmless monitor binds and observes alongside the active writer",
                "real",
                o["state"] in ("prompt_idle", "running", "staged"),
                {
                    "observe": {
                        k: o.get(k)
                        for k in (
                            "state",
                            "ctx_used_pct",
                            "revision",
                            "events",
                            "after_seq",
                            "next_after_seq",
                        )
                    }
                },
            )
            self.expect(
                "R4c-monitor-submit",
                "a monitor cannot submit",
                lambda: B2.submit(self.mon_bid, "ctrl-B", "m1", "hello", 1),
                "not_writer",
            )

        with self.guard("R4d-rebind-supersedes", label="rebind"):
            old_bid = self.bid
            nb = B.bind(
                self.ws, self.surf, self.session_id, self.pid, str(self.scratch), C
            )
            superseded = old_bid in (nb.get("superseded_bindings") or [])
            try:
                B.submit(old_bid, self.ctl, "r4d-old", "Reply with the single word STALE.", 1)
                lost = {"code": None, "unexpected_success": True}
            except BridgeError as e:
                lost = {"code": e.code, "message": e.message}
            self.bid = nb["binding_id"]
            self.rec.record(
                "R4d-rebind-supersedes",
                "re-binding the same controller supersedes the old binding, whose submit then fails "
                "lease_lost; the run continues on the new binding",
                "real",
                superseded and lost.get("code") == "lease_lost",
                {
                    "old_binding": old_bid,
                    "new_binding": self.bid,
                    "superseded_bindings": nb.get("superseded_bindings"),
                    "old_submit": lost,
                },
            )

        with self.guard("R4e-simultaneous-writers", label="simultaneous writers"):
            self.case_r4e(B, C)

        with self.guard("R5-stale-revision"):
            cur = self.rev()
            self.expect(
                "R5-stale-revision",
                "submit with a stale expected_revision is refused",
                lambda: B.submit(self.bid, self.ctl, "r5", "hello", cur - 1),
                "stale_revision",
            )

        with self.guard("R6-busy", "R6-turn-complete", label="busy"):
            self.case_r6(B)

        with self.guard("R7-non-claude", label="non-claude surface"):
            shell_surf = self.new_surface(str(self.scratch))
            self.wait_shell(shell_surf)
            self.expect(
                "R7-non-claude",
                "binding a plain shell surface is refused",
                lambda: B.bind(
                    self.ws, shell_surf, self.session_id, self.pid, str(self.scratch), C
                ),
                "bind_evidence_missing",
                note="no sidebar claude_code status and no process-env link for that surface",
            )
            self.close_owned(shell_surf)

        with self.guard("R8-finite-wait"):
            # Measure the finite-timeout bound over a genuinely QUIET window (seq239): with no
            # request_id or after_seq, wait starts from the stale binding cursor and can 'satisfy' on a
            # historical Stop event from the previous case (R6's turn ended at Stop 34802). Settle to
            # idle, snapshot the latest seq, then bound-wait FROM that fresh cursor so only new events
            # count and 'nothing happens' actually holds. This tests the timeout bound, not stale
            # evidence, and does not weaken any gate.
            B.wait(self.bid, self.ctl, "idle", timeout_s=30)
            fresh_cursor = self.latest_seq()
            w8 = B.wait(self.bid, self.ctl, "turn_complete", timeout_s=6, after_seq=fresh_cursor)
            self.rec.record(
                "R8-finite-wait",
                "wait returns a bounded timeout when nothing happens, measured from a fresh drained "
                "cursor so a historical Stop event cannot satisfy it",
                "real",
                w8["outcome"] == "timeout" and w8.get("elapsed_s", 99) <= 12,
                {"wait": w8, "after_seq": fresh_cursor},
            )

        with self.guard("R8b-observe-then-wait", label="observe does not consume"):
            self.case_r8b(B)

        with self.guard("R9-lost-before", label="lost before delivery"):
            self.case_r9(B)
        with self.guard("R10-lost-after", label="lost after delivery"):
            self.case_r10(B)
        with self.guard("R10b-gap-no-resend", label="events gap"):
            self.case_r10b(B)
        with self.guard("R10c-foreign-input", label="foreign input"):
            self.case_r10c(B)

        with self.guard(
            "R11a-checkpoint-task",
            "R11b-not-due",
            "R11c-compact",
            "R11d-compact-replay",
            "R11e-checkpoint-refusals",
            label="compaction",
        ):
            self.case_r11(B)

        with self.guard("R12-expired-lease", label="expired lease"):
            bpath = self.state_dir / "bindings" / f"{self.bid}.json"
            bj = json.loads(bpath.read_text())
            saved = bj["lease_expires_at"]
            bj["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
            bpath.write_text(json.dumps(bj))
            try:
                self.expect(
                    "R12-expired-lease",
                    "an expired lease is refused",
                    lambda: B.observe(self.bid, self.ctl),
                    "lease_expired",
                    kind="state-manipulated",
                )
            finally:
                bj["lease_expires_at"] = saved
                bpath.write_text(json.dumps(bj))

        with self.guard("R13-auth-external", "R13b-missing-socket", label="auth"):
            self.case_r13()

        with self.guard("R15-release", label="release"):
            rel = B.release(self.bid, self.ctl)
            rel2 = self.b2.release(self.mon_bid, "ctrl-B") if self.mon_bid else {"released": None}
            self.rec.record(
                "R15-release",
                "release removes the leases and leaves the session running",
                "real",
                bool(rel.get("released")) and self.bridge._pid_alive(self.pid),
                {
                    "writer": rel,
                    "monitor": rel2,
                    "pid_alive": self.bridge._pid_alive(self.pid),
                },
            )

    # ------------------------------------------------------------- individual cases
    def case_r3(self, B):
        recv = self.scratch / "received.txt"
        with contextlib.suppress(OSError):
            recv.unlink()
        req = "r3-literal"
        # The delivered brief IS the immutable, bridge-owned task file (mode 0444) submit() writes
        # at this deterministic path; the executor reads it via its path-scoped Read grant.
        # Byte-finding resolution (rev2 amendment seq221): the earlier R3 FAIL was NOT a Write /
        # filesystem trimming bug. Preflight3's public tool trace showed the executor READ all 169B
        # (incl. the 3 trailing spaces) but the model's Write INPUT was already 166B before the
        # filesystem — the model dropped the trailing whitespace while composing byte-sensitive
        # content by hand. So R3 no longer asks the model to retype the payload. It instructs a
        # DETERMINISTIC foreground copy: one allowlisted `sed` command extracts the payload verbatim
        # from between the markers in the executor's OWN delivered task file into received.txt. This
        # tests what the case claims — the bridge stored the payload byte-exact and it round-trips
        # verbatim — without depending on the model's manual trailing-whitespace fidelity. The
        # strict exact-169B sha256 oracle is UNCHANGED. `sed` is in the disposable session's
        # inspection allowlist (no broad Bash, no process substitution, no pipe, no background, no
        # interpreter -c); the anchored ^<<<BEGIN>>>$ / ^<<<END>>>$ addresses match only the
        # standalone marker lines, so the sed command echoed inside this prose does not perturb the
        # extraction (verified locally: byte-exact 169B, sha256 14d3a989…c29694).
        taskfile = str(B.store.path(f"tasks/{self.bid}/{req}.txt"))
        sed_cmd = _r3_copy_command(taskfile, str(recv))
        text = (
            f"Your delivered task brief is the file {taskfile}. It contains a byte-sensitive "
            "payload (quotes, backticks, $, Unicode, an emoji, a literal tab, and trailing spaces) "
            "between the two marker lines shown at the end of this brief. Do NOT retype or "
            "reconstruct the payload by hand — hand-copying silently loses invisible characters such "
            f"as trailing spaces. Instead reproduce it deterministically into the file {recv} (that "
            "exact absolute path) by running this ONE command EXACTLY as written, verbatim, in a "
            "single foreground Bash call:\n"
            f"{sed_cmd}\n"
            "Do not add, remove, or change any character of that command, and do not use the "
            "Write or Edit tools for the payload. After it completes, reply with the single word "
            "DONE-R3.\n<<<BEGIN>>>\n" + PAYLOAD + "<<<END>>>"
        )
        rev0 = self.rev()
        cur0 = self.latest_seq()
        r = B.submit(self.bid, self.ctl, req, text, rev0)
        payload_sha = sha256_bytes(PAYLOAD.encode())
        # Two-phase, so the drained attestation is GROUNDED in the completed turn rather than
        # guessed at submit time (seq178): (1) wait for the accepted turn to end, (2) ground the
        # attestation from the durable transcript + idle prompt, (3) assert task_complete with it.
        # A stuck (modal) foreground call leaves the turn incomplete and the attestation None, so
        # task_complete stays honestly unsatisfied instead of burning 210s on a doomed poll.
        w_fallback = B.wait(self.bid, self.ctl, "turn_complete", timeout_s=210, request_id=req)
        drain_att = self._fixture_drain_attestation(B, req)
        w = (
            B.wait(
                self.bid, self.ctl,
                "task_complete",
                timeout_s=45,
                request_id=req,
                evidence={
                    "file": str(recv),
                    "sha256": payload_sha,
                    "drained_attestation": drain_att,
                },
            )
            if drain_att
            else {"outcome": "unsatisfied_not_drained", "turn_complete": w_fallback}
        )
        got = recv.read_bytes() if recv.exists() else None
        # Acceptance is counted from the durable transcript by exact delivered-payload match on
        # distinct UUIDs (seq169 fact 2); the raw UserPromptSubmit event count is kept only as a
        # diagnostic because it over-counts internal skill/command-message turns.
        ups_raw = self.ups_count(cur0)
        accepts, delivered_text_seen, tr_scanned = self.delivered_prompt_count(B, req)
        rec_r3 = B.store.read(f"requests/{self.bid}/{req}.json") or {}
        delivery_mode = rec_r3.get("delivery_mode")
        payload_file = (rec_r3.get("payload") or {}).get("file")
        exact = got is not None and got == PAYLOAD.encode()
        modulo_nl = got is not None and got.decode("utf-8", "replace").rstrip(
            "\n"
        ) == PAYLOAD.rstrip("\n")
        # Strict pass requires EXACT bytes: the case claims verbatim delivery and its evidence is
        # the exact payload sha256, so modulo-newline equality is retained only as a labeled
        # diagnostic (bytes_equal_ignoring_final_newline), never as the pass predicate.
        ok = r["status"] == "accepted" and accepts == 1 and exact
        tpath = Path(self.meta.get("transcript_path", ""))
        self.meta["transcript_exists"] = tpath.is_file() if str(tpath) else False
        self.rec.record(
            "R3-literal",
            "multi-line byte-sensitive text (quotes, backticks, $, Unicode, emoji, tab, trailing "
            "spaces) is delivered as an immutable task-file reference — never pasted raw — accepted "
            "exactly once, and its payload round-trips byte-exact into received.txt via a "
            "deterministic foreground copy (an allowlisted sed extraction from the executor's own "
            "delivered task file), not by the model retyping byte-sensitive content",
            "real",
            ok,
            {
                "submit": r,
                "delivery_mode": delivery_mode,
                "payload_file": payload_file,
                "wait": w,
                "wait_fallback": w_fallback,
                "drained_attestation": drain_att,
                "delivered_prompt_count": accepts,
                "userpromptsubmit_events_raw": ups_raw,
                "delivered_text_seen": delivered_text_seen,
                "transcript_records_scanned": tr_scanned,
                "bytes_exact": exact,
                "bytes_equal_ignoring_final_newline": modulo_nl,
                "payload_sha256": payload_sha,
                "received_len": got and len(got),
                "expected_len": len(PAYLOAD.encode()),
                "transcript_path": str(tpath),
                "transcript_exists": self.meta["transcript_exists"],
            },
            note=""
            if exact
            else "executor's file differs from the payload only by trailing-newline handling"
            if modulo_nl
            else f"received={got!r}",
        )
        if not ok:
            self.fixture("R3-literal", {"submit": r, "wait": w})
        # Completion/drain gate recorded SEPARATELY from the literal-byte pass (seq185): exact bytes
        # must never conceal an unsatisfied task_complete. task_complete is 'satisfied' only with the
        # grounded drained attestation; a modal-stuck turn (no attestation) shows here as a red gate
        # even when the file bytes happen to be correct.
        completion_ok = w.get("outcome") == "satisfied"
        self._r3_completion_ok = completion_ok
        self.rec.record(
            "R3-completion-gate",
            "task_complete is satisfied only with acceptance + turn + fresh artifact + idle + a "
            "grounded drained attestation; recorded apart from the literal-byte pass so good bytes "
            "cannot hide an unsatisfied completion/drain wait",
            "real",
            completion_ok,
            {
                "wait": w,
                "turn_complete": w_fallback,
                "drained_attestation": drain_att,
            },
            note=""
            if completion_ok
            else f"task_complete not satisfied: outcome={w.get('outcome')}",
        )
        # R3b: replay with the now-stale revision used for R3
        sends = count_calls(self.cli.log, "send")
        keys = count_calls(self.cli.log, "send-key")
        r3b = B.submit(self.bid, self.ctl, req, text, rev0)
        sends_after = count_calls(self.cli.log, "send")
        keys_after = count_calls(self.cli.log, "send-key")
        self.rec.record(
            "R3b-replay-original-revision",
            "a duplicate submit with the same request_id and the original (now stale) revision replays "
            "the persisted receipt and sends nothing",
            "real",
            bool(r3b.get("duplicate_call"))
            and r3b.get("status") == r["status"]
            and sends_after == sends
            and keys_after == keys,
            {
                "receipt": r3b,
                "sends_added": sends_after - sends,
                "send_keys_added": keys_after - keys,
                "expected_revision_used": rev0,
            },
        )
        # R3c must NOT run when the R3 completion/drain gate failed (seq185): a transport-vocabulary
        # diagnostic presupposes a completed R3 turn, so dispatching it on an unsatisfied completion
        # gate would let good bytes conceal the completion failure. Record it not-run and stop.
        if not completion_ok:
            self.rec.not_run(
                "R3c-diagnostic-terms",
                "a reply quoting transport vocabulary is observed as output, not a transport failure",
                "R3 completion/drain wait was not satisfied; R3c not dispatched",
            )
            return
        # R3c: a reply that QUOTES transport vocabulary must be observed as normal assistant output,
        # never misclassified as a transport failure. Three-cause fix (seq231/233), all together:
        #  (1) the probe is EXPLICIT benign synthetic/quoted transport-test FIXTURE data, not a bare
        #      status line — a well-aligned model has no reason to refuse clearly-labelled sample
        #      tokens (the original bare probe was refused by the executor as fabricated status);
        #  (2) the four terms are correlated to the child's PUBLIC ASSISTANT output for THIS exact
        #      accepted request, read from the durable transcript's `assistant` records — an ECHO of
        #      the user prompt (always a `user` record), a REFUSAL (assistant text without the terms),
        #      or a STALE earlier turn cannot satisfy it — AND a fresh bounded REAL read-screen must
        #      succeed, exercising the live cmux output/transport path (core.observe returns only
        #      rows[-8:], so the transcript ALONE never proves screen transport, core.py:1262);
        #  (3) an EXPECTED finite `events --no-heartbeat` poll timeout (core._events tolerates it,
        #      core.py:328) is distinguished from a REAL read-screen/send transport error — BOTH are
        #      retained in evidence, and only a real transport error fails the case.
        cur = self.rev()
        log_n = len(self.cli.log)
        terms = ("Access denied", "internal_error", "not_found", "Broken pipe")
        probe = (
            "This is a benign terminal-transport round-trip test. The four items on the last line "
            "are SYNTHETIC TEST FIXTURE STRINGS — quoted sample tokens for a diagnostic, not real "
            "system status, and nothing has failed. Help exercise the transport by replying with "
            "exactly one line that lists the four quoted fixtures separated by ' | ', and nothing "
            "else:\n'Access denied' | 'internal_error' | 'not_found' | 'Broken pipe'"
        )
        r3c = B.submit(self.bid, self.ctl, "r3c-diagnostic", probe, cur)
        w3c = B.wait(
            self.bid, self.ctl, "turn_complete", timeout_s=120, request_id="r3c-diagnostic"
        )
        # Correlate the terms to the child's own PUBLIC assistant reply for THIS delivered prompt
        # (not the echoed user prompt). Poll briefly for the async transcript to flush.
        rec3c = B.store.read(f"requests/{self.bid}/r3c-diagnostic.json") or {}
        dtext3c = rec3c.get("delivered_text")
        dsha3c = rec3c.get("delivered_text_sha256")
        reply3c, found3c, accepts3c, scanned3c = "", False, 0, 0
        for i in range(6):
            recs3c = self._read_transcript_records()
            reply3c, found3c = assistant_reply_after_delivered(recs3c, dtext3c, dsha3c)
            accepts3c = count_delivered_prompts(recs3c, dtext3c, dsha3c)
            scanned3c = len(recs3c)
            if found3c and all(t in reply3c for t in terms):
                break
            if i < 5:
                time.sleep(0.5)
        terms_in_reply = found3c and all(t in reply3c for t in terms)
        # Fresh bounded REAL read-screen — exercises the live cmux output/transport path. A failed
        # read surfaces as state 'unknown' with reason 'read failed: ...' (core.observe), so a live
        # transport is required, not merely a completed transcript.
        o3c = B.observe(self.bid, self.ctl, lines=30)
        read_failed = str(o3c.get("reason") or "").startswith("read failed")
        screen_read_ok = o3c["state"] != "unknown" and not read_failed
        idle = o3c["state"] in ("prompt_idle", "idle")
        # Cause 3: split CLI errors during this case into EXPECTED events-poll timeouts (tolerated by
        # core._events) vs REAL transport errors on read-screen/send/etc. Retain BOTH in evidence.
        expected_poll_timeouts, transport_errors = split_transport_errors(
            self.cli.log[log_n:]
        )
        ok3c = (
            r3c["status"] == "accepted"
            and w3c.get("outcome") == "satisfied"
            and accepts3c == 1
            and terms_in_reply
            and screen_read_ok
            and idle
            and not transport_errors
        )
        self.rec.record(
            "R3c-diagnostic-terms",
            "a reply quoting transport vocabulary (Access denied / internal_error / not_found / Broken "
            "pipe) is observed as normal assistant output, not classified as a transport failure: the "
            "four terms are correlated to the child's PUBLIC assistant reply for the exact accepted "
            "request (echo/refusal/stale cannot pass), a fresh REAL read-screen exercises live "
            "transport, and expected finite events-poll timeouts are distinguished from real "
            "transport errors",
            "real",
            ok3c,
            {
                "submit": r3c,
                "wait": w3c,
                "delivered_prompt_count": accepts3c,
                "assistant_reply_found": found3c,
                "terms_in_assistant_reply": terms_in_reply,
                "assistant_reply": reply3c[:400],
                "transcript_records_scanned": scanned3c,
                "state": o3c["state"],
                "read_screen_ok": screen_read_ok,
                "read_screen_reason": o3c.get("reason"),
                "screen_tail": o3c.get("screen_tail"),
                "expected_events_poll_timeouts": expected_poll_timeouts,
                "real_transport_errors": transport_errors,
            },
            note=""
            if ok3c
            else (
                "assistant reply did not carry all four quoted fixtures (refusal/echo/stale)"
                if not terms_in_reply
                else "real read-screen/transport error"
                if not screen_read_ok or transport_errors
                else f"accepts={accepts3c} turn={w3c.get('outcome')} state={o3c['state']}"
            ),
        )
        if not ok3c:
            self.fixture(
                "R3c-diagnostic-terms",
                {"observe": o3c, "assistant_reply": reply3c[:400]},
            )

    def case_r4e(self, B, C):
        """Two processes bind the same surface as writers at the same instant."""
        script = self.out / "race_bind.py"
        script.write_text(RACE_SCRIPT)
        barrier = self.out / "race-barrier"
        with contextlib.suppress(OSError):
            barrier.unlink()
        pre_release = B.release(self.bid, self.ctl)  # the race needs the writer lease free
        procs, cfgs = [], []
        for ctl in ("ctrl-X", "ctrl-Y"):
            outp = self.out / f"race-{ctl}.json"
            with contextlib.suppress(OSError):
                outp.unlink()
            cfgs.append({"ctl": ctl, "out": outp})
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(script),
                        str(self.state_dir),
                        str(BRIDGE_DIR),
                        self.ws,
                        self.surf,
                        self.session_id,
                        str(self.pid),
                        str(self.scratch),
                        ctl,
                        str(barrier),
                        str(outp),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        time.sleep(1.0)
        barrier.write_text("go")
        results = []
        for p, c in zip(procs, cfgs):
            try:
                p.wait(timeout=180)
            except subprocess.TimeoutExpired:
                p.kill()
            if c["out"].exists():
                results.append(json.loads(c["out"].read_text()))
            else:
                results.append(
                    {"ok": False, "controller": c["ctl"], "code": "no_output"}
                )
        winners = [r for r in results if r.get("ok")]
        losers = [r for r in results if not r.get("ok")]
        ok = len(winners) == 1 and [r.get("code") for r in losers] == ["lease_conflict"]
        for w in winners:
            with contextlib.suppress(BridgeError, Exception):
                B.release(w["binding_id"], w["controller"])
        nb = B.bind(self.ws, self.surf, self.session_id, self.pid, str(self.scratch), C)
        self.bid = nb["binding_id"]
        self.rec.record(
            "R4e-simultaneous-writers",
            "two controllers binding the same surface at the same instant: exactly one writer lease is "
            "granted, the other is refused lease_conflict",
            "real",
            ok,
            {
                "results": results,
                "pre_release": pre_release,
                "rebound_binding": self.bid,
            },
        )

    def case_r8b(self, B):
        cur = self.rev()
        rid = "r8b-observe"
        r = B.submit(self.bid, self.ctl, rid, "Reply with the single word DONE-R8B.", cur)
        w_idle = B.wait(self.bid, self.ctl, "idle", timeout_s=90, request_id=rid)
        bpath = self.state_dir / "bindings" / f"{self.bid}.json"
        cursor_before = json.loads(bpath.read_text())["cursor_seq"]
        o = B.observe(self.bid, self.ctl, lines=20)
        cursor_after = json.loads(bpath.read_text())["cursor_seq"]
        w_turn = B.wait(self.bid, self.ctl, "turn_complete", timeout_s=90, request_id=rid)
        ev = w_turn.get("evidence") or {}
        stop_evidence = ("stop_seq" in ev) or ("transcript_turn_end_at" in ev)
        ok = (
            r["status"] == "accepted"
            and cursor_after == cursor_before
            and o.get("after_seq") == cursor_before
            and w_turn["outcome"] == "satisfied"
            and stop_evidence
        )
        self.rec.record(
            "R8b-observe-then-wait",
            "observe consumes nothing (the binding cursor is caller-owned), so a later wait for the same "
            "request still finds its turn-completion evidence",
            "real",
            ok,
            {
                "submit": r,
                "wait_idle": w_idle,
                "binding_cursor_before": cursor_before,
                "binding_cursor_after": cursor_after,
                "observe": {
                    k: o.get(k)
                    for k in ("state", "after_seq", "next_after_seq", "events")
                },
                "wait_turn_complete": w_turn,
            },
        )
        if not ok:
            self.fixture("R8b-observe-then-wait", {"wait": w_turn})

    def case_r6(self, B):
        """Busy-refusal against a GENUINE running turn (seq239/242/243/246).

        The host rejects a bare `sleep`, so the old fixture never made the executor busy and r6b was
        accepted against an idle session. Instead the executor runs a BOUNDED foreground finite stream
        copy — `head -c N <owned-fifo> > <owned-out>` (allowlisted, inside its scratch): `head -c N`
        returns as soon as N bytes arrive, so the copy is bounded by byte count, not a timer. The
        controller holds the FIFO open O_RDWR|O_NONBLOCK via `r6_stream_fixture`, so it is a durable
        reader+writer: the release write never blocks, buffered bytes survive a late `head`, and the
        fixture's `finally` writes the finite payload, unlinks BOTH the FIFO and the out file, and
        closes the fd on EVERY path — including an observe/submit/expect/wait exception — so no `head`
        blocks past cleanup and no FIFO is left behind (seq246 closed the earlier gap where the wait and
        both unlinks sat outside `finally`). r6b is submitted ONLY once the executor is actually
        running; R6-turn-complete passes only with real running + a released producer + a non-empty
        finite copy, never a satisfied turn from a tool that never ran."""
        cur = self.rev()
        fifo = self.scratch / "r6.fifo"
        outp = self.scratch / "r6.out"
        # EXACT-length finite input so the executor's `head -c N` returns on exactly N bytes.
        payload = (b"r6-finite-stream-fixture-" * 4)[:64]
        head_n = len(payload)
        r6, running, o6 = None, False, {}
        w6, copied = {}, b""
        with r6_stream_fixture(fifo, outp, payload) as stream:
            r6 = B.submit(
                self.bid, self.ctl,
                "r6-busy",
                "Run this one foreground command with the Bash tool (it is pre-approved), exactly as "
                f"written: head -c {head_n} {fifo} > {outp}\nIt copies a short finite stream and returns "
                f"as soon as {head_n} bytes arrive; then reply with the single word DONE-R6.",
                cur,
            )
            # Real wall-clock deadline (not an observe count) to reach a genuinely running turn.
            deadline = time.monotonic() + R6_RUNNING_DEADLINE_S
            while time.monotonic() < deadline:
                o6 = B.observe(self.bid, self.ctl, lines=20)
                if o6.get("state") == "running":
                    running = True
                    break
                time.sleep(R6_POLL_S)
            if running:
                self.expect(
                    "R6-busy",
                    "submit while the executor is running is refused as busy",
                    lambda: B.submit(self.bid, self.ctl, "r6b", "second task", o6["revision"]),
                    "busy",
                    note=f"observed state={o6.get('state')} running_established=True",
                )
                # Release the genuinely-running reader by feeding EXACTLY head's byte count.
                stream.release()
                w6 = B.wait(
                    self.bid, self.ctl, "turn_complete", timeout_s=150, request_id="r6-busy"
                )
                if outp.exists():
                    copied = outp.read_bytes()
            else:
                # Never send r6b against an idle session: the busy precondition was not established,
                # so this is honestly not-run rather than a spurious pass or fail (seq243).
                self.rec.not_run(
                    "R6-busy",
                    "submit while the executor is running is refused as busy",
                    f"executor never reached a running turn within the deadline "
                    f"(last state={o6.get('state')}); r6b not sent",
                )
        # The fixture has now guaranteed the FIFO/out-file cleanup + fd close on every path above.
        ok6 = r6_completion_ok(w6.get("outcome"), running, stream.released, len(copied))
        self.rec.record(
            "R6-turn-complete",
            "wait until=turn_complete returns on the Stop/turn evidence AFTER the executor actually "
            "ran and completed the bounded finite stream copy (not a satisfied turn from a tool that "
            "never ran)",
            "real",
            ok6,
            {
                "submit": r6,
                "observe_during": {k: o6.get(k) for k in ("state", "reason")},
                "running_established": running,
                "released": stream.released,
                "bytes_copied": len(copied),
                "wait": w6,
            },
            note=""
            if ok6
            else f"running={running} released={stream.released} bytes={len(copied)} "
            f"outcome={w6.get('outcome')}",
        )

    def case_r9(self, B):
        cur = self.rev()
        cur0 = self.latest_seq()
        rid = "r9-lost-before"
        armed = {"on": True}

        def before(args):
            if armed["on"] and args[:1] == ["send"]:
                armed["on"] = False
                raise SimulatedFault(
                    "timeout", "injected: request lost before delivery"
                )

        self.cli.fault = FaultInjector(before=before)
        first = "no exception"
        try:
            B.submit(self.bid, self.ctl, rid, "Reply with the single word DONE-R9.", cur)
        except BridgeError as e:
            first = f"BridgeError {e.code}"
        except SimulatedFault as e:
            first = f"SimulatedFault {e.kind}"
        finally:
            self.cli.fault = None
        rec1 = B.store.read(f"requests/{self.bid}/{rid}.json") or {}
        real_sends_before = count_calls(self.cli.log, "send", simulated=False)
        r9 = B.submit(self.bid, self.ctl, rid, "Reply with the single word DONE-R9.", cur)
        real_sends_after = count_calls(self.cli.log, "send", simulated=False)
        ups_raw = self.ups_count(cur0)
        accepts, delivered_text_seen, tr_scanned = self.delivered_prompt_count(B, rid)
        # The property is the substantive one-send / one-delivery evidence (seq239): first send a
        # simulated zero-byte loss, retry accepted, exactly one delivered prompt for this id, exactly
        # one real send added. `reconciled` is an OPTIONAL receipt marker the core's simulated-pre-send
        # branch does not emit, so it is recorded as a diagnostic below, not required by the oracle.
        real_sends_delta = real_sends_after - real_sends_before
        ok = r9_redelivered_exactly_once(first, rec1, r9, accepts, real_sends_delta)
        self.rec.record(
            "R9-lost-before",
            "a request lost BEFORE delivery (simulated pre-send fault proves zero bytes sent) is "
            "re-delivered on the retry and accepted exactly once",
            "real+injected",
            ok,
            {
                "first_call": first,
                "record_after_first": {
                    k: rec1.get(k) for k in ("status", "send_result", "simulated")
                },
                "second_call": r9,
                "delivered_prompt_count": accepts,
                "userpromptsubmit_events_raw": ups_raw,
                "delivered_text_seen": delivered_text_seen,
                "transcript_records_scanned": tr_scanned,
                "real_sends_added_by_retry": real_sends_delta,
                "reconciled_marker": r9.get("reconciled"),
            },
        )
        if not ok:
            self.fixture("R9-lost-before", {"record": rec1, "second": r9})
        B.wait(self.bid, self.ctl, "turn_complete", timeout_s=90, request_id=rid)

    def case_r10(self, B):
        cur = self.rev()
        cur0 = self.latest_seq()
        rid = "r10-lost-after"
        armed = {"on": True}

        def after(args, res):
            if armed["on"] and args[:1] == ["send-key"]:
                armed["on"] = False
                raise SimulatedFault(
                    "timeout", "injected: response lost after delivery"
                )

        self.cli.fault = FaultInjector(after=after)
        first = "no exception"
        try:
            B.submit(self.bid, self.ctl, rid, "Reply with the single word DONE-R10.", cur)
        except BridgeError as e:
            first = f"BridgeError {e.code}"
        except SimulatedFault as e:
            first = f"SimulatedFault {e.kind}"
        finally:
            self.cli.fault = None
        rec1 = B.store.read(f"requests/{self.bid}/{rid}.json") or {}
        keys_before = count_calls(self.cli.log, "send-key")
        sends_before = count_calls(self.cli.log, "send", simulated=False)
        r10 = B.submit(self.bid, self.ctl, rid, "Reply with the single word DONE-R10.", cur)
        keys_after = count_calls(self.cli.log, "send-key")
        sends_after = count_calls(self.cli.log, "send", simulated=False)
        ups_raw = self.ups_count(cur0)
        accepts, delivered_text_seen, tr_scanned = self.delivered_prompt_count(B, rid)
        ok = (
            first.startswith(("SimulatedFault", "BridgeError"))
            and rec1.get("status") == "submitted_unconfirmed"
            and r10.get("status") == "accepted"
            and keys_after == keys_before
            and sends_after == sends_before
            and accepts == 1
        )
        self.rec.record(
            "R10-lost-after",
            "a response lost AFTER the Enter keypress is reconciled from the transcript: acceptance is "
            "confirmed with no second Enter and no second send",
            "real+injected",
            ok,
            {
                "first_call": first,
                "record_after_first": {
                    k: rec1.get(k) for k in ("status", "send_result", "submitted_at")
                },
                "second_call": r10,
                "send_key_calls_added": keys_after - keys_before,
                "send_calls_added": sends_after - sends_before,
                "delivered_prompt_count": accepts,
                "userpromptsubmit_events_raw": ups_raw,
                "delivered_text_seen": delivered_text_seen,
                "transcript_records_scanned": tr_scanned,
            },
        )
        if not ok:
            self.fixture("R10-lost-after", {"record": rec1, "second": r10})
        B.wait(self.bid, self.ctl, "turn_complete", timeout_s=90, request_id=rid)

    @staticmethod
    def _gap_rewrite(args, res):
        """Flip the events ack `gap` flag to true without touching the real transport."""
        if args[:1] != ["events"] or res.returncode != 0:
            return None
        lines = []
        touched = False
        for line in res.stdout.splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                lines.append(line)
                continue
            if j.get("type") == "ack":
                j.setdefault("resume", {})["gap"] = True
                touched = True
            lines.append(json.dumps(j))
        if not touched:
            return None
        return CmuxResult(
            list(args),
            res.returncode,
            "\n".join(lines) + "\n",
            res.stderr,
            res.duration_s,
        )

    def case_r10b(self, B):
        """Text staged, response lost, and then the events stream reports a gap: never a second send."""
        cur = self.rev()
        rid = "r10b-gap"
        text = "Reply with the single word DONE-R10B."
        armed = {"on": True}

        def after(args, res):
            if armed["on"] and args[:1] == ["send"]:
                armed["on"] = False
                raise SimulatedFault(
                    "timeout", "injected: response lost after the text was staged"
                )

        self.cli.fault = FaultInjector(after=after)
        first = "no exception"
        try:
            B.submit(self.bid, self.ctl, rid, text, cur)
        except BridgeError as e:
            first = f"BridgeError {e.code}"
        except SimulatedFault as e:
            first = f"SimulatedFault {e.kind}"
        finally:
            self.cli.fault = None
        rec1 = B.store.read(f"requests/{self.bid}/{rid}.json") or {}
        staged_state = self.state()
        sends_before = count_calls(self.cli.log, "send", simulated=False)
        self.cli.fault = FaultInjector(rewrite=self._gap_rewrite)
        outcome = {}
        try:
            outcome = {"result": B.submit(self.bid, self.ctl, rid, text, cur)}
        except BridgeError as e:
            outcome = {"error_code": e.code, "message": e.message, "detail": e.detail}
        finally:
            self.cli.fault = None
        sends_after = count_calls(self.cli.log, "send", simulated=False)
        no_resend = sends_after == sends_before
        status = (outcome.get("result") or {}).get("status") or outcome.get(
            "error_code"
        )
        allowed = status in (
            "uncertain",
            "staged_unverified",
            "accepted",
            "uncertain_foreign",
        )
        # clear the editor if the reconciliation left text staged
        cleared, how = None, "none needed"
        st = self.state()
        if st["state"] == "staged":
            self.cli.run_ok("send-key", "--surface", self.surf, "ctrl+c")
            how = "send-key ctrl+c"
            time.sleep(1.0)
            st = self.state()
            if st["state"] == "staged":
                self.cli.run_ok("send-key", "--surface", self.surf, "ctrl+c")
                how = "send-key ctrl+c (twice)"
                time.sleep(1.0)
                st = self.state()
            cleared = st["state"] == "prompt_idle"
        elif st["state"] == "running":
            w = B.wait(self.bid, self.ctl, "turn_complete", timeout_s=120, request_id=rid)
            how = f"reconciliation submitted the staged text; waited for turn_complete ({w['outcome']})"
            st = self.state()
            cleared = st["state"] == "prompt_idle"
        else:
            cleared = st["state"] == "prompt_idle"
        ok = no_resend and allowed and bool(cleared)
        self.rec.record(
            "R10b-gap-no-resend",
            "with the payload staged and the events stream reporting a gap, the retry never sends a "
            "second time; the retained uncertainty is reported and the editor is cleared",
            "real+injected",
            ok,
            {
                "first_call": first,
                "record_after_first": {
                    k: rec1.get(k) for k in ("status", "send_result")
                },
                "screen_after_fault": staged_state,
                "retry_outcome": outcome,
                "status": status,
                "real_sends_added": sends_after - sends_before,
                "editor_cleared": cleared,
                "cleared_how": how,
                "screen_after_clear": st,
            },
            note=f"retry status={status}; editor cleared via {how}",
        )
        if not ok:
            self.fixture("R10b-gap-no-resend", {"outcome": outcome, "screen": st})

    def case_r10c(self, B):
        """Payload staged and response lost; a human types something else; the retry must not resend."""
        cur = self.rev()
        rid = "r10c-foreign"
        text = "Reply with the single word DONE-R10C."
        armed = {"on": True}

        def after(args, res):
            if armed["on"] and args[:1] == ["send"]:
                armed["on"] = False
                raise SimulatedFault(
                    "timeout", "injected: response lost after the text was staged"
                )

        self.cli.fault = FaultInjector(after=after)
        first = "no exception"
        try:
            B.submit(self.bid, self.ctl, rid, text, cur)
        except BridgeError as e:
            first = f"BridgeError {e.code}"
        except SimulatedFault as e:
            first = f"SimulatedFault {e.kind}"
        finally:
            self.cli.fault = None
        rec1 = B.store.read(f"requests/{self.bid}/{rid}.json") or {}
        # clear the staged payload, then type a foreign prompt directly with the cmux CLI
        st = self.state()
        clear_how = "none needed"
        if st["state"] == "staged":
            self.cli.run_ok("send-key", "--surface", self.surf, "ctrl+c")
            clear_how = "send-key ctrl+c"
            time.sleep(1.0)
            st = self.state()
        cur0 = self.latest_seq()
        self.cli.run_ok(
            "send", "--surface", self.surf, "--", "Reply with the single word FOREIGN."
        )
        time.sleep(1.0)
        self.cli.run_ok("send-key", "--surface", self.surf, "Enter")
        stop = self.wait_event(
            "agent.hook.Stop",
            lambda e: canonical_session_id(e.get("payload", {}).get("session_id"))
            == self.session_id,
            cur0,
            150,
        )
        sends_before = count_calls(self.cli.log, "send", simulated=False)
        keys_before = count_calls(self.cli.log, "send-key")
        outcome = {}
        try:
            outcome = {"result": B.submit(self.bid, self.ctl, rid, text, cur)}
        except BridgeError as e:
            outcome = {"error_code": e.code, "message": e.message, "detail": e.detail}
        sends_after = count_calls(self.cli.log, "send", simulated=False)
        keys_after = count_calls(self.cli.log, "send-key")
        status = (outcome.get("result") or {}).get("status") or outcome.get(
            "error_code"
        )
        ok = (
            status == "uncertain_foreign"
            and sends_after == sends_before
            and keys_after == keys_before
        )
        self.rec.record(
            "R10c-foreign-input",
            "a foreign user message that landed after the request started makes the retry terminal "
            "uncertain_foreign: never a resend, never an Enter",
            "real+injected",
            ok,
            {
                "first_call": first,
                "record_after_first": {
                    k: rec1.get(k) for k in ("status", "send_result")
                },
                "editor_cleared_how": clear_how,
                "foreign_stop_seq": stop and stop.get("seq"),
                "retry_outcome": outcome,
                "status": status,
                "real_sends_added": sends_after - sends_before,
                "send_keys_added": keys_after - keys_before,
            },
        )
        if not ok:
            self.fixture("R10c-foreign-input", {"outcome": outcome})

    def case_r11(self, B):
        nonce = f"DRAINED-{uuidmod.uuid4().hex[:8]}"
        cp_file = self.scratch / ".bridge" / "checkpoint.md"
        with contextlib.suppress(OSError):
            cp_file.unlink()
        cur = self.rev()
        rid = "r11-checkpoint"
        r = B.submit(
            self.bid, self.ctl,
            rid,
            f"Using the Write tool, create the file {cp_file} (that exact absolute path, not any "
            "other directory) containing a "
            f"single line: {nonce}. Then reply with the single word DONE-R11.",
            cur,
        )
        checkpoint = {"file": str(cp_file), "contains": nonce}
        # Two-phase grounded attestation (seq178): finish the turn, ground drain from the durable
        # transcript + idle prompt, then assert task_complete. The grounded attestation is reused
        # for the R11c compact below so job state is attested once, from real evidence.
        wt = B.wait(self.bid, self.ctl, "turn_complete", timeout_s=210, request_id=rid)
        self._r11_att = self._fixture_drain_attestation(B, rid)
        w = (
            B.wait(
                self.bid, self.ctl,
                "task_complete",
                timeout_s=45,
                request_id=rid,
                evidence={**checkpoint, "drained_attestation": self._r11_att},
            )
            if self._r11_att
            else {"outcome": "unsatisfied_not_drained", "turn_complete": wt}
        )
        ok_a = r["status"] == "accepted" and w.get("outcome") == "satisfied"
        self.rec.record(
            "R11a-checkpoint-task",
            "the drained-checkpoint artifact is written by the executor and proved by task_complete "
            "(file + contains + a grounded drained attestation), not by existence alone",
            "real",
            ok_a,
            {
                "submit": r,
                "turn_complete": wt,
                "wait": w,
                "checkpoint": checkpoint,
                "drained_attestation": self._r11_att,
            },
        )
        if not ok_a:
            self.fixture("R11a-checkpoint-task", {"wait": w})
            self.rec.not_run(
                "R11b-not-due",
                "compact below the due threshold takes no action",
                "no verified checkpoint artifact to compact against",
            )
            self.rec.not_run(
                "R11c-compact",
                "controller-supplied 31% used compacts once",
                "no verified checkpoint",
            )
            self.rec.not_run(
                "R11d-compact-replay",
                "duplicate compact replays the receipt",
                "no verified checkpoint",
            )
            self.rec.not_run(
                "R11e-checkpoint-refusals",
                "compact refuses a missing/malformed checkpoint",
                "no verified checkpoint",
            )
            return
        o = B.observe(self.bid, self.ctl, lines=20)
        pct = o.get("ctx_used_pct")
        cur = o["revision"]
        if pct is not None and pct >= 30.0:
            self.rec.not_run(
                "R11b-not-due",
                "compact below the due threshold takes no action",
                f"session context is already {pct}% used, so not_due cannot be demonstrated without "
                "triggering a real compaction",
            )
        else:
            c1 = B.compact(
                self.bid, self.ctl, "c1", cur, checkpoint=dict(checkpoint), ctx_used_pct=None
            )
            self.rec.record(
                "R11b-not-due",
                "compact with no controller percentage and a screen meter below 30% takes no action",
                "real",
                c1.get("outcome") == "not_due",
                {"compact": c1, "screen_ctx_used_pct": pct},
            )
        cur = self.rev()
        compacts_pre = count_calls(self.cli.log, "send", contains="/compact")
        c_noatt = B.compact(
            self.bid, self.ctl, "c2n", cur, checkpoint=dict(checkpoint), ctx_used_pct=31.0
        )
        compacts_post = count_calls(self.cli.log, "send", contains="/compact")
        self.rec.record(
            "R11c0-needs-attestation",
            "a due compaction with a fresh checkpoint and an idle prompt but NO drained attestation "
            "refuses (needs_drained_attestation) and sends no /compact — the footer agent hint is not "
            "job tracking",
            "real",
            c_noatt.get("outcome") == "needs_drained_attestation"
            and compacts_post == compacts_pre,
            {
                "compact": c_noatt,
                "compact_sends_added": compacts_post - compacts_pre,
            },
        )
        # Reuse the SAME grounded attestation established for R11a task_complete (seq178) rather
        # than a bare asserted one — reaching here implies ok_a held, so self._r11_att is a real,
        # evidence-backed attestation (transcript drain + idle), not "no owned background jobs".
        #
        # Capture the evidence-file append boundary (byte offset) BEFORE this compaction so hooks that
        # fire for THIS c2 are scoped unambiguously by position (seq246) — no seconds-vs-milliseconds
        # timestamp ambiguity, and a PreCompact left by an EARLIER compaction cannot be mistaken for
        # this one. The boundary sits on a completed record's newline (all prior hook processes have
        # exited), so the post-boundary slice starts at a clean `{`.
        ev_boundary = (
            self.evfile.stat().st_size if self.evfile and self.evfile.exists() else 0
        )
        c2 = B.compact(
            self.bid, self.ctl,
            "c2",
            cur,
            checkpoint=dict(checkpoint),
            ctx_used_pct=31.0,
            drained_attestation=self._r11_att,
            timeout_s=240,
        )
        # Recover the executor hook stream with a robust decoder that tolerates a record serialized
        # across newlines and RETAINS parse-failure stats (seq240): the old per-line json.loads
        # silently dropped every two-line record and read a real compaction as kinds=[]. Read ONLY the
        # records appended AFTER the pre-c2 boundary, so PreCompact/SessionStart evidence is scoped to
        # THIS compaction (seq246). Byte-slice then decode: the writer emits ASCII (json.dumps
        # ensure_ascii), so the byte offset is also the char offset and the slice starts at a clean
        # record. Correlate a valid-payload PreCompact AND SessionStart(compact)/session_start_seqs, so
        # malformed-but-nonempty evidence surfaces as a decode failure, never a silent missing dispatch.
        ev_bytes = (
            self.evfile.read_bytes() if self.evfile and self.evfile.exists() else b""
        )
        ev_text = ev_bytes[ev_boundary:].decode("utf-8", "replace")
        kinds, records, hook_parse = parse_hook_evidence(ev_text)
        boundary = (c2.get("evidence") or {}).get("compact_boundary") or {}
        w2 = c2.get("wait") or {}
        starts = ((w2.get("evidence") or {}).get("session_start_seqs")) or []
        # SessionStart also fires at initial STARTUP, so a bare "SessionStart in kinds" could match the
        # startup event, not this compaction (seq243). Correlate to THIS compaction with either the
        # bridge-correlated session_start_seqs (already scoped to after the compact submit) OR a
        # SessionStart hook whose payload.source == 'compact'. PreCompact fires only on compaction.
        sessionstart_compact = any(
            r.get("event") == "SessionStart"
            and (r.get("payload") or {}).get("source") == "compact"
            for r in records
        )
        session_started = bool(starts) or sessionstart_compact
        # Reject a `_raw`/empty/non-native PreCompact as success evidence (seq246): require a PreCompact
        # in THIS compaction's slice whose payload is a non-empty native dict without a `_raw` key.
        precompact_valid = precompact_valid_native(records)
        ok_c = (
            c2.get("outcome") == "completed"
            and bool(boundary.get("compact_boundary_at"))
            and "meter_stale" in c2
            and precompact_valid  # a valid native PreCompact for THIS compaction, not any/_raw
            and session_started
            and hook_parse[
                "recovered_all"
            ]  # fail on unrecovered/invalid evidence, never silent
        )
        self.rec.record(
            "R11c-compact",
            "a controller-supplied 31% used submits /compact once; completion is a transcript "
            "compact_boundary after this request's submit, corroborated by SessionStart, with a stale "
            "footer meter reported as staleness rather than failure",
            "real",
            ok_c,
            {
                "compact": {
                    k: c2.get(k)
                    for k in (
                        "outcome",
                        "policy",
                        "ctx_before",
                        "ctx_after_screen",
                        "meter_stale",
                        "revision",
                        "accepted_at",
                        "evidence",
                    )
                },
                "compact_boundary": boundary,
                "session_start_seqs": starts,
                "session_started": session_started,
                "sessionstart_source_compact": sessionstart_compact,
                "precompact_valid": precompact_valid,
                "evidence_append_boundary": ev_boundary,
                "executor_hook_events": kinds[-12:],
                "hook_parse": hook_parse,
                "wait": w2,
            },
        )
        if not ok_c:
            self.fixture("R11c-compact", {"compact": c2})
        compacts_before = count_calls(self.cli.log, "send", contains="/compact")
        c2b = B.compact(
            self.bid, self.ctl, "c2", cur, checkpoint=dict(checkpoint), ctx_used_pct=31.0
        )
        compacts_after = count_calls(self.cli.log, "send", contains="/compact")
        self.rec.record(
            "R11d-compact-replay",
            "a duplicate compact with the same request_id and the original revision replays the receipt "
            "and never sends a second /compact",
            "real",
            bool(c2b.get("duplicate_call")) and compacts_after == compacts_before,
            {
                "receipt": c2b,
                "compact_sends_added": compacts_after - compacts_before,
                "expected_revision_used": cur,
            },
        )
        # refusals: a checkpoint pointing at a missing file, and a malformed checkpoint
        missing = {
            "file": str(self.scratch / ".bridge" / "no-such-checkpoint.md"),
            "contains": nonce,
        }
        codes = {}
        rev_now = self.rev()
        for name, cp, rid2 in (
            ("missing_file", missing, "c3"),
            ("malformed", {"file": str(cp_file)}, "c4"),
        ):
            try:
                codes[name] = {
                    "outcome": B.compact(
                        self.bid, self.ctl, rid2, rev_now, checkpoint=cp, ctx_used_pct=31.0
                    )
                }
            except BridgeError as e:
                codes[name] = {"code": e.code, "message": e.message}
            rev_now = self.rev()
        ok_e = (
            codes.get("missing_file", {}).get("code")
            in ("checkpoint_stale", "checkpoint_required")
            and codes.get("malformed", {}).get("code") == "checkpoint_required"
        )
        self.rec.record(
            "R11e-checkpoint-refusals",
            "compact refuses a checkpoint whose artifact is missing and a checkpoint with no content "
            "assertion (existence is not evidence)",
            "real",
            ok_e,
            {"outcomes": codes},
            note=f"missing-file code={codes.get('missing_file', {}).get('code')}, "
            f"malformed code={codes.get('malformed', {}).get('code')}",
        )

    def case_r13(self):
        # Mode-aware external-access contract (seq162/164). The expectation is selected from the
        # ACTUAL cmux access mode, not from environment presence: the native cmux CLI auto-loads its
        # own saved password, so removing CMUX_SOCKET_PASSWORD / the wrapper file env does NOT make a
        # CLI call unauthenticated. We read transport.access_mode from an authenticated discover
        # (the harness bridge carries the owner-only password via CMUX_BRIDGE_PASSWORD_FILE) and then
        # probe from an EXTERNAL launchd process (ppid 1, not cmux-descended):
        #   password : the external CLI/bridge AUTHENTICATES and reaches the socket (as the
        #              configured MCP does), AND a RAW unix socket with NO auth operation is rejected
        #              ("authentication required"). The raw arm bypasses the CLI entirely so the
        #              auto-loaded password cannot leak in.
        #   cmuxOnly : the external process is denied access_denied (the retired ancestry contract).
        # The password is never read, printed, or embedded — only the owner-only file PATH is passed
        # through. Listener/auth settings are never changed here.
        disc0 = self.bridge.discover()
        tr0 = disc0.get("transport", {}) or {}
        mode = tr0.get("access_mode")
        socket_path = (
            tr0.get("socket_path")
            or os.environ.get("CMUX_BRIDGE_SOCKET")
            or str(Path.home() / ".local" / "state" / "cmux" / f"cmux-{os.getuid()}.sock")
        )
        pwpath = os.environ.get("CMUX_BRIDGE_PASSWORD_FILE")
        probe_py = self.out / "launchd-probe.py"
        probe = self.out / "launchd-probe.sh"
        outp = self.out / "launchd-probe.json"
        probe_py.write_text(
            "import json, os, socket, sys, tempfile\n"
            f"sys.path.insert(0, {str(BRIDGE_DIR)!r})\n"
            "from cmux_bridge.core import Bridge\n"
            "from cmux_bridge.cmuxcli import CmuxCLI\n"
            "from cmux_bridge.state import StateStore\n"
            f"pwpath = {pwpath!r}\n"
            f"socket_path = {socket_path!r}\n"
            "out = {'ppid': os.getppid(), 'auth_had_password_env': bool(pwpath)}\n"
            # AUTHENTICATED arm: CLI/bridge with the owner-only password (path only; the CLI reads
            # the secret from the file). The CLI would also auto-load the saved password on its own.
            "os.environ.pop('CMUX_SOCKET_PASSWORD', None)\n"
            "if pwpath:\n"
            "    os.environ['CMUX_BRIDGE_PASSWORD_FILE'] = pwpath\n"
            "try:\n"
            "    out['auth'] = Bridge(cli=CmuxCLI(), store=StateStore(tempfile.mkdtemp())).discover()\n"
            "except Exception as e:\n"
            "    out['auth'] = {'error': f'{type(e).__name__}: {e}'}\n"
            # RAW UNAUTHENTICATED arm: a direct AF_UNIX socket, no auth op, no credential. This is the
            # only way to prove unauthenticated rejection, since the CLI auto-authenticates.
            "raw = {'socket': socket_path, 'sent': 'ping\\n'}\n"
            "try:\n"
            "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "    s.settimeout(10)\n"
            "    s.connect(socket_path)\n"
            "    s.sendall(b'ping\\n')\n"
            "    data = s.recv(4096).decode('utf-8', 'replace')\n"
            "    s.close()\n"
            "    raw['response'] = data\n"
            "    low = data.lower()\n"
            "    raw['rejected'] = ('authentication' in low and 'required' in low)\n"
            "except Exception as e:\n"
            "    raw['error'] = f'{type(e).__name__}: {e}'\n"
            "    raw['rejected'] = None\n"
            "out['raw'] = raw\n"
            "print(json.dumps(out))\n"
        )
        probe.write_text(
            f"#!/bin/bash\ncd {BRIDGE_DIR}\nexec {sys.executable} {probe_py} > {outp} 2>&1\n"
        )
        probe.chmod(0o755)
        label = f"com.claude.bridge-auth-probe-{int(time.time())}"
        subprocess.run(
            [
                "launchctl",
                "submit",
                "-l",
                label,
                "-o",
                str(self.out / "launchd-probe.log"),
                "-e",
                str(self.out / "launchd-probe.err"),
                "--",
                str(probe),
            ],
            check=False,
        )
        for _ in range(180):  # launchd may take a while to start the job; 90 s bound
            time.sleep(0.5)
            if outp.exists() and outp.stat().st_size > 0:
                break
        subprocess.run(["launchctl", "remove", label], check=False)
        try:
            pj = json.loads(outp.read_text())
            external = pj.get("ppid") == 1
            auth = pj.get("auth") or {}
            auth_tr = auth.get("transport", {}) if isinstance(auth, dict) else {}
            auth_ok = auth_tr.get("ok") is True
            auth_denied = (
                auth_tr.get("ok") is False
                and auth_tr.get("error_kind") == "access_denied"
            )
            raw = pj.get("raw", {}) or {}
            raw_rejected = raw.get("rejected")  # True / False / None(inconclusive)
            evidence = {"access_mode": mode, "probe": pj}
            if mode == "password":
                if raw_rejected is None:
                    self.rec.not_run(
                        "R13-auth-external",
                        "password-mode external auth probe",
                        "raw unauthenticated socket probe inconclusive "
                        f"(error={raw.get('error')!r}, response={raw.get('response')!r}); "
                        f"external ppid1={external}, auth_ok={auth_ok}",
                    )
                else:
                    self.rec.record(
                        "R13-auth-external",
                        "password socketControlMode: an external (launchd ppid1, non cmux-descended) "
                        "process authenticates and reaches the socket with the owner-only password, "
                        "and a raw unix socket with no auth is rejected (authentication required)",
                        "real",
                        external and auth_ok and (raw_rejected is True),
                        evidence,
                    )
            elif mode == "cmuxOnly":
                self.rec.record(
                    "R13-auth-external",
                    "cmuxOnly socketControlMode: an external (launchd ppid1) process is denied "
                    "access_denied and told the exact requirement",
                    "real",
                    external and auth_denied,
                    evidence,
                )
            else:
                self.rec.not_run(
                    "R13-auth-external",
                    "external auth probe",
                    f"unexpected/undetected access_mode={mode!r}; refusing to assert "
                    f"(external ppid1={external}, auth_ok={auth_ok}, raw_rejected={raw_rejected})",
                )
        except Exception as e:
            self.rec.not_run(
                "R13-auth-external",
                "external auth probe",
                f"{type(e).__name__}: {e}; raw={outp.read_text()[:300] if outp.exists() else None!r}",
            )
        bad = Bridge(
            cli=CmuxCLI(socket="/tmp/definitely-missing.sock", timeout_s=10),
            store=StateStore(self.state_dir),
        )
        d2 = bad.discover()
        self.rec.record(
            "R13b-missing-socket",
            "a missing socket path is classified socket_missing, not a generic error",
            "real",
            d2["transport"].get("ok") is False
            and d2["transport"].get("error_kind") == "socket_missing",
            d2["transport"],
        )

    # ------------------------------------------------------------- teardown / output
    def teardown(self):
        try:
            if not self.session_id or not self.surf:
                raise RuntimeError("no disposable session to exit")
            _, latest = self.bridge._latest()
            # NEVER press Enter onto anything but OUR OWN single-line `/exit` staged on a proved-idle
            # prompt. On a permission modal Enter selects the default and APPROVES the pending tool
            # call, so a blind /exit+Enter would authorize an operation this fixture must never
            # approve (preflight2: teardown's Enter cleared a stuck Write modal). Two windows are
            # guarded:
            #   (a) reaching idle: ctrl+c clears staged text and interrupts a modal or a running turn
            #       (a decline, the opposite of Enter), then we re-check;
            #   (b) the stage->Enter race (codex-fixture-stage-guard-review-r3): a modal or a foreign
            #       draft can appear AFTER we send `/exit` and BEFORE Enter. So immediately before
            #       Enter we read a FRESH screen and require staged_is_ours("/exit", fresh) — Enter
            #       fires only when our own single-line /exit is the staged text, never on a modal
            #       (screen_not_staged), a foreign single line, or an ambiguous continuation row. If
            #       the guard refuses, ctrl+c declines whatever is on screen (never Enter) and R16
            #       records an honest SessionEnd-absent failure. The finally-block force-closes the
            #       surface either way (a cmux close approves nothing).
            pre_state = self.state()["state"]
            if pre_state != "prompt_idle":
                self.cli.run_ok("send-key", "--surface", self.surf, "ctrl+c")
                time.sleep(0.7)
                pre_state = self.state()["state"]
            exited = None
            staged_ok = None
            guard = None
            pre_enter_state = None
            if pre_state == "prompt_idle":
                self.cli.run_ok("send", "--surface", self.surf, "--", "/exit")
                time.sleep(0.5)
                fresh = self.state()  # fresh screen read immediately before Enter
                pre_enter_state = fresh["state"]
                staged_ok, guard = staged_is_ours("/exit", fresh)
                if staged_ok:
                    self.cli.run_ok("send-key", "--surface", self.surf, "Enter")
                    exited = self.wait_event(
                        "agent.hook.SessionEnd",
                        lambda e: (
                            canonical_session_id(e.get("payload", {}).get("session_id"))
                            == self.session_id
                        ),
                        latest,
                        45,
                    )
                else:
                    # a modal appeared after the stage, or a foreign draft landed: decline with
                    # ctrl+c and never press Enter onto it.
                    self.cli.run_ok("send-key", "--surface", self.surf, "ctrl+c")
                    time.sleep(0.5)
            self.rec.record(
                "R16-teardown",
                "the disposable session exited (SessionEnd) only after a fresh pre-Enter screen "
                "proved our own single-line /exit staged (staged_is_ours); Enter is never sent onto "
                "a modal (whose default Enter would approve the pending operation) or a foreign "
                "draft, and only run-created surfaces were closed",
                "real",
                bool(exited),
                {
                    "session_end_seq": exited and exited["seq"],
                    "pre_exit_state": pre_state,
                    "pre_enter_state": pre_enter_state,
                    "staged_is_ours": staged_ok,
                    "staged_guard": guard,
                    "owned_surfaces": self.owned.uuids,
                },
            )
        except Exception as e:
            self.rec.record(
                "R16-teardown",
                f"teardown problem: {type(e).__name__}: {e}",
                "real",
                False,
                {"owned_surfaces": self.owned.uuids},
            )
        finally:
            for s in list(self.owned.uuids):
                if s not in self.owned.closed:
                    self.close_owned(s)
            self.write_outputs()

    def write_outputs(self):
        for cid in CASE_IDS:
            if not self.rec.has(cid):
                self.rec.not_run(
                    cid, "case not reached", "the run stopped before this case"
                )
        (self.out / "cli-log.json").write_text(json.dumps(self.cli.log, indent=1))
        if self.evfile and Path(self.evfile).exists():
            shutil.copy(self.evfile, self.out / "executor-evidence.jsonl")
        for f in ("received.txt",):
            if (self.scratch / f).exists():
                shutil.copy(self.scratch / f, self.out / f)
        tpath = Path(self.meta.get("transcript_path") or "/nonexistent")
        if tpath.is_file():
            lines = tpath.read_text(errors="replace").splitlines()[-200:]
            (self.out / "transcript-tail.jsonl").write_text("\n".join(lines) + "\n")
        state_copy = self.out / "state"
        if self.state_dir.resolve() != state_copy.resolve() and self.state_dir.exists():
            shutil.rmtree(state_copy, ignore_errors=True)
            shutil.copytree(self.state_dir, state_copy)
        focus_after = None
        surfaces_left = None
        with contextlib.suppress(Exception):
            focus_after = json.loads(self.cli.run_ok("identify", "--json")).get(
                "focused"
            )
        with contextlib.suppress(Exception):
            surfaces_left = sorted(self.bridge._surfaces(self.ws))
        summary = {
            **self.meta,
            "finished_at": utcnow(),
            "surface": self.surf,
            "claude_pid": self.pid,
            "claude_session_id": self.session_id,
            "scratch": str(self.scratch),
            "focus_before": self.focus_before,
            "focus_after": focus_after,
            "owned_surfaces": self.owned.uuids,
            "owned_surfaces_closed": self.owned.closed,
            "unresolved_surface_refs": self.owned.unresolved,
            "surfaces_left_in_workspace": surfaces_left,
            "transcript_exists": self.meta.get("transcript_exists"),
            "passed": sum(1 for c in self.rec.cases if c["pass"]),
            "total": len(self.rec.cases),
            "failed": self.rec.failed(),
            "cases": self.rec.cases,
        }
        (self.out / "summary.json").write_text(
            json.dumps(summary, indent=2, default=str)
        )
        print("\n--- case summary ---", flush=True)
        for c in self.rec.cases:
            print(
                f"{'PASS' if c['pass'] else 'FAIL'}  {c['id']:<30} {c['label'][:90]}"
                + (f"  [{c['note'][:80]}]" if c["note"] else ""),
                flush=True,
            )
        print(
            json.dumps(
                {
                    k: summary[k]
                    for k in (
                        "passed",
                        "total",
                        "failed",
                        "focus_after",
                        "owned_surfaces",
                        "unresolved_surface_refs",
                    )
                }
            ),
            flush=True,
        )


def main():
    ap = argparse.ArgumentParser(
        description="live acceptance for the hardened cmux bridge"
    )
    ap.add_argument(
        "--workspace",
        required=True,
        help="cmux workspace UUID that will host the disposable surface",
    )
    ap.add_argument(
        "--scratch",
        required=True,
        help="disposable scratch worktree (created if absent)",
    )
    ap.add_argument(
        "--out", required=True, help="output directory for receipts and fixtures"
    )
    ap.add_argument(
        "--model",
        default=os.environ.get("CMUX_BRIDGE_ACCEPTANCE_MODEL", "sonnet"),
        help="model alias/id for the disposable Claude session (default: sonnet)",
    )
    ap.add_argument(
        "--state-dir",
        default=None,
        help="private bridge state dir (default <out>/state)",
    )
    a = ap.parse_args()
    if not UUID_RE.fullmatch(a.workspace or ""):
        sys.exit(f"--workspace must be a UUID, got {a.workspace!r}")
    porcelain = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    # untracked evidence under checks/ (this run's own --out, earlier receipts) is expected and is
    # not source; any modified tracked file or any other untracked path still means "not frozen".
    dirty = "\n".join(
        l for l in porcelain if l.strip() and not l.startswith("?? checks/")
    )
    if dirty and os.environ.get("ALLOW_DIRTY") != "1":
        sys.exit(
            f"refusing to run: the source under test is not frozen (git status --porcelain):\n{dirty}\n"
            "commit or stash first, or set ALLOW_DIRTY=1 deliberately"
        )
    r = Runner(a)
    try:
        r.preflight()
        r.setup()
        r.run()
    except Exception as e:
        r.rec.record(
            "RX-exception",
            f"runner exception: {type(e).__name__}: {e}",
            "real",
            False,
            {"traceback": traceback.format_exc()[-3000:]},
        )
    finally:
        r.teardown()
    failed = r.rec.failed()
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
