"""Model routing policy for Codex-driven Claude Code sessions.

The default rule: **Fable is planning-only.** Implementation runs on an explicitly chosen Anthropic
implementation model — Opus by default, Sonnet or Haiku when the operator picks one — and never on
a model silently inherited from `~/.claude/settings.json` (whose default may be Fable) or on a
session that is, or was, a Fable planning session.

The gate is configurable through the environment, read on every check (nothing is cached):

* `TANDEM_PLANNING_ONLY_MODELS` — comma-separated model families (fable, opus, sonnet, haiku) that
  are planning-only. Default `fable`. Every other known family is an implementation family.
* `TANDEM_ALLOW_UNKNOWN_MODEL` — `1`/`true`/`yes`/`on` lets an implementation session run on a model
  that names no known family. Default off (refused).

A malformed value fails closed: launches are refused with code `model_policy_config` and session
checks return `ok: False` with that code. Nothing falls back to a looser default.

This module is the single source of truth for that policy. It is imported by the bridge
(`core.bind` / `core.submit` gate writer bindings by purpose and model) and mirrored VERBATIM
into the installed Codex skill script `skill/cmux-driver/scripts/executor_session.py`
(which cannot import this package once installed under ~/.codex). The block between the
`# --- model policy: begin ---` / `# --- model policy: end ---` markers must stay byte-identical in
both files; `bridge/tests/test_model_routing.py::test_launcher_mirrors_policy_module` enforces it.

Three independent kinds of model evidence exist for a running session, and the policy treats them
differently because they answer different questions:

* the **footer** (`Model: Opus 5 | … Ctx Used:`) is the model the session will use for its NEXT
  turn — the live, resolved value; it is the gate;
* the process **argv** (`claude … --model opus …`) is what was REQUESTED at launch; a missing
  `--model` means the session inherited the settings default; a Fable argv means the session was
  started as a planning session even if `/model` switched it later;
* the **transcript** (`message.model` on assistant entries) is what actually answered; a Fable
  entry means Fable context was built in this session.

Fable in ANY of the three disqualifies a session for implementation: the footer says it is on
Fable now; argv/transcript say it is a planning session being reused. Neither the pane's window
nor its focus state is evidence of anything here (UI placement is not a security boundary).
"""

from __future__ import annotations

import os
import re

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
