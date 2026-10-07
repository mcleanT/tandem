"""SIMULATED transport tests for the tandem bridge (kind: simulated).

Everything here runs against `FakeCLI` (an in-process stand-in that models cmux's argument arrays,
screens, event frames and the durable Claude Code transcript) and a `StateStore` rooted in a pytest
`tmp_path`. The real `cmux` binary is never executed, no real socket is contacted, the real state
directory and the real ~/.claude/projects are never touched, and `python -m cmux_bridge` is never
started. Waits use a fake clock, so nothing sleeps.

These tests therefore prove the bridge's *logic* — classification, identity gates, transactional
leases, replayable receipts, payload identity, gap handling, honest states, idempotent compaction,
caller-owned cursors, bounds and the MCP surface. They are NOT evidence about the real runtime:
real-runtime evidence lives in `checks/bridge-*/` and in `tests/fixtures/` (verbatim captured rows,
see fixtures/PROVENANCE.txt). Faults injected through `FaultInjector` are labelled `simulated=True`
in the CLI call log precisely so the two can never be confused.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cmux_bridge import core
from cmux_bridge.cmuxcli import (
    CmuxCLI,
    CmuxResult,
    FaultInjector,
    SimulatedFault,
    classify,
)
from cmux_bridge.core import (
    Bridge,
    BridgeError,
    classify_screen,
    clamp_timeout,
    queued_is_ours,
    staged_is_ours,
)
from cmux_bridge.state import StateError, StateStore

from tests.fakecli import (
    SEP,
    FakeCLI,
    FakeClock,
    claude_screen,
    empty_screen,
    modal_screen,
    pasted_screen,
    zsh_screen,
)

BRIDGE_ROOT = str(Path(__file__).resolve().parents[1])
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A bound-able world: one Claude surface, a fake clock, private state + transcript under tmp."""
    monkeypatch.setenv("CMUX_BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CMUX_BRIDGE_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.delenv("CMUX_BRIDGE_PASSWORD_FILE", raising=False)
    wt = tmp_path / "wt"
    wt.mkdir()
    cwd = os.path.realpath(str(wt))
    clk = FakeClock()
    cli = FakeCLI(clock=clk, cwd=cwd, transcript_root=tmp_path / "projects")
    surf = cli.add_claude_surface()
    store = StateStore(tmp_path / "state")
    bridge = Bridge(cli=cli, store=store, clock=clk, sleep=clk.sleep)
    holder = {
        "proc_start": "Tue Sep  8 11:47:38 2026",
        # the executor's own command line as `ps -o command=` prints it: an explicit --model
        "argv": "claude --dangerously-skip-permissions --model opus --session-id 00000000-0000-4000-8000-000000000000",
    }
    monkeypatch.setattr(
        core.Bridge, "_proc_start", staticmethod(lambda pid: holder["proc_start"])
    )
    monkeypatch.setattr(core.Bridge, "_proc_argv", staticmethod(lambda pid: holder["argv"]))
    return SimpleNamespace(
        cli=cli,
        clk=clk,
        store=store,
        bridge=bridge,
        surf=surf,
        cwd=cwd,
        tmp_path=tmp_path,
        root=tmp_path / "state",
        proc=holder,
    )


def bind_writer(env, controller="ctl-A", role="writer", surface=None, **kw):
    s = env.cli.surfaces[surface or env.surf]
    claude = env.cli.surfaces[env.surf]
    return env.bridge.bind(
        env.cli.workspace,
        s["uuid"],
        claude["session_id"],
        s["pid"] or claude["pid"],
        env.cwd,
        controller,
        role,
        **kw,
    )


def artifact(env, name: str, text: str) -> dict:
    """Write an executor artifact and stamp its mtime with the FAKE clock (the bridge compares it
    against fake-clock request timestamps)."""
    p = env.tmp_path / name
    p.write_text(text)
    os.utime(p, (env.clk(), env.clk()))
    return {
        "file": str(p),
        "contains": text.strip().splitlines()[0] if text.strip() else "",
    }


def rewrite_ping(stderr: str, rc: int = 1):
    def _rw(args, res):
        if args and args[0] == "ping":
            alt = CmuxResult(list(args), rc, "", stderr, 0.001)
            alt.error_kind = classify(rc, "", stderr)
            return alt
        return None

    return _rw


# ------------------------------------------------- item 1: classify only classifies failures


REAL_FAILURES = [
    ("Error: not_found: Workspace not found\n", "not_found"),
    ("Error: not_found: Pane or workspace not found\n", "not_found"),
    ("Error: internal_error: Failed to read terminal text\n", "no_terminal_view"),
    ("Error: Failed to write to socket (Broken pipe, errno 32)\n", "access_denied"),
    ("Error: Socket not found at /run/cmux.sock\n", "socket_missing"),
    ("Error: Connection refused (errno 61)\n", "connection_refused"),
    ("Error: something nobody has seen before\n", "other"),
]


@pytest.mark.parametrize("stderr,kind", REAL_FAILURES)
def test_classify_real_failure_strings(stderr, kind):
    assert classify(1, "", stderr) == kind


@pytest.mark.parametrize("stderr,_kind", REAL_FAILURES)
def test_classify_same_strings_on_rc_zero_are_not_errors(stderr, _kind):
    """The positive neighbour: identical text, exit 0 -> not a transport failure."""
    assert classify(0, stderr, "") is None
    assert classify(0, "", stderr) is None


def test_classify_successful_stdout_quoting_diagnostics_is_none():
    out = "The program printed Access denied during its test; see internal_error and not_found: x"
    assert classify(0, out, "") is None


def test_classify_prefers_stderr_kind_over_stdout_kind():
    assert (
        classify(1, "not_found: nothing here", "Error: Connection refused (errno 61)")
        == "connection_refused"
    )


def test_observe_claude_reply_quoting_diagnostics_is_normal_state_simulated(env):
    b = bind_writer(env)
    rows = [
        "  Claude: the log said 'Access denied' and 'internal_error'; grep found not_found: x"
    ] + claude_screen()
    env.cli.set_screen(env.surf, rows)
    o = env.bridge.observe(b["binding_id"], b["controller_id"])
    assert o["state"] == "prompt_idle"
    assert "error_kind" not in o.get("reason", "")
    assert all(e.get("error_kind") in (None, "timeout") for e in env.cli.log)


def test_classify_read_screen_without_terminal_view_is_distinct_from_internal_error():
    err = "Error: internal_error: Failed to read terminal text"
    assert classify(1, "", err) == "no_terminal_view"
    assert classify(1, "", "Error: internal_error: boom") == "internal_error"


def test_no_terminal_view_is_a_refusal_not_a_transport_fault_simulated(env):
    b = bind_writer(env)
    env.cli.set_no_terminal_view(env.surf)
    sends = env.cli.send_count
    o = env.bridge.observe(b["binding_id"], b["controller_id"])
    assert o["state"] == "unknown" and o["error_kind"] == "no_terminal_view"
    assert "cmux select-workspace" in o["instruction"]
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-nv", "hello", b["revision"])
    assert ei.value.code == "no_terminal_view"
    assert env.cli.send_count == sends  # nothing was typed, nothing queued for resend
    env.cli.set_no_terminal_view(env.surf, False)
    assert env.bridge.observe(b["binding_id"], b["controller_id"])["state"] == "prompt_idle"


def test_bind_refuses_a_surface_with_no_terminal_view_simulated(env):
    env.cli.set_no_terminal_view(env.surf)
    with pytest.raises(BridgeError) as ei:
        bind_writer(env)
    assert ei.value.code == "no_terminal_view"


# ------------------------------------------------- item 2: state store safety + locking


def test_state_store_refuses_symlinked_managed_subdir_and_mutates_nothing(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o755)
    root = tmp_path / "state"
    root.mkdir()
    (root / "receipts").symlink_to(outside)
    with pytest.raises(StateError) as ei:
        StateStore(root)
    assert ei.value.code == "unsafe_state_path"
    assert (os.stat(outside).st_mode & 0o777) == 0o755
    assert list(outside.iterdir()) == []


def test_state_store_refuses_symlinked_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o755)
    link = tmp_path / "linked-state"
    link.symlink_to(outside)
    with pytest.raises(StateError) as ei:
        StateStore(link)
    assert ei.value.code == "unsafe_state_path"
    assert (os.stat(outside).st_mode & 0o777) == 0o755
    assert list(outside.iterdir()) == []


def test_append_receipt_refuses_symlinked_target_and_writes_nothing_outside(tmp_path):
    store = StateStore(tmp_path / "state")
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.root / "receipts" / "receipts.jsonl").symlink_to(outside / "receipts.jsonl")
    with pytest.raises(StateError) as ei:
        store.append_receipt("bind", {"x": 1})
    assert ei.value.code == "unsafe_state_path"
    assert not (outside / "receipts.jsonl").exists()


def test_state_path_rejects_escape_and_absolute(tmp_path):
    store = StateStore(tmp_path / "state")
    for bad in ("../evil.json", "/etc/passwd", "leases/../../evil.json"):
        with pytest.raises(StateError):
            store.path(bad)


def test_lock_is_reentrant_and_times_out_across_processes(tmp_path):
    store = StateStore(tmp_path / "state")
    with store.lock():
        with store.lock():  # re-entrant in-process
            store.write("bindings/x.json", {"a": 1})
    assert store.read("bindings/x.json") == {"a": 1}
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time;sys.path.insert(0,%r);"
                "from cmux_bridge.state import StateStore;"
                "s=StateStore(%r);\n"
                "import contextlib\n"
                "with s.lock('state', 5.0):\n"
                "    print('held', flush=True); time.sleep(3)\n"
                % (BRIDGE_ROOT, str(tmp_path / "state"))
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    try:
        with pytest.raises(StateError) as ei:
            with store.lock("state", 0.2):
                pass
        assert ei.value.code == "lock_timeout"
    finally:
        holder.kill()
        holder.wait()


# ------------------------------------------------- item 3: transactional leases


LEASE_RACE_CHILD = """
import json, os, sys, time
cfg = json.load(open(sys.argv[1]))
sys.path.insert(0, cfg["bridge_root"])
os.environ["CMUX_BRIDGE_CLAUDE_PROJECTS"] = cfg["projects"]
from cmux_bridge.core import Bridge, BridgeError
from cmux_bridge.state import StateStore
from tests.fakecli import FakeCLI, FakeClock
clk = FakeClock()
cli = FakeCLI(clock=clk, cwd=cfg["cwd"], transcript_root=cfg["projects"])
cli.workspace = cfg["workspace"]
cli.add_claude_surface(uuid=cfg["surface"], session_id=cfg["session_id"], pid=cfg["pid"])
br = Bridge(cli=cli, store=StateStore(cfg["state"]), clock=clk, sleep=clk.sleep)
Bridge._proc_start = staticmethod(lambda pid: "Tue Sep  8 11:47:38 2026")
open(cfg["ready"], "w").write("1")
while not os.path.exists(cfg["barrier"]):
    time.sleep(0.002)
try:
    b = br.bind(cfg["workspace"], cfg["surface"], cfg["session_id"], cfg["pid"], cfg["cwd"], cfg["controller"], "writer")
    json.dump({"ok": True, "binding_id": b["binding_id"]}, open(cfg["out"], "w"))
except BridgeError as e:
    json.dump({"ok": False, "code": e.code}, open(cfg["out"], "w"))
"""


def test_two_processes_racing_for_the_writer_lease_simulated(env, tmp_path):
    """Two real subprocesses, one shared state dir, a filesystem barrier: exactly one writer."""
    script = tmp_path / "race_child.py"
    script.write_text(LEASE_RACE_CHILD)
    barrier = tmp_path / "barrier"
    common = {
        "bridge_root": BRIDGE_ROOT,
        "state": str(env.root),
        "cwd": env.cwd,
        "projects": str(tmp_path / "projects"),
        "workspace": env.cli.workspace,
        "surface": env.surf,
        "session_id": env.cli.surfaces[env.surf]["session_id"],
        "pid": os.getpid(),
        "barrier": str(barrier),
    }
    procs, cfgs = [], []
    for i, ctl in enumerate(("ctl-A", "ctl-B")):
        cfg = {
            **common,
            "controller": ctl,
            "out": str(tmp_path / f"out{i}.json"),
            "ready": str(tmp_path / f"ready{i}"),
        }
        cp = tmp_path / f"cfg{i}.json"
        cp.write_text(json.dumps(cfg))
        cfgs.append(cfg)
        procs.append(subprocess.Popen([sys.executable, str(script), str(cp)]))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not all(
        os.path.exists(c["ready"]) for c in cfgs
    ):
        time.sleep(0.01)
    barrier.write_text("go")
    for p in procs:
        p.wait(timeout=60)
    results = [json.loads(Path(c["out"]).read_text()) for c in cfgs]
    assert sum(1 for r in results if r["ok"]) == 1, results
    assert [r["code"] for r in results if not r["ok"]] == ["lease_conflict"], results


