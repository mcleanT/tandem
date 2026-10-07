"""In-process fake for the cmux CLI, plus screen builders that use the real byte shapes.

SIMULATED ONLY (kind: simulated). `FakeCLI` subclasses `CmuxCLI` and overrides exactly one method,
`_execute`, so the real class's `FaultInjector` hooks, call log and `simulated` labelling apply
unchanged. The real `cmux` binary is never invoked and no real socket is touched.

Screen rows, event names and payload shapes follow the real cmux/Claude Code runtime:
  idle input line   '❯\xa0'
  footer            '  Model: … | Context: … Ctx Used: 17.0% | v2.1.263 | Session: 49m | ↻ 1'
  spinner           '· Simmering… (5m 41s · ↓ 26.1k tokens)'
  background agents '  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent' (shown only when
                    background agents exist)
  sidebar payload   args = 'claude_code Running --icon=… --tab=<ws> --panel=<surface> --pid=<pid>'
  agent hooks       payload {session_id: 'cmux-feed-v1:<b64 claude>:<b64 uuid>' (real cmux 0.64.25;
                             legacy 'claude-<uuid>' when fake.legacy_session_ids), _ppid, cwd, workspace_id,
                             hook_event_name, occurred_at, tool_input_length}

The fake also models the DURABLE Claude Code transcript, because that — not the redacted event
stream — is what the bridge correlates against: `<transcript_root>/<slug>/<session-uuid>.jsonl`
with slug = re.sub(r"[^A-Za-z0-9]", "-", realpath(cwd)). Transcript writing lives in the command
models (`_cmd_send_key`), never in `_execute`, so the single-override rule holds.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading as _threading
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

from cmux_bridge.cmuxcli import CmuxCLI, CmuxResult, classify

NBSP = "\xa0"
IDLE_INPUT = "❯" + NBSP
SEP = "─" * 46
SPINNER = "· Simmering… (5m 41s · ↓ 26.1k tokens)"
BRACKET_START = "\x1b[200~"
BRACKET_END = "\x1b[201~"
COMPACT_CONTENT = "<command-name>/compact</command-name>\n<command-message>compact</command-message>\n<command-args></command-args>"


DEFAULT_MODEL = "Opus 5"  # an executor's footer; planning-session tests pass model="Fable 5.1"


def footer(pct: float = 17.0, model: str = DEFAULT_MODEL) -> str:
    return f"  Model: {model} | Context: … Ctx Used: {pct:.1f}% | v2.1.263 | Session: 49m | ↻ 1"


def tail_extra(agents: int | None = None) -> list[str]:
    bypass = "  ⏵⏵ bypass permissions on (shift+tab to cycle)"
    if agents is not None:
        bypass += f" · ← {agents} agent"
    return ["  Thinking: xhigh | Mem: 33.9G/64.0G | Weekly: 7.0% | 5h: 18.0%", bypass]


def feed_session_id(session_id: str) -> str:
    """Real cmux 0.64.25 hook form: 'cmux-feed-v1:' + b64('claude') + ':' + b64('<uuid>'), unpadded."""
    enc = lambda t: base64.b64encode(t.encode()).decode().rstrip("=")  # noqa: E731
    return "cmux-feed-v1:" + enc("claude") + ":" + enc(session_id.removeprefix("claude-"))


def claude_screen(
    staged: str = "",
    *,
    running: bool = False,
    pct: float = 17.0,
    agents: int | None = None,
    model: str = DEFAULT_MODEL,
) -> list[str]:
    """A Claude Code surface: optional spinner above, the ❯ input line, separator, footer."""
    lines: list[str] = []
    if running:
        lines.append(SPINNER)
    lines += [
        "  ⎿ " + NBSP + "Tip: Run /help for a list of commands",
        SEP + " ultracode ─",
        IDLE_INPUT + staged,
        SEP,
        footer(pct, model),
    ] + tail_extra(agents)
    return lines


def pasted_screen(
    extra_lines: int,
    *,
    pct: float = 17.0,
    agents: int | None = None,
    model: str = DEFAULT_MODEL,
) -> list[str]:
    return claude_screen(
        f"[Pasted text #1 +{extra_lines} lines]", pct=pct, agents=agents, model=model
    )


def modal_screen(pct: float = 17.0) -> list[str]:
    """Trust dialog: dialog text and a footer, but no ❯ input line."""
    return [
        "╭────────────────────────────────────────────╮",
        "│ Do you trust the files in this folder?      │",
        "│ /private/tmp/worktree                       │",
        "│   1. Yes, proceed                           │",
        "│   2. No, exit                               │",
        "│ Enter to confirm · Esc to cancel            │",
        "╰────────────────────────────────────────────╯",
        footer(pct),
    ] + tail_extra()


def zsh_screen() -> list[str]:
    return [
        "(base) user@host dir % ls",
        "README.md  src  tests",
        "(base) user@host dir % ",
    ]


def empty_screen() -> list[str]:
    return ["", "   ", ""]


class FakeClock:
    """Monotonic-enough fake clock. `sleep` advances it; nothing ever blocks."""

    def __init__(self, start: float | None = None):
        self.t = float(start if start is not None else time.time())

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)

    def sleep(self, dt: float) -> None:
        self.t += float(dt)


def _opt(args: list[str], name: str, default: str | None = None) -> str | None:
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return default


class FakeCLI(CmuxCLI):
    """Models the argument arrays the bridge sends. Overrides only `_execute`."""

    legacy_session_ids = False  # True: hooks carry the pre-0.64 bare 'claude-<uuid>' form

    def __init__(
        self,
        clock: FakeClock | None = None,
        cwd: str = "/tmp",
        fault=None,
        events_tick: float = 2.0,
        transcript_root: str | os.PathLike | None = None,
        cmd_tick: float = 0.05,
    ):
        super().__init__(
            cli_path="/fake/bin/cmux", socket=None, timeout_s=5.0, fault=fault, log=[]
        )
        self.clock = clock or FakeClock()
        self.events_tick = float(events_tick)
        self.cmd_tick = float(cmd_tick)
        self.cwd = str(cwd)
        self.transcript_root = Path(transcript_root) if transcript_root else None
        self.boot_id = "boot-" + _uuid.uuid4().hex[:12]
        self.gap = False
        self.workspace = str(_uuid.uuid4()).upper()
        self.surfaces: dict[str, dict] = {}
        self.events: list[dict] = []
        self.seq = 0
        self.calls: list[list[str]] = []
        self.argvs: list[list[str]] = []
        self.send_count = 0
        self.enter_count = 0
        self.paste_count = 0
        self.buffers: dict[str, str] = {}
        self._next_ref = 47
        # knobs
        self.write_transcript = True
        self.emit_prompt_submit = True
        self.compact_emits_session_start = True
        self.compact_emits_boundary = True
        self.drop_paste = False  # send returns ok but nothing is staged
        self.send_error_once: str | None = (
            None  # next `send` fails at the CLI: nothing is staged
        )
        self.mangle_paste = False  # paste marker rendered with the wrong line count
        self.foreign_after_send: str | None = (
            None  # a foreign process replaces our staging right after `send`
        )
        self.send_gate: float | None = (
            None  # first `send` holds this many real seconds (race window)
        )
        self.send_gate_started = _threading.Event()
        self._send_gate_fired = False
        self.stop_on_enter = False  # emit Stop immediately when Enter is accepted
        self.stop_after_events: int | None = (
            None  # emit Stop after N further `events` calls
        )
        self.ping_reply: tuple[int, str, str] | None = (
            None  # (rc, stdout, stderr) override
        )
        self.proc_start = (
            "Tue Sep  8 11:47:38 2026"  # what tests monkeypatch Bridge._proc_start to
        )
        self._pending_stop: tuple[str, int] | None = None
        # queued-steer knobs (opt-in; default off preserves every existing test's behaviour)
        self.keep_running_on_send = False  # a `send` into a busy turn keeps the spinner (running)
        self.queue_on_enter = False  # Enter QUEUES: no transcript user-message, turn stays running

    # ------------------------------------------------------------ scenario construction
    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(
            timespec="milliseconds"
        )

    def _ref(self) -> str:
        self._next_ref += 1
        return f"surface:{self._next_ref}"

    def add_claude_surface(
        self,
        pct: float = 17.0,
        pid: int | None = None,
        title: str = "claude",
        uuid: str | None = None,
        session_id: str | None = None,
        agents: int | None = None,
        model: str = DEFAULT_MODEL,
    ) -> str:
        u = (uuid or str(_uuid.uuid4())).upper()
        self.surfaces[u] = {
            "uuid": u,
            "ref": self._ref(),
            "title": title,
            "kind": "claude",
            "session_id": session_id or ("claude-" + str(_uuid.uuid4())),
            "pid": int(pid if pid is not None else os.getpid()),
            "pct": pct,
            "agents": agents,
            "model": model,
            "screen": claude_screen(pct=pct, agents=agents, model=model),
            "staged": "",
        }
        self.emit_sidebar(u)
        self.emit_agent(u, "SessionStart")
        return u

    def add_shell_surface(self, title: str = "zsh") -> str:
        u = str(_uuid.uuid4()).upper()
        self.surfaces[u] = {
            "uuid": u,
            "ref": self._ref(),
            "title": title,
            "kind": "shell",
            "session_id": None,
            "pid": None,
            "pct": None,
            "agents": None,
            "model": None,
            "screen": zsh_screen(),
            "staged": "",
        }
        return u

    def set_screen(self, surface: str, lines: list[str]) -> None:
        self.surfaces[surface.upper()]["screen"] = list(lines)

    def set_no_terminal_view(self, surface: str, on: bool = True) -> None:
        """cmux 0.64.x: a surface created unfocused has no terminal view; read-screen fails."""
        self.surfaces[surface.upper()]["no_view"] = bool(on)

    def set_ctx_pct(self, surface: str, pct: float) -> None:
        s = self.surfaces[surface.upper()]
        s["pct"] = pct
        s["screen"] = claude_screen(pct=pct, agents=s["agents"], model=s["model"])

    def set_background_agents(self, surface: str, n: int | None) -> None:
        s = self.surfaces[surface.upper()]
        s["agents"] = n
        s["screen"] = claude_screen(pct=s["pct"], agents=n, model=s["model"])

    def set_model(self, surface: str, model: str) -> None:
        """Model the footer shows from now on (a `/model` switch inside the session)."""
        s = self.surfaces[surface.upper()]
        s["model"] = model
        s["screen"] = claude_screen(
            s["staged"], pct=s["pct"] or 17.0, agents=s["agents"], model=model
        )

    # ------------------------------------------------------------ transcript modelling
    def transcript_file(self, surface: str) -> Path | None:
        if not self.transcript_root:
            return None
        s = self.surfaces[surface.upper()]
        slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.cwd))
        return (
            self.transcript_root
            / slug
            / f"{s['session_id'].removeprefix('claude-')}.jsonl"
        )

    def _append_transcript(self, surface: str, obj: dict) -> None:
        p = self.transcript_file(surface)
        if not (self.write_transcript and p):
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(obj) + "\n")

    def write_user_message(self, surface: str, text: str, **extra) -> None:
        s = self.surfaces[surface.upper()]
        self._append_transcript(
            surface,
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "timestamp": self._now_iso(),
                "sessionId": s["session_id"].removeprefix("claude-"),
                "cwd": self.cwd,
                "uuid": str(_uuid.uuid4()),
                "promptId": str(_uuid.uuid4()),
                **extra,
            },
        )

    def inject_foreign_user_message(
        self, surface: str, text: str = "a human typed this by hand"
    ) -> None:
        """A human prompt landing on the same session; the bridge must never claim it as its own."""
        self.clock.advance(self.cmd_tick)
        self.write_user_message(surface, text)

    def write_turn_end(self, surface: str) -> None:
        s = self.surfaces[surface.upper()]
        self._append_transcript(
            surface,
            {
                "type": "system",
                "subtype": "turn_duration",
                "timestamp": self._now_iso(),
                "sessionId": s["session_id"].removeprefix("claude-"),
            },
        )

    def write_compact_boundary(self, surface: str) -> None:
        s = self.surfaces[surface.upper()]
        sid = s["session_id"].removeprefix("claude-")
        self._append_transcript(
            surface,
            {
                "type": "system",
                "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "manual", "preTokens": 120000},
                "timestamp": self._now_iso(),
                "sessionId": sid,
            },
        )
        self.write_user_message(
            surface, "This session is being continued …", isCompactSummary=True
        )

    def _append(self, name: str, payload: dict) -> dict:
        self.seq += 1
        ev = {
            "type": "event",
            "seq": self.seq,
            "name": name,
            "workspace_id": self.workspace,
            "occurred_at": self._now_iso(),
            "payload": payload,
        }
        self.events.append(ev)
        return ev

    def emit_sidebar(
        self, surface: str, status: str = "Running", pid: int | None = None
    ) -> dict:
        s = self.surfaces[surface.upper()]
        p = int(pid if pid is not None else (s["pid"] or os.getpid()))
        args = (
            f"claude_code {status} --icon=bolt.fill --color=#4C8DFF "
            f"--tab={self.workspace} --panel={s['uuid']} --pid={p}"
        )
        return self._append("sidebar.metadata.updated", {"args": args})

    def emit_agent(
        self,
        surface: str,
        hook: str,
        session_id: str | None = None,
        pid: int | None = None,
        **extra,
    ) -> dict:
        s = self.surfaces[surface.upper()]
        sid = session_id or (
            s["session_id"]
            if self.legacy_session_ids or not s["session_id"]
            else feed_session_id(s["session_id"])
        )
        payload = {
            "session_id": sid,
            "_ppid": int(pid if pid is not None else s["pid"]),
            "cwd": self.cwd,
            "workspace_id": self.workspace,
            "hook_event_name": hook,
            "occurred_at": self._now_iso(),
            "tool_input_length": extra.pop("tool_input_length", 0),
        }
        payload.update(extra)
        return self._append(f"agent.hook.{hook}", payload)

    def finish_turn(self, surface: str) -> dict:
        """Emit agent.hook.Stop, write the transcript turn end, return to the idle prompt."""
        s = self.surfaces[surface.upper()]
        ev = self.emit_agent(surface, "Stop")
        self.write_turn_end(surface)
        s["screen"] = claude_screen(pct=s["pct"], agents=s["agents"], model=s["model"])
        return ev

    def bump_boot_id(self) -> None:
        self.boot_id = "boot-" + _uuid.uuid4().hex[:12]

    # ------------------------------------------------------------ the single override
    def _execute(
        self, argv: list[str], args: list[str], timeout: float, t0: float
    ) -> CmuxResult:
        self.calls.append(list(args))
        self.argvs.append(list(argv))
        self.clock.advance(self.cmd_tick)
        out, err, rc = self._dispatch(list(args))
        res = CmuxResult(list(args), rc, out, err, 0.001)
        res.error_kind = classify(rc, out, err)
        return res

    # ------------------------------------------------------------ command modelling
    def _dispatch(self, args: list[str]) -> tuple[str, str, int]:
        cmd = args[0] if args else ""
        fn = getattr(self, f"_cmd_{cmd.replace('-', '_')}", None)
        if fn is None:
            return "", f"unknown command: {cmd}\n", 2
        return fn(args)

    def _cmd_ping(self, args):
        if self.ping_reply is not None:
            rc, out, err = self.ping_reply
            return out, err, rc
        return "PONG\n", "", 0

    def _cmd_capabilities(self, args):
        return (
            json.dumps(
                {
                    "access_mode": "cmuxOnly",
                    "version": "0.64.17",
                    "commands": ["ping", "events", "read-screen", "send", "send-key"],
                }
            )
            + "\n",
            "",
            0,
        )

    def _cmd_identify(self, args):
        return (
            json.dumps(
                {
                    "caller": {"pid": os.getpid(), "descended_from_cmux": True},
                    "focused": {
                        "workspace_uuid": self.workspace,
                        "surface_uuid": next(iter(self.surfaces), None),
                    },
                    "socket_path": "/fake/run/cmux.sock",
                }
            )
            + "\n",
            "",
            0,
        )

    def _cmd_list_workspaces(self, args):
        return f"  workspace:1 {self.workspace}  main\n", "", 0

    def _cmd_list_pane_surfaces(self, args):
        ws = (_opt(args, "--workspace") or "").upper()
        if ws != self.workspace:
            return "", "Error: not_found: Workspace not found\n", 1
        rows = [
            f"  {s['ref']} {s['uuid']}  {s['title']}" for s in self.surfaces.values()
        ]
        return "\n".join(rows) + "\n", "", 0

    def _surface_or_none(self, args) -> dict | None:
        return self.surfaces.get((_opt(args, "--surface") or "").upper())

    def _cmd_read_screen(self, args):
        s = self._surface_or_none(args)
        if s is None:
            return "", "Error: not_found: Pane or workspace not found\n", 1
        if s.get("no_view"):
            return "", "Error: internal_error: Failed to read terminal text\n", 1
        n = int(_opt(args, "--lines", "40"))
        return "\n".join(s["screen"][-n:]), "", 0

    def _cmd_send(self, args):
        s = self._surface_or_none(args)
        if s is None:
            return "", "Error: not_found: Pane or workspace not found\n", 1
        text = args[-1]
        if self.send_error_once:
            err, self.send_error_once = self.send_error_once, None
            return "", err, 1
        self.send_count += 1
        if self.send_gate is not None and not self._send_gate_fired:
            self._send_gate_fired = True
            self.send_gate_started.set()
            time.sleep(
                self.send_gate
            )  # hold the first send so a racing second send would collide
        if self.drop_paste:
            return "", "", 0  # the CLI reports success but nothing reaches the editor
        if text.startswith(BRACKET_START) and text.endswith(BRACKET_END):
            self.paste_count += 1
            inner = text[len(BRACKET_START) : -len(BRACKET_END)]
            s["staged"] = inner
            shown = inner.count("\n") + (1 if self.mangle_paste else 0)
            s["screen"] = pasted_screen(shown, pct=s["pct"] or 17.0, agents=s["agents"], model=s["model"])
        else:
            s["staged"] = text
            s["screen"] = claude_screen(
                text.splitlines()[-1] if text else "",
                running=self.keep_running_on_send,
                pct=s["pct"] or 17.0,
                agents=s["agents"], model=s["model"],
            )
        if self.foreign_after_send is not None:
            # a foreign process pastes its own text right after our send, before our verify read
            foreign = self.foreign_after_send
            s["staged"] = foreign
            s["screen"] = claude_screen(
                foreign.splitlines()[-1] if foreign else "",
                pct=s["pct"] or 17.0,
                agents=s["agents"], model=s["model"],
            )
        return "", "", 0

    def _cmd_set_buffer(self, args):
        # set-buffer [--name <name>] <text>
        name = _opt(args, "--name", "default")
        self.buffers[name] = args[-1]
        return "", "", 0

    def _cmd_paste_buffer(self, args):
        # paste-buffer [--name <name>] --surface <id> : cmux drives the app's bracketed paste, so
        # multi-line text stages cleanly (unlike a raw escape through `send` on this build).
        s = self._surface_or_none(args)
        if s is None:
            return "", "Error: not_found: Pane or workspace not found\n", 1
        name = _opt(args, "--name", "default")
        text = self.buffers.get(name, "")
        self.paste_count += 1
        if self.drop_paste:
            return "", "", 0
        s["staged"] = text
        shown = text.count("\n") + (1 if self.mangle_paste else 0)
        s["screen"] = pasted_screen(shown, pct=s["pct"] or 17.0, agents=s["agents"], model=s["model"])
        return "", "", 0

    def _cmd_send_key(self, args):
        s = self._surface_or_none(args)
        if s is None:
            return "", "Error: not_found: Pane or workspace not found\n", 1
        key = args[-1]
        if key != "Enter":
            return "", f"unsupported key: {key}\n", 2
        self.enter_count += 1
        staged = s["staged"]
        if not staged:
            return "", "", 0
        s["staged"] = ""
        if self.emit_prompt_submit:
            self.emit_agent(
                s["uuid"],
                "UserPromptSubmit",
                tool_input_length=len(staged),
                context_length=54321,
            )
        if staged.strip() == "/compact":
            self.write_user_message(s["uuid"], COMPACT_CONTENT)
            s["screen"] = claude_screen(pct=s["pct"] or 17.0, agents=s["agents"], model=s["model"])
            if self.compact_emits_session_start:
                self.emit_agent(s["uuid"], "SessionStart")
            if self.compact_emits_boundary:
                self.clock.advance(self.cmd_tick)
                self.write_compact_boundary(s["uuid"])
            return "", "", 0
        if self.queue_on_enter:
            # a steer QUEUED into a running turn: the input clears and a UserPromptSubmit may fire,
            # but the message is NOT yet a user turn in the transcript — it is absorbed only when the
            # running turn yields. The bridge must not claim acceptance from the empty editor or the
            # event alone; absorption is modelled later by an explicit write_user_message.
            s["screen"] = claude_screen(
                running=True, pct=s["pct"] or 17.0, agents=s["agents"], model=s["model"]
            )
            return "", "", 0
        self.write_user_message(s["uuid"], staged)
        s["screen"] = claude_screen(
            running=True, pct=s["pct"] or 17.0, agents=s["agents"], model=s["model"]
        )
        if self.stop_on_enter:
            self.finish_turn(s["uuid"])
        elif self.stop_after_events is not None:
            self._pending_stop = (s["uuid"], int(self.stop_after_events))
        return "", "", 0

    def _cmd_events(self, args):
        self.clock.advance(self.events_tick)
        if self._pending_stop is not None:
            surf, n = self._pending_stop
            if n <= 0:
                self._pending_stop = None
                self.finish_turn(surf)
            else:
                self._pending_stop = (surf, n - 1)
        after = int(_opt(args, "--after", "0"))
        limit = int(_opt(args, "--limit", "100"))
        latest = self.seq
        ack = {
            "type": "ack",
            "boot_id": self.boot_id,
            "resume": {
                "after_seq": after,
                "gap": bool(self.gap),
                "latest_seq": latest,
                "next_seq": latest + 1,
                "oldest_seq": 1,
            },
        }
        lines = [json.dumps(ack)]
        for e in self.events:
            if e["seq"] > after:
                lines.append(json.dumps(e))
                if len(lines) - 1 >= limit:
                    break
        return "\n".join(lines) + "\n", "", 0
