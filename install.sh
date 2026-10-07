#!/usr/bin/env bash
# Install the cmux-driver skill for Codex (symlink) and print the MCP registration command.
# Usage: ./install.sh [--uninstall]
# Never edits AGENTS.md, CLAUDE.md or config.toml, and never runs `codex mcp add` for you.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$REPO_ROOT/skill/cmux-driver"
SKILLS_DIR="${CODEX_HOME:-$HOME/.codex}/skills"
DEST="$SKILLS_DIR/cmux-driver"

if [ "${1:-}" = "--uninstall" ]; then
  if [ -L "$DEST" ] && [ "$(readlink "$DEST")" = "$SRC" ]; then
    rm "$DEST"
    echo "removed symlink $DEST"
  elif [ -e "$DEST" ] || [ -L "$DEST" ]; then
    echo "not removing $DEST: it is not the symlink this script created" >&2
    exit 1
  else
    echo "nothing to remove at $DEST"
  fi
  echo "If registered, remove the MCP server yourself: codex mcp remove tandem"
  exit 0
fi

if [ -n "${1:-}" ]; then
  echo "usage: $0 [--uninstall]" >&2
  exit 2
fi

mkdir -p "$SKILLS_DIR"
if [ -L "$DEST" ]; then
  if [ "$(readlink "$DEST")" = "$SRC" ]; then
    echo "skill already linked: $DEST -> $SRC"
  else
    echo "refusing to replace $DEST: it is a symlink to $(readlink "$DEST")" >&2
    exit 1
  fi
elif [ -e "$DEST" ]; then
  echo "refusing to clobber $DEST: it exists and is not a symlink" >&2
  exit 1
else
  ln -s "$SRC" "$DEST"
  echo "linked $DEST -> $SRC"
fi

cat <<MSG

Register the bridge with Codex (not run by this script). Review, then run:

  codex mcp add tandem -- uv run --quiet --project "$REPO_ROOT" tandem-bridge

Password-mode cmux socket only (see docs/installation.md), add an env var:

  codex mcp add tandem --env CMUX_BRIDGE_PASSWORD_FILE="\$HOME/.config/tandem/cmux-socket-password" \\
    -- uv run --quiet --project "$REPO_ROOT" tandem-bridge

Then restart Codex and check: codex mcp list
MSG