def test_rebind_supersedes_the_old_binding_which_then_loses_the_lease(env):
    old = bind_writer(env, "ctl-A")
    new = bind_writer(env, "ctl-A")
    assert new["binding_id"] != old["binding_id"]
    assert old["binding_id"] in new["superseded_bindings"]
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(old["binding_id"], old["controller_id"], "r-old", "hello", 1)
    assert ei.value.code == "lease_lost"
    assert env.cli.send_count == 0
    assert (
        env.bridge.submit(new["binding_id"], new["controller_id"], "r-new", "hello", 1)["status"]
        == "accepted"
    )


def test_monitor_coexists_with_another_controllers_writer(env):
    w = bind_writer(env, "ctl-A", "writer")
    m = bind_writer(env, "ctl-B", "monitor")
    assert env.bridge.observe(m["binding_id"], m["controller_id"])["state"] == "prompt_idle"
    assert env.bridge.observe(w["binding_id"], w["controller_id"])["state"] == "prompt_idle"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(m["binding_id"], m["controller_id"], "r-m", "hello", 1)
    assert ei.value.code == "not_writer"


def test_second_writer_with_different_controller_is_lease_conflict(env):
    bind_writer(env, "ctl-A")
    with pytest.raises(BridgeError) as ei:
        bind_writer(env, "ctl-B")
    assert ei.value.code == "lease_conflict"


def test_release_marks_released_and_removes_lease_files(env):
    b = bind_writer(env)
    out = env.bridge.release(b["binding_id"], b["controller_id"])
    assert out["released"] is True and len(out["leases_removed"]) == 2
    assert not list((env.root / "leases").glob("*.json"))
    with pytest.raises(BridgeError) as ei:
        env.bridge.observe(b["binding_id"], b["controller_id"])
    assert ei.value.code == "released"


def test_lease_expiry_via_clock_advance(env):
    b = bind_writer(env, lease_ttl_s=60)
    env.clk.advance(61)
    with pytest.raises(BridgeError) as ei:
        env.bridge.observe(b["binding_id"], b["controller_id"])
    assert ei.value.code == "lease_expired"


def test_release_during_staging_blocks_the_enter_that_follows(env, monkeypatch):
    """item 2 (RED before the fix): the reservation-time preflight already catches a release that
    lands BEFORE it, but a release() landing right AFTER the payload is staged (screen verified as
    ours) and right BEFORE Enter must be caught too -- not just the earlier preflight."""
    b = bind_writer(env)
    orig_staged_is_ours = core.staged_is_ours
    state = {"done": False}

    def staged_is_ours_then_release(text, screen):
        ok, why = orig_staged_is_ours(text, screen)
        if ok and not state["done"]:
            state["done"] = True
            out = env.bridge.release(b["binding_id"], b["controller_id"])
            assert out["released"] is True
        return ok, why

    monkeypatch.setattr(core, "staged_is_ours", staged_is_ours_then_release)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-rel", "do the thing", 1)
    assert ei.value.code == "lease_lost"
    assert env.cli.send_count == 1 and env.cli.enter_count == 0
    rec = env.store.read(f"requests/{b['binding_id']}/r-rel.json")
    assert rec["status"] == "lease_lost"


def test_rebind_during_staging_blocks_the_enter_that_follows(env, monkeypatch):
    """item 2 (RED before the fix): a same-controller rebind that supersedes this binding right
    after staging (but before Enter) must block that Enter too, exactly like release."""
    b = bind_writer(env, controller="ctl-A")
    orig_staged_is_ours = core.staged_is_ours
    state = {"done": False}

    def staged_is_ours_then_rebind(text, screen):
        ok, why = orig_staged_is_ours(text, screen)
        if ok and not state["done"]:
            state["done"] = True
            bind_writer(
                env, controller="ctl-A"
            )  # supersedes b's lease with a new binding
        return ok, why

    monkeypatch.setattr(core, "staged_is_ours", staged_is_ours_then_rebind)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-rb", "do the thing", 1)
    assert ei.value.code == "lease_lost"
    assert env.cli.send_count == 1 and env.cli.enter_count == 0
    rec = env.store.read(f"requests/{b['binding_id']}/r-rb.json")
    assert rec["status"] == "lease_lost"


# ------------------------------------------------- item 4: replayable receipts + atomic recovery


def test_lost_reply_retried_with_the_original_revision_returns_the_saved_receipt(env):
    b = bind_writer(env)
    first = env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    assert (
        first["status"] == "accepted"
        and first["revision"] == 2
        and first["duplicate_call"] is False
    )
    sends, enters = env.cli.send_count, env.cli.enter_count
    again = env.bridge.submit(
        b["binding_id"], b["controller_id"], "r-1", "do the thing", 1
    )  # the STALE revision
    assert again["status"] == "accepted" and again["duplicate_call"] is True
    assert again["replayed_from"] == "request_record"
    assert again["evidence"] == first["evidence"]
    assert (env.cli.send_count, env.cli.enter_count) == (sends, enters)


def test_stale_revision_still_blocks_a_new_request(env):
    b = bind_writer(env)
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "one", 1)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-2", "two", 1)
    assert ei.value.code == "stale_revision"
    assert env.cli.enter_count == 1


def test_crash_between_record_and_binding_write_is_repaired_on_load(env):
    b = bind_writer(env)
    bid = b["binding_id"]
    rel = f"requests/{bid}/r-crash.json"
    env.store.write(
        rel,
        {
            "request_id": "r-crash",
            "binding_id": bid,
            "kind": "task",
            "text_sha256": "x",
            "status": "accepted",
            "revision_after": 2,
            "started_at": core._iso(env.clk()),
            "history": [],
        },
    )
    raw = env.store.read(f"bindings/{bid}.json")
    raw["inflight"] = "r-crash"
    env.store.write(f"bindings/{bid}.json", raw)  # binding bump never happened
    repaired = env.bridge._load(bid, "ctl-A", need_writer=True)
    assert repaired["revision"] == 2 and repaired["inflight"] is None
    assert repaired["repaired_from"] == "r-crash"


def test_same_request_id_different_text_is_request_conflict(env):
    b = bind_writer(env)
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "one", 1)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "different", 2)
    assert ei.value.code == "request_conflict"


# ------------------------------------------------- item 5: payload identity


def test_staged_evidence_rule_unit():
    assert staged_is_ours("hello", classify_screen(claude_screen("hello")))[0] is True
    assert staged_is_ours("hello", classify_screen(claude_screen("hellp")))[0] is False
    assert staged_is_ours("hello", classify_screen(claude_screen()))[0] is False
    # multi-line text is NEVER verifiable from the screen (item 1): a paste marker's line count is
    # not identity, so foreign pasted text with the same count must not pass.
    ok, why = staged_is_ours("a\nb\nc", classify_screen(pasted_screen(2)))
    assert ok is False and why["rule"] == "multiline_not_screen_verifiable"
    ok, why = staged_is_ours("a\nb\nc", classify_screen(pasted_screen(3)))
    assert ok is False and why["rule"] == "multiline_not_screen_verifiable"
    # single-line trailing whitespace is not compared, and that is disclosed in the check
    ok, why = staged_is_ours("hello   ", classify_screen(claude_screen("hello")))
    assert ok is True and why["trailing_ws_ignored"] is True


def test_staged_is_ours_rejects_a_foreign_continuation_row():
    """item 3 (RED before the fix): classify_screen must not discard a nonempty continuation row,
    and staged_is_ours must refuse Enter when one is present even though the first line matches
    exactly -- the review's own reproducer (`Reply READY.` followed by a foreign row). Dropping
    paste-count matching alone does not close this; the extra row itself must be checked."""
    rows = claude_screen("Reply READY.")
    rows.insert(rows.index(SEP), "  FOREIGN additional instruction")
    screen = classify_screen(rows)
    assert screen["state"] == "staged"
    assert screen["continuation"] == ["  FOREIGN additional instruction"]
    assert screen["continuation_lines"] == 1
    ok, why = staged_is_ours("Reply READY.", screen)
    assert ok is False
    assert why["rule"] == "ambiguous_continuation_row"
    # a genuinely single-line match with no continuation row is unaffected
    clean = classify_screen(claude_screen("Reply READY."))
    assert clean.get("continuation") == []
    assert staged_is_ours("Reply READY.", clean)[0] is True


def test_submit_single_line_accepted_by_exact_transcript_correlation(env):
    b = bind_writer(env)
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "write the migration report", 1)
    assert out["status"] == "accepted" and out["revision"] == 2
    assert out["evidence"]["rule"] == "exact transcript correlation"
    assert env.cli.send_count == 1 and env.cli.enter_count == 1


def test_submit_multiline_is_delivered_as_task_file_reference(env):
    """A multi-line payload is written to an immutable (0444) bridge-owned task file and delivered
    as a single-line reference \u2014 NEVER pasted raw (item 1). The delivered line is what is Enter'd
    and transcript-correlated; the payload file holds the literal bytes and is verified by hash."""
    b = bind_writer(env)
    text = "line one\nline two: \u2713 \U0001f680 `x` $HOME\nline three\ttab   "
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-multi", text, 1)
    assert out["status"] == "accepted"
    assert env.cli.send_count == 1 and env.cli.enter_count == 1
    # no buffer paste and no raw bracketed-paste escape ever went out
    assert not any(
        a for a in env.cli.calls if a and a[0] in ("set-buffer", "paste-buffer")
    )
    assert not any(
        a
        for a in env.cli.calls
        if a and a[0] == "send" and "\x1b[200~" in (a[-1] or "")
    )
    rec = env.store.read(f"requests/{b['binding_id']}/r-multi.json")
    assert rec["delivery_mode"] == "task_file"
    p = Path(rec["payload"]["file"])
    assert p.is_file() and (p.stat().st_mode & 0o777) == 0o444
    assert p.read_text() == text  # literal bytes preserved
    import hashlib

    assert rec["payload"]["payload_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert (
        rec["delivered_text"].startswith("Task brief: ")
        and str(p) in rec["delivered_text"]
    )


def test_multiline_foreign_paste_after_send_is_never_submitted(env):
    """The concrete mcp-hardening-followup #1 failure: a foreign paste of the SAME line count landing
    right after our send used to be Enter'd. Now the delivered line is a single-line task reference,
    so a foreign replacement can never match it: no Enter, nothing foreign in the transcript."""
    b = bind_writer(env)
    env.cli.foreign_after_send = "FOREIGN alpha\nFOREIGN beta\nFOREIGN gamma"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(
            b["binding_id"], b["controller_id"],
            "r-foreign",
            "our line one\nour line two\nour line three",
            1,
        )
    assert ei.value.code == "staged_unverified"
    assert env.cli.enter_count == 0
    assert (
        env.store.read(f"requests/{b['binding_id']}/r-foreign.json")["status"]
        == "staged_unverified"
    )


def test_single_line_foreign_replacement_after_send_is_never_submitted(env):
    b = bind_writer(env)
    env.cli.foreign_after_send = "rm -rf ~ # a human typed this"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-fs", "run the migration", 1)
    assert ei.value.code == "staged_unverified"
    assert env.cli.enter_count == 0


def test_dropped_send_refuses_staged_unverified_and_presses_no_enter(env):
    b = bind_writer(env)
    env.cli.drop_paste = True  # `send` reports ok but nothing reaches the editor
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-drop", "a\nb\nc", 1)
    assert ei.value.code == "staged_unverified"
    assert env.cli.enter_count == 0
    assert (
        env.store.read(f"requests/{b['binding_id']}/r-drop.json")["status"]
        == "staged_unverified"
    )


def test_intervening_human_input_is_uncertain_foreign_and_never_resent(env):
    b = bind_writer(env)
    env.cli.emit_prompt_submit = False
    env.cli.write_transcript = False  # our own Enter leaves no correlated message
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-f", "our payload", 1)
    assert out["status"] == "uncertain"
    env.cli.write_transcript = True
    env.cli.inject_foreign_user_message(env.surf, "a human typed this instead")
    sends, enters = env.cli.send_count, env.cli.enter_count
    again = env.bridge.submit(b["binding_id"], b["controller_id"], "r-f", "our payload", 1)
    assert again["status"] == "uncertain_foreign"
    assert (env.cli.send_count, env.cli.enter_count) == (sends, enters)
    assert (
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-f", "our payload", 1)["duplicate_call"]
        is True
    )


def test_event_without_transcript_stays_uncertain(env):
    b = bind_writer(env)
    env.cli.write_transcript = False  # UserPromptSubmit fires, transcript stays empty
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-e", "our payload", 1)
    assert out["status"] == "uncertain"
    rec = env.store.read(f"requests/{b['binding_id']}/r-e.json")
    assert rec["acceptance"] == {"event_seen": True, "transcript": "missing"}
    assert out["revision"] == 1  # no revision bump on corroboration alone


