"""Unit tests for the pure helpers in live_acceptance.py. No cmux CLI, no claude, no network."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from live_acceptance import (  # noqa: E402
    CaseRecorder,
    OwnedSurfaces,
    Runner,
    SurfaceResolutionError,
    _bash_drain_status,
    count_calls,
    count_delivered_prompts,
    drain_check_transcript,
    parse_surface_listing,
    resolve_surface_ref,
    sha256_bytes,
    surface_ref_from_json,
    user_record_text,
)

U1 = "6C1F0A32-1111-4A2B-8C3D-0123456789AB"
U2 = "7D2E1B43-2222-4B3C-9D4E-123456789ABC"
LISTING = f"  surface:52 {U1}  claude — bridge scratch\n  surface:53 {U2}  zsh\n"


# --------------------------------------------------------------- ref -> UUID resolution
def test_listing_rows_map_refs_to_uuids():
    assert parse_surface_listing(LISTING) == {"surface:52": U1, "surface:53": U2}


def test_resolve_picks_the_exact_ref_not_a_set_difference():
    assert resolve_surface_ref("surface:53", LISTING) == U2


def test_resolve_is_case_insensitive_on_the_uuid_but_normalises_upper():
    assert (
        resolve_surface_ref("surface:52", f"  surface:52 {U1.lower()}  title\n") == U1
    )


def test_unknown_ref_raises_rather_than_guessing():
    with pytest.raises(SurfaceResolutionError):
        resolve_surface_ref("surface:99", LISTING)
    with pytest.raises(SurfaceResolutionError):
        resolve_surface_ref("", LISTING)


def test_a_ref_quoted_in_a_title_is_not_a_row():
    text = f"  surface:52 {U1}  a pane titled surface:99 {U2}\n"
    assert parse_surface_listing(text) == {"surface:52": U1}


def test_two_uuids_for_one_ref_is_an_ambiguity_not_last_one_wins():
    with pytest.raises(SurfaceResolutionError):
        parse_surface_listing(f"  surface:52 {U1}  a\n  surface:52 {U2}  b\n")


def test_surface_ref_is_read_from_new_surface_json():
    assert (
        surface_ref_from_json(
            json.dumps({"surface_ref": "surface:52", "workspace": "x"})
        )
        == "surface:52"
    )
    with pytest.raises(SurfaceResolutionError):
        surface_ref_from_json(json.dumps({"uuid": U1}))
    with pytest.raises(SurfaceResolutionError):
        surface_ref_from_json("not json")


# --------------------------------------------------------------- owned-surface bookkeeping
def test_owned_file_is_written_immediately_and_is_idempotent(tmp_path):
    p = tmp_path / "owned-surfaces.json"
    owned = OwnedSurfaces(p)
    assert json.loads(p.read_text())["owned"] == []
    owned.add(U1.lower())
    assert json.loads(p.read_text())["owned"] == [
        U1
    ]  # normalised, persisted before anything else
    owned.add(U1)
    assert owned.uuids == [U1]


def test_only_added_surfaces_are_owned(tmp_path):
    owned = OwnedSurfaces(tmp_path / "o.json")
    owned.add(U1)
    assert owned.owns(U1) and owned.owns(U1.lower())
    assert not owned.owns(U2)
    assert not owned.owns(None)


def test_a_non_uuid_can_never_become_owned(tmp_path):
    owned = OwnedSurfaces(tmp_path / "o.json")
    with pytest.raises(SurfaceResolutionError):
        owned.add("surface:52")
    assert owned.uuids == []


def test_unresolved_refs_are_recorded_for_manual_cleanup(tmp_path):
    p = tmp_path / "o.json"
    owned = OwnedSurfaces(p)
    owned.add_unresolved("surface:52")
    assert json.loads(p.read_text())["unresolved_refs"] == ["surface:52"]
    assert not owned.owns("surface:52")


def test_closed_surfaces_are_tracked(tmp_path):
    p = tmp_path / "o.json"
    owned = OwnedSurfaces(p)
    owned.add(U1)
    owned.mark_closed(U1.lower())
    assert json.loads(p.read_text())["closed"] == [U1]


# --------------------------------------------------------------- case recording
def test_unsupported_case_is_never_a_pass(tmp_path):
    rec = CaseRecorder(tmp_path / "cases.json")
    out = rec.record(
        "R9",
        "injected fault",
        "real+injected",
        True,
        {},
        note="fault hook unavailable",
        supported=False,
    )
    assert out["pass"] is False
    assert out["note"].startswith("not run: ")


def test_not_run_records_pass_false_with_the_reason(tmp_path):
    rec = CaseRecorder(tmp_path / "cases.json")
    out = rec.not_run("R11b", "compact not due", "context already above the threshold")
    assert out["pass"] is False
    assert out["note"] == "not run: context already above the threshold"
    assert rec.failed() == ["R11b"]
    assert rec.has("R11b") and not rec.has("R11c")


def test_a_genuine_pass_is_still_a_pass_and_is_persisted(tmp_path):
    p = tmp_path / "cases.json"
    rec = CaseRecorder(p)
    rec.record("R2e", "bind succeeds", "real", True, {"revision": 1})
    rec.record("R3", "literal delivery", "real", False, {})
    on_disk = json.loads(p.read_text())
    assert [c["pass"] for c in on_disk] == [True, False]
    assert rec.failed() == ["R3"]


def test_falsy_evidence_does_not_become_a_pass(tmp_path):
    rec = CaseRecorder(tmp_path / "cases.json")
    assert rec.record("R1", "discover", "real", None, {})["pass"] is False
    assert rec.record("R1b", "discover", "real", [], {})["pass"] is False


def test_unknown_case_kind_is_rejected(tmp_path):
    rec = CaseRecorder(tmp_path / "cases.json")
    with pytest.raises(ValueError):
        rec.record("R1", "x", "simulated", True, {})


# --------------------------------------------------------------- call-log counting
def test_count_calls_separates_simulated_from_real():
    log = [
        {"args": ["send", "--surface", U1, "--", "hi"], "simulated": False},
        {
            "args": ["send", "--surface", U1, "--", "hi"],
            "simulated": True,
            "fault": "before",
        },
        {"args": ["send-key", "--surface", U1, "Enter"], "simulated": False},
        {"args": ["send", "--surface", U1, "--", "/compact"], "simulated": False},
    ]
    assert count_calls(log, "send") == 3
    assert count_calls(log, "send", simulated=False) == 2
    assert count_calls(log, "send", simulated=True) == 1
    assert count_calls(log, "send-key") == 1
    assert count_calls(log, "send", contains="/compact") == 1
    assert count_calls([], "send") == 0


# ---------------------------------------- delivered-prompt acceptance count (seq169 fact 2)
# Mirrors the REAL run#2 child transcript shape (checkpoint rev32 fixture): one genuine delivered
# brief (isMeta absent), one internal `workflow-authoring` skill command-message turn (isMeta=True),
# its skill-body turn (isMeta=True), and several tool-result injections (toolUseResult present).
DELIVERED = "Task brief: /tmp/wf39.42b503/state/tasks/b-734bdd4ff51a/r3-literal.txt"


def _run2_shaped_records():
    return (
        [
            {
                "type": "user",
                "uuid": "56140349-4ffc-4ccd-b577-1f112e7efb0a",
                "message": {"role": "user", "content": DELIVERED},
            },
            {  # internal skill command-message turn — fires UserPromptSubmit but is NOT a delivery
                "type": "user",
                "uuid": "bff27831-a30c-49c4-911d-a4a4b7309884",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": "<command-message>workflow-authoring</command-message>\n"
                    "<command-name>workflow-authoring</command-name>\n<skill-format>true</skill-format>",
                },
            },
            {  # skill-body turn (isMeta) — list-of-text content, still excluded
                "type": "user",
                "uuid": "324ae376-df82-47b3-9590-ef2e1858d932",
                "isMeta": True,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "# Workflow authoring reference\n\n...",
                        }
                    ],
                },
            },
        ]
        + [
            {  # tool-result injections are not prompt submissions
                "type": "user",
                "uuid": u,
                "toolUseResult": {"stdout": ""},
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "ok"}],
                },
            }
            for u in (
                "ec275c81-1529-4e1b-a7b2-8b5ba72283a8",
                "19a15cf8-1849-4490-aecd-3cbd54dc17b5",
                "02d85d46-f71f-4a4f-bf0d-9710d5f70997",
                "cba87624-4d9f-411e-98ed-0a8d932ea7e6",
            )
        ]
    )


def test_delivered_count_is_one_despite_internal_skill_turn():
    """seq169 fact 2 regression: raw UserPromptSubmit was 2 (brief + skill turn); the authoritative
    distinct-UUID exact-payload count of a single correct delivery is 1."""
    recs = _run2_shaped_records()
    assert count_delivered_prompts(recs, DELIVERED) == 1


def test_delivered_count_matches_by_sha256_fallback():
    recs = _run2_shaped_records()
    sha = sha256_bytes(DELIVERED.encode())
    assert count_delivered_prompts(recs, None, sha) == 1
    assert count_delivered_prompts(recs, "totally-different-text", sha) == 1


def test_a_genuine_duplicate_delivery_is_still_detected():
    """Two distinct-UUID user records with identical delivered text -> count 2 (duplicate flagged),
    so accepts==1 correctly fails. This preserves duplicate detection."""
    recs = _run2_shaped_records()
    recs.append(
        {
            "type": "user",
            "uuid": "deadbeef-0000-0000-0000-000000000001",
            "message": {"role": "user", "content": DELIVERED},
        }
    )
    assert count_delivered_prompts(recs, DELIVERED) == 2


def test_reread_of_same_uuid_never_inflates_count():
    recs = _run2_shaped_records()
    recs.append(recs[0])  # exact same record (same uuid) appearing twice
    assert count_delivered_prompts(recs, DELIVERED) == 1


def test_meta_and_tool_result_and_nonmatching_text_are_excluded():
    recs = _run2_shaped_records()
    # skill command-message text must never be counted even if asked for verbatim
    assert (
        count_delivered_prompts(
            recs,
            "<command-message>workflow-authoring</command-message>\n"
            "<command-name>workflow-authoring</command-name>\n<skill-format>true</skill-format>",
        )
        == 0
    )
    assert count_delivered_prompts(recs, "no such prompt was ever delivered") == 0
    assert count_delivered_prompts([], DELIVERED) == 0
    assert (
        count_delivered_prompts(recs, "") == 0
    )  # empty delivered_text with no sha -> 0


def test_inline_single_line_delivery_is_counted():
    """R9/R10 inline (raw) single-line deliveries land as string user records too."""
    recs = [
        {
            "type": "user",
            "uuid": "aaaa1111-0000-0000-0000-000000000001",
            "message": {
                "role": "user",
                "content": "Reply with the single word DONE-R9.",
            },
        }
    ]
    assert count_delivered_prompts(recs, "Reply with the single word DONE-R9.") == 1
    assert count_delivered_prompts(recs, "Reply with the single word DONE-R10.") == 0


def test_user_record_text_shapes():
    assert user_record_text("plain string prompt") == "plain string prompt"
    assert (
        user_record_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        == "ab"
    )
    # any non-text block (tool_result, image, ...) means it is not a plain prompt
    assert user_record_text([{"type": "tool_result", "content": "x"}]) is None
    assert user_record_text([{"type": "text", "text": "a"}, {"type": "image"}]) is None
    assert user_record_text([]) is None
    assert user_record_text(None) is None
    assert user_record_text(123) is None


# ---------------------------------------- grounded drain check (seq178, item 4)
# Mirrors the REAL child transcript shape (checkpoint rev32 fixture + live3 run): assistant records
# carry `tool_use` content blocks; user records carry `tool_result` blocks matched by tool_use_id.


def _asst(tid, name, inp=None):
    return {
        "type": "assistant",
        "uuid": f"a-{tid}",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": inp or {}}
            ],
        },
    }


def _result(tid):
    return {
        "type": "user",
        "uuid": f"r-{tid}",
        "toolUseResult": {"stdout": "ok"},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}],
        },
    }


def test_drain_clean_all_foreground_matched_is_drained():
    recs = [
        _asst("toolu_1", "Read", {"file_path": "/x"}),
        _result("toolu_1"),
        _asst("toolu_2", "Write", {"file_path": "/y", "content": "z"}),
        _result("toolu_2"),
        {"type": "assistant", "message": {"role": "assistant", "content": "DONE-R3"}},
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is True
    assert d["tool_use"] == 2 and d["tool_result"] == 2
    assert d["unmatched_tool_use"] == [] and d["background_or_async"] == []


def test_drain_unmatched_tool_use_is_not_drained_live3_modal_cascade():
    """live3 R3: 6 tool_use but the final Bash (sed/wc/xxd inspection) hit the approval modal and
    never produced a tool_result -> a foreground call still in flight -> NOT drained (fail-closed)."""
    recs = [
        _asst("toolu_1", "Read", {"file_path": "/x"}),
        _result("toolu_1"),
        _asst("toolu_w", "Write", {"file_path": "/received.txt", "content": "p"}),
        _result("toolu_w"),
        _asst(
            "toolu_stuck", "Bash", {"command": "sed -n 1p f | xxd | tail"}
        ),  # no result
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is False
    assert d["unmatched_tool_use"] == ["toolu_stuck"]


def test_drain_backgrounded_bash_is_not_drained_even_if_result_present():
    """A Bash run_in_background=True returns a tool_result immediately while its shell keeps running,
    so a matched result does NOT prove drain (item 4: backgrounding exists)."""
    recs = [
        _asst("toolu_bg", "Bash", {"command": "sleep 100", "run_in_background": True}),
        _result("toolu_bg"),
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is False
    assert d["background_or_async"] == [
        {"id": "toolu_bg", "name": "Bash", "reason": "input_run_in_background"}
    ]
    assert (
        d["unmatched_tool_use"] == []
    )  # it IS matched; background is the disqualifier


def test_drain_task_subagent_is_not_drained():
    recs = [_asst("toolu_task", "Task", {"prompt": "go"}), _result("toolu_task")]
    d = drain_check_transcript(recs)
    assert d["drained"] is False
    assert d["background_or_async"] == [
        {"id": "toolu_task", "name": "Task", "reason": "subagent_spawn"}
    ]


def _result_bg(tid, task_id="b1"):
    """A tool_result whose metadata marks the task as backgrounded (Claude Code sets
    toolUseResult.backgroundTaskId), even when the tool_use input had no explicit flag."""
    return {
        "type": "user",
        "uuid": f"r-{tid}",
        "toolUseResult": {"stdout": "", "backgroundTaskId": task_id},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": ""}],
        },
    }


def test_drain_shell_trailing_ampersand_is_not_drained_seq184_hole1():
    """`sleep 100 &` backgrounds within the shell and returns a foreground tool_result, but the work
    continues — a matched result alone must NOT prove drain (supervisor seq184 hole 1)."""
    recs = [
        _asst("toolu_amp", "Bash", {"command": "sleep 100 &"}),
        _result("toolu_amp"),
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is False
    assert d["background_or_async"] == [
        {"id": "toolu_amp", "name": "Bash", "reason": "shell_background_operator"}
    ]
    # `&&` is logical-and, not backgrounding
    ok = drain_check_transcript(
        [_asst("toolu_and", "Bash", {"command": "a && b"}), _result("toolu_and")]
    )
    assert ok["drained"] is True


def test_drain_result_backgroundtaskid_is_not_drained_seq184_hole2():
    """A Bash with no run_in_background flag whose RESULT carries backgroundTaskId is backgrounded
    (supervisor seq184 hole 2)."""
    recs = [
        _asst("toolu_b1", "Bash", {"command": "long_job.sh"}),
        _result_bg("toolu_b1", "b1"),
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is False
    assert d["background_or_async"] == [
        {"id": "toolu_b1", "name": "Bash", "reason": "result_backgroundTaskId"}
    ]


def test_drain_backgrounded_agent_is_not_drained_seq184_hole3():
    """Agent(run_in_background=True) with a matched result is still async work (supervisor seq184
    hole 3); Agents run in the background by default, so a bare Agent is async too."""
    recs = [
        _asst("toolu_ag", "Agent", {"prompt": "go", "run_in_background": True}),
        _result("toolu_ag"),
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is False
    # input_run_in_background is checked before subagent_spawn; either way it is flagged
    assert d["background_or_async"][0]["id"] == "toolu_ag"
    assert d["background_or_async"][0]["name"] == "Agent"
    # a bare Agent (no explicit flag) is still async
    bare = drain_check_transcript(
        [_asst("toolu_ag2", "Agent", {"prompt": "go"}), _result("toolu_ag2")]
    )
    assert bare["drained"] is False
    assert bare["background_or_async"][0]["reason"] == "subagent_spawn"


def test_drain_no_tools_or_empty_is_trivially_drained():
    assert drain_check_transcript([])["drained"] is True
    # a reply-only turn with string content and no tool calls -> nothing pending
    recs = [
        {"type": "assistant", "message": {"role": "assistant", "content": "PILOT-DONE"}}
    ]
    assert drain_check_transcript(recs)["drained"] is True


def test_drain_is_robust_to_malformed_records():
    recs = [
        None,
        123,
        {"type": "assistant", "message": {"role": "assistant", "content": None}},
        {"type": "user", "message": {}},
        _asst("toolu_1", "Read", {"file_path": "/x"}),
        _result("toolu_1"),
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is True and d["tool_use"] == 1 and d["tool_result"] == 1


# ---------------------------------------- bash drain classification (seq185/186)
def test_bash_status_background_operator_anywhere_not_just_trailing():
    # backgrounding: a job-control '&' returns while the job runs
    assert _bash_drain_status("sleep 100 &") == "background"
    assert _bash_drain_status("sleep 100 & echo launched") == "background"  # seq185
    assert _bash_drain_status("sleep 100 & # still running") == "background"  # seq185
    assert _bash_drain_status("nohup ./svc &") == "background"


def test_bash_status_foreground_supports_control_redirects_and_quoted_data():
    assert _bash_drain_status("a && b") == "foreground"  # logical-and control
    assert _bash_drain_status("make && ./run") == "foreground"
    assert _bash_drain_status('echo "a & b"') == "foreground"  # quoted data
    assert _bash_drain_status("grep '&' file") == "foreground"  # quoted data
    assert _bash_drain_status("cmd 2>&1") == "foreground"  # redirect
    assert _bash_drain_status("cmd &> log") == "foreground"  # redirect
    assert _bash_drain_status("cat received.txt") == "foreground"
    assert (
        _bash_drain_status("wc -c received.txt") == "foreground"
    )  # -c flag, not interpreter
    assert _bash_drain_status("grep -c pattern f") == "foreground"
    assert _bash_drain_status("plain command") == "foreground"


def test_bash_status_unknown_for_substitutions_and_interpreter_strings_seq186():
    # backgrounding hidden inside an executable substitution -> unknown, not fail-open
    assert (
        _bash_drain_status('echo "$(sleep 100 >/dev/null 2>&1 & echo launched)"')
        == "unknown"
    )
    assert (
        _bash_drain_status("x=$(git rev-parse HEAD)") == "unknown"
    )  # bounded: refuse subst
    assert _bash_drain_status("cat `ls`") == "unknown"  # backtick subst
    assert _bash_drain_status("diff <(a) <(b)") == "unknown"  # process subst
    assert _bash_drain_status("sh -c 'sleep 100 &'") == "unknown"  # interpreter string
    assert _bash_drain_status('bash -c "do_stuff"') == "unknown"
    assert _bash_drain_status("python3 -c 'print(1)'") == "unknown"
    assert _bash_drain_status(None) == "unknown"


def test_drain_mid_command_background_operator_is_not_drained_seq185():
    recs = [
        _asst("toolu_mid", "Bash", {"command": "sleep 100 & echo launched"}),
        _result("toolu_mid"),
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is False
    assert d["background_or_async"][0]["reason"] == "shell_background_operator"
    # a synchronous && with the same tokens must stay drained
    ok = drain_check_transcript(
        [_asst("toolu_sync", "Bash", {"command": "a && b"}), _result("toolu_sync")]
    )
    assert ok["drained"] is True


def test_drain_unknown_shell_form_is_not_drained_seq186():
    """A backgrounding hidden inside command substitution (or an interpreter string) must fail
    CLOSED as unknown, even with a matched foreground result (supervisor seq186)."""
    recs = [
        _asst(
            "toolu_sub",
            "Bash",
            {"command": 'echo "$(sleep 100 >/dev/null 2>&1 & echo launched)"'},
        ),
        _result("toolu_sub"),
    ]
    d = drain_check_transcript(recs)
    assert d["drained"] is False
    assert d["background_or_async"][0]["reason"] == "shell_unknown_form"
    # interpreter string likewise
    d2 = drain_check_transcript(
        [
            _asst("toolu_i", "Bash", {"command": "sh -c 'sleep 100 &'"}),
            _result("toolu_i"),
        ]
    )
    assert d2["drained"] is False
    assert d2["background_or_async"][0]["reason"] == "shell_unknown_form"
    # a plain synchronous inspection utility remains drained (supported scope)
    ok = drain_check_transcript(
        [
            _asst("toolu_ok", "Bash", {"command": "wc -c received.txt"}),
            _result("toolu_ok"),
        ]
    )
    assert ok["drained"] is True


# ---------------------------------------- Runner drain-attestation transcript-availability (hole 4)
class _FakeStore:
    def __init__(self, rec):
        self._rec = rec

    def read(self, rel):
        return self._rec


class _FakeB:
    def __init__(self, rec, idle="satisfied"):
        self.store = _FakeStore(rec)
        self._idle = idle

    def wait(self, bid, ctl, until, timeout_s=30, **kw):
        return {"outcome": self._idle}


class _StubRunner:
    # bind the real method under test onto a minimal stub (no live bridge/cmux)
    _fixture_drain_attestation = Runner._fixture_drain_attestation

    def __init__(self, records, dpc):
        self.bid = "b-test"
        self.ctl = "ctrl-A"
        self.meta = {"session_id": "claude-test"}
        self._records = records
        self._dpc = dpc  # (accepts, delivered_text, records_scanned)

    def _read_transcript_records(self):
        return self._records

    def delivered_prompt_count(self, B, req_id, **kw):
        return self._dpc


ACCEPTED = {"accepted_at": "2026-09-09T22:00:00.000+00:00", "delivered_text": "hi"}


def test_attestation_refuses_when_transcript_unavailable_hole4():
    """records_scanned==0 => the transcript is not readable/flushed; a vacuous no-tools drain must
    NOT be attested (supervisor seq184 hole 4)."""
    stub = _StubRunner(records=[], dpc=(0, None, 0))
    assert stub._fixture_drain_attestation(_FakeB(ACCEPTED), "r") is None


def test_attestation_refuses_when_accepted_request_not_in_transcript_hole4():
    """The transcript has records but not the accepted request (accepts<1) => not groundable."""
    stub = _StubRunner(records=[{"type": "user"}], dpc=(0, "hi", 5))
    assert stub._fixture_drain_attestation(_FakeB(ACCEPTED), "r") is None


def test_attestation_refuses_when_not_accepted():
    stub = _StubRunner(records=[], dpc=(1, "hi", 3))
    assert stub._fixture_drain_attestation(_FakeB({}), "r") is None  # no accepted_at


def test_attestation_grounds_reply_only_completed_request():
    """Request present + reply-only (no tool calls) + idle => a real grounded attestation."""
    recs = [
        {"type": "assistant", "message": {"role": "assistant", "content": "PILOT-DONE"}}
    ]
    stub = _StubRunner(records=recs, dpc=(1, "hi", 1))
    att = stub._fixture_drain_attestation(_FakeB(ACCEPTED), "r")
    assert att is not None and att["drained"] is True
    assert att["attested_by"] == "wfi-live-acceptance-fixture"
    assert att["evidence"]["delivered_prompt_present"] == 1
    assert att["evidence"]["background_or_async"] == []
    assert att["at"] >= ACCEPTED["accepted_at"]


def test_attestation_refuses_when_present_but_not_drained():
    """Request present but a backgrounded Bash is pending => not drained => None (not attested)."""
    recs = [
        _asst("toolu_bg", "Bash", {"command": "sleep 100", "run_in_background": True}),
        _result("toolu_bg"),
    ]
    stub = _StubRunner(records=recs, dpc=(1, "hi", 2))
    assert stub._fixture_drain_attestation(_FakeB(ACCEPTED), "r") is None


def test_attestation_refuses_when_prompt_not_idle():
    recs = [{"type": "assistant", "message": {"role": "assistant", "content": "DONE"}}]
    stub = _StubRunner(records=recs, dpc=(1, "hi", 1))
    assert (
        stub._fixture_drain_attestation(_FakeB(ACCEPTED, idle="timed_out"), "r") is None
    )


# --------------------------------------------------- teardown stage-to-Enter guard (seq204)
# codex-fixture-stage-guard-review-r3: teardown must reuse staged_is_ours("/exit", fresh) on a
# screen read IMMEDIATELY before Enter, because a permission modal (or a foreign draft) can appear
# AFTER `send /exit` and BEFORE Enter, and Enter onto a modal approves its default (the preflight2
# artifact). These bind the REAL Runner.teardown onto a minimal stub and script the screen states;
# no cmux, no claude, no network. wait_event returns a SessionEnd only when reached (success path).
class _RecCLI:
    def __init__(self):
        self.calls = []

    def run_ok(self, *args):
        self.calls.append(tuple(args))
        return ("", "", 0)


class _RecBridge:
    def _latest(self):
        return (None, 4242)


class _RecRec:
    def __init__(self):
        self.entries = []

    def record(self, cid, label, kind, ok, evidence=None, note="", supported=True):
        self.entries.append({"id": cid, "ok": bool(ok), "evidence": evidence or {}})
        return self.entries[-1]


class _TeardownRunner:
    teardown = Runner.teardown  # the real method under test

    def __init__(self, states):
        self.session_id = "claude-xyz"
        self.surf = "SURF-A"
        self._states = list(states)
        self.cli = _RecCLI()
        self.bridge = _RecBridge()
        self.owned = types.SimpleNamespace(uuids=[], closed=set())
        self.rec = _RecRec()
        self.wait_calls = 0

    def state(self):
        return self._states.pop(0)

    def wait_event(self, name, pred, latest, timeout):
        self.wait_calls += 1
        return {"seq": 777}

    def write_outputs(self):
        pass

    def close_owned(self, s):  # pragma: no cover - no owned surfaces in these stubs
        pass


def _sent_exit(r):
    return any(c[0] == "send" and c[-1] == "/exit" for c in r.cli.calls)


def _sent_enter(r):
    return any(c[0] == "send-key" and c[-1] == "Enter" for c in r.cli.calls)


def _ctrl_c(r):
    return sum(1 for c in r.cli.calls if c[0] == "send-key" and c[-1] == "ctrl+c")


@pytest.fixture
def no_sleep(monkeypatch):
    import live_acceptance as la

    monkeypatch.setattr(la.time, "sleep", lambda *a, **k: None)


def test_teardown_idle_success_presses_enter_on_our_own_exit(no_sleep):
    """Case 1 (idle success): /exit stages as ours on an idle prompt -> Enter -> SessionEnd."""
    r = _TeardownRunner(
        [
            {"state": "prompt_idle"},
            {"state": "staged", "staged_text": "/exit", "continuation": []},
        ]
    )
    r.teardown()
    assert _sent_exit(r) and _sent_enter(r)
    assert _ctrl_c(r) == 0
    assert r.wait_calls == 1
    e = r.rec.entries[-1]
    assert e["id"] == "R16-teardown" and e["ok"] is True
    assert e["evidence"]["staged_is_ours"] is True
    assert e["evidence"]["session_end_seq"] == 777


def test_teardown_persistent_modal_never_sends_exit_or_enter(no_sleep):
    """Case 2 (persistent modal): never reaches idle -> never stages /exit, never Enter."""
    r = _TeardownRunner([{"state": "modal"}, {"state": "modal"}])
    r.teardown()
    assert not _sent_exit(r) and not _sent_enter(r)
    assert _ctrl_c(r) >= 1  # a decline, never Enter
    assert r.wait_calls == 0
    e = r.rec.entries[-1]
    assert e["ok"] is False and e["evidence"]["pre_exit_state"] == "modal"


def test_teardown_modal_after_stage_refuses_enter_and_declines(no_sleep):
    """Case 3 (modal after stage): a modal appears after send /exit -> Enter refused, ctrl+c decline."""
    r = _TeardownRunner([{"state": "prompt_idle"}, {"state": "modal"}])
    r.teardown()
    assert _sent_exit(r) and not _sent_enter(
        r
    )  # staged, but Enter refused onto the modal
    assert _ctrl_c(r) >= 1
    assert r.wait_calls == 0
    e = r.rec.entries[-1]
    assert e["ok"] is False
    assert e["evidence"]["pre_enter_state"] == "modal"
    assert e["evidence"]["staged_is_ours"] is False
    assert e["evidence"]["staged_guard"]["rule"] == "screen_not_staged"


def test_teardown_foreign_draft_after_stage_refuses_enter(no_sleep):
    """Case 4 (foreign draft): a foreign single line replaces our /exit -> Enter refused."""
    r = _TeardownRunner(
        [
            {"state": "prompt_idle"},
            {"state": "staged", "staged_text": "rm -rf /tmp/x", "continuation": []},
        ]
    )
    r.teardown()
    assert _sent_exit(r) and not _sent_enter(r)
    assert _ctrl_c(r) >= 1
    assert r.wait_calls == 0
    e = r.rec.entries[-1]
    assert e["ok"] is False
    assert e["evidence"]["staged_is_ours"] is False
    assert e["evidence"]["staged_guard"]["rule"] == "exact_single_line"


def test_teardown_foreign_continuation_row_refuses_enter(no_sleep):
    """Bonus: /exit staged but a foreign continuation row follows -> ambiguous -> Enter refused."""
    r = _TeardownRunner(
        [
            {"state": "prompt_idle"},
            {
                "state": "staged",
                "staged_text": "/exit",
                "continuation": ["  rm -rf /tmp"],
            },
        ]
    )
    r.teardown()
    assert _sent_exit(r) and not _sent_enter(r)
    e = r.rec.entries[-1]
    assert e["ok"] is False
    assert e["evidence"]["staged_guard"]["rule"] == "ambiguous_continuation_row"


def test_r3_copy_command_extracts_payload_byte_exact_adversarial_path(tmp_path):
    """The rev2 R3 byte-copy (seq221) reproduces the 169B PAYLOAD verbatim via ONE allowlisted sed
    extraction from the executor's own delivered task file — no model retyping (which drops trailing
    whitespace). Both path operands are shlex-quoted, so a scratch/state path containing a space AND
    an apostrophe still resolves to one argv token (path-quoting review seq225); the anchored
    ^<<<BEGIN>>>$ / ^<<<END>>>$ markers extract only the payload, never the command line that echoes
    the marker regex in prose. The strict exact-169B sha256 oracle must hold."""
    import hashlib
    import subprocess

    from live_acceptance import PAYLOAD, _r3_copy_command

    d = tmp_path / "a b's dir" / "tasks" / "BIND"  # name has BOTH a space and an apostrophe
    d.mkdir(parents=True)
    taskfile = d / "r3-literal.txt"
    recv = tmp_path / "a b's dir" / "received.txt"
    cmd = _r3_copy_command(str(taskfile), str(recv))
    # brief embeds the command in prose AND the marker-delimited payload (the real self-referential
    # shape): the extraction must ignore the echoed marker regex and pull only the payload.
    brief = (
        "Reproduce the payload deterministically by running exactly:\n"
        f"{cmd}\n"
        "<<<BEGIN>>>\n" + PAYLOAD + "<<<END>>>"
    )
    taskfile.write_bytes(brief.encode())
    subprocess.run(cmd, shell=True, check=True)
    got = recv.read_bytes()
    assert got == PAYLOAD.encode()
    assert len(got) == 169
    assert hashlib.sha256(got).hexdigest() == hashlib.sha256(PAYLOAD.encode()).hexdigest()


# --- R3c three-cause oracle neighbors (seq231/233) -------------------------------------------------
# R3c proves a reply QUOTING transport vocabulary is read as normal assistant output, not a transport
# failure. Its oracle rests on two pure pieces exercised here: assistant_reply_after_delivered (the
# four terms must appear in the child's PUBLIC assistant reply for the exact delivered prompt, so an
# echoed user prompt or a refusal cannot pass) and split_transport_errors (an expected finite
# events-poll timeout is tolerated; a real read-screen/send transport error fails).

_R3C_TERMS = ("Access denied", "internal_error", "not_found", "Broken pipe")


def _r3c_user(text, uuid="u-r3c"):
    return {"type": "user", "uuid": uuid, "message": {"content": text}}


def _r3c_assistant(text, uuid="a-r3c"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def test_r3c_echo_and_refusal_replies_do_not_satisfy_terms():
    """Neighbor 1 (FAIL): neither an echo of the user prompt nor a refusal can satisfy R3c. The four
    terms are correlated to the child's own ASSISTANT reply, and the delivered prompt is a `user`
    record — so terms present only in the prompt echo never leak into the reply, and a refusal reply
    carries none of them."""
    from live_acceptance import assistant_reply_after_delivered

    delivered = "benign transport test ... 'Access denied' | 'internal_error' | 'not_found' | 'Broken pipe'"
    # Echo-only: the delivered prompt (a user record) contains all four terms, but the assistant
    # reply that follows does not — the case must NOT pass on the echoed prompt.
    recs_echo = [
        _r3c_user(delivered),
        _r3c_assistant("Sure, happy to help with the transport test."),
    ]
    reply, found = assistant_reply_after_delivered(recs_echo, delivered)
    assert found is True
    assert not all(t in reply for t in _R3C_TERMS)  # -> terms_in_reply False -> FAIL

    # Refusal: the executor refuses (exactly the preflight5 failure) — reply carries no terms.
    recs_refusal = [
        _r3c_user(delivered),
        _r3c_assistant(
            "I won't output that — it reads as a fabricated system error message."
        ),
    ]
    reply2, found2 = assistant_reply_after_delivered(recs_refusal, delivered)
    assert found2 is True
    assert not all(t in reply2 for t in _R3C_TERMS)  # -> FAIL


def test_r3c_matched_reply_with_expected_events_poll_timeout_passes():
    """Neighbor 2 (PASS): the assistant reply carries all four quoted fixtures AND the only errored
    CLI call this case is a finite `events --no-heartbeat` poll timeout (tolerated by core._events).
    terms_in_reply is True and there are no REAL transport errors -> the oracle's data predicate holds."""
    from live_acceptance import (
        assistant_reply_after_delivered,
        split_transport_errors,
    )

    delivered = "benign transport round-trip test; reply with the four quoted fixtures."
    recs = [
        _r3c_user(delivered),
        _r3c_assistant(
            "'Access denied' | 'internal_error' | 'not_found' | 'Broken pipe'"
        ),
    ]
    reply, found = assistant_reply_after_delivered(recs, delivered)
    assert found is True
    terms_in_reply = all(t in reply for t in _R3C_TERMS)
    assert terms_in_reply is True

    log_slice = [
        {"args": ["events", "--after", "10", "--no-heartbeat"], "error_kind": "timeout"},
        {"args": ["read-screen", "--surface", "S"], "error_kind": None},  # a clean read
    ]
    expected, real = split_transport_errors(log_slice)
    assert len(expected) == 1 and real == []  # expected poll timeout retained, no real error
    assert terms_in_reply and not real  # combined data predicate -> PASS


