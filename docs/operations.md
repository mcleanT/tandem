# Operations

## Normal flow

1. `python3 "${CODEX_HOME:-$HOME/.codex}/skills/cmux-driver/scripts/executor_session.py" launch --cwd <worktree> --model <family>` (the install.sh symlink; same file as `skill/cmux-driver/scripts/executor_session.py` in the repo) opens a dedicated Claude workspace and writes a launch receipt (`--dry-run` shows the resolved model and argv). Default permission posture is `--dangerously-skip-permissions`; use `--permission-mode` for a stricter one.
2. `bridge_discover`, then `bridge_bind` with exact UUIDs, a stable, non-empty `controller_id` (one per Codex session, never shared; worktree identity is the case-insensitive-canonical path) and `expected_model`. A new writer bind by the same controller supersedes its previous writer binding.
3. `bridge_submit` with a fresh `request_id` and the current `expected_revision`; `bridge_wait` until `accepted`, then `turn_complete`. Loop on non-terminal wait results.
4. `bridge_observe` for state and events; `bridge_compact` around 30% context used, before 40%.
5. `bridge_release` when done. It drops leases only; the surface and process stay.

Reusing an existing session: run `executor_session.py verify --surface ... --pid ...` (same path as above) first.

Prompt suggestions are disabled for launched sessions (`CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION=false` via `--env`; receipt `launch.prompt_suggestions`), because Claude Code's ghost-text next-prompt suggestion looks like typed text to `read-screen` and would make an idle session read `staged` / submit `busy`. A hand-started session must be started with that env var (or `promptSuggestionEnabled: false`) before binding; the binding's `prompt_suggestions` shows `enabled_or_unknown` otherwise and observe/`busy` add an `instruction` when it sees `staged` with nothing of ours in flight. Clear a stray suggestion by hand; the bridge does not.

## Pause

Retention: in-flight request records hold the prompt text; terminal records keep only sha256 + counts; `tasks/<binding_id>/` (0444 task briefs, written only after a submit passes all refusal gates) is deleted on `bridge_release` unless a non-terminal request is in flight; the CLI call log is capped at 500 entries with `send` text redacted. See README "Retention of prompt text".

`bridge_pause` / `bridge_resume` (owner `controller_id` only; as is every other binding tool). Paused bindings refuse submit and compact; use it to stop delivery without releasing the lease. Submit, reconcile and compact re-check lease and pause under one lock right before Enter. A pause after staging leaves the payload staged, no Enter: resume, then re-call `bridge_submit` with the SAME `request_id`. Compact is not re-Entered: resume, clear the staged `/compact`, call `bridge_compact` with a NEW `request_id`. A rebind of the same surface carries the pause over, even after release or lease expiry; `bridge_release` does not clear it. Residual race: the check and keystroke are not atomic.

## Troubleshooting

| Symptom | Cause and action |
|---|---|
| `bridge_discover` returns `transport.ok: false` | cmux socket unreachable (not running, `cmuxOnly` from outside cmux, wrong password). Follow the reported requirement; see installation.md. |
| Bridge ignores the password | The file is group/world readable. `chmod 600`. |
| `lease_conflict` | A different controller holds the worktree or surface writer lease. Never take it over: wait for expiry, ask its owner to release, or bind as `role=monitor`. |
| `lease_expired` | Lease TTL (default 1800 s, `lease_ttl_s`) lapsed; no tool renews it. Rebind the SAME Claude session with the SAME `controller_id`; do not launch a new session. |
| `lease_lost` | This binding was superseded (same-controller rebind, or its lease no longer held). Use the newest binding id. |
| `blocked_modal` / state `modal` | Claude is on a permission or selection prompt. Observe, resolve it in the cmux surface, retry. Do not submit over it. |
| `send_response_lost`, `send_failed`, `pre_enter_gate_error` | Text may be staged, Enter not pressed. Retry the SAME `request_id` to reconcile. `send_not_attempted`: nothing sent. `bad_delivery`: bridge bug, nothing sent; report it. |
| status `uncertain` | Delivery not proven. Keep the same `request_id`, call `bridge_wait`/`bridge_submit` again to reconcile; never use a new id or resend text. |
| `paused` | Binding was paused. `bridge_resume` with the owning `controller_id`. A pause after staging yields `paused` with the request marked `uncertain`, payload staged, no Enter sent; re-call with the SAME `request_id` (submit) or clear `/compact` and use a NEW one (compact). |
| `not_owner` | Any binding tool (observe, submit, wait, compact, pause, resume, release) called with a different `controller_id` than the bind. |
| `stale_revision` | Observe again and pass the current `expected_revision`. |
| `busy` | Claude is not at an idle prompt. Wait for `idle`. |
| `model_required`, `model_unknown`, `model_policy`, `planning_session`, `model_mismatch`, `model_unverified`, `model_changed` | Model gate. Relaunch with an explicit model, bind with the right `expected_model`, or check the session footer. Planning-only families (default `fable`) are refused for implementation; adjust `TANDEM_PLANNING_ONLY_MODELS`. |
| `model_policy_config` | Malformed `TANDEM_*` env value; fix it (fails closed). |
| Unknown model spelling refused | Set `TANDEM_ALLOW_UNKNOWN_MODEL=1` if intended. |
| `no_terminal_view` (bind/observe/submit; launcher receipt `error_code`) | cmux gives a terminal created unfocused no view: `read-screen` fails with `Failed to read terminal text` and its shell never starts. Not a transport fault; nothing was sent, no resend. Select the workspace once (`cmux select-workspace`) or relaunch via `executor_session.py launch` (which realizes the view with a brief ~1s selection, then restores your selection), then retry. |
| `identity` / `unknown_binding` / `released` after a cmux restart | The binding is stale (surface UUIDs and pids changed). Run `bridge_discover`, `bridge_release` the old binding, bind again. |

## Live acceptance

`bridge/run_live.sh` runs the offline suite, then `bridge/live_acceptance.py` against a real cmux (model via `CMUX_BRIDGE_ACCEPTANCE_MODEL`, default `sonnet`). Default interpreter: `uv run --quiet --project "$REPO_ROOT" --extra test python` (override with `PY`). Operator-run only; clean worktree required (`ALLOW_DIRTY=1` overrides). Evidence goes to `checks/live-<timestamp>/`.