def test_reconcile_presses_enter_only_on_a_matching_staged_editor(env):
    b = bind_writer(env)

    def after(args, res):
        if args and args[0] == "send-key":
            raise SimulatedFault("timeout", "response lost after delivery (simulated)")

    env.cli.fault = FaultInjector(after=after)
    with pytest.raises(SimulatedFault):
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-half", "do the thing", 1)
    assert (
        env.store.read(f"requests/{b['binding_id']}/r-half.json")["status"]
        == "submitted_unconfirmed"
    )
    assert env.cli.enter_count == 1
    # the fault stays armed: a second send-key would raise, so reaching accepted proves none happened
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-half", "do the thing", 1)
    assert out["status"] == "accepted" and out["reconciled"] is True
    assert env.cli.enter_count == 1 and env.cli.send_count == 1


def test_reconcile_refuses_foreign_staged_text(env):
    b = bind_writer(env)
    env.cli.write_transcript = False
    env.cli.emit_prompt_submit = False
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-s", "ours", 1)
    env.cli.set_screen(env.surf, claude_screen("something a human typed"))
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-s", "ours", 1)
    assert ei.value.code == "staged_unverified"
    assert env.cli.enter_count == 1  # the first one only


def test_submit_docstring_does_not_promise_exactly_once():
    assert "exactly once" not in Bridge.submit.__doc__.lower()
    assert "at most once" in Bridge.submit.__doc__.lower()


# ------------------------------------------------- item 6: no resend after gaps


def test_simulated_pre_send_fault_is_labelled_and_allows_one_redelivery(env):
    b = bind_writer(env)
    seen = {"n": 0}

    def before(args):
        if args and args[0] == "send":
            seen["n"] += 1
            if seen["n"] == 1:
                raise SimulatedFault(
                    "timeout", "request lost before delivery (simulated)"
                )

    env.cli.fault = FaultInjector(before=before)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-lost", "do the thing", 1)
    assert (
        ei.value.code == "send_not_attempted" and ei.value.detail["simulated"] is True
    )
    rec = env.store.read(f"requests/{b['binding_id']}/r-lost.json")
    assert rec["send_result"] == "not_attempted_simulated" and rec["simulated"] is True
    assert env.cli.send_count == 0
    assert any(e.get("fault") == "before" and e["simulated"] for e in env.cli.log)

    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-lost", "do the thing", 1)
    assert out["status"] == "accepted"
    assert env.cli.send_count == 1 and env.cli.enter_count == 1
    assert (
        env.store.read(f"requests/{b['binding_id']}/r-lost.json")["resend_basis"]
        == "simulated_pre_send_fault"
    )


def test_real_cli_error_on_send_is_never_resent(env):
    """A non-zero `send` exit is NOT proof of non-delivery (item 3): the effect may have applied
    before the channel failed. The retry never resends and never presses Enter; it retains
    uncertainty until a human or the transcript resolves it."""
    b = bind_writer(env)
    env.cli.send_error_once = "Error: internal_error: Failed to write to socket\n"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-cli", "do the thing", 1)
    assert ei.value.code == "send_failed"
    assert (
        env.store.read(f"requests/{b['binding_id']}/r-cli.json")["send_result"]
        == "cli_error:internal_error"
    )
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-cli", "do the thing", 1)
    assert out["status"] == "uncertain"  # NOT accepted, NOT resent
    assert env.cli.send_count == 0 and env.cli.enter_count == 0
    assert "resend_basis" not in env.store.read(
        f"requests/{b['binding_id']}/r-cli.json"
    )


def test_reconcile_revalidates_identity_before_acting(env):
    """A reconcile must re-check identity before any further Enter/send (item 3)."""
    b = bind_writer(env)
    env.cli.write_transcript = False
    env.cli.emit_prompt_submit = False
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-id", "do the thing", 1)
    assert out["status"] == "uncertain"
    sends, enters = env.cli.send_count, env.cli.enter_count
    env.cli.bump_boot_id()  # identity now broken
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-id", "do the thing", 1)
    assert ei.value.code == "identity_lost"
    assert (env.cli.send_count, env.cli.enter_count) == (sends, enters)


def test_overlapping_identical_retries_deliver_once(env):
    """mcp-hardening-followup #2: two threads submitting the SAME request concurrently used to send
    twice. The per-binding delivery lock now serialises them: exactly one send + one Enter; one call
    delivers-and-accepts, the other observes/replays without delivering."""
    b = bind_writer(env)
    env.cli.send_gate = 0.3  # hold the first send so a racing second send would collide
    results, errors = [], []

    def call():
        try:
            results.append(
                env.bridge.submit(
                    b["binding_id"], b["controller_id"], "r-race", "do the concurrent thing", 1
                )
            )
        except BridgeError as e:  # pragma: no cover - should not happen
            errors.append(e.code)

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    assert env.cli.send_gate_started.wait(
        3.0
    )  # t1 holds the delivery lock inside _deliver
    t2.start()
    t1.join(10)
    t2.join(10)
    assert env.cli.send_count == 1 and env.cli.enter_count == 1
    assert not errors and len(results) == 2
    reals = [r for r in results if not r.get("duplicate_call")]
    dups = [r for r in results if r.get("duplicate_call")]
    assert len(reals) == 1 and reals[0]["status"] == "accepted"
    assert len(dups) == 1  # the second call observed or replayed, never delivered


def test_overlapping_different_ids_on_one_writer_serialize(env):
    """Two different request_ids racing on one writer binding: exactly one mutation per revision;
    the loser sees stale_revision, and only one payload is ever delivered."""
    b = bind_writer(env)
    env.cli.send_gate = 0.3
    outs, errs = {}, {}

    def call(rid):
        try:
            outs[rid] = env.bridge.submit(b["binding_id"], b["controller_id"], rid, f"task {rid}", 1)
        except BridgeError as e:
            errs[rid] = e.code

    t1 = threading.Thread(target=call, args=("r-A",))
    t2 = threading.Thread(target=call, args=("r-B",))
    t1.start()
    assert env.cli.send_gate_started.wait(3.0)
    t2.start()
    t1.join(10)
    t2.join(10)
    assert env.cli.send_count == 1 and env.cli.enter_count == 1
    assert outs.get("r-A", {}).get("status") == "accepted"
    # r-B is refused without delivering — stale_revision if it reached the reservation, busy if the
    # surface was already running r-A's turn. Either way there is exactly one mutation per revision.
    assert errs.get("r-B") in ("stale_revision", "busy")


def test_rebind_during_preflight_is_caught_by_the_reservation(env):
    """A rebind that supersedes this binding during preflight (after _load, before the reservation)
    must be caught by the reservation's writer re-check — no send (item 2)."""
    b = bind_writer(env)
    orig_validate = env.bridge._validate
    state = {"done": False}

    def validate_then_rebind(bb, deadline=None):
        v = orig_validate(bb, deadline)
        if not state["done"]:
            state["done"] = True
            bind_writer(
                env, controller="ctl-A"
            )  # supersedes b's lease with a new binding
        return v

    env.bridge._validate = validate_then_rebind
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-x", "do the thing", 1)
    assert ei.value.code in ("lease_lost", "stale_revision")
    assert env.cli.send_count == 0 and env.cli.enter_count == 0


def test_wait_never_regresses_an_accepted_record_or_double_bumps_on_replay(env):
    """item 1 (RED before the fix): two real threads exercise public submit() and wait(). wait()
    loads the request record while it is still 'delivering', then -- only after the concurrent
    submit() has already committed it as accepted -- persists its own milestones. Before the fix
    `_persist_milestones` wrote its stale whole snapshot back over the current record, regressing
    the durable 'accepted' status to 'delivering'; replaying the original request afterward then
    incremented the binding revision a SECOND time for one actual send."""
    b = bind_writer(env)
    rel = f"requests/{b['binding_id']}/r-wait.json"
    sent = threading.Event()
    release_send = threading.Event()
    monitor_loaded = threading.Event()
    release_monitor = threading.Event()
    orig_send = env.cli._cmd_send
    orig_events = env.bridge._events

    def gated_send(args):
        value = orig_send(args)
        sent.set()
        assert release_send.wait(8), "send barrier timed out"
        return value

    def gated_events(*args, **kw):
        if threading.current_thread().name == "monitor" and not monitor_loaded.is_set():
            monitor_loaded.set()
            assert release_monitor.wait(8), "monitor barrier timed out"
        return orig_events(*args, **kw)

    env.cli._cmd_send = gated_send
    env.bridge._events = gated_events
    result = {}

    def writer():
        result["submit"] = env.bridge.submit(
            b["binding_id"], b["controller_id"], "r-wait", "Reply READY.", 1, accept_timeout_s=1
        )

    def monitor():
        result["wait"] = env.bridge.wait(
            b["binding_id"], b["controller_id"], "accepted", timeout_s=1, request_id="r-wait", poll_s=0.5
        )

    w = threading.Thread(target=writer, name="writer")
    w.start()
    assert sent.wait(8)
    m = threading.Thread(target=monitor, name="monitor")
    m.start()
    assert monitor_loaded.wait(8)  # wait() has loaded the still-'delivering' record
    assert env.store.read(rel)["status"] == "delivering"
    release_send.set()
    w.join(8)
    assert not w.is_alive()
    assert result["submit"]["status"] == "accepted"
    rev_after_accept = env.store.read(f"bindings/{b['binding_id']}.json")["revision"]
    assert rev_after_accept == 2
    assert env.store.read(rel)["status"] == "accepted"
    release_monitor.set()  # now wait()'s persist runs, AFTER the accept was committed
    m.join(8)
    assert not m.is_alive()
    # SAFE: the durable accepted record must not have been regressed by wait()
    assert env.store.read(rel)["status"] == "accepted"
    assert (
        env.store.read(f"bindings/{b['binding_id']}.json")["revision"]
        == rev_after_accept
    )
    # SAFE: replaying the original request must not double-bump the revision for one actual send
    replay = env.bridge.submit(
        b["binding_id"], b["controller_id"], "r-wait", "Reply READY.", 1, accept_timeout_s=1
    )
    assert replay["duplicate_call"] is True and replay["status"] == "accepted"
    assert (
        env.store.read(f"bindings/{b['binding_id']}.json")["revision"]
        == rev_after_accept
    )
    assert env.cli.send_count == 1 and env.cli.enter_count == 1


def test_event_gap_never_authorises_a_resend(env):
    b = bind_writer(env)
    env.cli.write_transcript = False
    env.cli.emit_prompt_submit = False
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-gap", "do the thing", 1)
    assert out["status"] == "uncertain"
    env.cli.gap = True  # events aged out of the window
    env.cli.set_screen(env.surf, claude_screen())  # and the screen is idle again
    sends, enters = env.cli.send_count, env.cli.enter_count
    again = env.bridge.submit(b["binding_id"], b["controller_id"], "r-gap", "do the thing", 1)
    assert again["status"] == "uncertain"
    assert (env.cli.send_count, env.cli.enter_count) == (sends, enters)
    assert (
        env.store.read(f"requests/{b['binding_id']}/r-gap.json")["event_gap"]["gap"]
        is True
    )


def test_wait_reports_reconnect_gap_and_sends_nothing(env):
    b = bind_writer(env)
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    env.cli.bump_boot_id()
    w = env.bridge.wait(
        b["binding_id"], b["controller_id"], "turn_complete", timeout_s=10, request_id="r-1"
    )
    assert w["outcome"] == "reconnect_gap"
    assert env.cli.send_count == 1 and env.cli.enter_count == 1


# ------------------------------------------------- item 7: identity


def test_writer_without_an_agent_event_is_refused(env):
    env.cli.surfaces[env.surf]["session_id"] = (
        "claude-00000000-0000-4000-8000-000000000001"
    )
    with pytest.raises(BridgeError) as ei:
        bind_writer(env)
    assert ei.value.code == "bind_evidence_missing"
    assert "monitor" in ei.value.detail["instruction"]


# ------------------------- item 7 (r2): conflicting hook-event workspace reconciliation (seq184/190/191)
#
# The hook event's self-reported workspace_id is stamped by cmux and was observed (preflight1) to
# conflict with the workspace the session is genuinely in. bind() no longer vetoes on that field;
# instead, on a conflict it RE-VERIFIES live from the current process (fresh _proc_cmux_ids match on
# workspace AND surface), requires a STABLE proc_start across the re-read, and requires the retained
# event to be FRESH (occurred at/after this process's start). Anything unconfirmed refuses.

WRONG_WS = "4ACE429E-9DC4-4C81-A5F6-B21A2ABC9C7C"


def _emit_conflicting_ws(env, wrong=WRONG_WS):
    """cmux stamps a later agent hook event for this session with a DIFFERENT workspace_id than the
    one the session is really in (the observed preflight1 anomaly). _agent_index keeps the latest
    event per session, so this becomes the event bind() sees."""
    env.cli.emit_agent(env.surf, "UserPromptSubmit", workspace_id=wrong)


def _proc_start_str(env, days):
    """A `ps -o lstart=` style local-clock string offset from the fake clock by `days`, with a
    correct weekday and local wall time, so freshness assertions are timezone-independent."""
    from datetime import datetime, timedelta, timezone

    base = datetime.fromtimestamp(env.clk(), timezone.utc) + timedelta(days=days)
    return base.astimezone().strftime("%a %b %d %H:%M:%S %Y")