def test_r3c_real_read_screen_transport_error_fails():
    """Neighbor 3 (FAIL): a real read-screen/send transport error is NOT the tolerated events-poll
    timeout — it is retained separately and fails the case even when an expected poll timeout is also
    present."""
    from live_acceptance import split_transport_errors

    log_slice = [
        {"args": ["events", "--after", "10", "--no-heartbeat"], "error_kind": "timeout"},
        {"args": ["read-screen", "--surface", "S"], "error_kind": "socket_missing"},
        {"args": ["send", "--surface", "S"], "error_kind": "timeout"},
    ]
    expected, real = split_transport_errors(log_slice)
    assert len(expected) == 1  # the events poll timeout is still recognised as expected
    assert len(real) == 2  # read-screen socket_missing + send timeout are REAL transport errors
    assert any((e.get("args") or [""])[0] == "read-screen" for e in real)
    assert not (not real)  # -> transport_errors non-empty -> FAIL


# --- full40 harness-cause regression neighbors (seq239/240) ----------------------------------------
# R11c failed because evidence.sh serialized each hook object across two lines and the per-line
# json.loads silently dropped both fragments (kinds=[]), reading a real compaction as missing
# dispatch. parse_hook_evidence recovers complete objects from the concatenated stream (old OR new
# format) and retains parse-failure stats. R9 failed because the oracle required an OPTIONAL
# `reconciled` marker the core's simulated-pre-send branch never emits; r9_redelivered_exactly_once
# asserts the substantive one-send/one-delivery property instead.


