#!/usr/bin/env python3
"""Launch or verify a Claude Code EXECUTOR session in cmux with an explicit, policy-checked model.

Each Codex session gets its own dedicated Claude Code session. Implementation runs on Opus by
default — or on Sonnet/Haiku when the operator names one — and never on a model silently inherited
from `~/.claude/settings.json`, and never through a bare `claude --dangerously-skip-permissions`
with no `--model`.

    executor_session.py launch --cwd <worktree> [--model opus|sonnet|haiku|<id>] [--purpose implementation|planning]
                               [--name <title>] [--window <id|ref>] [--focus true|false] [--session-id <uuid>]
                               [--subagent-model sonnet] [--permission-mode <mode>] [--wait-s 45] [--realize-timeout-s 10]
                               [--receipt <path>] [--dry-run]
    executor_session.py verify --surface <uuid> --pid <pid> [--session-id <uuid>] [--cwd <worktree>]
                               [--purpose implementation|planning] [--expected-model <m>]
    executor_session.py policy [--purpose P] [--model M]

`launch` creates a DEDICATED workspace (identity by UUIDs, never by window or focus) whose initial
command is the absolute `claude` binary with `--model <resolved>` and a pre-generated `--session-id`,
so the executor's exact native session identity is known BEFORE it starts. It exports
`CLAUDE_CODE_SUBAGENT_MODEL=<implementation model>` into that workspace so the executor's own
workers/subagents cannot inherit a planning model either; then it waits (bounded) for the Claude
prompt and footer and reports the REQUESTED model next to the RESOLVED footer model.
With `--focus false` (default) the user's focus is not taken: cmux gives an unfocused terminal no view
(the command would not even start), so launch makes a brief (~1s) selection of the new workspace to
create the view, then restores the previous selection unless the user already switched elsewhere.
The receipt records `view_realized` and `focus_restored`; if no view appears, launch fails with
`no_terminal_view` (exit 4) and types nothing. A trust dialog
is reported as `modal` and never auto-confirmed. A launch receipt (JSON) is written for the bridge.
Nothing is typed into the session. The default permission posture is
`--dangerously-skip-permissions`; pass `--permission-mode <mode>` to use another.

`verify` is the reuse gate for an ALREADY RUNNING session: footer model (live), process argv
(requested at launch), transcript tail (what answered) — the same policy the bridge's
`bridge_bind(purpose=…)` applies. Exit 0 = ok to use for that purpose; 3 = refused.

Exit codes: 0 ok | 1 cmux/transport failure | 2 usage or preflight | 3 model policy refusal |
4 launched but NOT verified (identity known; model/prompt not yet confirmed — verify before use).

Stdlib only; runs on the system python3 (3.9+). The policy block below is a verbatim mirror of
`bridge/cmux_bridge/model_policy.py` (test-enforced); edit it there first.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

# --- model policy: begin ---
PURPOSES = ("implementation", "planning")
DEFAULT_PURPOSE = "implementation"
KNOWN_FAMILIES = ("fable", "opus", "sonnet", "haiku")
DEFAULT_PLANNING_ONLY_FAMILIES = ("fable",)
DEFAULT_IMPLEMENTATION_MODEL = "opus"
PLANNING_ONLY_ENV = "TANDEM_PLANNING_ONLY_MODELS"
ALLOW_UNKNOWN_ENV = "TANDEM_ALLOW_UNKNOWN_MODEL"
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

# A family token bounded by non-letters, so 'claude-opus-5', 'opus', 'Opus 5', 'claude-fable-5-1[1m]'
# and 'Fable 5.1' all resolve, while unrelated words never do.
_FAMILY_RE = re.compile(r"(?<![A-Za-z])(fable|opus|sonnet|haiku)(?![A-Za-z])", re.IGNORECASE)
# The Claude Code status footer: '  Model: Opus 5 | Context: … Ctx Used: 11.6% | …'
FOOTER_MODEL_RE = re.compile(r"Model:\s*([^|]+?)\s*\|")
# `claude … --model opus …` or `--model=opus` on a process command line.
ARGV_MODEL_RE = re.compile(r"(?:^|\s)--model(?:=|\s+)(\S+)")


class ModelPolicyError(ValueError):
    """A refused model/purpose combination. `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


def planning_only_families() -> tuple:
    """Planning-only families from TANDEM_PLANNING_ONLY_MODELS (default 'fable'). Unset means the
    default; any malformed value (empty, unknown family, stray comma) raises ModelPolicyError."""
    raw = os.environ.get(PLANNING_ONLY_ENV)
    if raw is None:
        return DEFAULT_PLANNING_ONLY_FAMILIES
    parts = [p.strip().lower() for p in raw.split(",")]
    if not parts or any(p not in KNOWN_FAMILIES for p in parts):
        raise ModelPolicyError(
            "model_policy_config",
            f"{PLANNING_ONLY_ENV}={raw!r} must be a comma-separated list of {list(KNOWN_FAMILIES)}",
            env=PLANNING_ONLY_ENV,
            value=raw,
        )
    return tuple(dict.fromkeys(parts))


def implementation_families() -> tuple:
    """Known families that are not planning-only, in KNOWN_FAMILIES order."""
    planning = planning_only_families()
    return tuple(f for f in KNOWN_FAMILIES if f not in planning)


def allow_unknown_model() -> bool:
    """TANDEM_ALLOW_UNKNOWN_MODEL: default False; malformed raises ModelPolicyError."""
    raw = os.environ.get(ALLOW_UNKNOWN_ENV)
    if raw is None:
        return False
    v = raw.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ModelPolicyError(
        "model_policy_config",
        f"{ALLOW_UNKNOWN_ENV}={raw!r} must be one of {list(_TRUE + _FALSE)}",
        env=ALLOW_UNKNOWN_ENV,
        value=raw,
    )


def model_family(name) -> "str | None":
    """Family ('fable' | 'opus' | 'sonnet' | 'haiku') of a model spelled as a CLI alias, a model
    id, or the footer display name. None when the spelling names no known family."""
    if not name:
        return None
    m = _FAMILY_RE.search(str(name))
    return m.group(1).lower() if m else None


def footer_model(lines) -> "str | None":
    """The display name after 'Model:' on the LAST footer row of a screen read, or None."""
    for line in reversed(list(lines or [])):
        m = FOOTER_MODEL_RE.search(line)
        if m:
            return m.group(1).strip()
    return None


def argv_model(command_line) -> "str | None":
    """The value of `--model` on a process command line, or None when absent (inherited)."""
    if not command_line:
        return None
    m = ARGV_MODEL_RE.search(str(command_line))
    return m.group(1) if m else None


def transcript_model(entries) -> "str | None":
    """`message.model` of the LAST assistant entry in a transcript tail, or None."""
    for e in reversed(list(entries or [])):
        if not isinstance(e, dict) or e.get("type") != "assistant":
            continue
        msg = e.get("message")
        if isinstance(msg, dict) and msg.get("model"):
            return str(msg["model"])
    return None


def resolve_launch_model(purpose, requested) -> dict:
    """Decide the explicit `--model` for a NEW session, or raise ModelPolicyError.

    implementation: default 'opus'; any implementation-family spelling is accepted as given; planning-only
    families and (unless TANDEM_ALLOW_UNKNOWN_MODEL) unknown spellings are refused (fail closed — nothing is ever launched on an inherited default).
    planning: the model must be named explicitly (no silent default); any known family is allowed.
    """
    if purpose not in PURPOSES:
        raise ModelPolicyError(
            "bad_purpose", f"purpose must be one of {list(PURPOSES)}", purpose=purpose
        )
    planning_only = planning_only_families()
    impl = implementation_families()
    allow_unknown = allow_unknown_model()
    if purpose == "implementation":
        model = requested or DEFAULT_IMPLEMENTATION_MODEL
        fam = model_family(model)
        if fam in planning_only:
            raise ModelPolicyError(
                "model_policy",
                f"{model!r} is planning-only; implementation runs on {', '.join(impl) or 'no configured family'}",
                purpose=purpose,
                requested=model,
                family=fam,
                allowed=list(impl),
            )
        if fam not in impl and not (fam is None and allow_unknown):
            raise ModelPolicyError(
                "model_unknown",
                f"{model!r} names no supported implementation model family",
                purpose=purpose,
                requested=model,
                allowed=list(impl),
            )
        return {
            "purpose": purpose,
            "model": model,
            "family": fam,
            "defaulted": requested is None or requested == "",
        }
    if not requested:
        raise ModelPolicyError(
            "model_required",
            "a planning session names its model explicitly; there is no silent default",
            purpose=purpose,
        )
    fam = model_family(requested)
    if fam not in KNOWN_FAMILIES and not (fam is None and allow_unknown):
        raise ModelPolicyError(
            "model_unknown",
            f"{requested!r} names no known model family",
            purpose=purpose,
            requested=requested,
            allowed=list(KNOWN_FAMILIES),
        )
    return {"purpose": purpose, "model": requested, "family": fam, "defaulted": False}


def check_session_model(
    purpose,
    footer=None,
    argv=None,
    transcript=None,
    expected=None,
) -> dict:
    """Verdict on REUSING an existing session for `purpose`, from up to three evidence sources
    (display names / ids / aliases; None = unavailable). Never raises; returns
    {"ok": bool, "code": str|None, "family": str|None, "reason": str, "evidence": {...}}.

    The footer is the gate (it is the live model). A planning-only footer is `model_policy`; a planning-only argv or
    transcript under an implementation footer is `planning_session` (the session was started or ran as
    the planning session and is being reused). An unreadable footer is `model_unverified`, never a
    pass. `expected` (what the caller asked for) must match the footer family: `model_mismatch`.
    For purpose=planning the footer must still be readable, and any known family passes.
    """
    try:
        planning_only = planning_only_families()
        impl = implementation_families()
        allow_unknown = allow_unknown_model()
    except ModelPolicyError as e:
        return {
            "ok": False,
            "code": e.code,
            "family": model_family(footer),
            "purpose": purpose,
            "reason": e.message,
            "evidence": {"footer": footer, "argv": argv, "transcript": transcript, "expected": expected},
        }
    ev = {
        "footer": footer,
        "footer_family": model_family(footer),
        "argv": argv,
        "argv_family": model_family(argv),
        "transcript": transcript,
        "transcript_family": model_family(transcript),
        "expected": expected,
        "expected_family": model_family(expected) if expected else None,
    }

    def verdict(ok, code, reason):
        return {
            "ok": ok,
            "code": code,
            "family": ev["footer_family"],
            "purpose": purpose,
            "reason": reason,
            "evidence": ev,
        }

    if purpose not in PURPOSES:
        return verdict(False, "bad_purpose", f"purpose must be one of {list(PURPOSES)}")
    unknown_ok = ev["footer_family"] is None and bool(footer) and allow_unknown
    if ev["footer_family"] is None and not unknown_ok:
        return verdict(
            False,
            "model_unverified",
            "the session's model could not be read from its footer; nothing is assumed",
        )
    if expected and (
        str(expected).strip().lower() != str(footer).strip().lower()
        if unknown_ok
        else ev["expected_family"] != ev["footer_family"]
    ):
        return verdict(
            False,
            "model_mismatch",
            f"footer model {footer!r} is not the requested {expected!r}",
        )
    if purpose == "planning":
        return verdict(True, None, f"planning session on {footer!r}")
    if ev["footer_family"] in planning_only:
        return verdict(
            False,
            "model_policy",
            f"the session is on {footer!r}, which is planning-only; implementation needs {', '.join(impl) or 'a configured family'}",
        )
    if ev["footer_family"] not in impl and not unknown_ok:
        return verdict(False, "model_policy", f"{footer!r} is not an implementation model")
    for src in ("argv", "transcript"):
        if ev[f"{src}_family"] in planning_only:
            return verdict(
                False,
                "planning_session",
                f"the session's {src} shows {ev[src]!r}: it was started or ran as a planning-only session and is not reused for implementation",
            )
    return verdict(True, None, f"implementation session on {footer!r}")


# --- model policy: end ---

EXIT_OK, EXIT_CMUX, EXIT_USAGE, EXIT_POLICY, EXIT_UNVERIFIED = 0, 1, 2, 3, 4
DEFAULT_CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"
DEFAULT_SUBAGENT_MODEL = "sonnet"

UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
WORKSPACE_REF_RE = re.compile(r"workspace:(\d+)")
# Claude's input line is "❯" + NBSP at column 0; a dialog cursor is " ❯ option" (leading plain
# space) and must NOT count as a prompt. Same shape as the bridge's PROMPT_RE.
PROMPT_RE = re.compile("^❯(?:\u00a0(.*))?$")
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _emit(obj: dict, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    else:
        for k, v in obj.items():
            print(f"{k}: {v}")


def _fail(code: int, kind: str, message: str, **detail) -> int:
    _emit({"ok": False, "error": kind, "message": message, **detail})
    return code


def transcript_path(session_id: str, cwd: str) -> Path:
    root = Path(os.environ.get("CMUX_BRIDGE_CLAUDE_PROJECTS") or (Path.home() / ".claude/projects"))
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    return root / slug / f"{str(session_id).removeprefix('claude-')}.jsonl"


def read_transcript_tail(p: Path, tail_bytes: int = 2_000_000) -> list:
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()
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


def classify(lines: list) -> dict:
    """Minimal screen classification: modal / prompt / footer-only / not_claude. The launcher never
    types, so this only decides whether the executor is READY (prompt + footer) or needs a human
    (modal) or more time. It mirrors the bridge's classify_screen conservatively."""
    tail = [l for l in lines[-40:]]
    model = footer_model(tail)
    prompt = any(PROMPT_RE.match(l) for l in tail)
    region = [l for l in tail if l.strip()][-12:]
    # A dialog replaces the input box, so its hints sit in the last rows; the folder-trust dialog
    # shows its own " ❯ No, exit" cursor (default = exit!), which is not an input prompt.
    if not prompt and any(h in l for l in region for h in MODAL_HINTS):
        return {"state": "modal", "model": model}
    if model and prompt:
        return {"state": "prompt", "model": model}
    if model:
        return {"state": "footer_only", "model": model}
    return {"state": "not_claude", "model": None}


class Cmux:
    """Thin argv runner around the installed cmux CLI (no shell). The socket password, if any, comes
    from CMUX_SOCKET_PASSWORD or the owner-only file named by CMUX_BRIDGE_PASSWORD_FILE — never argv."""

    def __init__(self, cli: str, timeout_s: float = 15.0):
        self.cli = cli
        self.timeout_s = timeout_s
        self.calls: list = []

    def _env(self) -> dict:
        env = dict(os.environ)
        env["CMUX_QUIET"] = "1"
        pw_file = env.get("CMUX_BRIDGE_PASSWORD_FILE")
        if pw_file and not env.get("CMUX_SOCKET_PASSWORD"):
            try:
                env["CMUX_SOCKET_PASSWORD"] = Path(pw_file).read_text().strip()
            except OSError:
                pass
        return env

    def run(self, *args: str, timeout: "float | None" = None) -> subprocess.CompletedProcess:
        argv = [self.cli, *args]
        t0 = time.monotonic()
        try:
            r = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout or self.timeout_s, env=self._env()
            )
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess(argv, 124, "", "timeout")
        except OSError as e:
            r = subprocess.CompletedProcess(argv, 127, "", f"cli_missing: {e}")
        self.calls.append({"args": args, "rc": r.returncode, "s": round(time.monotonic() - t0, 3)})
        return r

    def ok(self, *args: str, timeout: "float | None" = None) -> str:
        r = self.run(*args, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError(f"cmux {' '.join(args[:2])}: rc={r.returncode} {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    def json(self, *args: str, timeout: "float | None" = None):
        out = self.ok(*args, timeout=timeout)
        try:
            return json.loads(out)
        except ValueError:
            raise RuntimeError(f"cmux {' '.join(args[:2])}: non-JSON output {out.strip()[:200]!r}")


def _executable(path: "str | None") -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def cmux_wrapper_for(cmux_cli: "str | None") -> "str | None":
    """The cmux app's claude wrapper beside the cmux CLI. It injects --settings (hooks that emit
    agent.hook.* events) when CMUX_SURFACE_ID is set (it is, in a terminal cmux creates), keeps an
    explicit --session-id, and execs the real claude with the original argv, so `ps` identity holds."""
    cli = cmux_cli if cmux_cli and os.sep in cmux_cli else shutil.which(cmux_cli or "cmux")
    if not cli:
        return None
    cand = os.path.join(os.path.dirname(os.path.abspath(cli)), "cmux-claude-wrapper")
    return cand if _executable(cand) else None


def resolve_claude_bin(explicit: "str | None", cmux_cli: "str | None" = None) -> "tuple | None":
    """(path, source). An explicit --claude-bin / CLAUDE_BIN wins; otherwise the cmux wrapper (a bare
    `claude` found on PATH outside cmux is the raw binary and would launch without cmux hooks), then
    PATH, then ~/.local/bin. The path is used as found (not realpath'd): the versioned target's
    basename is a version number, and `ps` must keep showing a `claude` argv[0] for pid_for_session."""
    cands = (
        ("explicit", explicit),
        ("env", os.environ.get("CLAUDE_BIN")),
        ("cmux-wrapper", cmux_wrapper_for(cmux_cli)),
        ("path", shutil.which("claude")),
        ("home-local", str(Path.home() / ".local/bin/claude")),
    )
    for source, cand in cands:
        if _executable(cand):
            return cand, source
    return None


def canonical_cwd(path: str) -> str:
    """realpath, then macOS's true-case spelling (F_GETPATH) so the transcript slug and --cwd match the
    spelling Claude Code and the bridge use on a case-insensitive volume."""
    rp = os.path.realpath(path)
    try:
        import fcntl

        fd = os.open(rp, os.O_RDONLY)
        try:
            buf = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", 50), b"\0" * 1024)
        finally:
            os.close(fd)
        true = buf.split(b"\0", 1)[0].decode("utf-8")
        if true and os.path.isabs(true):
            return os.path.realpath(true)
    except (OSError, ImportError, ValueError, AttributeError):
        pass
    return rp


FEED_SESSION_PREFIX = "cmux-feed-v1:"


def hook_session_uuid(raw) -> "str | None":
    """Hook payload session id -> bare uuid. Mirrors bridge canonical_session_id: 'cmux-feed-v1:<b64 agent>:
    <b64 id>' (urlsafe or standard base64, padding optional); legacy 'claude-<uuid>' and a bare uuid pass."""
    if not isinstance(raw, str):
        return None
    if raw.startswith(FEED_SESSION_PREFIX):
        parts = raw[len(FEED_SESSION_PREFIX):].split(":")
        if len(parts) != 2:
            return None

        def dec(t: str) -> str:
            t = t.strip()
            return base64.b64decode(t.replace("-", "+").replace("_", "/") + "=" * (-len(t) % 4), validate=True).decode("utf-8")

        try:
            agent, ident = dec(parts[0]), dec(parts[1])
        except (ValueError, UnicodeDecodeError):
            return None
        if agent != "claude":
            return None
        raw = ident
    return str(raw).removeprefix("claude-").lower() or None


EVENTS_POLL_S = 10.0


def events_latest_seq(cm: Cmux) -> "int | None":
    """Read-only: the ack frame's latest_seq, captured before launch."""
    r = cm.run("events", "--after", "0", "--limit", "1", "--no-heartbeat", timeout=10)
    for line in (r.stdout or "").splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("type") == "ack":
            try:
                return int(j["resume"]["latest_seq"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def hooks_seen_for(cm: Cmux, after_seq: int, session_id: str, timeout_s: float = EVENTS_POLL_S) -> bool:
    """Poll (read-only, bounded) for an agent.hook.* event carrying our session id. A stream read that
    hits its timeout still has its partial stdout, so each pass is bounded to ~3s."""
    want = session_id.lower()
    deadline = time.monotonic() + timeout_s
    cursor = after_seq
    while True:
        argv = [cm.cli, "events", "--after", str(cursor), "--limit", "400", "--no-heartbeat"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=min(3.0, max(0.5, deadline - time.monotonic())), env=cm._env())
            out = r.stdout or ""
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        except OSError:
            out = ""
        for line in out.splitlines():
            try:
                j = json.loads(line)
            except ValueError:
                continue
            if j.get("type") == "event" and isinstance(j.get("seq"), int):
                cursor = max(cursor, j["seq"])  # page forward: >400 foreign events must not hide ours
            if j.get("type") == "event" and str(j.get("name", "")).startswith("agent.hook."):
                if hook_session_uuid((j.get("payload") or {}).get("session_id")) == want:
                    return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def workspace_uuid_for_ref(cm: Cmux, ref: str) -> "str | None":
    tree = cm.json("tree", "--all", "--json")
    for w in tree.get("windows", []) or []:
        for ws in w.get("workspaces", []) or []:
            if ws.get("ref") == ref and ws.get("id"):
                return str(ws["id"]).upper()
    return None


def surfaces_of(cm: Cmux, workspace_uuid: str) -> list:
    d = cm.json("list-pane-surfaces", "--workspace", workspace_uuid, "--id-format", "both", "--json")
    return [
        {"uuid": str(s.get("id")).upper(), "ref": s.get("ref"), "type": s.get("type"), "title": s.get("title")}
        for s in (d.get("surfaces") or [])
        if s.get("id")
    ]


def read_screen(cm: Cmux, surface_uuid: str, lines: int = 20) -> list:
    r = cm.run("read-screen", "--surface", surface_uuid, "--lines", str(lines))
    if r.returncode != 0:
        return []
    return r.stdout.splitlines()


_UUID_ANY_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
REALIZE_TIMEOUT_S = 10.0
REALIZE_STEP_S = 0.25
DEFAULT_NAME_PREFIX = "executor · "


def selected_workspace_info(cm: Cmux, window: "str | None" = None) -> "tuple | None":
    """(UUID, name) of the selected workspace (the `list-workspaces` row marked '[selected]') in
    `window` (default: the caller's window)."""
    args = ["list-workspaces", "--id-format", "both"] + (["--window", window] if window else [])
    r = cm.run(*args)
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        if "[selected]" in line:
            m = _UUID_ANY_RE.search(line)
            if not m:
                return None
            name = line[m.end():].replace("[selected]", "").strip()
            return m.group(0).upper(), name
    return None


def selected_workspace(cm: Cmux, window: "str | None" = None) -> "str | None":
    info = selected_workspace_info(cm, window)
    return info[0] if info else None


RECENT_LAUNCH_S = 60.0


def own_launch_workspace(name: str, uuid: str) -> "str | None":
    """Why `uuid` (the workspace selected before our launch) must NOT be restored as 'the user's': a
    concurrent launch is realizing its own view right now. Its default name starts with 'executor · ',
    or its receipt (written when it finishes) names this workspace and is under 60 s old."""
    if (name or "").startswith(DEFAULT_NAME_PREFIX):
        return "name_prefix"
    try:
        for f in default_receipt_dir().glob("*.json"):
            if time.time() - f.stat().st_mtime > RECENT_LAUNCH_S:
                continue
            if str((json.loads(f.read_text()).get("identity") or {}).get("workspace_uuid") or "").upper() == uuid:
                return "recent_receipt"
    except (OSError, ValueError, AttributeError):
        pass
    return None


def realize_terminal_view(
    cm: Cmux,
    ws: str,
    surface: str,
    prev_selected: "str | None",
    timeout_s: float = REALIZE_TIMEOUT_S,
    window: "str | None" = None,
) -> dict:
    """cmux 0.64.x creates an UNFOCUSED terminal with no view: read-screen fails and the shell/command
    does not even start. Realize it with a brief selection of the new workspace (poll read-screen,
    0.25 s steps, bounded ~10 s), then restore the previous selection ONLY if the selected workspace
    is still ours (a user's own switch is never overridden). The view persists after switching back."""
    out = {"view_realized": False, "focus_restored": False, "previous_workspace": prev_selected}
    win = ["--window", window] if window else []
    try:
        cm.ok("select-workspace", "--workspace", ws, *win)
    except RuntimeError as e:
        out["select_error"] = str(e)[:200]
        return out
    deadline = time.monotonic() + timeout_s
    while True:
        if cm.run("read-screen", "--surface", surface, "--workspace", ws, "--lines", "3").returncode == 0:
            out["view_realized"] = True
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(REALIZE_STEP_S)
    if prev_selected and prev_selected != ws and selected_workspace(cm, window) == ws:
        out["focus_restored"] = cm.run("select-workspace", "--workspace", prev_selected, *win).returncode == 0
    return out


def pid_for_session(session_id: str) -> "int | None":
    """The claude process whose own argv carries `--session-id <our uuid>`: an exact identity."""
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    needle = f"--session-id {session_id}"
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or needle not in line:
            continue
        pid, first = parts[0], parts[1]
        # argv[0] must be the claude binary: this excludes the launcher's own `python3 … --session-id`
        if "claude" not in os.path.basename(first):
            continue
        try:
            return int(pid)
        except ValueError:
            continue
    return None


def proc_argv(pid: int) -> "str | None":
    try:
        out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    return out.strip() or None


_ARGV_KEEP_RE = re.compile(
    r"(--model(?:=|\s+)\S+|--session-id(?:=|\s+)\S+|--permission-mode(?:=|\s+)\S+|--dangerously-skip-permissions)"
)


def argv_summary(argv: "str | None") -> "str | None":
    """What a receipt may persist about the Claude command line: the executable name plus an allowlist
    of identity/model/permission flags. The raw argv is never written: wrappers inject secrets into it
    (seen live: cmux's claude shim passes an --mcp-config carrying a socket auth token)."""
    if not argv:
        return None
    exe = os.path.basename(argv.split()[0])
    return " ".join([exe] + _ARGV_KEEP_RE.findall(argv))


def proc_cwd(pid: int) -> "str | None":
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(int(pid)), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    for l in out.splitlines():
        if l.startswith("n/"):
            return l[1:]
    return None


def proc_cmux_surface(pid: int) -> "str | None":
    """CMUX_SURFACE_ID from the process's own environment (cmux sets it for the terminal it runs
    in): the native surface↔process binding. None when unreadable."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    m = re.search(r"(?:^|\s)CMUX_SURFACE_ID=([0-9A-Fa-f-]{36})(?:\s|$)", out)
    return m.group(1).upper() if m else None


def default_receipt_dir() -> Path:
    root = Path(os.environ.get("CMUX_BRIDGE_STATE_DIR") or (Path.home() / ".local/state/tandem"))
    return root / "launches"


# ------------------------------------------------------------------------------------------ launch


def cmd_launch(a: argparse.Namespace) -> int:
    try:
        pol = resolve_launch_model(a.purpose, a.model)
        sub = resolve_launch_model("implementation", a.subagent_model)
    except ModelPolicyError as e:
        return _fail(EXIT_POLICY, e.code, e.message, **e.detail, launched=False)
    cwd = canonical_cwd(a.cwd)
    if not os.path.isdir(cwd):
        return _fail(EXIT_USAGE, "bad_cwd", f"--cwd is not a directory: {a.cwd}", launched=False)
    session_id = (a.session_id or str(_uuid.uuid4())).lower()
    if not UUID_RE.match(session_id):
        return _fail(EXIT_USAGE, "bad_session_id", "--session-id must be a UUID", launched=False)
    tp = transcript_path(session_id, cwd)
    if tp.exists():
        return _fail(
            EXIT_USAGE,
            "session_exists",
            "a transcript for that session id already exists in this worktree; never start a second process on an existing conversation",
            transcript=str(tp),
            launched=False,
        )
    resolved_bin = resolve_claude_bin(a.claude_bin, a.cmux)
    if not resolved_bin:
        return _fail(EXIT_USAGE, "claude_missing", "claude binary not found (pass --claude-bin or set CLAUDE_BIN)", launched=False)
    claude_bin, claude_bin_source = resolved_bin
    if a.focus not in ("true", "false"):
        return _fail(EXIT_USAGE, "bad_focus", "--focus must be true or false", launched=False)

    argv = [claude_bin]
    if a.permission_mode:
        argv += ["--permission-mode", a.permission_mode]
    else:
        argv.append("--dangerously-skip-permissions")  # default permission posture
    argv += ["--model", pol["model"], "--session-id", session_id]
    command = shlex.join(argv)
    name = a.name or f"{DEFAULT_NAME_PREFIX}{os.path.basename(cwd)} · {pol['model']}"
    env_pairs = [
        f"CLAUDE_CODE_SUBAGENT_MODEL={sub['model']}",
        f"TANDEM_SESSION_PURPOSE={pol['purpose']}",
        f"TANDEM_REQUESTED_MODEL={pol['model']}",
        # Claude Code shows a dim ghost-text "prompt suggestion" after each turn; plain-text read-screen
        # cannot tell it from typed text, so the bridge would see 'staged'. Off for every launched session.
        "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false",
    ]
    ws_args = ["new-workspace", "--name", name, "--cwd", cwd, "--command", command, "--focus", a.focus, "--json"]
    if a.window:
        ws_args += ["--window", a.window]
    for kv in env_pairs:
        ws_args += ["--env", kv]

    receipt = {
        "kind": "executor_launch",
        "at": _now(),
        "requested": {
            "purpose": pol["purpose"],
            "model": pol["model"],
            "family": pol["family"],
            "model_defaulted": pol["defaulted"],
            "subagent_model": sub["model"],
            "permission_mode": a.permission_mode or "bypass (--dangerously-skip-permissions)",
        },
        "launch": {
            "claude_bin": claude_bin,
            "claude_bin_source": claude_bin_source,
            "command": command,
            "cwd": cwd,
            "name": name,
            "window": a.window,
            "focus": a.focus,
            "workspace_env": env_pairs,
            "prompt_suggestions": "disabled",
        },
        "identity": {
            "session_id": session_id,
            "transcript_path": str(tp),
            "workspace_uuid": None,
            "surface_uuid": None,
            "pid": None,
        },
        "resolved": {"state": None, "footer_model": None, "footer_family": None, "model_verified": False},
        "launched": False,
        "ok": False,
    }
    if a.dry_run:
        receipt["dry_run"] = True
        receipt["cmux_args"] = ws_args
        receipt["ok"] = True
        _emit(receipt)
        return EXIT_OK

    cm = Cmux(a.cmux)
    try:
        ping = cm.run("ping", timeout=10)
        if ping.returncode != 0:
            return _fail(EXIT_CMUX, "transport", f"cmux ping failed: {(ping.stderr or ping.stdout).strip()[:300]}", launched=False)
        prev_selected, prev_skipped = None, None
        if a.focus == "false":
            info = selected_workspace_info(cm, a.window)
            if info:
                prev_skipped = own_launch_workspace(info[1], info[0])
                prev_selected = None if prev_skipped else info[0]
        seq0 = events_latest_seq(cm)
        raw = cm.ok(*ws_args)
    except RuntimeError as e:
        return _fail(EXIT_CMUX, "transport", str(e), launched=False)
    receipt["launched"] = True
    receipt["cmux_raw"] = raw.strip()[:400]
    ref = None
    try:
        j = json.loads(raw)
        ref = j.get("workspace_ref") or j.get("ref")
        if j.get("workspace_id") or j.get("id"):
            receipt["identity"]["workspace_uuid"] = str(j.get("workspace_id") or j.get("id")).upper()
    except ValueError:
        m = WORKSPACE_REF_RE.search(raw)
        ref = m.group(0) if m else None
    receipt["identity"]["workspace_ref"] = ref
    try:
        if not receipt["identity"]["workspace_uuid"] and ref:
            receipt["identity"]["workspace_uuid"] = workspace_uuid_for_ref(cm, ref)
        ws = receipt["identity"]["workspace_uuid"]
        if not ws:
            raise RuntimeError(f"could not resolve the new workspace's UUID from {raw.strip()[:120]!r}")
        surfs = surfaces_of(cm, ws)
        terms = [s for s in surfs if s.get("type") == "terminal"]
        if len(terms) != 1:
            raise RuntimeError(f"expected exactly one terminal surface in the new workspace, found {len(terms)}")
        receipt["identity"]["surface_uuid"] = terms[0]["uuid"]
        receipt["identity"]["surface_ref"] = terms[0]["ref"]
        try:
            receipt["launch"]["workspace_env_applied"] = cm.ok("workspace", "env", ws, "--mask").strip()[:400]
        except RuntimeError as e:
            receipt["launch"]["workspace_env_applied"] = f"unverified: {e}"
    except RuntimeError as e:
        receipt["error"] = str(e)
        _write_receipt(a, receipt)
        _emit(receipt)
        return EXIT_UNVERIFIED

    if a.focus == "false":
        view = realize_terminal_view(
            cm, ws, receipt["identity"]["surface_uuid"], prev_selected, float(a.realize_timeout_s), a.window
        )
        if prev_skipped:
            view["previous_workspace_skipped"] = f"selected workspace belongs to another launch ({prev_skipped}); not restoring"
        receipt["view_realized"] = view["view_realized"]
        receipt["focus_restored"] = view["focus_restored"]
        receipt["launch"]["view"] = view
        if not view["view_realized"]:
            receipt["error_code"] = "no_terminal_view"
            receipt["error"] = (
                "the new workspace never got a terminal view (cmux creates unfocused terminals without one); "
                "nothing was typed. Select the workspace once in cmux, then re-run `verify`"
            )
            _write_receipt(a, receipt)
            _emit(receipt)
            return EXIT_UNVERIFIED

    # bounded wait for the executor's prompt + footer; never type into it
    deadline = time.monotonic() + max(5.0, float(a.wait_s))
    state, model, last = "unknown", None, []
    while time.monotonic() < deadline:
        last = read_screen(cm, receipt["identity"]["surface_uuid"], 20)
        c = classify(last)
        state, model = c["state"], c["model"]
        if state in ("prompt", "modal"):
            break
        time.sleep(1.5)
    receipt["resolved"]["state"] = state
    receipt["resolved"]["footer_model"] = model
    receipt["resolved"]["footer_family"] = model_family(model)
    receipt["resolved"]["screen_tail"] = [l[:160] for l in last[-6:]]
    pid = pid_for_session(session_id)
    receipt["identity"]["pid"] = pid
    raw_argv = proc_argv(pid) if pid else None
    receipt["identity"]["pid_argv"] = argv_summary(raw_argv)
    receipt["identity"]["pid_argv_model"] = argv_model(raw_argv)
    verified = bool(model) and model_family(model) == pol["family"] and state == "prompt"
    receipt["resolved"]["model_verified"] = verified
    if model and model_family(model) != pol["family"]:
        receipt["resolved"]["note"] = f"footer shows {model!r} but {pol['model']!r} was requested: do NOT use this session; report it"
    elif state == "modal":
        receipt["resolved"]["note"] = "a dialog (folder trust?) is showing; a human confirms it, then re-run `verify`"
    elif state != "prompt":
        receipt["resolved"]["note"] = "no Claude prompt+footer yet; re-run `verify` before binding"
    receipt["ok"] = verified
    receipt["hooks_seen"] = None
    if verified and seq0 is not None:
        receipt["hooks_seen"] = hooks_seen_for(cm, seq0, session_id)
        if not receipt["hooks_seen"]:
            receipt["ok"] = False
            receipt["error_code"] = "no_hook_events"
            receipt["error"] = (
                "no agent.hook.* event for this session: it lacks cmux hooks, so a writer bind will fail "
                "(launch via the cmux claude wrapper: cmux-claude-wrapper beside the cmux CLI)"
            )
    _write_receipt(a, receipt)
    _emit(receipt)
    return EXIT_OK if receipt["ok"] else EXIT_UNVERIFIED


def _write_receipt(a: argparse.Namespace, receipt: dict) -> None:
    """Owner-only: dir 0700 (state root too, when we create it), file 0600 via O_EXCL|O_NOFOLLOW on a temp
    name then os.replace; a symlinked dir or target is refused."""
    path = Path(a.receipt) if a.receipt else default_receipt_dir() / f"{receipt['identity']['session_id']}.json"
    try:
        d = path.parent
        if d.is_symlink() or path.is_symlink():
            raise OSError(f"refusing symlinked receipt path: {path}")
        if d.is_dir() and not a.receipt and d.stat().st_uid == os.getuid():
            os.chmod(d, 0o700)  # a pre-existing default receipts dir may be looser (umask / older run)
        if not d.exists():
            if not a.receipt and not d.parent.exists():
                d.parent.mkdir(parents=True, mode=0o700)
                os.chmod(d.parent, 0o700)
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(d, 0o700)
        receipt["receipt_path"] = str(path)
        tmp = d / f".{path.name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        receipt["receipt_path"] = None
        receipt["receipt_error"] = str(e)


# ------------------------------------------------------------------------------------------ verify


def cmd_verify(a: argparse.Namespace) -> int:
    if a.purpose not in PURPOSES:
        return _fail(EXIT_USAGE, "bad_purpose", f"--purpose must be one of {list(PURPOSES)}")
    if not UUID_RE.match(a.surface or ""):
        return _fail(EXIT_USAGE, "bad_surface", "--surface must be a surface UUID (refs like surface:N are not identities)")
    cm = Cmux(a.cmux)
    lines = read_screen(cm, a.surface.upper(), 20)
    c = classify(lines)
    argv = proc_argv(a.pid)
    cwd = a.cwd or proc_cwd(a.pid)
    sid = a.session_id or (re.search(r"--session-id\s+(\S+)", argv or "") or [None, None])[1]
    tr = None
    tp = None
    if sid and cwd:
        tp = transcript_path(sid, cwd)
        tr = transcript_model(read_transcript_tail(tp))
    v = check_session_model(a.purpose, footer=c["model"], argv=argv_model(argv), transcript=tr, expected=a.expected_model)
    out = {
        "kind": "executor_verify",
        "at": _now(),
        "ok": v["ok"],
        "code": v["code"],
        "reason": v["reason"],
        "purpose": a.purpose,
        "family": v["family"],
        "screen_state": c["state"],
        "identity": {"surface_uuid": a.surface.upper(), "pid": a.pid, "session_id": sid, "cwd": cwd, "transcript_path": str(tp) if tp else None},
        "evidence": {**v["evidence"], "argv_summary": argv_summary(argv), "argv_present": argv is not None, "screen_tail": [l[:160] for l in lines[-4:]]},
    }
    _emit(out)
    if v["ok"]:
        return EXIT_OK
    return EXIT_UNVERIFIED if v["code"] == "model_unverified" else EXIT_POLICY


def cmd_policy(a: argparse.Namespace) -> int:
    try:
        r = resolve_launch_model(a.purpose, a.model)
    except ModelPolicyError as e:
        _emit({"ok": False, **e.to_dict()})
        return EXIT_POLICY
    _emit({"ok": True, **r})
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="executor_session.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    L = sub.add_parser("launch", help="launch a dedicated executor workspace with an explicit model")
    L.add_argument("--cwd", required=True, help="authorized worktree the executor runs in")
    L.add_argument("--purpose", default=DEFAULT_PURPOSE, choices=PURPOSES)
    L.add_argument("--model", default=None, help="opus (default for implementation) | sonnet | haiku | a claude-* id; Fable is refused for implementation")
    L.add_argument("--subagent-model", default=DEFAULT_SUBAGENT_MODEL, help="CLAUDE_CODE_SUBAGENT_MODEL exported into the workspace so the executor's workers cannot inherit Fable (default sonnet)")
    L.add_argument("--name", default=None, help="workspace title (display only; identity is by UUID)")
    L.add_argument("--window", default=None, help="target window id/ref (optional; default: the caller's window). Placement is not a security boundary")
    L.add_argument("--focus", default="false", help="true|false (default false: no lasting focus change; a brief ~1s selection creates the terminal view, then is restored)")
    L.add_argument("--realize-timeout-s", type=float, default=REALIZE_TIMEOUT_S, help="bound for the terminal-view wait when --focus false (default 10)")
    L.add_argument("--session-id", default=None, help="UUID to run the session under (default: generated)")
    L.add_argument("--permission-mode", default=None, help="claude --permission-mode value instead of --dangerously-skip-permissions")
    L.add_argument("--claude-bin", default=None)
    L.add_argument("--cmux", default=os.environ.get("CMUX_BRIDGE_CLI", DEFAULT_CMUX))
    L.add_argument("--wait-s", default=45, type=float, help="bounded wait for the prompt+footer")
    L.add_argument("--receipt", default=None, help="launch receipt path (default: $CMUX_BRIDGE_STATE_DIR/launches/<session>.json)")
    L.add_argument("--dry-run", action="store_true", help="resolve policy, identity and the exact cmux argv; launch nothing")
    L.set_defaults(func=cmd_launch)

    V = sub.add_parser("verify", help="verify an existing session's model/purpose before reusing it")
    V.add_argument("--surface", required=True)
    V.add_argument("--pid", required=True, type=int)
    V.add_argument("--session-id", default=None)
    V.add_argument("--cwd", default=None)
    V.add_argument("--purpose", default=DEFAULT_PURPOSE, choices=PURPOSES)
    V.add_argument("--expected-model", default=None)
    V.add_argument("--cmux", default=os.environ.get("CMUX_BRIDGE_CLI", DEFAULT_CMUX))
    V.set_defaults(func=cmd_verify)

    P = sub.add_parser("policy", help="resolve what --model a launch would use (no cmux)")
    P.add_argument("--purpose", default=DEFAULT_PURPOSE)
    P.add_argument("--model", default=None)
    P.set_defaults(func=cmd_policy)
    return p


def main(argv: "list | None" = None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if not getattr(a, "func", None):
        p.print_help()
        return EXIT_USAGE
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