def test_event_at_or_after_start_boundary_and_undecidable():
    """The freshness helper accepts an event at/after the process start and refuses one before it,
    at whole-second precision. lstart floors DOWN to the whole second, so there is no negative
    grace (seq191): equality accepts, one second before refuses. Missing/unparseable timestamps are
    undecidable (None) so the identity-critical caller fails closed."""
    from datetime import datetime, timedelta

    ps_str = "Tue Sep  8 11:47:38 2026"
    # the exact instant the helper derives from ps_str (aware, system-local) — deriving the event
    # from it keeps the assertion independent of the test runner's timezone.
    ps_dt = datetime.strptime(ps_str, "%a %b %d %H:%M:%S %Y").astimezone()
    f = core.Bridge._event_at_or_after_start
    assert f(ps_dt.isoformat(), ps_str) is True  # equality accepts
    assert (
        f((ps_dt + timedelta(seconds=1)).isoformat(), ps_str) is True
    )  # just after accepts
    assert (
        f((ps_dt - timedelta(seconds=1)).isoformat(), ps_str) is False
    )  # one second before refuses
    assert f(None, ps_str) is None
    assert f(ps_dt.isoformat(), None) is None
    assert f("not-a-timestamp", ps_str) is None
    assert f(ps_dt.isoformat(), "not-a-date") is None


def test_bind_reconciles_a_conflicting_hook_workspace_when_live_env_confirms(
    env, monkeypatch
):
    """A writer bind is admitted despite a conflicting event workspace WHEN the live process env
    confirms the requested workspace+surface, proc_start is stable, and the event is fresh."""
    assert env.cli.workspace != WRONG_WS
    s = env.cli.surfaces[env.surf]
    env.proc["proc_start"] = _proc_start_str(
        env, -2
    )  # process started 2 days before the event
    _emit_conflicting_ws(env)
    monkeypatch.setattr(
        core.Bridge,
        "_proc_cmux_ids",
        staticmethod(
            lambda pid: {"workspace_uuid": env.cli.workspace, "surface_uuid": s["uuid"]}
        ),
    )
    b = bind_writer(env)
    assert b["binding_id"] and b["identity_evidence"] == "full"
    assert b["hook_event_workspace"] == WRONG_WS
    assert b["hook_event_workspace_conflict"] is True
    assert b["hook_event_workspace_reconciled"] is True
    wr = b["workspace_reconciliation"]
    assert wr["authoritative_workspace"] == env.cli.workspace
    assert wr["authoritative_surface"] == s["uuid"]
    assert wr["event_fresh"] is True


def test_bind_refuses_conflicting_hook_workspace_when_live_env_unconfirmed(
    env, monkeypatch
):
    """If the live process env cannot confirm the requested workspace/surface (None), a conflicting
    event workspace is NOT reconciled — the writer bind refuses as identity (not silently trusted)."""
    env.proc["proc_start"] = _proc_start_str(env, -2)
    _emit_conflicting_ws(env)
    monkeypatch.setattr(core.Bridge, "_proc_cmux_ids", staticmethod(lambda pid: None))
    with pytest.raises(BridgeError) as ei:
        bind_writer(env)
    assert ei.value.code == "identity"
    assert ei.value.detail["hook_event_workspace"] == WRONG_WS


def test_bind_refuses_a_stale_event_even_with_perfectly_matching_live_env(
    env, monkeypatch
):
    """seq190: a reused pid can show matching live workspace/surface while an older RETAINED event
    (from a prior incarnation) carries the conflicting workspace. Even with a PERFECT live
    _proc_cmux_ids match and a stable proc_start, a stale event (occurred BEFORE this process's
    start) must refuse — env=None is not the only way to fail the reconcile."""
    s = env.cli.surfaces[env.surf]
    _emit_conflicting_ws(env)  # occurred_at ~ fake clock (now)
    env.proc["proc_start"] = _proc_start_str(
        env, +2
    )  # this process started 2 days AFTER the event
    monkeypatch.setattr(
        core.Bridge,
        "_proc_cmux_ids",
        staticmethod(
            lambda pid: {"workspace_uuid": env.cli.workspace, "surface_uuid": s["uuid"]}
        ),
    )
    with pytest.raises(BridgeError) as ei:
        bind_writer(env)
    assert ei.value.code == "identity"
    assert ei.value.detail["event_fresh"] is False
    assert ei.value.detail["live_process_ids"]["workspace_uuid"] == env.cli.workspace


def test_monitor_without_an_agent_event_is_allowed_as_partial(env, monkeypatch):
    env.cli.surfaces[env.surf]["session_id"] = (
        "claude-00000000-0000-4000-8000-000000000002"
    )
    monkeypatch.setattr(core.Bridge, "_pid_cwd", staticmethod(lambda pid: env.cwd))
    b = bind_writer(env, "ctl-M", "monitor")
    assert b["identity_evidence"] == "partial"
    assert env.bridge.observe(b["binding_id"], b["controller_id"])["state"] == "prompt_idle"


def test_bind_records_proc_start_and_a_change_is_identity_lost(env):
    b = bind_writer(env)
    assert b["proc_start"] == "Tue Sep  8 11:47:38 2026"
    env.proc["proc_start"] = "Tue Sep  8 12:03:01 2026"  # pid reused by another process
    assert "proc_start_changed" in env.bridge._validate(b)["problems"]
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-x", "hello", 1)
    assert ei.value.code == "identity_lost"
    assert env.cli.send_count == 0


def test_session_end_makes_identity_lost(env):
    b = bind_writer(env)
    env.cli.emit_agent(env.surf, "SessionEnd")
    assert "session_ended" in env.bridge._validate(b)["problems"]
    o = env.bridge.observe(b["binding_id"], b["controller_id"])
    assert o["state"] == "unknown" and "session_ended" in o["identity"]["problems"]


def test_bind_rejects_unknown_workspace_and_refs(env):
    with pytest.raises(BridgeError) as ei:
        env.bridge.bind(
            "workspace:3", env.surf, "claude-x", os.getpid(), env.cwd, "ctl"
        )
    assert ei.value.code == "bad_request"
    other = "11111111-2222-4333-8444-555555555555"
    with pytest.raises(BridgeError) as ei:
        env.bridge.bind(
            other,
            env.surf,
            env.cli.surfaces[env.surf]["session_id"],
            os.getpid(),
            env.cwd,
            "ctl",
        )
    assert ei.value.code == "identity"


def test_bind_cwd_mismatch_is_identity(env, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(BridgeError) as ei:
        env.bridge.bind(
            env.cli.workspace,
            env.surf,
            env.cli.surfaces[env.surf]["session_id"],
            env.cli.surfaces[env.surf]["pid"],
            str(other),
            "ctl",
        )
    assert ei.value.code == "identity"


def test_bind_non_claude_screen_is_not_claude(env):
    shell = env.cli.add_shell_surface()
    env.cli.emit_sidebar(env.surf)
    with pytest.raises(BridgeError) as ei:
        env.bridge.bind(
            env.cli.workspace,
            shell,
            env.cli.surfaces[env.surf]["session_id"],
            env.cli.surfaces[env.surf]["pid"],
            env.cwd,
            "ctl",
        )
    assert ei.value.code in ("bind_evidence_missing", "not_claude")


# ------------------------------------------------- item 8: honest states


def test_observe_reports_raw_background_agents(env):
    b = bind_writer(env)
    assert env.bridge.observe(b["binding_id"], b["controller_id"])["background_agents"] is None
    env.cli.set_background_agents(env.surf, 2)
    assert env.bridge.observe(b["binding_id"], b["controller_id"])["background_agents"] == 2


def test_task_complete_needs_contains_or_sha256(env):
    b = bind_writer(env)
    env.cli.stop_on_enter = True
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    for ev in (None, {}, {"file": str(env.tmp_path / "x.md")}, {"contains": "x"}):
        with pytest.raises(BridgeError) as ei:
            env.bridge.wait(
                b["binding_id"], b["controller_id"],
                "task_complete",
                timeout_s=5,
                request_id="r-1",
                evidence=ev,
            )
        assert ei.value.code == "bad_request"
        assert (
            "existence is not evidence" in ei.value.message
            or "must be" in ei.value.message
        )


def test_task_complete_not_satisfied_by_an_empty_or_unrelated_file(env):
    b = bind_writer(env)
    env.cli.stop_on_enter = True
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    empty = artifact(env, "empty.md", "")
    w = env.bridge.wait(
        b["binding_id"], b["controller_id"],
        "task_complete",
        timeout_s=6,
        request_id="r-1",
        evidence={"file": empty["file"], "contains": "REPORT DONE"},
    )
    assert w["outcome"] == "timeout"


def test_task_complete_satisfied_by_matching_fresh_artifact(env):
    b = bind_writer(env)
    env.cli.stop_on_enter = True
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    ev = artifact(env, "report.md", "REPORT DONE\nbody")
    # item 4: task_complete is a completion PREDICATE and needs a current, evidenced drained
    # attestation, not just acceptance + turn + fresh artifact + idle prompt.
    ev["drained_attestation"] = {
        "drained": True,
        "attested_by": "ctl-A",
        "evidence": "all owned child jobs finished",
        "at": core._iso(env.clk()),
    }
    w = env.bridge.wait(
        b["binding_id"], b["controller_id"], "task_complete", timeout_s=30, request_id="r-1", evidence=ev
    )
    assert w["outcome"] == "satisfied"
    assert w["evidence"]["executor_evidence"]["content_match"] is True
    assert w["evidence"]["screen"]["state"] == "prompt_idle"
    assert w["evidence"]["job_state"] == "controller_attested"


def test_task_complete_not_satisfied_without_a_current_drained_attestation(env):
    """item 4 (RED before the fix): acceptance + turn + fresh artifact + idle prompt alone is NOT
    task_complete. Neither a missing attestation, nor a bare {'drained': true} with no
    attester/evidence/time, nor a well-formed but STALE one (timestamped before this request was
    accepted) satisfies it -- job state stays honestly unknown rather than a false 'satisfied'."""
    b = bind_writer(env)
    env.cli.stop_on_enter = True
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    ev = artifact(env, "report.md", "REPORT DONE\nbody")
    w = env.bridge.wait(
        b["binding_id"], b["controller_id"], "task_complete", timeout_s=6, request_id="r-1", evidence=ev
    )
    assert w["outcome"] == "timeout"
    ev["drained_attestation"] = {"drained": True}
    w2 = env.bridge.wait(
        b["binding_id"], b["controller_id"], "task_complete", timeout_s=6, request_id="r-1", evidence=ev
    )
    assert w2["outcome"] == "timeout"
    rec = env.store.read(f"requests/{b['binding_id']}/r-1.json")
    stale_at = core._iso(core._parse_iso(rec["accepted_at"]).timestamp() - 3600)
    ev["drained_attestation"] = {
        "drained": True,
        "attested_by": "ctl-A",
        "evidence": "stale claim",
        "at": stale_at,
    }
    w3 = env.bridge.wait(
        b["binding_id"], b["controller_id"], "task_complete", timeout_s=6, request_id="r-1", evidence=ev
    )
    assert w3["outcome"] == "timeout"


def test_wait_idle_blocked_by_modal(env):
    b = bind_writer(env)
    env.cli.set_screen(env.surf, modal_screen())
    w = env.bridge.wait(b["binding_id"], b["controller_id"], "idle", timeout_s=10)
    assert w["outcome"] == "blocked_modal"


# ------------------------------------------------- item 9: idempotent compaction


def checkpoint(env, name="checkpoint.md"):
    return artifact(
        env, name, "CHECKPOINT\nobjective / scope / done / active jobs / next action"
    )


def drained(env):
    """A fresh, current controller drained attestation (item 4): timestamped on the test's own
    fake clock so it always passes the currency check, regardless of when this is called."""
    return {
        "drained": True,
        "attested_by": "ctl-A",
        "evidence": "all owned child jobs finished",
        "at": core._iso(env.clk()),
    }


def test_compact_requires_a_drained_attestation(env):
    """A fresh checkpoint + an idle prompt is NOT proof jobs are drained (item 4): compaction needs
    an explicit controller attestation. The footer '← N agent' hint never substitutes."""
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 33.0)
    out = env.bridge.compact(b["binding_id"], b["controller_id"], "c-1", 1, checkpoint(env))
    assert out["outcome"] == "needs_drained_attestation"
    assert env.cli.send_count == 0 and env.cli.enter_count == 0


def test_compact_records_controller_attested_not_native(env):
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 33.0)
    out = env.bridge.compact(
        b["binding_id"], b["controller_id"], "c-1", 1, checkpoint(env), drained_attestation=drained(env)
    )
    assert out["outcome"] == "completed"
    rec = env.store.read(f"requests/{b['binding_id']}/c-1.json")
    assert rec["job_state"] == "controller_attested"
    assert rec["drained_attestation"]["source"] == "controller_attested"