def test_parse_hook_evidence_recovers_old_two_line_format():
    """The historical writer closed the outer object on a SECOND line; the record is still valid JSON
    once whitespace is ignored, so raw_decode over the stream recovers it (per-line json.loads did
    not) -- this is the exact R11c PreCompact-missing bug."""
    from live_acceptance import parse_hook_evidence

    old = (
        '{"event":"PreCompact","at":"T","payload":{"trigger":"manual"}\n}\n'
        '{"event":"SessionStart","at":"T2","payload":{"source":"compact"}\n}\n'
    )
    kinds, records, stats = parse_hook_evidence(old)
    assert kinds == ["PreCompact", "SessionStart"]
    assert stats["objects"] == 2 and stats["recovered_all"] is True
    assert stats["decode_errors"] == 0 and stats["remainder_len"] == 0


def test_parse_hook_evidence_recovers_new_one_line_format():
    """The fixed writer emits one complete record per line (empty stdin -> payload null)."""
    from live_acceptance import parse_hook_evidence

    new = (
        '{"event":"PreCompact","at":"T","payload":{"trigger":"manual"}}\n'
        '{"event":"Stop","at":"T2","payload":null}\n'
    )
    kinds, records, stats = parse_hook_evidence(new)
    assert kinds == ["PreCompact", "Stop"] and stats["recovered_all"] is True


