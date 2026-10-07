#!/usr/bin/env bash
# Launcher for the live acceptance run. Operator-run only; never run from CI. The source under test must be frozen (clean worktree) and
# the simulated suite must pass BEFORE anything is allowed to touch a real cmux session: a broken
# dependency must stop the run, not be logged and stepped over. The script's own exit status is the
# failing step's status, so a caller can never read a failed precheck as a completed live run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRIDGE_DIR="$REPO_ROOT/bridge"
PY="${PY:-uv run --quiet --project \"$REPO_ROOT\" --extra test python}"
OUT_DIR="${OUT_DIR:-$REPO_ROOT/checks/live-$(date -u +%Y%m%dT%H%M%SZ)}"
PRECHECK_CMD="${PRECHECK_CMD:-$PY -m pytest tests -q -p no:cacheprovider}"
LIVE_CMD="${LIVE_CMD:-$PY live_acceptance.py}"

mkdir -p "$OUT_DIR"
STATUS_JSON="$OUT_DIR/launcher-status.json"

dirty="$(cd "$REPO_ROOT" && git status --porcelain | grep -v '^?? checks/' || true)"  # untracked evidence under checks/ is not source
if [ -n "$dirty" ] && [ "${ALLOW_DIRTY:-0}" != "1" ]; then
  printf 'refusing to launch: worktree is not clean (set ALLOW_DIRTY=1 to override)\n%s\n' "$dirty" >&2
  head="$(cd "$REPO_ROOT" && git rev-parse --verify -q HEAD 2>/dev/null || echo unknown)"
  printf '{"head":"%s","dirty":true,"precheck_exit":null,"live_exit":null,"outcome":"refused_dirty_worktree"}\n' "$head" > "$STATUS_JSON"
  exit 3
fi

HEAD="$(cd "$REPO_ROOT" && git rev-parse --verify -q HEAD 2>/dev/null || echo unknown)"

set +e
( cd "$BRIDGE_DIR" && eval "$PRECHECK_CMD" ) > "$OUT_DIR/precheck.log" 2>&1
PRECHECK_EXIT=$?
set -e

if [ "$PRECHECK_EXIT" -ne 0 ]; then
  printf '{"head":"%s","dirty":%s,"precheck_exit":%d,"live_exit":null,"outcome":"precheck_failed"}\n' \
    "$HEAD" "$([ -n "$dirty" ] && echo true || echo false)" "$PRECHECK_EXIT" > "$STATUS_JSON"
  echo "precheck failed (exit $PRECHECK_EXIT); the live runner was NOT launched. See $OUT_DIR/precheck.log" >&2
  exit "$PRECHECK_EXIT"
fi

set +e
( cd "$BRIDGE_DIR" && eval "$LIVE_CMD" ) > "$OUT_DIR/live.log" 2>&1
LIVE_EXIT=$?
set -e

printf '{"head":"%s","dirty":%s,"precheck_exit":%d,"live_exit":%d,"outcome":"%s","out_dir":"%s"}\n' \
  "$HEAD" "$([ -n "$dirty" ] && echo true || echo false)" "$PRECHECK_EXIT" "$LIVE_EXIT" \
  "$([ "$LIVE_EXIT" -eq 0 ] && echo completed || echo live_failed)" "$OUT_DIR" > "$STATUS_JSON"
exit "$LIVE_EXIT"