def test_compact_requires_a_checkpoint(env):
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 33.0)
    with pytest.raises(BridgeError) as ei:
        env.bridge.compact(b["binding_id"], b["controller_id"], "c-1", 1, None)
    assert ei.value.code == "checkpoint_required"
    with pytest.raises(BridgeError) as ei:
        env.bridge.compact(
            b["binding_id"], b["controller_id"], "c-1", 1, {"file": str(env.tmp_path / "x.md")}
        )
    assert ei.value.code == "checkpoint_required"
    assert env.cli.send_count == 0


def test_compact_refuses_a_stale_checkpoint_even_with_force(env):
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 33.0)
    cp = checkpoint(env)
    os.utime(cp["file"], (env.clk() - 10_000, env.clk() - 10_000))
    env.cli.stop_on_enter = True
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    with pytest.raises(BridgeError) as ei:
        env.bridge.compact(b["binding_id"], b["controller_id"], "c-1", 2, cp, force=True)
    assert ei.value.code == "checkpoint_stale"


def test_compact_completes_on_transcript_boundary_and_reports_meter_stale(env):
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 33.0)
    out = env.bridge.compact(
        b["binding_id"], b["controller_id"], "c-1", 1, checkpoint(env), drained_attestation=drained(env)
    )
    assert out["outcome"] == "completed" and out["revision"] == 2
    assert out["evidence"]["rule"].startswith("transcript compact_boundary")
    assert out["meter_stale"] is True
    assert out["accepted"] is True


def test_compact_replays_a_lost_completed_response_without_resending(env):
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 33.0)
    cp = checkpoint(env)
    first = env.bridge.compact(
        b["binding_id"], b["controller_id"], "c-1", 1, cp, drained_attestation=drained(env)
    )
    assert first["outcome"] == "completed"
    sends, enters = env.cli.send_count, env.cli.enter_count
    again = env.bridge.compact(
        b["binding_id"], b["controller_id"], "c-1", 1, cp
    )  # stale revision on purpose
    assert again["duplicate_call"] is True and again["outcome"] == "completed"
    assert (env.cli.send_count, env.cli.enter_count) == (sends, enters)


def test_compact_ignores_an_old_retained_session_start(env):
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 33.0)
    env.cli.emit_agent(env.surf, "SessionStart")  # an OLD one, before this request
    env.cli.compact_emits_boundary = False  # no compact_boundary is ever written
    out = env.bridge.compact(
        b["binding_id"], b["controller_id"],
        "c-1",
        1,
        checkpoint(env),
        drained_attestation=drained(env),
        timeout_s=8,
    )
    assert out["outcome"] == "uncertain"
    assert "do not resend" in out["instruction"]
    assert any(e["name"] == "agent.hook.SessionStart" for e in env.cli.events)
    assert env.cli.enter_count == 1


def test_compact_not_due_at_20_pct(env):
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 20.0)
    out = env.bridge.compact(b["binding_id"], b["controller_id"], "c-1", 1, checkpoint(env))
    assert out["outcome"] == "not_due" and env.cli.send_count == 0


def test_compact_deferral_needs_a_reason_when_busy(env):
    b = bind_writer(env)
    env.cli.set_ctx_pct(env.surf, 33.0)
    env.cli.set_screen(env.surf, claude_screen(running=True, pct=33.0))
    cp = checkpoint(env)
    with pytest.raises(BridgeError) as ei:
        env.bridge.compact(b["binding_id"], b["controller_id"], "c-1", 1, cp)
    assert ei.value.code == "deferral_needs_reason"
    out = env.bridge.compact(b["binding_id"], b["controller_id"], "c-2", 1, cp, reason="turn in flight")
    assert out["outcome"] == "deferred" and env.cli.send_count == 0


def test_observe_never_mutates_the_binding_cursor(env):
    b = bind_writer(env)
    before = env.store.read(f"bindings/{b['binding_id']}.json")["cursor_seq"]
    env.cli.emit_agent(env.surf, "PreToolUse")
    o = env.bridge.observe(b["binding_id"], b["controller_id"])
    assert o["next_after_seq"] > o["after_seq"]
    assert env.store.read(f"bindings/{b['binding_id']}.json")["cursor_seq"] == before


def test_stop_consumed_by_an_earlier_observe_is_still_found_by_wait(env):
    b = bind_writer(env)
    env.cli.stop_on_enter = True
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    o = env.bridge.observe(b["binding_id"], b["controller_id"])  # consumes the Stop
    assert o["events"]["last_stop_seq"] is not None
    w = env.bridge.wait(
        b["binding_id"], b["controller_id"], "turn_complete", timeout_s=20, request_id="r-1"
    )
    assert w["outcome"] == "satisfied"


def test_completion_before_the_wait_call_stays_discoverable(env):
    b = bind_writer(env)
    env.cli.stop_on_enter = True
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    env.clk.advance(30)  # the turn ended long ago
    w = env.bridge.wait(
        b["binding_id"], b["controller_id"], "turn_complete", timeout_s=20, request_id="r-1"
    )
    assert w["outcome"] == "satisfied"
    assert (
        env.store.read(f"requests/{b['binding_id']}/r-1.json").get("stop_seq")
        is not None
    )


def test_wait_accepts_a_caller_supplied_after_seq(env):
    b = bind_writer(env)
    env.cli.stop_on_enter = True
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-1", "do the thing", 1)
    rec = env.store.read(f"requests/{b['binding_id']}/r-1.json")
    w = env.bridge.wait(
        b["binding_id"], b["controller_id"], "turn_complete", timeout_s=20, after_seq=rec["cursor_before"]
    )
    assert w["outcome"] == "satisfied"


def test_wait_accepted_requires_a_request_id(env):
    b = bind_writer(env)
    with pytest.raises(BridgeError) as ei:
        env.bridge.wait(b["binding_id"], b["controller_id"], "accepted", timeout_s=5)
    assert ei.value.code == "bad_request"


# ------------------------------------------------- bridge-local pause


def test_pause_blocks_submit_and_resume_allows(env):
    b = bind_writer(env, controller="ctl-A")
    bid = b["binding_id"]
    out = env.bridge.pause(bid, "ctl-A", "operator hold")
    assert out["paused"]["reason"] == "operator hold" and out["paused"]["at"]
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(bid, "ctl-A", "r-p1", "do the thing", 1)
    assert ei.value.code == "paused"
    assert env.cli.send_count == 0 and env.cli.enter_count == 0
    assert env.bridge.resume(bid, "ctl-A")["paused"] is None
    out = env.bridge.submit(bid, "ctl-A", "r-p2", "do the thing", 1)
    assert out["status"] == "accepted"
    assert env.cli.enter_count == 1


def test_pause_during_staging_prevents_the_enter(env, monkeypatch):
    b = bind_writer(env, controller="ctl-A")
    orig_staged_is_ours = core.staged_is_ours
    state = {"done": False}

    def staged_is_ours_then_pause(text, screen):
        ok, why = orig_staged_is_ours(text, screen)
        if ok and not state["done"]:
            state["done"] = True
            env.bridge.pause(b["binding_id"], "ctl-A", "hold landed mid-staging")
        return ok, why

    monkeypatch.setattr(core, "staged_is_ours", staged_is_ours_then_pause)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-ps", "do the thing", 1)
    assert ei.value.code == "paused"
    assert env.cli.send_count == 1 and env.cli.enter_count == 0
    rec = env.store.read(f"requests/{b['binding_id']}/r-ps.json")
    assert rec["status"] == "uncertain"
    # the reconcile path honors the pause too: still no Enter while paused
    monkeypatch.setattr(core, "staged_is_ours", orig_staged_is_ours)
    with pytest.raises(BridgeError) as ei2:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-ps", "do the thing", 1)
    assert ei2.value.code == "paused"
    assert env.cli.enter_count == 0


def test_reconcile_enter_is_withheld_when_paused_after_staging(env, monkeypatch):
    """A staged, un-Entered request reconciled after a pause is resumed must still be gated: the
    reconcile Enter re-reads the binding too."""
    b = bind_writer(env, controller="ctl-A")
    bid = b["binding_id"]
    orig = core.staged_is_ours
    state = {"n": 0}

    def hook(text, screen):
        ok, why = orig(text, screen)
        state["n"] += 1
        if ok and state["n"] == 1:
            env.bridge.pause(bid, "ctl-A", "x")
        return ok, why

    monkeypatch.setattr(core, "staged_is_ours", hook)
    with pytest.raises(BridgeError):
        env.bridge.submit(bid, "ctl-A", "r-rc", "do the thing", 1)
    assert env.cli.enter_count == 0
    env.bridge.resume(bid, "ctl-A")
    monkeypatch.setattr(core, "staged_is_ours", orig)
    # reconcile: the text is still staged; with the pause cleared the Enter is allowed
    env.bridge.submit(bid, "ctl-A", "r-rc", "do the thing", 1)
    assert env.cli.enter_count == 1


def test_pause_message_says_staged_and_same_request_id_reconciles_after_resume(
    env, monkeypatch
):
    b = bind_writer(env, controller="ctl-A")
    bid = b["binding_id"]
    orig = core.staged_is_ours
    state = {"n": 0}

    def hook(text, screen):
        ok, why = orig(text, screen)
        state["n"] += 1
        if ok and state["n"] == 1:
            env.bridge.pause(bid, "ctl-A", "mid-staging")
        return ok, why

    monkeypatch.setattr(core, "staged_is_ours", hook)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(bid, "ctl-A", "r-2b", "do the thing", 1)
    assert ei.value.code == "paused"
    assert "staged in the prompt box" in ei.value.message
    assert "Enter was NOT pressed" in ei.value.message
    assert "SAME request_id" in ei.value.detail["instruction"]
    assert env.cli.enter_count == 0
    env.bridge.resume(bid, "ctl-A")
    monkeypatch.setattr(core, "staged_is_ours", orig)
    out = env.bridge.submit(bid, "ctl-A", "r-2b", "do the thing", 1)
    assert out["status"] == "accepted" and env.cli.enter_count == 1
    assert env.cli.send_count == 1  # reconciled: never re-sent


def test_compact_pause_during_staging_prevents_the_enter(env, monkeypatch):
    b = bind_writer(env, controller="ctl-A")
    bid = b["binding_id"]
    env.cli.set_ctx_pct(env.surf, 33.0)
    orig = core.staged_is_ours

    def hook(text, screen):
        ok, why = orig(text, screen)
        if ok and text == "/compact":
            env.bridge.pause(bid, "ctl-A", "mid-compact")
        return ok, why

    monkeypatch.setattr(core, "staged_is_ours", hook)
    with pytest.raises(BridgeError) as ei:
        env.bridge.compact(bid, "ctl-A", "c-p", 1, checkpoint(env), drained_attestation=drained(env))
    assert ei.value.code == "paused"
    assert env.cli.enter_count == 0
    assert env.store.read(f"requests/{bid}/c-p.json")["status"] == "uncertain"


def test_compact_lease_lost_during_staging_prevents_the_enter(env, monkeypatch):
    b = bind_writer(env, controller="ctl-A")
    bid = b["binding_id"]
    env.cli.set_ctx_pct(env.surf, 33.0)
    orig = core.staged_is_ours

    def hook(text, screen):
        ok, why = orig(text, screen)
        if ok and text == "/compact":
            env.bridge.release(bid, "ctl-A")
        return ok, why

    monkeypatch.setattr(core, "staged_is_ours", hook)
    with pytest.raises(BridgeError) as ei:
        env.bridge.compact(bid, "ctl-A", "c-l", 1, checkpoint(env), drained_attestation=drained(env))
    assert ei.value.code == "lease_lost"
    assert env.cli.enter_count == 0


def test_rebind_on_same_surface_carries_the_pause_over(env):
    old = bind_writer(env, controller="ctl-A")
    env.bridge.pause(old["binding_id"], "ctl-A", "hold")
    new = bind_writer(env, controller="ctl-A")
    assert new["paused"]["reason"] == "hold"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(new["binding_id"], new["controller_id"], "r-pp", "x", 1)
    assert ei.value.code == "paused"
    assert env.cli.send_count == 0
    assert env.bridge.resume(new["binding_id"], "ctl-A")["paused"] is None


def test_pause_survives_release_then_rebind(env):
    a = bind_writer(env, controller="ctl-A")
    env.bridge.pause(a["binding_id"], "ctl-A", "hold")
    env.bridge.release(a["binding_id"], "ctl-A")
    b = bind_writer(env, controller="ctl-A")
    assert b["paused"]["reason"] == "hold"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], "ctl-A", "r-1", "hello", 1)
    assert ei.value.code == "paused"
    # a resume on the newest binding clears it for good: the older paused one is not resurrected
    env.bridge.resume(b["binding_id"], "ctl-A")
    env.bridge.release(b["binding_id"], "ctl-A")
    assert "paused" not in bind_writer(env, controller="ctl-A")


def test_pause_survives_lease_expiry_then_rebind(env):
    a = bind_writer(env, controller="ctl-A", lease_ttl_s=60)
    env.bridge.pause(a["binding_id"], "ctl-A", "hold")
    env.clk.advance(61)
    b = bind_writer(env, controller="ctl-A")
    assert b["paused"]["reason"] == "hold"