def test_parse_hook_evidence_retains_failure_evidence_not_silent_empty():
    """Malformed-but-nonempty data must surface as a decode failure, never a silent 'no hooks fired':
    the recoverable object is kept AND the failure is recorded (decode_errors/remainder, recovered_all
    False) so a broken stream can never read as a clean absence of dispatch."""
    from live_acceptance import parse_hook_evidence

    bad = '{"event":"PreCompact","payload":{"a":1}}\n{"event":"Stop" garbage no close\n'
    kinds, records, stats = parse_hook_evidence(bad)
    assert "PreCompact" in kinds
    assert stats["recovered_all"] is False
    assert stats["decode_errors"] > 0 or stats["remainder_len"] > 0
    # empty input is a clean, honest zero (distinct from a decode failure)
    k2, r2, s2 = parse_hook_evidence("")
    assert k2 == [] and s2["recovered_all"] is True and s2["bytes_total"] == 0


def test_r9_redelivered_exactly_once_matches_real_evidence_without_reconciled_marker():
    """The R9 receipt predicate holds on the ACTUAL evidence shape (lost-before -> re-delivered ->
    accepted once, one real send) with NO `reconciled` flag present, and fails when any substantive
    part is wrong."""
    from live_acceptance import r9_redelivered_exactly_once

    first = "BridgeError send_not_attempted"
    rec1 = {"send_result": "not_attempted_simulated"}
    r9_ok = {"status": "accepted"}  # note: NO "reconciled" key -- optional marker absent
    assert r9_redelivered_exactly_once(first, rec1, r9_ok, accepts=1, real_sends_delta=1) is True
    # two deliveries -> not "exactly once"
    assert r9_redelivered_exactly_once(first, rec1, r9_ok, accepts=2, real_sends_delta=1) is False
    # retry added no real send -> nothing actually re-delivered
    assert r9_redelivered_exactly_once(first, rec1, r9_ok, accepts=1, real_sends_delta=0) is False
    # first call did not register the simulated zero-byte loss
    assert r9_redelivered_exactly_once("no exception", rec1, r9_ok, accepts=1, real_sends_delta=1) is False
    assert r9_redelivered_exactly_once(first, {"send_result": "ok"}, r9_ok, 1, 1) is False


