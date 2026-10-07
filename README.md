# tandem

Each Codex session gets its own dedicated Claude Code session, running in the [cmux](https://cmux.dev) terminal app. Codex launches that session and drives it directly through a small MCP server (the bridge) over the cmux socket.

Origin: extracted from sporedrive.

## Architecture

```
Codex session --MCP stdio--> tandem (bridge/cmux_bridge) --cmux socket--> Claude Code in a cmux surface
```

- The bridge wraps the cmux CLI through argument arrays only: no shell, no generic RPC, no pane deletion, no process cleanup.
- Sessions are identified by UUID (workspace, surface, Claude session id, pid), never by title or focus.
- Delivery is accepted only when the submitted text is found in Claude's own transcript (transcript-hash acceptance). Editor staging, a send "OK" or a hook event alone prove nothing. An unconfirmed submit is `uncertain` and is reconciled under the same `request_id`, never resent blindly.
- `skill/cmux-driver/` is the Codex skill that holds the driver protocol; `skill/cmux-driver/scripts/executor_session.py` launches and verifies the dedicated Claude session. From any worktree, call it through the install symlink: `python3 "${CODEX_HOME:-$HOME/.codex}/skills/cmux-driver/scripts/executor_session.py"`.

## Tools

| Tool | Purpose |
|---|---|
| `bridge_discover` | Diagnose cmux socket access; list Claude Code sessions visible in cmux |
| `bridge_bind` | Bind a controller to one session (writer or monitor), take a lease, run the model gate |
| `bridge_observe` | Screen state, context use and events after a sequence number |
| `bridge_submit` | Deliver a message; keyed by `request_id`, replays instead of resending |
| `bridge_wait` | Wait for accepted / turn_complete / idle (default 25 s; loop on non-terminal results) |
| `bridge_compact` | Compact the session with a checkpoint |
| `bridge_pause` | Pause a binding (owner only, reason required) |
| `bridge_resume` | Resume a paused binding (owner only) |
| `bridge_release` | Drop the leases; never closes the surface or process |

## Identity and one writer

One Claude per Codex session, enforced: the Codex session's identity is its `controller_id`, stable for that Codex session. A new writer bind by the same `controller_id` supersedes its other live writer bindings (they are released, leases dropped; their ids come back as `superseded_bindings`). Two Codex sessions must never share a `controller_id`. A worktree has one writer at a time, enforced by leases; a writer bind by a different controller is refused with `lease_conflict`. Every binding tool (`bridge_observe`, `bridge_submit`, `bridge_wait`, `bridge_compact`, `bridge_pause`, `bridge_resume`, `bridge_release`) takes `controller_id` and refuses `not_owner` unless it matches the one the binding was bound with.

Choose `controller_id` once per Codex session: the Codex thread/session id if the environment exposes one, else a uuid generated once and kept in your notes. Reuse it on every bridge call. It must be a non-empty, non-whitespace string of at most 200 chars (`bad_request` otherwise). Worktree identity is the case-insensitive-canonical on-disk path (macOS `F_GETPATH`), so differently-cased spellings of one directory share one writer lease.

**Retention of prompt text (PHI minimisation).** While a request is in flight its record under `requests/<binding_id>/<request_id>.json` holds the delivered text (inline) and a 120-char `first_line`; a multi-line/long payload also lives in an 0444 file `tasks/<binding_id>/<request_id>.txt`. Task files are written only after every refusal gate passes (a refused submit writes nothing). Once a request is terminal (`accepted`, `completed`, `uncertain_foreign`) its record keeps only `text_sha256`/`delivered_text_sha256` plus length/line counts (inline `delivered_text` and `first_line` are nulled; a task-file record keeps just the `Task brief: <path>` reference). `bridge_release` deletes `tasks/<binding_id>/` unless a non-terminal request is still in flight on the binding. The in-memory CLI call log keeps only the last 500 calls and redacts `send` text as `<redacted N chars>`. Receipts under `receipts/` never contain prompt text. Task files of a never-released binding remain until it is released.

## Leases

Default TTL 1800 s (`lease_ttl_s` on `bridge_bind`, clamped 60..86400). No tool renews a lease. When it lapses, calls refuse with `lease_expired`; fix it by rebinding the SAME Claude session with the SAME `controller_id` (do not launch a new one). Distinguish `lease_conflict` (another controller holds the writer lease at bind time) and `lease_lost` (this binding was superseded or its lease no longer held).

## Pause

`bridge_pause(binding_id, controller_id, reason)` marks the binding paused. While paused, `bridge_submit` and `bridge_compact` refuse with `paused`; `bridge_observe` and `bridge_wait` keep working and observe reports `paused`. Immediately before every Enter (submit, reconcile of staged text, compact) the bridge re-checks the writer lease and the pause flag under one state-lock hold. A pause landing after staging raises `paused`, marks the request `uncertain`, and leaves the payload staged with Enter NOT pressed. For submit: `bridge_resume`, then call `bridge_submit` again with the SAME `request_id` to reconcile. For compact: `bridge_resume`, clear the staged `/compact` from the prompt box, call `bridge_compact` with a NEW `request_id` (compact is not re-Entered). A rebind on the same surface carries the pause to the new binding, even after release or lease expiry. `bridge_release` does not clear a pause; it makes the binding unusable. `bridge_resume` clears it. Residual race: the check and the keystroke are not atomic, so a pause or release landing in that last gap is not caught.

## Model gate

Implementation sessions must run on an explicitly chosen, verified model. Configure through the environment of the bridge (and of `executor_session.py`):

| Variable | Meaning | Default |
|---|---|---|
| `TANDEM_PLANNING_ONLY_MODELS` | Comma-separated families (`fable`, `opus`, `sonnet`, `haiku`) refused for implementation | `fable` |
| `TANDEM_ALLOW_UNKNOWN_MODEL` | `1/true/yes/on` admits a readable footer naming no known family; `expected` must then match as text | off |

A malformed value fails closed with `model_policy_config`. Other refusals: `model_policy` / `planning_session` (planning-only family), `model_required` (planning purpose without a model), `model_unknown`, `model_mismatch`, `model_unverified`, `model_changed`. Full list: `skill/cmux-driver/references/bridge-mcp.md`.

`executor_session.py launch` defaults to `--focus false`, but cmux creates an unfocused terminal with no view (its command would not start), so launch selects the new workspace briefly (~1s) to create the view, then restores your previous selection unless you already switched elsewhere. The receipt records `view_realized` and `focus_restored`; if no view appears, launch exits 4 with `error_code: no_terminal_view` and types nothing. The bridge refuses a viewless surface with `no_terminal_view` (bind/submit) or reports it per observe; it is not a transport fault and never triggers a resend.

**Prompt suggestions.** After each turn Claude Code renders a predicted next prompt as dim ghost text in the input editor; plain-text `read-screen` cannot tell it from typed text, so the screen would classify as `staged` and submit would refuse `busy`. `executor_session.py launch` therefore passes `--env CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false` to `new-workspace` (the receipt records `prompt_suggestions: "disabled"`; the command and argv, hence the pid identity checks, are unchanged). `bridge_bind` reads that one variable from the Claude process environment (nothing else is kept) and records `prompt_suggestions: "disabled"|"enabled_or_unknown"` on the binding. Binding a hand-started session requires suggestions off: start it with `CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false claude ...` or set `promptSuggestionEnabled: false` in settings (a settings-only opt-out is not visible to the bridge, so the binding stays `enabled_or_unknown` and the hint remains). When a binding is not `disabled` and the editor shows `staged` with no request of ours in flight, observe and the submit `busy` refusal carry an `instruction` naming this. The bridge never clears the editor.

Other environment variables: `CMUX_BRIDGE_STATE_DIR`, `CMUX_BRIDGE_SOCKET`, `CMUX_BRIDGE_CLI`, `CMUX_BRIDGE_PASSWORD_FILE`, `CMUX_BRIDGE_CLAUDE_PROJECTS`.

## Permissions: bypass by default

`executor_session.py launch` starts Claude with `--dangerously-skip-permissions` by default. The executor can run any command and edit any file the user can, without prompting. Launch only in worktrees you are willing to have modified, and pass `--permission-mode <mode>` to use a stricter posture instead.

## State directory is sensitive

State lives in `~/.local/state/tandem` (override `CMUX_BRIDGE_STATE_DIR`), mode 0700: `leases`, `bindings`, `requests`, `receipts`, `locks`, `tasks`, plus launch receipts. Request records contain the full text sent to Claude. Treat it like a transcript; do not commit or share it.

## Install

Prerequisites: macOS, cmux, the `claude` and `codex` CLIs, `uv`, Python 3.11+.

1. `./install.sh` links `skill/cmux-driver` into `${CODEX_HOME:-~/.codex}/skills/` and prints the `codex mcp add tandem ...` command. Run that command yourself. `./install.sh --uninstall` removes the symlink.
2. Make sure the cmux socket is reachable (see [docs/installation.md](docs/installation.md)).
3. Restart Codex; `codex mcp list` should show `tandem`.

Details: [docs/installation.md](docs/installation.md), [docs/operations.md](docs/operations.md).

## Testing

- Offline simulation suite (no cmux needed):
  `uv run --python 3.13 --with pytest python -m pytest bridge/tests -q -p no:cacheprovider`
- Live acceptance (`bridge/run_live.sh`) drives a real cmux session. Operator-run only, never CI; it requires a clean worktree and a passing offline suite first. Its default interpreter is `uv run --quiet --project "$REPO_ROOT" --extra test python`; override with `PY`.

## Optional AGENTS.md snippet for Codex

```markdown
## Driving a Claude Code session

- To supervise or delegate to Claude, use the `cmux-driver` skill; Claude is the sole writer in that worktree.
- Open the executor with `python3 "${CODEX_HOME:-$HOME/.codex}/skills/cmux-driver/scripts/executor_session.py" launch --cwd <worktree>` and an explicit `--model`; reuse a session only after `verify` passes.
- Bind by exact UUIDs, with a stable `controller_id` for this Codex session (never shared with another session).
- Prove delivery by transcript acceptance; keep an `uncertain` request under the same `request_id` and reconcile, never resend.
- Never send competing tasks or compact during conflicting work.
```