def test_same_controller_can_rebind_the_same_session_after_lease_expiry(env):
    a = bind_writer(env, controller="ctl-A", lease_ttl_s=60)
    env.clk.advance(61)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(a["binding_id"], "ctl-A", "r-0", "hello", 1)
    assert ei.value.code == "lease_expired"
    b = bind_writer(env, controller="ctl-A")
    assert b["binding_id"] != a["binding_id"]
    out = env.bridge.submit(b["binding_id"], "ctl-A", "r-1", "hello", 1)
    assert out["status"] == "accepted"


def test_supersede_via_worktree_lease_drops_the_old_surface_lease(env):
    s2 = env.cli.add_claude_surface()
    a = bind_writer(env, controller="ctl-A")
    surf1 = f"leases/surface-{env.surf}.json"
    assert env.store.read(surf1)["binding_id"] == a["binding_id"]
    c = env.cli.surfaces[s2]
    b = env.bridge.bind(
        env.cli.workspace, c["uuid"], c["session_id"], c["pid"], env.cwd, "ctl-A", "writer"
    )
    assert a["binding_id"] in b["superseded_bindings"]
    assert env.store.read(surf1) is None  # no dead binding keeps a live lease
    old = env.store.read(f"bindings/{a['binding_id']}.json")
    assert old["released"] and old["superseded"]
    # the old surface is free for another controller (worktree is held by ctl-A, so use a monitor-free
    # check: the surface lease alone no longer blocks)
    assert env.store.read(f"leases/surface-{s2}.json")["binding_id"] == b["binding_id"]


def test_supersede_frees_the_old_surface_for_a_different_controller(env):
    s2 = env.cli.add_claude_surface()
    a = bind_writer(env, controller="ctl-A")
    c = env.cli.surfaces[s2]
    env.bridge.bind(
        env.cli.workspace, c["uuid"], c["session_id"], c["pid"], env.cwd, "ctl-A", "writer"
    )
    # ctl-A (S2,W1) still holds W1; release it so only the S1 lease question remains
    env.bridge.release(
        env.store.read(f"leases/surface-{s2}.json")["binding_id"], "ctl-A"
    )
    assert env.store.read(f"leases/surface-{env.surf}.json") is None
    d = bind_writer(env, controller="ctl-B")
    assert d["role"] == "writer"


def test_every_binding_tool_refuses_a_foreign_controller(env):
    b = bind_writer(env, controller="ctl-A")
    bid = b["binding_id"]
    calls = {
        "observe": lambda: env.bridge.observe(bid, "ctl-B"),
        "submit": lambda: env.bridge.submit(bid, "ctl-B", "r-1", "hello", 1),
        "wait": lambda: env.bridge.wait(bid, "ctl-B", "idle", timeout_s=1),
        "compact": lambda: env.bridge.compact(
            bid, "ctl-B", "c-1", 1, {"file": "/x", "contains": "x"}
        ),
        "pause": lambda: env.bridge.pause(bid, "ctl-B", "no"),
        "resume": lambda: env.bridge.resume(bid, "ctl-B"),
        "release": lambda: env.bridge.release(bid, "ctl-B"),
        "empty": lambda: env.bridge.release(bid, ""),
    }
    for name, call in calls.items():
        with pytest.raises(BridgeError) as ei:
            call()
        assert ei.value.code == "not_owner", name
    assert env.cli.send_count == 0
    raw = env.store.read(f"bindings/{bid}.json")
    assert not raw["released"] and "paused" not in raw
    assert env.store.read(f"leases/surface-{env.surf}.json")["binding_id"] == bid


def test_release_does_not_clear_a_pause_it_makes_the_binding_unusable(env):
    b = bind_writer(env, controller="ctl-A")
    env.bridge.pause(b["binding_id"], "ctl-A", "hold")
    env.bridge.release(b["binding_id"], b["controller_id"])
    assert env.store.read(f"bindings/{b['binding_id']}.json")["paused"]
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-rel", "x", 1)
    assert ei.value.code == "released"


def test_writer_bind_supersedes_the_same_controllers_other_live_writer(env):
    """One Claude per controller: a binding on another surface/worktree is superseded, atomically."""
    a = bind_writer(env, controller="ctl-A")
    far = (env.clk() + 3600)
    other = {**env.store.read(f"bindings/{a['binding_id']}.json")}
    other.update(
        binding_id="b-other000001",
        surface_uuid="surf-other",
        worktree_key="wt-other",
        lease_expires_at=core._iso(far),
    )
    env.store.write("bindings/b-other000001.json", other)
    for rel, extra in (
        ("leases/surface-surf-other.json", {}),
        ("leases/worktree-wt-other.json", {"worktree_realpath": "/x"}),
    ):
        env.store.write(
            rel,
            {
                "binding_id": "b-other000001",
                "controller_id": "ctl-A",
                "role": "writer",
                "expires_at": core._iso(far),
                "surface_uuid": "surf-other",
                **extra,
            },
        )
    # a monitor of the same controller is unaffected
    mon = bind_writer(env, controller="ctl-A", role="monitor")
    new = bind_writer(env, controller="ctl-A")
    assert "b-other000001" in new["superseded_bindings"]
    ob = env.store.read("bindings/b-other000001.json")
    assert ob["released"] and ob["superseded"] and ob["superseded_by"] == new["binding_id"]
    assert env.store.read("leases/surface-surf-other.json") is None
    assert env.store.read("leases/worktree-wt-other.json") is None
    assert not env.store.read(f"bindings/{mon['binding_id']}.json").get("superseded")
    # a different controller still conflicts with the live writer
    with pytest.raises(BridgeError) as ei:
        bind_writer(env, controller="ctl-B")
    assert ei.value.code == "lease_conflict"


def test_foreign_controller_cannot_pause_or_resume(env):
    b = bind_writer(env, controller="ctl-A")
    bid = b["binding_id"]
    with pytest.raises(BridgeError) as ei:
        env.bridge.pause(bid, "ctl-B", "nope")
    assert ei.value.code == "not_owner"
    env.bridge.pause(bid, "ctl-A", "mine")
    with pytest.raises(BridgeError) as ei:
        env.bridge.resume(bid, "ctl-B")
    assert ei.value.code == "not_owner"
    assert env.store.read(f"bindings/{bid}.json")["paused"]["reason"] == "mine"


def test_observe_shows_paused_and_compact_is_refused(env):
    b = bind_writer(env, controller="ctl-A")
    bid = b["binding_id"]
    assert env.bridge.observe(bid, "ctl-A")["paused"] is None
    env.bridge.pause(bid, "ctl-A", "hold")
    assert env.bridge.observe(bid, "ctl-A")["paused"]["reason"] == "hold"
    with pytest.raises(BridgeError) as ei:
        env.bridge.compact(bid, "ctl-A", "c-1", 1, {"file": "/nonexistent", "contains": "x"})
    assert ei.value.code == "paused"
    assert env.cli.send_count == 0


def test_pause_requires_a_reason(env):
    b = bind_writer(env, controller="ctl-A")
    with pytest.raises(BridgeError) as ei:
        env.bridge.pause(b["binding_id"], "ctl-A", "  ")
    assert ei.value.code == "bad_request"


def test_wait_default_timeout_is_25s(env):
    assert core.DEFAULT_WAIT_S == 25.0
    b = bind_writer(env)
    w = env.bridge.wait(b["binding_id"], b["controller_id"], "turn_complete")
    assert w["outcome"] == "timeout" and 25.0 <= w["elapsed_s"] < 31.0


# ------------------------------------------------- item 11: bounds and contracts


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -5.0, 0, "nonsense", None])
def test_clamp_timeout_rejects_non_finite_and_negative(bad):
    v = clamp_timeout(bad, 120.0, 1.0, 600.0)
    assert isinstance(v, float) and 1.0 <= v <= 600.0


def test_wait_timeout_is_finite_and_clamped(env):
    b = bind_writer(env)
    w = env.bridge.wait(b["binding_id"], b["controller_id"], "turn_complete", timeout_s=float("inf"))
    assert w["outcome"] == "timeout" and w["elapsed_s"] <= core.MAX_WAIT_S


def test_submit_validates_kind_and_text(env):
    b = bind_writer(env)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-k", "hello", 1, kind="anything")
    assert ei.value.code == "bad_request"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-s", "/compact now", 1, kind="task")
    assert ei.value.code == "bad_request"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-c", "bad\x07bell", 1)
    assert ei.value.code == "bad_request"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-l", "x" * 16001, 1)
    assert ei.value.code == "bad_request"
    assert env.cli.send_count == 0


def test_wait_rejects_an_unknown_until(env):
    b = bind_writer(env)
    with pytest.raises(BridgeError) as ei:
        env.bridge.wait(b["binding_id"], b["controller_id"], "whenever", timeout_s=5)
    assert ei.value.code == "bad_request"


# ------------------------------------------------- real captured screen fixtures


def test_real_trust_dialog_fixture_classifies_as_modal():
    rows = (FIXTURES / "real-trust-dialog.txt").read_text().split("\n")
    r = classify_screen(rows)
    assert r["state"] == "modal" and r["claude"] is True


def test_real_dialog_scrollback_then_idle_fixture_classifies_as_prompt_idle():
    rows = (FIXTURES / "real-dialog-scrollback-then-idle.txt").read_text().split("\n")
    r = classify_screen(rows)
    assert r["state"] == "prompt_idle" and r["ctx_used_pct"] == 17.0


def _placeholder_rows(editor="Try \"fix typecheck errors\""):
    rows = (FIXTURES / "real-idle-placeholder-v2.1.292.txt").read_text().split("\n")
    return [("❯\xa0" + editor) if r.startswith("❯") else r for r in rows]


def test_real_idle_placeholder_fixture_is_prompt_idle():
    rows = (FIXTURES / "real-idle-placeholder-v2.1.292.txt").read_text().split("\n")
    r = classify_screen(rows)
    assert r["state"] == "prompt_idle"
    assert r["placeholder"] == 'Try "fix typecheck errors"'
    assert r["ctx_used_pct"] == 0.0 and r["model"] == "Sonnet 5.5"


def test_real_typed_text_over_placeholder_fixture_is_staged():
    r = classify_screen(_placeholder_rows("hello"))
    assert r["state"] == "staged" and r["staged_text"] == "hello"
    assert r["ctx_used_pct"] == 0.0


def test_placeholder_shaped_payload_is_still_verified_as_ours():
    # raw editor text is compared, independent of the placeholder rule
    screen = classify_screen(_placeholder_rows('Try "x"'))
    assert screen["state"] == "prompt_idle"
    assert staged_is_ours('Try "x"', screen)[0] is True
    assert queued_is_ours('Try "x"', screen)[0] is True
    # a placeholder is never mistaken for a different payload
    assert staged_is_ours("hello", screen)[0] is False
    assert queued_is_ours("hello", screen)[0] is False
    # an ordinary idle prompt (no editor text) is never ours
    idle = classify_screen(_placeholder_rows(""))
    assert idle["state"] == "prompt_idle"
    assert staged_is_ours('Try "x"', idle)[0] is False


def test_staged_is_ours_unaffected_for_ordinary_staged_text():
    screen = classify_screen(_placeholder_rows("hello"))
    assert staged_is_ours("hello", screen)[0] is True
    assert staged_is_ours("other", screen)[0] is False


# ------------------------------------------------- screen classification (simulated shapes)


def test_classify_screen_shapes():
    assert classify_screen(claude_screen())["state"] == "prompt_idle"
    assert (
        classify_screen(claude_screen("write the report"))["staged_text"]
        == "write the report"
    )
    assert classify_screen(pasted_screen(12))["pasted_lines"] == 12
    assert classify_screen(claude_screen(running=True))["state"] == "running"
    assert classify_screen(modal_screen())["state"] == "modal"
    assert classify_screen(zsh_screen())["state"] == "not_claude"
    assert classify_screen(empty_screen())["state"] == "unknown"
    assert classify_screen(claude_screen(agents=3))["background_agents"] == 3


def test_classify_queued_placeholder_is_running_not_staged():
    # codex-live-queued-placeholder-review-r2: "Press up to edit queued messages" is Claude UI
    # chrome shown only while the agent is BUSY with queued input. It must never read as a staged
    # human draft, or a busy/queued session would look writable/compactable when the spinner sits
    # outside a short --lines read.
    scr = classify_screen(claude_screen("Press up to edit queued messages"))
    assert scr["state"] == "running"
    assert scr.get("queued_messages") is True
    # a genuine short draft on the same surface still stages correctly (no over-match)
    scr2 = classify_screen(claude_screen("write the report"))
    assert scr2["state"] == "staged" and scr2["staged_text"] == "write the report"
    # negative neighbor: a real draft that MENTIONS the phrase must still stage, not run
    # (codex-r2-classifier-neighbor-and-rollout-scope).
    scr3 = classify_screen(
        claude_screen("Explain the phrase Press up to edit queued messages")
    )
    assert scr3["state"] == "staged"
    assert scr3["staged_text"] == "Explain the phrase Press up to edit queued messages"


# ------------------------------------------------- discover / auth