# --- seq243 boundedness/false-pass neighbors -------------------------------------------------------
# R11c must not pass on malformed evidence, and SessionStart correlation must not accept the STARTUP
# event as this compaction's. R6-turn-complete must not false-pass from a satisfied turn when the
# executor never actually ran the bounded copy.


def test_parse_hook_evidence_returns_records_for_source_correlation():
    """The decoder returns the full records so a caller can require SessionStart payload.source ==
    'compact' (this compaction) rather than the initial STARTUP SessionStart (seq243)."""
    from live_acceptance import parse_hook_evidence

    stream = (
        '{"event":"SessionStart","at":"T0","payload":{"source":"startup"}}\n'
        '{"event":"PreCompact","at":"T1","payload":{"trigger":"manual"}}\n'
        '{"event":"SessionStart","at":"T2","payload":{"source":"compact"}}\n'
    )
    kinds, records, stats = parse_hook_evidence(stream)
    assert kinds == ["SessionStart", "PreCompact", "SessionStart"]
    startup = any(
        r.get("event") == "SessionStart" and r["payload"].get("source") == "startup"
        for r in records
    )
    compact = any(
        r.get("event") == "SessionStart" and r["payload"].get("source") == "compact"
        for r in records
    )
    assert startup and compact
    # the R11c correlation rule: a startup SessionStart alone must NOT satisfy "this compaction"
    only_startup = (
        '{"event":"SessionStart","at":"T0","payload":{"source":"startup"}}\n'
        '{"event":"PreCompact","at":"T1","payload":{"trigger":"manual"}}\n'
    )
    _, recs2, _ = parse_hook_evidence(only_startup)
    assert not any(
        r.get("event") == "SessionStart" and r["payload"].get("source") == "compact"
        for r in recs2
    )


