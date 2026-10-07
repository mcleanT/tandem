# Installation

Cloning installs nothing. Three steps, each changing something different.

| Step | What changes |
|---|---|
| 1. `./install.sh` | One symlink: `${CODEX_HOME:-~/.codex}/skills/cmux-driver` -> `skill/cmux-driver`. Refuses to replace anything that is not that symlink. |
| 2. `codex mcp add tandem ...` | Codex's MCP registry (`config.toml`), by the command `install.sh` prints. You run it. |
| 3. cmux socket access | Only if the bridge is not run from inside cmux (see below). |

Prerequisites: macOS, cmux, `claude` and `codex` CLIs, `uv`, Python 3.11+. Dependencies (`fastmcp==3.1.0`, `mcp==1.26.0`) are resolved by `uv` from `pyproject.toml`.

## MCP registration

```
codex mcp add tandem -- uv run --quiet --project /path/to/tandem tandem-bridge
```

Add `--env CMUX_BRIDGE_PASSWORD_FILE=...` only in password mode. Optional env: `CMUX_BRIDGE_STATE_DIR`, `CMUX_BRIDGE_SOCKET`, `CMUX_BRIDGE_CLI`, `TANDEM_PLANNING_ONLY_MODELS`, `TANDEM_ALLOW_UNKNOWN_MODEL`. Verify with `codex mcp list`, restart Codex, then call `bridge_discover`.

## cmux socket access modes

The bridge process must be able to open the cmux socket.

- `cmuxOnly` (cmux default): works only for processes descended from the cmux app, for example a Codex running inside a cmux terminal. Nothing to configure.
- `password`: needed when Codex runs outside cmux. Set `automation.socketControlMode=password` in cmux and keep the password in an owner-only file (mode 0600) named by `CMUX_BRIDGE_PASSWORD_FILE`. Never pass it on argv or commit it.
- Never use `allowAll`; the bridge does not use TCP.

`scripts/cmux_access_plan.sh` (in this repo; run from the repo root) prepares password mode safely. It edits only `automation.socketControlMode` and `automation.socketPassword` in `~/.config/cmux/cmux.json`, writes a backup and receipt first, and never prints the secret.

```
scripts/cmux_access_plan.sh status
scripts/cmux_access_plan.sh dry-run
scripts/cmux_access_plan.sh prepare-secret
scripts/cmux_access_plan.sh activate --confirm
scripts/cmux_access_plan.sh rollback --confirm
```

Defaults: password file `~/.config/tandem/cmux-socket-password`, backups `~/.config/tandem/backups`, receipts `~/.config/tandem/access-receipts`. Run `status` and `dry-run` first; `activate` and `rollback` refuse without `--confirm`.

## Uninstall

`./install.sh --uninstall`, then `codex mcp remove tandem`. State in `~/.local/state/tandem` is left for you to delete.