@pytest.mark.parametrize(
    "stderr,kind",
    [
        ("Error: Failed to write to socket (Broken pipe, errno 32)\n", "access_denied"),
        ("Error: Socket not found at /run/user/cmux.sock\n", "socket_missing"),
        ("Error: Connection refused (errno 61)\n", "connection_refused"),
    ],
)
def test_discover_transport_failures_are_labelled_simulated(env, stderr, kind):
    env.cli.fault = FaultInjector(rewrite=rewrite_ping(stderr))
    d = env.bridge.discover()
    assert (
        d["transport"]["ok"] is False
        and d["transport"]["error_kind"] == kind
        and d["targets"] == []
    )
    pings = [e for e in env.cli.log if e["args"] and e["args"][0] == "ping"]
    assert pings and all(e["simulated"] is True for e in pings)


def test_discover_access_denied_requirement_mentions_password(env):
    env.cli.fault = FaultInjector(
        rewrite=rewrite_ping(
            "Error: Failed to write to socket (Broken pipe, errno 32)\n"
        )
    )
    req = env.bridge.discover()["transport"]["requirement"]
    assert "password" in req and "CMUX_BRIDGE_PASSWORD_FILE" in req


def test_discover_ok_lists_the_claude_target(env):
    d = env.bridge.discover()
    assert d["transport"]["ok"] is True and d["transport"]["access_mode"] == "cmuxOnly"
    assert [t["surface_uuid"] for t in d["targets"]] == [env.surf]
    t = d["targets"][0]
    assert t["pid"] == env.cli.surfaces[env.surf]["pid"] and t["alive"] is True
    assert (
        t["claude_session_id"] == env.cli.surfaces[env.surf]["session_id"]
        and t["cwd"] == env.cwd
    )


def test_env_puts_password_in_env_only_when_file_is_0600(tmp_path, monkeypatch):
    pw = tmp_path / "pw.txt"
    pw.write_text("s3cret-token\n")
    os.chmod(pw, 0o600)
    monkeypatch.setenv("CMUX_BRIDGE_PASSWORD_FILE", str(pw))
    cli = CmuxCLI(cli_path="/fake/bin/cmux")
    assert cli._env()["CMUX_SOCKET_PASSWORD"] == "s3cret-token"
    os.chmod(pw, 0o644)
    assert "CMUX_SOCKET_PASSWORD" not in cli._env()


def test_password_never_appears_on_argv(env, tmp_path, monkeypatch):
    pw = tmp_path / "pw.txt"
    pw.write_text("s3cret-token\n")
    os.chmod(pw, 0o600)
    monkeypatch.setenv("CMUX_BRIDGE_PASSWORD_FILE", str(pw))
    env.bridge.discover()
    assert env.cli.argvs and env.cli.argvs[0] == ["/fake/bin/cmux", "ping"]
    assert not any("s3cret-token" in part for argv in env.cli.argvs for part in argv)


# ------------------------------------------------- server layer


EXPECTED_READ_ONLY = {
    "bridge_discover": True,
    "bridge_bind": False,
    "bridge_observe": True,
    "bridge_submit": False,
    "bridge_wait": False,
    "bridge_compact": False,
    "bridge_pause": False,
    "bridge_resume": False,
    "bridge_release": False,
}
EXPECTED_IDEMPOTENT = {
    "bridge_bind": False,
    "bridge_submit": True,
    "bridge_wait": True,
    "bridge_compact": True,
    "bridge_pause": True,
    "bridge_resume": True,
    "bridge_release": True,
}


def test_server_exposes_nine_bridge_tools_with_documented_annotations(
    env, monkeypatch
):
    from fastmcp import Client
    from cmux_bridge import server

    monkeypatch.setattr(server, "_bridge", lambda: env.bridge)

    async def go():
        async with Client(server.mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(go())
    assert {t.name for t in tools} == set(EXPECTED_READ_ONLY)
    assert {t.name: t.annotations.readOnlyHint for t in tools} == EXPECTED_READ_ONLY
    got = {
        t.name: t.annotations.idempotentHint
        for t in tools
        if t.name in EXPECTED_IDEMPOTENT
    }
    assert got == EXPECTED_IDEMPOTENT


def test_server_refusal_surfaces_as_toolerror_with_code_prefix(env, monkeypatch):
    from fastmcp import Client
    from fastmcp.exceptions import ToolError
    from cmux_bridge import server

    monkeypatch.setattr(server, "_bridge", lambda: env.bridge)
    b = bind_writer(env)

    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool(
                "bridge_submit",
                {
                    "binding_id": b["binding_id"],
                    "controller_id": b["controller_id"],
                    "request_id": "r-1",
                    "text": "hello",
                    "expected_revision": 99,
                },
            )

    with pytest.raises(ToolError) as ei:
        asyncio.run(go())
    assert str(ei.value).startswith("stale_revision:")
    assert env.cli.send_count == 0


# ------------------------------------------------- launcher


def test_run_live_sh_stops_on_a_failing_precheck(tmp_path):
    out = tmp_path / "out"
    marker = tmp_path / "launched"
    r = subprocess.run(
        ["bash", str(Path(BRIDGE_ROOT) / "run_live.sh")],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "ALLOW_DIRTY": "1",
            "OUT_DIR": str(out),
            "PRECHECK_CMD": "false",
            "LIVE_CMD": f"touch {marker}",
        },
    )
    assert r.returncode != 0
    assert not marker.exists()
    status = json.loads((out / "launcher-status.json").read_text())
    assert status["outcome"] == "precheck_failed" and status["live_exit"] is None


def test_run_live_sh_runs_the_live_command_after_a_passing_precheck(tmp_path):
    out = tmp_path / "out"
    marker = tmp_path / "launched"
    r = subprocess.run(
        ["bash", str(Path(BRIDGE_ROOT) / "run_live.sh")],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "ALLOW_DIRTY": "1",
            "OUT_DIR": str(out),
            "PRECHECK_CMD": "true",
            "LIVE_CMD": f"touch {marker}",
        },
    )
    assert r.returncode == 0
    assert marker.exists()
    status = json.loads((out / "launcher-status.json").read_text())
    assert (
        status["precheck_exit"] == 0
        and status["live_exit"] == 0
        and status["outcome"] == "completed"
    )


def test_simulated_post_send_reply_loss_is_never_a_resend_basis(env):
    """SIMULATED. An `after` fault on `send` means the command ran and only its reply was lost: the
    text is really staged, so the record must say send_result=ok and reconciliation must press Enter
    on the verified staged text exactly once, never send again (review item 6)."""
    b = bind_writer(env)
    seen = {"n": 0}

    def after(args, res):
        if args and args[0] == "send":
            seen["n"] += 1
            if seen["n"] == 1:
                raise SimulatedFault("timeout", "reply lost after delivery (simulated)")

    env.cli.fault = FaultInjector(after=after)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-after", "do the thing", 1)
    assert (
        ei.value.code == "send_response_lost" and ei.value.detail["simulated"] is True
    )
    rec = env.store.read(f"requests/{b['binding_id']}/r-after.json")
    assert rec["send_result"] == "ok" and rec["response_lost_simulated"] is True
    assert env.cli.send_count == 1 and env.cli.enter_count == 0

    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-after", "do the thing", 1)
    assert out["status"] == "accepted" and out.get("reconciled") is True
    assert env.cli.send_count == 1 and env.cli.enter_count == 1
    assert "resend_basis" not in env.store.read(
        f"requests/{b['binding_id']}/r-after.json"
    )


def test_footer_agent_hint_is_raw_and_never_gates_writes(env):
    """SIMULATED. A brand-new real session shows "← 1 agent" in its footer (live run 2026-09-08T18:12Z),
    so the hint is not evidence of owned jobs: observe reports it raw and submit is not refused."""
    from tests.fakecli import claude_screen

    b = bind_writer(env)
    surf = env.surf
    env.cli.set_screen(
        surf,
        claude_screen() + ["  ⏵⏵ accept edits on (shift+tab to cycle) · ← 2 agents"],
    )
    o = env.bridge.observe(b["binding_id"], b["controller_id"])
    assert o["background_agents"] == 2 and o["state"] == "prompt_idle"
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-hint", "do the thing", 1)
    assert out["status"] == "accepted"


# ---------------------------------------------------------------- authorized scoped queued steering
# A steer (kind="steer") may queue its single-line payload into a RUNNING turn. Every identity/lease/
# gate check still applies; only the idle precondition is relaxed for this one case. Acceptance still
# requires the exact delivered payload to appear in the durable transcript — enqueue / an empty editor
# / a UserPromptSubmit event alone never prove it (contract R3).


def test_steer_into_running_turn_accepted_when_absorbed(env):
    b = bind_writer(env)
    env.cli.set_screen(env.surf, claude_screen(running=True))  # agent busy
    env.cli.keep_running_on_send = True  # typing keeps the spinner (queued input)
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-steer-ok", "scoped review: fix the gate", 1, "steer")
    assert out["status"] == "accepted" and out["revision"] == 2
    assert out["evidence"]["rule"] == "exact transcript correlation"
    assert env.cli.send_count == 1 and env.cli.enter_count == 1


def test_steer_queued_unconfirmed_when_not_absorbed_in_window(env):
    b = bind_writer(env)
    env.cli.set_screen(env.surf, claude_screen(running=True))
    env.cli.keep_running_on_send = True
    env.cli.queue_on_enter = True  # Enter QUEUES; no transcript user-message yet
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-steer-q", "checkpoint please", 1, "steer")
    # the bridge pressed Enter exactly once to queue, then found no transcript correlation in the
    # bounded window -> uncertain (bridge_link relabels this queued_unconfirmed). Nothing re-sent.
    assert out["status"] == "uncertain"
    assert env.cli.send_count == 1 and env.cli.enter_count == 1
    # a reconcile with the SAME request id must not press Enter again while still unabsorbed
    again = env.bridge.submit(b["binding_id"], b["controller_id"], "r-steer-q", "checkpoint please", 1, "steer")
    assert again["status"] == "uncertain"
    assert env.cli.enter_count == 1  # no second keystroke, no auto-resend


def test_steer_reconcile_accepts_once_absorbed_without_re_enter(env):
    b = bind_writer(env)
    env.cli.set_screen(env.surf, claude_screen(running=True))
    env.cli.keep_running_on_send = True
    env.cli.queue_on_enter = True
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-steer-a", "scoped review: recheck", 1, "steer")
    assert out["status"] == "uncertain" and env.cli.enter_count == 1
    # the running turn yields and absorbs the queued message: it becomes a real user turn now
    env.cli.write_user_message(env.surf, "scoped review: recheck")
    again = env.bridge.submit(b["binding_id"], b["controller_id"], "r-steer-a", "scoped review: recheck", 1, "steer")
    assert again["status"] == "accepted" and again["reconciled"] is True
    assert env.cli.enter_count == 1  # accepted via transcript correlation, never a second Enter


def test_ordinary_task_still_refused_busy_on_running_turn(env):
    b = bind_writer(env)
    env.cli.set_screen(env.surf, claude_screen(running=True))
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-task-busy", "do the thing", 1)  # kind defaults to task
    assert ei.value.code == "busy"
    assert env.cli.send_count == 0 and env.cli.enter_count == 0


def test_prompt_suggestion_env_is_recorded_and_hints_a_staged_busy_refusal(env, monkeypatch):
    b = bind_writer(env)  # the fake pid has no readable environment: unknown, never assumed disabled
    assert b["prompt_suggestions"] == "enabled_or_unknown"
    env.cli.set_screen(env.surf, claude_screen("Task brief: /x/state/tasks/b-"))  # ghost text reads as staged
    o = env.bridge.observe(b["binding_id"], b["controller_id"])
    assert o["state"] == "staged" and "promptSuggestionEnabled:false" in o["instruction"]
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-ghost", "do the thing", 1)
    assert ei.value.code == "busy" and "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false" in ei.value.detail["instruction"]
    assert env.cli.send_count == 0 and env.cli.enter_count == 0
    # a binding to a session launched with suggestions off carries no hint
    monkeypatch.setattr(core.Bridge, "_proc_prompt_suggestions", staticmethod(lambda pid: "disabled"))
    b2 = bind_writer(env)
    assert b2["prompt_suggestions"] == "disabled"
    assert "instruction" not in env.bridge.observe(b2["binding_id"], b2["controller_id"])
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b2["binding_id"], b2["controller_id"], "r-ghost2", "do the thing", 1)
    assert ei.value.code == "busy" and "instruction" not in ei.value.detail


def test_proc_prompt_suggestions_extracts_only_that_key(monkeypatch):
    def fake(out):
        return lambda *a, **k: SimpleNamespace(stdout=out)

    for out, want in [
        ("claude --model opus SECRET_TOKEN=abc CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false X=1", "disabled"),
        ("claude --model opus CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=true", "enabled_or_unknown"),
        ("claude --model opus SECRET_TOKEN=abc", "enabled_or_unknown"),
    ]:
        monkeypatch.setattr(core.subprocess, "run", fake(out))
        assert core.Bridge._proc_prompt_suggestions(123) == want