def test_r11c_gate_fails_on_unrecovered_evidence():
    """Malformed-but-nonempty evidence (recovered_all False) must fail the R11c gate, never pass — the
    parse-failure is retained, not silently read as a clean stream (seq243)."""
    from live_acceptance import parse_hook_evidence

    bad = '{"event":"PreCompact","payload":{"a":1}}\n{"event":"SessionStart" broken no close\n'
    kinds, records, stats = parse_hook_evidence(bad)
    # PreCompact present, but the stream did not fully recover -> the gate's recovered_all term is False
    assert "PreCompact" in kinds and stats["recovered_all"] is False
    # emulate the ok_c recovered_all term
    assert (("PreCompact" in kinds) and stats["recovered_all"]) is False


def test_r6_completion_ok_requires_running_release_and_bytes():
    """R6-turn-complete passes only with real running + producer release + a nonempty finite copy AND
    a satisfied turn — never a satisfied turn from a tool that never ran (seq243)."""
    from live_acceptance import r6_completion_ok

    assert r6_completion_ok("satisfied", True, True, 17) is True
    assert r6_completion_ok("satisfied", False, False, 0) is False  # the exact false-pass root found
    assert r6_completion_ok("satisfied", True, False, 0) is False  # released but nothing copied
    assert r6_completion_ok("satisfied", False, True, 17) is False  # copied but never observed running
    assert r6_completion_ok("timeout", True, True, 17) is False  # turn did not complete


