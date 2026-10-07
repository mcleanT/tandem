# cmux runbook (tandem 0.1.0)

Facts about driving Claude Code in cmux. Trust `cmux --help` of the installed binary over any web doc. The CLI is on PATH inside cmux terminals; elsewhere use its absolute path. The socket is per-uid, `~/.local/state/cmux/cmux-<uid>.sock` (`~/.local/state/cmux/last-socket-path` names the live one). Set `CMUX_QUIET=1` to silence alias notices.

## 0. Launch your Claude session

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/cmux-driver/scripts/executor_session.py" launch \
  --cwd '<ABSOLUTE_WORKTREE>' --name '<TITLE>' \
  [--model opus|sonnet|haiku|<claude-* id>] [--purpose implementation|planning] \
  [--subagent-model sonnet] [--permission-mode <mode>] [--session-id <uuid>] \
  [--window <ref|uuid>] [--focus false] [--receipt <path>] [--dry-run]
```

- Default posture is `claude --dangerously-skip-permissions`; `--permission-mode <mode>` replaces it.
- It resolves the model policy (a planning-only model is refused for implementation before any cmux call; no `--model` means `opus`), pre-generates the session UUID, and runs `new-workspace --cwd … --command '<absolute claude> … --model <m> --session-id <uuid>' --focus false` with `CLAUDE_CODE_SUBAGENT_MODEL` exported so Claude's own workers do not inherit a planning model.
- It resolves workspace and surface UUIDs, waits (bounded, `--wait-s`, default 45) for the Claude prompt and footer, and reports the REQUESTED model next to the RESOLVED footer model. `ok: true` only when the families match and a prompt is visible.
- Receipt (default `$CMUX_BRIDGE_STATE_DIR/launches/<session>.json`, i.e. `~/.local/state/tandem/launches/`) carries command, UUIDs, pid, argv and footer evidence.
- Exit 4 = launched but unverified: a folder-trust dialog (never auto-confirmed; a human decides) or a footer mismatch. Do not bind or send until `verify` passes.
- `--focus false` never leaves your focus changed, but cmux creates an unfocused terminal with NO view (read-screen fails, the command does not start). So launch briefly (~1s) selects the new workspace, polls `read-screen` (0.25 s steps, `--realize-timeout-s`, default 10), then restores the previous selection ONLY if the new workspace is still the selected one. Receipt fields: `view_realized`, `focus_restored`. No view in time: exit 4, `error_code: no_terminal_view`, nothing typed.
- Nothing is ever typed into the session by the launcher. Never launch with a bare `claude` or an alias that inherits the settings default model, and never bind whatever session happens to be focused.

Reuse an existing session only after verification:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/cmux-driver/scripts/executor_session.py" verify \
  --surface <SURFACE_UUID> --pid <CLAUDE_PID> --cwd <WORKTREE> [--expected-model opus]
```

Exit 0 usable, 3 refused (`model_policy`, `planning_session`, `model_mismatch`), 4 `model_unverified` (no readable footer). `bridge_bind(purpose=…)` applies the same gate. `executor_session.py policy [--purpose P] [--model M]` prints the resolved policy.

Launch passes `--env CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false` to `new-workspace` (receipt `launch.prompt_suggestions`). Without it Claude Code shows a dim ghost-text next-prompt suggestion after each turn that plain-text `read-screen` cannot distinguish from typed text, so the surface reads `staged` and submit refuses `busy`. For a hand-started session, set that env var (or `promptSuggestionEnabled: false`) before binding.

Facts: `--session-id` fixes the transcript path (`~/.claude/projects/<slug>/<uuid>.jsonl`) before the first prompt. `new-workspace` may return only `OK workspace:N`; resolve UUIDs immediately (refs are reassigned). A `--focus false` workspace is readable and writable by UUID once its terminal view exists (launch realizes it; see above) and does not leave the user's focus changed.

## 1. Identity

```
cmux identify --json                   # caller (this terminal) + focused surface; both carry UUIDs
cmux list-workspaces --id-format both
cmux tree --all
cmux list-pane-surfaces --workspace <UUID> --id-format both
```

- Bind by UUID only. `surface:N`/`workspace:N` refs are display labels and change.
- Always pass `--surface <UUID>`; an unknown `--workspace` ref with no `--surface` can silently fall back to the caller's own surface.
- `identify` separates caller from focused. Reads and sends target a surface explicitly and do not depend on focus.
- `internal_error: Failed to read terminal text` means UNKNOWN state, not empty output. The bridge reports it as `no_terminal_view` (a surface created unfocused has no view): select its workspace once (`cmux select-workspace`) or relaunch via `executor_session.py launch`, then retry.
- Claude session identity comes from `agent.hook.*` events (`session_id: claude-<uuid>`, `_ppid: <pid>`, `cwd`) and `sidebar.metadata.updated` (surface to pid). A mismatch means re-bind.

## 2. Send and receipt

`cmux send` delivers text literally and does not submit; a trailing or embedded newline, or raw bracketed-paste markers, can submit. `send` returning OK is transport only. Staged text is not delivery. The bridge therefore sends single-line text or a short task-file reference, presses Enter once, and confirms by transcript hash. Never send to a surface you have not bound and read first.

## 3. Observe and wait

`cmux events --after <seq> --limit <n> --no-heartbeat` replays events; the ack carries `boot_id`, `latest_seq`, `gap`. A changed `boot_id` or `gap: true` means re-bind and reconcile. Hook events relayed: `PreToolUse`, `Stop`, `UserPromptSubmit`, `SessionStart` (also after `/compact`), `SessionEnd`, `SubagentStop`, `Notification`; `PreCompact` is not. The footer `Ctx Used: NN%` is Claude's own meter.

## 4. Unknown state and reconnect

Missing socket, refused connection, access denied, or an app restart (new `boot_id`) all mean state is UNKNOWN: stop dependent mutation, re-bind, reconcile from transcript and files.

## 5. Not verified

`automation` socket mode; behavior with the Mac locked, across network loss or a cmux update; the contents of Claude permission dialogs (the bridge classifies them `modal` and refuses input).