def test_steer_refused_in_modal(env):
    b = bind_writer(env)
    env.cli.set_screen(env.surf, modal_screen())
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-steer-modal", "scoped review", 1, "steer")
    assert ei.value.code == "busy"  # a steer queues only into a running turn, never a dialog
    assert env.cli.enter_count == 0


def test_steer_foreign_input_after_send_is_never_queued(env):
    b = bind_writer(env)
    env.cli.set_screen(env.surf, claude_screen(running=True))
    env.cli.keep_running_on_send = True
    env.cli.foreign_after_send = "a human typed something else"
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-steer-foreign", "scoped review", 1, "steer")
    assert ei.value.code == "staged_unverified"  # the editor is not provably ours -> no Enter
    assert env.cli.enter_count == 0


# ------------------------------------------------- real cmux 0.64.25 drift (live audit)

_UUID = "eb5438be-5f12-4f69-9a89-70a6371dc4f5"
_REAL_FEED = "cmux-feed-v1:Y2xhdWRl:ZWI1NDM4YmUtNWYxMi00ZjY5LTlhODktNzBhNjM3MWRjNGY1"


def test_canonical_session_id_unit():
    cs = core.canonical_session_id
    assert cs(_REAL_FEED) == "claude-" + _UUID  # valid, as emitted by real cmux
    assert cs("cmux-feed-v1:Y2xhdWRl:YWI=") == "claude-ab"  # padded form
    assert cs("cmux-feed-v1:Y2xhdWRl:YWI") == "claude-ab"  # unpadded form
    # padded agent half ('claude' -> 'Y2xhdWRl' needs no pad; 'codex' -> 'Y29kZXg=' does)
    assert cs("cmux-feed-v1:Y29kZXg=:" + _REAL_FEED.rsplit(":", 1)[1]) == (
        "cmux-feed-v1:Y29kZXg=:" + _REAL_FEED.rsplit(":", 1)[1]
    )  # non-claude agent unchanged
    assert cs("cmux-feed-v1:Y29kZXg:abc") == "cmux-feed-v1:Y29kZXg:abc"
    assert cs("cmux-feed-v1:!!!:???") == "cmux-feed-v1:!!!:???"  # garbage base64
    assert cs("cmux-feed-v1:onlyone") == "cmux-feed-v1:onlyone"
    assert cs("claude-" + _UUID) == "claude-" + _UUID  # legacy form passes through
    assert cs(None) is None and cs("") == ""


def test_canonical_session_id_urlsafe_alphabet():
    import base64

    ident = "id?>~"  # b64 contains '/' and '+'-class chars in the standard alphabet
    enc = lambda t: base64.urlsafe_b64encode(t.encode()).decode().rstrip("=")  # noqa: E731
    assert core.canonical_session_id(f"cmux-feed-v1:{enc('claude')}:{enc(ident)}") == "claude-" + ident


def test_real_feed_hook_ids_discover_and_bind(env):
    sid = env.cli.surfaces[env.surf]["session_id"]
    ev = env.cli.emit_agent(env.surf, "UserPromptSubmit")
    assert ev["payload"]["session_id"].startswith("cmux-feed-v1:")  # fake emits the real form
    d = env.bridge.discover()
    assert [t["claude_session_id"] for t in d["targets"]] == [sid]
    assert core.Bridge.transcript_path(sid, env.cwd).name == sid.removeprefix("claude-") + ".jsonl"
    b = bind_writer(env)
    assert b["claude_session_id"] == sid


def test_legacy_hook_ids_still_work(env):
    env.cli.legacy_session_ids = True
    sid = env.cli.surfaces[env.surf]["session_id"]
    ev = env.cli.emit_agent(env.surf, "UserPromptSubmit")
    assert ev["payload"]["session_id"] == sid
    d = env.bridge.discover()
    assert [t["claude_session_id"] for t in d["targets"]] == [sid]
    assert bind_writer(env)["claude_session_id"] == sid


def test_footer_with_ctx_u_variant_and_nbsp_model():
    row = (
        "  Model: Opus\xa05.5 | Context: ▓▓░░░░░░░░Ctx(u) Used: 18.9% | v2.1.292 | Session: 5m"
    )
    r = classify_screen(claude_screen()[:-3] + [row])
    assert r["ctx_used_pct"] == 18.9 and r["state"] == "prompt_idle"


def test_background_panel_rows_after_footer_are_not_staged():
    panel = "  ◯ audit-tandem  ░░░░░░░░░░░░░░░░░░░░  0/4 · 1m11s · ↓ 303.1k tokens"
    r = classify_screen(claude_screen() + [panel])
    assert r["state"] == "prompt_idle", r
    # real continuation rows between the prompt and the border still count
    scr = claude_screen("first")
    i = next(k for k, l in enumerate(scr) if l.startswith("❯"))
    scr.insert(i + 1, "  second line")
    r = classify_screen(scr + [panel])
    assert r["state"] == "staged" and r["continuation_lines"] == 1


class _FlakyCLI:
    """Minimal CmuxCLI whose first call fails with the transient socket-setup error seen live."""

    @staticmethod
    def make(first_stderr):
        from cmux_bridge.cmuxcli import CmuxCLI, CmuxResult

        class Flaky(CmuxCLI):
            def __init__(self):
                super().__init__(cli_path="/nonexistent", socket=None)
                self.calls = []

            def _execute(self, argv, args, timeout, t0):
                self.calls.append(list(args))
                if len(self.calls) == 1:
                    r = CmuxResult(args, 1, "", first_stderr, 0.0)
                    r.error_kind = "other"
                    return r
                r = CmuxResult(args, 0, "OK", "", 0.0)
                r.error_kind = None
                return r

        return Flaky()


TRANSIENT = "Error: Failed to configure socket receive timeout (Invalid argument, errno 22)"


def test_read_only_command_retries_once_on_transient_socket_error():
    cli = _FlakyCLI.make(TRANSIENT)
    res = cli.run("list-workspaces", "--id-format", "both")
    assert res.error_kind is None and len(cli.calls) == 2
    assert cli.log[-1].get("retried_transient")


def test_send_is_never_retried_on_transient_socket_error():
    cli = _FlakyCLI.make(TRANSIENT)
    res = cli.run("send", "--surface", "S", "--", "hello")
    assert res.error_kind == "other" and len(cli.calls) == 1


def test_non_transient_error_is_not_retried():
    cli = _FlakyCLI.make("Error: something else broke")
    res = cli.run("list-workspaces")
    assert res.error_kind == "other" and len(cli.calls) == 1


# ------------------------------------------------- isolation / PHI-minimisation hardening


def test_worktree_lease_key_is_case_insensitive_canonical(env):
    swapped = env.cwd.swapcase()
    if not os.path.exists(swapped):
        pytest.skip("case-sensitive filesystem")
    assert core.canonical_dir(swapped) == core.canonical_dir(env.cwd)
    bind_writer(env, "ctl-A")
    s = env.cli.surfaces[env.surf]
    with pytest.raises(BridgeError) as ei:
        env.bridge.bind(
            env.cli.workspace, s["uuid"], s["session_id"], s["pid"], swapped, "ctl-B", "writer"
        )
    assert ei.value.code == "lease_conflict"


@pytest.mark.parametrize("bad", ["", "   ", None, 5, "x" * 201])
def test_bind_refuses_bad_controller_id(env, bad):
    with pytest.raises(BridgeError) as ei:
        bind_writer(env, bad)
    assert ei.value.code == "bad_request"


def test_refused_submit_writes_no_task_file(env):
    b = bind_writer(env)
    env.cli.set_screen(env.surf, modal_screen())
    with pytest.raises(BridgeError):
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-ref", "a\nb\nc", 1)
    assert not list((env.root / "tasks").rglob("*.txt"))
    assert not list((env.root / "requests").rglob("*.json"))


def test_release_deletes_task_files_after_terminal_request(env):
    b = bind_writer(env)
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-rel", "x\ny", 1)
    assert out["status"] == "accepted"
    assert list((env.root / "tasks" / b["binding_id"]).glob("*.txt"))
    env.bridge.release(b["binding_id"], b["controller_id"])
    assert not (env.root / "tasks" / b["binding_id"]).exists()


def test_release_keeps_task_files_while_request_inflight(env):
    b = bind_writer(env)
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-keep", "x\ny", 1)
    rel = f"requests/{b['binding_id']}/r-keep.json"
    rec = env.store.read(rel)
    rec["status"] = "uncertain"
    env.store.write(rel, rec)
    bb = env.store.read(f"bindings/{b['binding_id']}.json")
    bb["inflight"] = "r-keep"
    env.store.write(f"bindings/{b['binding_id']}.json", bb)
    env.bridge.release(b["binding_id"], b["controller_id"])
    assert list((env.root / "tasks" / b["binding_id"]).glob("*.txt"))


def test_terminal_request_record_keeps_only_hashes_and_replays(env):
    b = bind_writer(env)
    text = "SYNTHETIC-PROMPT-TEXT"
    out = env.bridge.submit(b["binding_id"], b["controller_id"], "r-scrub", text, 1)
    assert out["status"] == "accepted"
    raw = (env.root / "requests" / b["binding_id"] / "r-scrub.json").read_text()
    assert text not in raw
    rec = json.loads(raw)
    assert rec["delivered_text"] is None and rec["text_sha256"] and rec["delivered_text_sha256"]
    again = env.bridge.submit(b["binding_id"], b["controller_id"], "r-scrub", text, 2)
    assert again["duplicate_call"] is True and again["status"] == "accepted"


def test_ctx_re_ignores_malformed_percentage():
    from tests.fakecli import footer

    rows = claude_screen("")
    rows[rows.index(footer(17.0))] = footer(17.0).replace("17.0%", "1.2.3%")
    assert classify_screen(rows).get("ctx_used_pct") is None  # malformed: ignored, no raise
    assert core.CTX_RE.search("Ctx Used: 12.5%").group(1) == "12.5"


def test_editor_row_starting_with_model_between_prompt_and_border_is_continuation():
    rows = claude_screen("hi")
    i = next(k for k, r in enumerate(rows) if r.startswith("❯"))
    rows.insert(i + 1, "  Model: x")
    ok, _ = staged_is_ours("hi", classify_screen(rows))
    assert ok is False


def test_broken_pipe_is_not_a_transient_retry():
    from cmux_bridge.cmuxcli import TRANSIENT_SOCKET_RE

    assert not TRANSIENT_SOCKET_RE.search("Failed to write to socket (Broken pipe, errno 32)")
    assert TRANSIENT_SOCKET_RE.search("Connection reset by peer")


def test_call_log_is_bounded_and_redacts_send_text():
    from cmux_bridge.cmuxcli import CmuxCLI

    class _Fake(CmuxCLI):
        def _execute(self, argv, args, timeout, t0):
            return CmuxResult(args, 0, "", "", 0.0)

    cli = _Fake(cli_path="x")
    cli.run("send", "--surface", "S", "--", "secret member text")
    cli.run("send", "--surface", "S", "--", "/compact")
    assert cli.log[0]["args"] == ["send", "--surface", "S", "--", "<redacted 18 chars>"]
    assert cli.log[1]["args"][-1] == "/compact"
    for _ in range(600):
        cli.run("ping")
    assert len(cli.log) == 500 and len(cli.log[-3:]) == 3


def test_call_log_is_json_serialisable_and_marks_are_monotonic_past_the_cap():
    import json as _json

    from cmux_bridge.cmuxcli import CallLog

    log = CallLog(maxlen=5)
    for i in range(3):
        log.append({"args": ["x", str(i)]})
    m = log.mark()
    for i in range(10):
        log.append({"args": ["send", str(i)]})
    assert len(log) == 5 and log.total == 13
    assert len(log.since(m)) == 5 and log.since(log.mark()) == []
    assert _json.loads(_json.dumps(list(log)))[-1]["args"] == ["send", "9"]
    assert isinstance(_json.dumps(log), str)
    unb = CallLog(maxlen=None)
    for i in range(700):
        unb.append({"args": ["ping"]})
    assert len(unb) == 700 and len(unb[600:]) == 100


def test_refused_submits_leave_no_task_file(env):
    b = bind_writer(env)
    big = "line one\nline two\n" * 3
    tasks = Path(env.bridge.store.path(f"tasks/{b['binding_id']}"))
    env.bridge.submit(b["binding_id"], b["controller_id"], "r-ok", big, 1)
    assert (tasks / "r-ok.txt").is_file()
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-stale", big, 1)
    assert ei.value.code == "stale_revision"
    assert not (tasks / "r-stale.txt").exists()
    cur = env.bridge.store.read(f"bindings/{b['binding_id']}.json")
    cur["inflight"] = "someone-else"
    env.bridge.store.write(f"bindings/{b['binding_id']}.json", cur)
    with pytest.raises(BridgeError) as ei:
        env.bridge.submit(b["binding_id"], b["controller_id"], "r-busy", big, cur["revision"])
    assert ei.value.code == "busy"
    assert not (tasks / "r-busy.txt").exists()