# --- seq246 case_r6 exception/cleanup + R11c valid-payload scoping neighbors ----------------------
# The seq243 case_r6 left the turn-complete wait and BOTH unlinks OUTSIDE `finally`, so an
# observe/submit/wait exception leaked the FIFO; and R11c accepted ANY PreCompact, including a
# `_raw`-wrapped malformed payload (valid outer JSON, so recovered_all stayed True). These exercise
# the ACTUAL cleanup path + the actual gate, not only the boolean helpers.


def test_r6_stream_fixture_cleans_up_on_normal_exit(tmp_path):
    """Entering the fixture and leaving WITHOUT releasing still unlinks both the FIFO and the out file
    (and closes the controller fd) — no leftover blocking FIFO (seq246)."""
    from live_acceptance import r6_stream_fixture

    fifo = tmp_path / "r6.fifo"
    outp = tmp_path / "r6.out"
    with r6_stream_fixture(fifo, outp, b"x" * 64) as stream:
        assert fifo.exists()  # created inside the fixture
        assert stream.released is False
    assert not fifo.exists() and not outp.exists()


def test_r6_stream_fixture_cleans_up_on_exception(tmp_path):
    """An exception raised inside the `with` (mimicking an observe/submit/wait failure) still unlinks
    the FIFO — the seq243 leak was that the wait and both unlinks sat OUTSIDE `finally` (seq246)."""
    from live_acceptance import r6_stream_fixture

    fifo = tmp_path / "r6.fifo"
    outp = tmp_path / "r6.out"
    with pytest.raises(RuntimeError):
        with r6_stream_fixture(fifo, outp, b"x" * 64):
            raise RuntimeError("observe/submit/wait blew up")
    assert not fifo.exists() and not outp.exists()


def test_r6_stream_fixture_buffers_bytes_for_a_late_reader(tmp_path):
    """Because the controller holds the FIFO O_RDWR, a release() BEFORE any reader buffers the exact
    payload, so a late `head` still reads it (producer-first copy; no thread deadline race, seq246)."""
    import os

    from live_acceptance import r6_stream_fixture

    fifo = tmp_path / "r6.fifo"
    outp = tmp_path / "r6.out"
    payload = (b"r6-finite-stream-fixture-" * 4)[:64]
    got = b""
    with r6_stream_fixture(fifo, outp, payload) as stream:
        stream.release()  # write BEFORE any reader opens
        assert stream.released is True
        rfd = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
        try:
            got = os.read(rfd, len(payload))
        finally:
            os.close(rfd)
    assert got == payload
    assert not fifo.exists()


class _FakeRec:
    def __init__(self):
        self.calls = []

    def not_run(self, *a, **k):
        self.calls.append(("not_run", a, k))

    def record(self, *a, **k):
        self.calls.append(("record", a, k))


class _FakeR6Self:
    def __init__(self, scratch):
        self.scratch = scratch
        self.bid = "bid-r6"
        self.ctl = "ctrl-A"
        self.rec = _FakeRec()

    def rev(self):
        return 1

    def expect(self, *a, **k):
        self.rec.calls.append(("expect", a, k))


def test_case_r6_cleans_fifo_when_submit_raises(tmp_path):
    """case_r6 ITSELF (not just r6_completion_ok) leaves no FIFO when the first bridge call inside the
    fixture raises — the exact seq246 concern: an exception before cleanup must not leak (seq246)."""
    from live_acceptance import Runner

    class _B:
        def submit(self, *a, **k):
            raise RuntimeError("submit failed before running")

    me = _FakeR6Self(tmp_path)
    with pytest.raises(RuntimeError):
        Runner.case_r6(me, _B())
    assert not (tmp_path / "r6.fifo").exists()
    assert not (tmp_path / "r6.out").exists()


def test_case_r6_no_running_records_not_run_and_cleans_up(tmp_path, monkeypatch):
    """When the executor never reaches a running turn, case_r6 records not_run (never r6b vs idle),
    records R6-turn-complete as a non-pass, and still cleans the FIFO on exit (seq243/246)."""
    import live_acceptance as la

    monkeypatch.setattr(la, "R6_RUNNING_DEADLINE_S", 0.02)
    monkeypatch.setattr(la, "R6_POLL_S", 0.0)

    class _B:
        def submit(self, *a, **k):
            return {"status": "accepted"}

        def observe(self, *a, **k):
            return {"state": "prompt_idle", "revision": 2}

    me = _FakeR6Self(tmp_path)
    la.Runner.case_r6(me, _B())
    names = [(c[0], c[1][0]) for c in me.rec.calls]
    assert ("not_run", "R6-busy") in names
    rec_r6tc = [
        c for c in me.rec.calls if c[0] == "record" and c[1][0] == "R6-turn-complete"
    ]
    assert rec_r6tc and rec_r6tc[0][1][3] is False  # ok6 is False (running never established)
    assert not (tmp_path / "r6.fifo").exists()
    assert not (tmp_path / "r6.out").exists()


def test_r11c_precompact_valid_rejects_raw_and_empty_payloads():
    """The R11c PreCompact gate accepts a real native payload but rejects a `_raw`-wrapped malformed
    payload and an empty/None payload — even though the writer's `_raw` wrapper is valid outer JSON so
    the decoder's recovered_all stays True (the seq246 hole) (seq246)."""
    from live_acceptance import parse_hook_evidence, precompact_valid_native

    native = '{"event":"PreCompact","at":"T1","payload":{"session_id":"s","trigger":"manual"}}\n'
    raw = '{"event":"PreCompact","at":"T1","payload":{"_raw":"not json at all"}}\n'
    empty = '{"event":"PreCompact","at":"T1","payload":{}}\n'
    none = '{"event":"PreCompact","at":"T1","payload":null}\n'

    _, recs_native, _ = parse_hook_evidence(native)
    _, recs_raw, st_raw = parse_hook_evidence(raw)
    _, recs_empty, _ = parse_hook_evidence(empty)
    _, recs_none, _ = parse_hook_evidence(none)

    assert precompact_valid_native(recs_native) is True
    # the `_raw` wrapper is itself valid JSON, so recovered_all stays True yet the gate must reject it
    assert st_raw["recovered_all"] is True
    assert precompact_valid_native(recs_raw) is False
    assert precompact_valid_native(recs_empty) is False
    assert precompact_valid_native(recs_none) is False
    assert precompact_valid_native([]) is False


# ---------------------------------------- Runner.new_surface realizes the view, restores focus
class _Res:
    def __init__(self, ok=True, stdout=""):
        self.ok, self.stdout = ok, stdout


class _SurfaceCLI:
    def __init__(self, focused_surface, ws):
        self.calls = []
        self._ident = {"focused": {"workspace_uuid": ws, "surface_uuid": focused_surface}}

    def run_ok(self, *args, **kw):
        self.calls.append(list(args))
        if args[0] == "identify":
            return json.dumps(self._ident)
        if args[0] == "new-surface":
            return json.dumps({"surface_ref": "surface:53"})
        if args[0] == "list-pane-surfaces":
            return LISTING
        raise AssertionError(args)


class _SurfaceRunner:
    new_surface = Runner.new_surface

    def __init__(self, tmp, prev, ws="AAAAAAAA-0000-4000-8000-000000000001", read_ok=True):
        self.ws = ws
        self.cli = _SurfaceCLI(prev, ws)
        self.owned = OwnedSurfaces(tmp / "owned.json")
        self.surface_focus = None
        self._read_ok = read_ok

    def cm(self, *args, **kw):
        self.cli.calls.append(list(args))
        return _Res(ok=self._read_ok if args[0] == "read-screen" else True)


def test_new_surface_is_created_focused_then_previous_surface_refocused(monkeypatch, tmp_path):
    monkeypatch.setattr("live_acceptance.time.sleep", lambda s: None)
    r = _SurfaceRunner(tmp_path, prev=U1)
    assert r.new_surface("/tmp/x") == U2
    ns = next(c for c in r.cli.calls if c[0] == "new-surface")
    assert ns[ns.index("--focus") + 1] == "true"
    assert ["focus-panel", "--panel", U1, "--workspace", r.ws] in r.cli.calls
    assert r.cli.calls.index(["read-screen", "--surface", U2, "--lines", "3"]) < r.cli.calls.index(
        ["focus-panel", "--panel", U1, "--workspace", r.ws]
    )
    assert r.surface_focus["view_realized"] and r.surface_focus["focus_restored"]
    assert not any(c[0] in ("select-workspace", "focus-window") for c in r.cli.calls)


def test_new_surface_restores_focus_from_the_real_identify_shape(monkeypatch, tmp_path):
    """Live cmux 0.64.25: identify's `focused` carries workspace_id/surface_id (not *_uuid) and only
    with --id-format both; reading *_uuid left prev None so R2e saw focus on the runner's surface."""
    monkeypatch.setattr("live_acceptance.time.sleep", lambda s: None)
    r = _SurfaceRunner(tmp_path, prev=U1)
    r.cli._ident = {"focused": {"workspace_id": r.ws.lower(), "surface_id": U1.lower(), "surface_ref": "surface:7"}}
    r.new_surface("/tmp/x")
    ident = next(c for c in r.cli.calls if c[0] == "identify")
    assert ident == ["identify", "--json", "--id-format", "both"]
    assert ["focus-panel", "--panel", U1, "--workspace", r.ws] in r.cli.calls
    assert r.surface_focus["previous_surface"] == U1 and r.surface_focus["focus_restored"]


def test_new_surface_does_not_restore_focus_from_another_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr("live_acceptance.time.sleep", lambda s: None)
    r = _SurfaceRunner(tmp_path, prev=U1)
    r.cli._ident = {"focused": {"workspace_id": "BBBBBBBB-0000-4000-8000-000000000002", "surface_id": U1}}
    r.new_surface("/tmp/x")
    assert not any(c[0] == "focus-panel" for c in r.cli.calls)


def test_new_surface_without_view_does_not_pretend_it_was_realized(monkeypatch, tmp_path):
    monkeypatch.setattr("live_acceptance.time.sleep", lambda s: None)
    r = _SurfaceRunner(tmp_path, prev=U1, read_ok=False)
    r.new_surface("/tmp/x")
    assert r.surface_focus["view_realized"] is False


def test_parse_surface_listing_accepts_the_selected_row_marker():
    # cmux marks the selected surface with a leading '* ' (seen live on 0.64.25 after --focus true).
    text = (
        "  surface:7 AAD2A729-7D32-488A-AC4C-20A4B8C79FB6  some title\n"
        "* surface:45 843001BA-3F8D-4365-B9EA-6EEAF0304DDC  scratch  [selected]\n"
    )
    m = parse_surface_listing(text)
    assert m["surface:45"] == "843001BA-3F8D-4365-B9EA-6EEAF0304DDC"
    assert m["surface:7"] == "AAD2A729-7D32-488A-AC4C-20A4B8C79FB6"
