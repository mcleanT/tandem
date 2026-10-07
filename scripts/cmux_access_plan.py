#!/usr/bin/env python3
"""cmux socket access: status / dry-run / activate / rollback helper for password mode.

Scope: prepared operation only. `activate` and `rollback` refuse to run without --confirm, and the
operator is expected to inspect `status` and `dry-run` first. Nothing here prints, logs, or
receipts a secret value: only path / mode / sha256 evidence ever leaves this module.

Design notes:

  1. Canonical file + precedence. ~/.config/cmux/cmux.json is the canonical, owned file (this is
     what the installed 0.64.17 binary and the upstream cmux-settings skill both describe: "cmux
     creates this template on launch when ~/.config/cmux/cmux.json is missing" / "Legacy
     settings.json files are read only as fallback for keys not present here"). The legacy
     ~/.config/cmux/settings.json is read-only evidence, never written by this module, and never
     merged silently into the canonical file. Evidence here is installed-binary-and-upstream-doc
     based; no specific pinned upstream source commit is claimed.
  2. JSONC-safe editing. `strip_jsonc`/`parse_jsonc` below are a small string-aware scanner (not a
     regex) so a `//` or a trailing comma inside a JSON string value is never touched. Malformed
     input raises ParseError before anything is written.
  3. Preimage rollback. Every `activate` writes a receipt (paths, sha256 hashes, mode, owned-leaf
     presence/hash state -- never secret values) and a full-file backup via a uniquely-named temp
     file (tempfile.mkstemp, so an immediate rollback can never collide with or overwrite a
     same-second backup). `rollback` restores the exact preimage of only the two owned leaves by
     reading them back out of that backup file, applied on top of the *current* file so any
     unrelated edit made after activation survives. If the owned leaves changed since activation in
     a way the receipt didn't predict, rollback refuses with a conflict instead of guessing.
  4. Preflight + failure safety. Every mutating command rejects symlinked destinations/secret/backup
     paths before creating, chmod'ing, reading the secret, or writing a backup. The secret must be a
     nonempty, owner-only (mode & 0o077 == 0) regular file. Rendering + self-check happen as normal
     Python function calls/exceptions (no `write_settings "$(render ...)"` command-substitution
     footgun) -- a failed render can never reach the atomic-write step.

Owned canonical keys: automation.socketControlMode, automation.socketPassword.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import stat as statmod
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MODE_KEY = ("automation", "socketControlMode")
PASSWORD_KEY = ("automation", "socketPassword")
ACTIVATED_MODE = "password"


class ParseError(Exception):
    """Input did not parse as valid JSONC (or wasn't a JSON object)."""


class PreflightError(Exception):
    """A safety precondition failed; nothing was mutated."""


class ConflictError(Exception):
    """The owned settings leaves changed since the referenced activation; refusing to guess."""


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    settings_path: Path  # canonical: ~/.config/cmux/cmux.json
    legacy_settings_path: Path  # fallback evidence only: ~/.config/cmux/settings.json
    secret_file: Path  # CMUX_BRIDGE_PASSWORD_FILE
    backup_dir: Path
    receipt_dir: Path
    app_saved_password_file: (
        Path  # ~/Library/Application Support/cmux/socket-control-password
    )
    state_dir_password_file: (
        Path  # ~/.local/state/cmux/socket-control-password (upstream migration target)
    )
    cli_path: Path  # cmux CLI binary, used only for the best-effort live probe


def build_config_from_env(env: dict | None = None) -> Config:
    e = env if env is not None else os.environ
    home = Path(e.get("HOME", str(Path.home())))
    return Config(
        settings_path=Path(
            e.get("CMUX_SETTINGS_FILE", str(home / ".config/cmux/cmux.json"))
        ),
        legacy_settings_path=Path(
            e.get("CMUX_LEGACY_SETTINGS_FILE", str(home / ".config/cmux/settings.json"))
        ),
        secret_file=Path(
            e.get(
                "CMUX_BRIDGE_PASSWORD_FILE",
                str(home / ".config/tandem/cmux-socket-password"),
            )
        ),
        backup_dir=Path(
            e.get(
                "CMUX_ACCESS_BACKUP_DIR",
                str(home / ".config/tandem/backups"),
            )
        ),
        receipt_dir=Path(
            e.get(
                "CMUX_ACCESS_RECEIPT_DIR",
                str(home / ".config/tandem/access-receipts"),
            )
        ),
        app_saved_password_file=Path(
            e.get(
                "CMUX_APP_SAVED_PASSWORD_FILE",
                str(home / "Library/Application Support/cmux/socket-control-password"),
            )
        ),
        state_dir_password_file=Path(
            e.get(
                "CMUX_APP_STATE_DIR_PASSWORD_FILE",
                str(home / ".local/state/cmux/socket-control-password"),
            )
        ),
        cli_path=Path(
            e.get(
                "CMUX_BRIDGE_CLI", "/Applications/cmux.app/Contents/Resources/bin/cmux"
            )
        ),
    )


# ---------------------------------------------------------------------------
# JSONC: string-aware scanner (not regex) -> plain JSON -> dict
# ---------------------------------------------------------------------------


def _skip_ws_and_comments(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j == -1:
                raise ParseError("unterminated block comment")
            i = j + 2
        else:
            break
    return i


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas, without ever touching string content."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            start = i
            i += 1
            closed = False
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == "\n":
                    raise ParseError(
                        "unterminated string literal (bare newline before closing quote)"
                    )
                if text[i] == '"':
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                raise ParseError("unterminated string literal")
            out.append(text[start:i])
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j == -1:
                raise ParseError("unterminated block comment")
            i = j + 2
            continue
        if c == ",":
            k = _skip_ws_and_comments(text, i + 1)
            if k < n and text[k] in "}]":
                i += 1  # drop trailing comma
                continue
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_jsonc(text: str) -> dict:
    stripped = strip_jsonc(text)
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ParseError(
            f"invalid JSON after comment/trailing-comma removal: {e}"
        ) from e
    if not isinstance(obj, dict):
        raise ParseError("top-level JSON value must be an object")
    return obj


def render_jsonc(doc: dict) -> str:
    rendered = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    reparsed = json.loads(
        rendered
    )  # checked dependency: never write what didn't round-trip
    if reparsed != doc:
        raise ParseError("rendered settings failed round-trip self-check")
    return rendered


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


def sha256_bytes(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def _reject_symlink(path: Path, what: str) -> None:
    if path.is_symlink():
        raise PreflightError(f"refusing: {what} is a symlink: {path}")
    parent = path.parent
    if parent.exists() and parent.is_symlink():
        raise PreflightError(
            f"refusing: {what} parent directory is a symlink: {parent}"
        )


def _validate_secret(path: Path) -> None:
    _reject_symlink(path, "secret file")
    if not path.exists():
        raise PreflightError(
            f"refusing: secret file absent: {path} (run prepare-secret first)"
        )
    st = path.lstat()
    if not statmod.S_ISREG(st.st_mode):
        raise PreflightError(f"refusing: secret path is not a regular file: {path}")
    if st.st_size == 0:
        raise PreflightError(f"refusing: secret file is empty: {path}")
    if st.st_mode & 0o077:
        raise PreflightError(
            f"refusing: secret file is not owner-only (mode {oct(st.st_mode & 0o777)}): {path}"
        )


# ---------------------------------------------------------------------------
# owned-leaf state (never carries the raw password -- only presence + hash)
# ---------------------------------------------------------------------------


def _get_leaf(doc: dict, key: tuple[str, str]) -> dict:
    a, b = key
    sub = doc.get(a)
    if not isinstance(sub, dict) or b not in sub:
        return {"present": False}
    return {"present": True, "value": sub[b]}


def owned_state(doc: dict) -> dict:
    mode_leaf = _get_leaf(doc, MODE_KEY)
    pw_leaf = _get_leaf(doc, PASSWORD_KEY)
    mode_state = {"present": mode_leaf["present"], "value": mode_leaf.get("value")}
    pw_state = {
        "present": pw_leaf["present"],
        "sha256": sha256_str(str(pw_leaf["value"])) if pw_leaf["present"] else None,
    }
    return {".".join(MODE_KEY): mode_state, ".".join(PASSWORD_KEY): pw_state}


# ---------------------------------------------------------------------------
# atomic write / unique backups / receipts
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    _reject_symlink(path, "destination file")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    """Byte-exact sibling of `_atomic_write`, used to restore an app-owned credential file
    without any text-mode/encoding translation risk."""
    _reject_symlink(path, "destination file")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _make_backup(src: Path, backup_dir: Path, tag: str) -> Path:
    _reject_symlink(backup_dir, "backup dir")
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    fd, name = tempfile.mkstemp(
        prefix=f"{src.name}.{tag}.", suffix=".bak", dir=str(backup_dir)
    )
    bpath = Path(name)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(src.read_bytes())
        os.chmod(bpath, 0o600)
    except BaseException:
        bpath.unlink(missing_ok=True)
        raise
    return bpath


def _write_receipt(receipt_dir: Path, prefix: str, data: dict) -> Path:
    _reject_symlink(receipt_dir, "receipt dir")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(receipt_dir, 0o700)
    fd, name = tempfile.mkstemp(
        prefix=f"{prefix}.", suffix=".json", dir=str(receipt_dir)
    )
    rpath = Path(name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.chmod(rpath, 0o600)
    except BaseException:
        rpath.unlink(missing_ok=True)
        raise
    return rpath


def _verify_receipt(receipt_path: Path, expected: dict) -> None:
    """Read back a just-persisted receipt and confirm it matches exactly. Used to persist AND
    verify the prepared transaction journal before any live settings mutation happens (see
    module docstring correction 3 and the activation-followup review)."""
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise PreflightError(
            f"refusing: prepared receipt missing immediately after write: {receipt_path}"
        )
    try:
        on_disk = json.loads(receipt_path.read_text())
    except Exception as e:
        raise PreflightError(
            f"refusing: prepared receipt failed to verify (unreadable): {e}"
        ) from e
    if on_disk != expected:
        raise PreflightError(
            "refusing: prepared receipt failed round-trip verification"
        )


def _latest_receipt(receipt_dir: Path, settings_path: Path, op: str) -> Path | None:
    if not receipt_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in sorted(receipt_dir.iterdir()):
        if not p.is_file() or p.is_symlink():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if data.get("op") == op and data.get("settings_file") == str(settings_path):
            candidates.append((p.stat().st_mtime_ns, p))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


# ---------------------------------------------------------------------------
# read-only evidence helpers (legacy file, app-saved-password file) -- never mutated
# ---------------------------------------------------------------------------


def _file_evidence(path: Path) -> dict:
    if path.is_symlink():
        return {"path": str(path), "present": "symlink-refused", "sha256": None}
    if path.is_file():
        return {"path": str(path), "present": True, "sha256": sha256_bytes(path)}
    return {"path": str(path), "present": False, "sha256": None}


def _mode_str(path: Path) -> str | None:
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# candidate credential stores
# ---------------------------------------------------------------------------
# The installed app's actual socket-control-password store is UNVERIFIED: the
# 0.64.17 binary carries BOTH legacy-Keychain symbols and state-dir-migration
# symbols, and no credential exists in any store today. Rather than guess which
# one a live password activation will populate, we baseline EVERY candidate file
# store before the mutation and, after the app's supported reload, OBSERVE which
# one actually changed (cmd_observe_postimage). Keychain is not a file, so only
# its non-secret metadata (presence/service/account) is baselined and observed --
# never the secret value.
#
# Each tuple: (store_key, filesystem path, receipt key). The receipt key mirrors
# the app_saved_password_file block shape so rollback reconciliation is generic.
KEYCHAIN_SERVICE = "cmux"
KEYCHAIN_ACCOUNT = "socket-control-password"


def _candidate_file_stores(cfg: Config) -> list[tuple[str, Path, str]]:
    return [
        ("app_support", cfg.app_saved_password_file, "app_saved_password_file"),
        ("state_dir", cfg.state_dir_password_file, "state_dir_password_file"),
    ]


def _keychain_metadata(env: dict | None = None) -> dict:
    """Non-secret Keychain presence probe for the cmux socket-control-password item.

    Uses `security find-generic-password` WITHOUT `-w`, so the secret value is
    never requested, printed, or returned -- only whether such an item exists.
    Best-effort and platform-guarded: on a non-darwin host, when `security` is
    absent, or on any error, returns {"available": False} rather than raising, so
    baselining and observation never depend on the Keychain being probeable.
    """
    import shutil
    import subprocess

    e = env if env is not None else os.environ
    if e.get("CMUX_SKIP_KEYCHAIN_PROBE"):
        # Hermetic opt-out (tests, or a host where a real Keychain query is undesirable).
        return {"available": False, "reason": "disabled"}
    if sys.platform != "darwin":
        return {"available": False, "reason": "not_darwin"}
    security = shutil.which("security")
    if not security:
        return {"available": False, "reason": "security_tool_absent"}
    try:
        proc = subprocess.run(
            [
                security,
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "reason": "probe_failed"}
    # rc 0 => an item exists; rc 44 (errSecItemNotFound) => absent; other => unknown.
    if proc.returncode == 0:
        present: bool | None = True
    elif proc.returncode == 44:
        present = False
    else:
        present = None
    return {
        "available": True,
        "present": present,
        "service": KEYCHAIN_SERVICE,
        "account": KEYCHAIN_ACCOUNT,
    }


def _read_store_snapshot(store_path: Path) -> tuple[dict, bytes | None]:
    """Read a candidate store EXACTLY ONCE and return (evidence, raw_bytes).

    The returned sha256 is computed from precisely the `raw` bytes also returned, so a caller
    that both records the hash AND inspects the content is guaranteed they describe the same
    immutable read. This is what prevents the snapshot-connection TOCTOU
    (activation-snapshot-connection review): hashing one read and verifying a separate read
    could record a hash for bytes that never passed the connection check. A symlink is refused
    (never followed); an absent or unreadable file yields raw=None.
    """
    if store_path.is_symlink():
        return {
            "path": str(store_path),
            "present": "symlink-refused",
            "sha256": None,
        }, None
    try:
        raw = store_path.read_bytes()
    except OSError:
        return {"path": str(store_path), "present": False, "sha256": None}, None
    return {
        "path": str(store_path),
        "present": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }, raw


def _bytes_plaintext_is_operation_secret(raw: bytes, secret_value: str) -> bool:
    """Privately decide whether the ALREADY-READ store bytes are, as plaintext, the credential
    this operation activated with.

    Takes the exact bytes the caller will also hash (no second read) so the recorded hash and
    the verified content are guaranteed identical. An absent->present transition is NOT proof of
    ownership (activation-observed-creation review); the only supported evidence is that the
    on-disk representation, decoded as plaintext, logically equals the prepared secret. The
    comparison is entirely in memory with a constant-time compare; neither the secret nor the
    store content is ever logged, returned, or written. A non-UTF-8 (opaque/encoded)
    representation cannot be connected this way and returns False -> the caller reports
    unknown/unsupported and preserves the file.
    """
    import hmac

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    a = text.strip().encode("utf-8")
    b = secret_value.strip().encode("utf-8")
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# app-saved credential reconciliation (finding 2, activation-followup review)
# ---------------------------------------------------------------------------


def _reconcile_store_credential(
    receipt: dict, receipt_key: str, store_path: Path, mutation_landed: bool
) -> tuple[str, dict]:
    """Reconcile ONE candidate credential store's file on rollback.

    Store-agnostic (app-support, state-dir, ...): the receipt block at
    `receipt_key` carries this store's pre-activation evidence, backup, and any
    verified `operation_postimage`. The safety logic is identical for every store.

    Preexistence, or absent-before/present-after timing, is NOT proof that a credential
    change was caused by this operation; treating it as sufficient could delete or overwrite
    an unrelated credential. So:

      1. If the settings mutation itself never landed (`mutation_landed` is False -- the
         prepared journal was persisted but the atomic replace never happened, e.g. process
         interruption), this operation had NO credential side effect by construction. Nothing
         is ever touched.
      2. If nothing changed relative to the pre-activation evidence, nothing is touched
         (trivially safe, no evidence required).
      3. Otherwise, acting on the change (removing a newly-present file, or restoring a changed
         preexisting one) requires a VERIFIED "operation postimage" hash on the receipt --
         the actual sha256 of the credential this operation is independently known to have
         produced, as recorded (per store) by `amend_receipt_with_verified_store_postimage`
         after `cmd_observe_postimage` observed which store actually changed during a bounded,
         real, controlled activation. Absent that verified evidence, or if the current file
         doesn't match it exactly (an unrelated credential, or an unrelated rotation), this
         refuses to touch the file and reports why.
    """
    info = receipt.get(receipt_key)
    current = _file_evidence(store_path)
    if not isinstance(info, dict) or "pre" not in info:
        # Pre-fix / unmodeled receipt shape carries no pre-activation evidence -- never guess.
        return "unknown_receipt_shape_skipped", current

    if not mutation_landed:
        return "not_landed_untouched", current

    pre = info["pre"]

    if (
        pre.get("present") == current["present"]
        and pre.get("sha256") == current["sha256"]
    ):
        label = (
            "unchanged_absent" if not pre.get("present") else "unchanged_preexisting"
        )
        return label, current

    op_postimage = info.get("operation_postimage")
    op_sha = op_postimage.get("sha256") if isinstance(op_postimage, dict) else None
    if not op_sha:
        return "no_verified_operation_postimage_untouched", current

    _reject_symlink(store_path, f"{receipt_key} store")

    if current["sha256"] != op_sha:
        # Changed to something other than this operation's verified write: an unrelated
        # credential (if none existed before) or an unrelated rotation (if one did). Never
        # touch it.
        label = (
            "unrelated_credential_untouched"
            if not pre.get("present")
            else "unrelated_rotation_untouched"
        )
        return label, current

    # The current content IS exactly this operation's verified postimage.
    if not pre.get("present"):
        store_path.unlink()
        return "removed_operation_owned", _file_evidence(store_path)

    backup_path_str = info.get("backup_path")
    backup_sha = info.get("backup_sha256")
    if backup_path_str and backup_sha:
        bpath = Path(backup_path_str)
        if (
            not bpath.is_symlink()
            and bpath.is_file()
            and sha256_bytes(bpath) == backup_sha
        ):
            _atomic_write_bytes(store_path, bpath.read_bytes(), mode=0o600)
            return "restored_preexisting", _file_evidence(store_path)

    return "conflict_preexisting_not_restored", current


def _reconcile_app_password(
    cfg: Config, receipt: dict, mutation_landed: bool
) -> tuple[str, dict]:
    """Backward-compatible wrapper: reconcile the app-support saved-credential file."""
    return _reconcile_store_credential(
        receipt, "app_saved_password_file", cfg.app_saved_password_file, mutation_landed
    )


def amend_receipt_with_verified_store_postimage(
    receipt_path: Path, receipt_key: str, verified_sha256: str
) -> dict:
    """Amend an already-persisted activation receipt with a VERIFIED operation-owned postimage
    hash for ONE candidate credential store's file (app-support, state-dir, ...).

    This is deliberately a separate, explicit step from `cmd_activate`: the app's own write of
    its credential store (if any) happens asynchronously via the app's own supported reload,
    strictly AFTER the settings mutation returns, so it cannot be known at `cmd_activate` call
    time. Only a caller that has itself performed the app's supported reload during a
    controlled, live activation AND read back + hashed the app's actual on-disk credential
    representation may call this with that verified hash -- as `cmd_observe_postimage` does when
    it observes an absent->present transition on exactly this store. `rollback` only ever
    removes or restores a changed credential when this verified postimage is present on the
    receipt and the file it is acting on matches it exactly; nothing here weakens that.

    Secret VALUES never pass through this function or land in the receipt -- only the pre-
    verified sha256 hash does.
    """
    _reject_symlink(receipt_path, "receipt file")
    if not receipt_path.is_file():
        raise PreflightError(f"refusing: receipt file missing: {receipt_path}")
    receipt = json.loads(receipt_path.read_text())
    info = receipt.get(receipt_key)
    if not isinstance(info, dict):
        raise PreflightError(
            f"refusing: receipt has no {receipt_key} evidence to amend"
        )
    info = dict(info)
    info["operation_postimage"] = {
        "sha256": verified_sha256,
        "recorded_ts": _now_iso(),
    }
    receipt = dict(receipt)
    receipt[receipt_key] = info
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    _atomic_write(receipt_path, rendered, mode=0o600)
    _verify_receipt(receipt_path, receipt)
    return receipt


def amend_receipt_with_verified_app_password_postimage(
    receipt_path: Path, verified_sha256: str
) -> dict:
    """Backward-compatible wrapper for the app-support store."""
    return amend_receipt_with_verified_store_postimage(
        receipt_path, "app_saved_password_file", verified_sha256
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_status(cfg: Config) -> dict:
    out: dict = {
        "settings_file": {
            "path": str(cfg.settings_path),
            "exists": cfg.settings_path.is_file(),
            "mode": _mode_str(cfg.settings_path),
            "sha256": sha256_bytes(cfg.settings_path)
            if cfg.settings_path.is_file()
            else None,
        },
        "legacy_settings_file": _file_evidence(cfg.legacy_settings_path),
        "secret_file": {
            "path": str(cfg.secret_file),
            "exists": cfg.secret_file.is_file(),
            "mode": _mode_str(cfg.secret_file),
            "sha256": sha256_bytes(cfg.secret_file)
            if cfg.secret_file.is_file()
            else None,
        },
        "app_saved_password_file": _file_evidence(cfg.app_saved_password_file),
    }
    if cfg.settings_path.is_file():
        try:
            out["owned_leaves"] = owned_state(
                parse_jsonc(cfg.settings_path.read_text())
            )
        except ParseError as e:
            out["owned_leaves"] = {"error": str(e)}
    else:
        out["owned_leaves"] = {"error": "settings file absent"}
    out["live_access_mode"] = _probe_live_mode(cfg.cli_path)
    return out


def _probe_live_mode(cli_path: Path) -> str:
    import subprocess

    try:
        r = subprocess.run(
            [str(cli_path), "capabilities"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "CMUX_QUIET": "1"},
        )
        if r.returncode != 0 or not r.stdout.strip():
            return "unreachable"
        return json.loads(r.stdout).get("access_mode", "unknown")
    except Exception:
        return "unreachable"


def cmd_dry_run(cfg: Config) -> dict:
    result: dict = {"status": cmd_status(cfg)}
    if cfg.settings_path.is_file():
        try:
            current_doc = parse_jsonc(cfg.settings_path.read_text())
            new_doc = copy.deepcopy(current_doc)
            automation = dict(new_doc.get("automation") or {})
            automation["socketControlMode"] = ACTIVATED_MODE
            automation["socketPassword"] = "<masked>"
            new_doc["automation"] = automation
            result["would_activate"] = render_jsonc(new_doc)
        except ParseError as e:
            result["would_activate_error"] = str(e)
    else:
        result["would_activate_error"] = "settings file absent"

    receipt_path = _latest_receipt(cfg.receipt_dir, cfg.settings_path, op="activate")
    result["would_rollback_from_receipt"] = str(receipt_path) if receipt_path else None
    return result


def cmd_prepare_secret(cfg: Config) -> dict:
    _reject_symlink(cfg.secret_file, "secret file")
    cfg.secret_file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.secret_file.parent, 0o700)
    if cfg.secret_file.exists():
        return {
            "created": False,
            "path": str(cfg.secret_file),
            "mode": _mode_str(cfg.secret_file),
            "sha256": sha256_bytes(cfg.secret_file),
        }
    raw = base64.b64encode(os.urandom(33)).decode("ascii")
    fd = os.open(
        str(cfg.secret_file),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "w") as f:
        f.write(raw)
    return {
        "created": True,
        "path": str(cfg.secret_file),
        "mode": _mode_str(cfg.secret_file),
        "sha256": sha256_bytes(cfg.secret_file),
    }


def cmd_activate(cfg: Config) -> dict:
    # Preflight first: reject unsafe paths and validate the secret's metadata before reading
    # any content, chmod'ing anything, or writing a backup.
    _validate_secret(cfg.secret_file)
    _reject_symlink(cfg.settings_path, "settings file")
    _reject_symlink(cfg.backup_dir, "backup dir")
    _reject_symlink(cfg.receipt_dir, "receipt dir")
    _reject_symlink(cfg.app_saved_password_file, "app saved password file")
    _reject_symlink(cfg.state_dir_password_file, "state-dir password file")

    if not cfg.settings_path.is_file():
        raise PreflightError(
            f"refusing: canonical settings file missing: {cfg.settings_path} "
            "(cmux creates it on first launch; launch cmux once, then retry)"
        )

    try:
        current_doc = parse_jsonc(cfg.settings_path.read_text())
    except ParseError as e:
        raise ParseError(f"refusing: settings file does not parse as JSONC: {e}") from e

    secret_value = cfg.secret_file.read_text().strip()
    if not secret_value:
        raise PreflightError(
            "refusing: secret file content is empty after stripping whitespace"
        )

    # Capture ALL pre-mutation evidence -- including the app's own saved-credential file --
    # before anything is written (finding 2: the credential capture must happen before the
    # settings mutation, not as read-only evidence gathered afterward).
    pre_state = owned_state(current_doc)
    app_pw_pre = _file_evidence(cfg.app_saved_password_file)
    state_dir_pre = _file_evidence(cfg.state_dir_password_file)
    keychain_pre = _keychain_metadata()

    new_doc = copy.deepcopy(current_doc)
    automation = dict(new_doc.get("automation") or {})
    automation["socketControlMode"] = ACTIVATED_MODE
    automation["socketPassword"] = secret_value
    new_doc["automation"] = automation

    rendered = render_jsonc(
        new_doc
    )  # checked dependency; raises before anything is written
    post_state = owned_state(new_doc)
    if post_state[".".join(MODE_KEY)]["value"] != ACTIVATED_MODE:
        raise PreflightError("refusing: rendered settings failed owned-key self-check")

    # Preflight + stage every destination before the live settings replacement.
    backup_path = _make_backup(cfg.settings_path, cfg.backup_dir, "pre-activate")
    pre_sha = sha256_bytes(cfg.settings_path)
    backup_sha = sha256_bytes(backup_path)
    if backup_sha != pre_sha:
        raise PreflightError(
            "refusing: backup copy integrity check failed before write"
        )

    app_pw_backup_path: Path | None = None
    app_pw_backup_sha: str | None = None
    if app_pw_pre["present"]:
        # A credential pre-exists: keep a real, verified byte-exact backup so rollback can
        # restore it later without ever having to guess or delete an unverified file.
        app_pw_backup_path = _make_backup(
            cfg.app_saved_password_file, cfg.backup_dir, "pre-activate-apppw"
        )
        app_pw_backup_sha = sha256_bytes(app_pw_backup_path)
        if app_pw_backup_sha != app_pw_pre["sha256"]:
            raise PreflightError(
                "refusing: app saved password backup integrity check failed before write"
            )

    state_dir_backup_path: Path | None = None
    state_dir_backup_sha: str | None = None
    if state_dir_pre["present"]:
        state_dir_backup_path = _make_backup(
            cfg.state_dir_password_file, cfg.backup_dir, "pre-activate-statedir"
        )
        state_dir_backup_sha = sha256_bytes(state_dir_backup_path)
        if state_dir_backup_sha != state_dir_pre["sha256"]:
            raise PreflightError(
                "refusing: state-dir password backup integrity check failed before write"
            )

    receipt = {
        "op": "activate",
        "ts": _now_iso(),
        "settings_file": str(cfg.settings_path),
        "pre_sha256": pre_sha,
        "post_sha256": sha256_str(rendered),
        "mode": "600",
        "backup_path": str(backup_path),
        "backup_sha256": backup_sha,
        "owned_leaves": {"pre": pre_state, "post": post_state},
        "secret_file": {
            "path": str(cfg.secret_file),
            "sha256": sha256_bytes(cfg.secret_file),
        },
        "app_saved_password_file": {
            "path": str(cfg.app_saved_password_file),
            "pre": app_pw_pre,
            "backup_path": str(app_pw_backup_path) if app_pw_backup_path else None,
            "backup_sha256": app_pw_backup_sha,
            # Only ever populated later, explicitly, by cmd_observe_postimage via
            # amend_receipt_with_verified_store_postimage -- never guessed here.
            "operation_postimage": None,
        },
        "state_dir_password_file": {
            "path": str(cfg.state_dir_password_file),
            "pre": state_dir_pre,
            "backup_path": (
                str(state_dir_backup_path) if state_dir_backup_path else None
            ),
            "backup_sha256": state_dir_backup_sha,
            "operation_postimage": None,
        },
        # Keychain is not a file: only non-secret presence/service/account metadata is
        # baselined. cmd_observe_postimage reports a presence transition; a Keychain item is
        # never auto-deleted on rollback (no verified file postimage exists for it).
        "keychain_metadata": {"pre": keychain_pre},
        "legacy_settings_file": _file_evidence(cfg.legacy_settings_path),
    }

    # Persist AND verify the prepared transaction journal BEFORE the settings replacement
    # (finding 1). If this raises -- including a synchronous injected/IO failure -- the live
    # settings file has not been touched yet, so the owned preimage is intact by construction;
    # there is nothing to restore. This reuses the same staged-file pattern as `_atomic_write`
    # rather than adding a second, optimistic post-write receipt.
    receipt_path = _write_receipt(cfg.receipt_dir, "activate", receipt)
    _verify_receipt(receipt_path, receipt)

    # Commit: the atomic settings replace is the only remaining step. `os.replace` is atomic,
    # so either it fully lands (matching the already-durable, verified journal above) or the
    # preimage survives untouched -- there is no window where a receipt exists without a
    # matching mutation, or a mutation lands without an already-durable receipt describing it.
    _atomic_write(cfg.settings_path, rendered, mode=0o600)
    post_sha = sha256_bytes(cfg.settings_path)
    if post_sha != receipt["post_sha256"]:
        # The atomic os.replace landed, but the file's FULL content no longer matches the
        # pre-verified journal -- something edited it AFTER our replace (an unrelated setting
        # such as a theme change, or the app migrating a credential store / owned leaf).
        #
        # A prior version blindly restored the ENTIRE pre-activation backup here, erasing that
        # unrelated edit -- and any app-migrated owned leaves -- while claiming it "restored the
        # owned preimage" (activation-postwrite-conflict-review). That is exactly the
        # preserve-unrelated-edits violation the rollback path already avoids. Do NOT restore
        # the whole backup and do NOT guess/overwrite migrated owned leaves. Reconcile on the
        # OWNED leaves only:
        #   - if OUR owned leaves (mode + password) did land as intended, the mismatch is only
        #     an unrelated concurrent edit; activation succeeded and that edit is preserved
        #     untouched (recorded as drift on the durable receipt);
        #   - otherwise leave the current file exactly as it is and report a conflict -- the
        #     journal-first receipt is already durable, so a conflict-aware `rollback` can
        #     reconcile owned leaves later without erasing unrelated or migrated content.
        try:
            current_after = parse_jsonc(cfg.settings_path.read_text())
        except ParseError as e:
            raise ConflictError(
                "refusing: post-write settings no longer parse as JSONC and were left intact "
                f"(durable receipt {receipt_path.name}); reconcile via rollback"
            ) from e
        current_owned = owned_state(current_after)
        if current_owned != post_state:
            raise ConflictError(
                "refusing: post-write owned leaves diverged from the intended activation and "
                "were left intact to avoid erasing unrelated or app-migrated content "
                f"(durable receipt {receipt_path.name}); reconcile via rollback"
            )
        # Owned intent landed; only unrelated content differs -- record the drift durably and
        # accept, preserving the concurrent edit.
        drift = {
            "observed_full_sha256": post_sha,
            "note": (
                "owned leaves match the intended activation; the live file also carries an "
                "unrelated concurrent edit made between the atomic write and readback, "
                "preserved untouched"
            ),
            "recorded_ts": _now_iso(),
        }
        receipt = dict(receipt)
        receipt["post_write_unrelated_drift"] = drift
        rendered_receipt = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
        _atomic_write(receipt_path, rendered_receipt, mode=0o600)
        _verify_receipt(receipt_path, receipt)

    return {"receipt_path": str(receipt_path), **receipt}


def cmd_rollback(cfg: Config, receipt_path: Path | None = None) -> dict:
    _reject_symlink(cfg.settings_path, "settings file")
    _reject_symlink(cfg.backup_dir, "backup dir")
    _reject_symlink(cfg.receipt_dir, "receipt dir")
    _reject_symlink(cfg.app_saved_password_file, "app saved password file")

    if receipt_path is None:
        receipt_path = _latest_receipt(
            cfg.receipt_dir, cfg.settings_path, op="activate"
        )
        if receipt_path is None:
            raise PreflightError(
                f"refusing: no activation receipt found for {cfg.settings_path}"
            )
    _reject_symlink(receipt_path, "receipt file")
    if not receipt_path.is_file():
        raise PreflightError(f"refusing: receipt file missing: {receipt_path}")
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("settings_file") != str(cfg.settings_path):
        raise PreflightError(
            "refusing: receipt settings_file does not match configured settings path"
        )

    backup_path = Path(receipt["backup_path"])
    _reject_symlink(backup_path, "backup file")
    if not backup_path.is_file():
        raise PreflightError(
            f"refusing: backup file referenced by receipt is missing: {backup_path}"
        )
    if sha256_bytes(backup_path) != receipt["backup_sha256"]:
        raise PreflightError(
            f"refusing: backup file integrity check failed: {backup_path}"
        )

    if not cfg.settings_path.is_file():
        raise PreflightError(f"refusing: settings file missing: {cfg.settings_path}")
    try:
        current_doc = parse_jsonc(cfg.settings_path.read_text())
    except ParseError as e:
        raise ParseError(f"refusing: settings file does not parse as JSONC: {e}") from e

    # Reconcile a prepared journal whose settings replacement never actually landed (process
    # interruption, or a raised exception, between the verified journal write and the atomic
    # replace). The journal is persisted before the mutation, so this is detectable here: the
    # current file's hash still equals the receipt's PRE-activation hash, i.e. the owned
    # preimage was never touched. There is nothing to restore for the settings file in that
    # case -- rollback reconciles cleanly instead of refusing.
    current_settings_sha = sha256_bytes(cfg.settings_path)
    mutation_landed = current_settings_sha != receipt.get("pre_sha256")

    current_state = owned_state(current_doc)
    if mutation_landed:
        expected_post = receipt["owned_leaves"]["post"]
        if current_state != expected_post:
            raise ConflictError(
                "refusing: owned settings leaves changed since activation "
                f"(receipt {receipt_path.name}); restore manually or re-run status first"
            )

        try:
            backup_doc = parse_jsonc(backup_path.read_text())
        except ParseError as e:
            raise ParseError(
                f"refusing: backup file does not parse as JSONC: {e}"
            ) from e

        # Base the result on the CURRENT file (preserves any unrelated edit made after
        # activation); only the two owned leaves are overwritten, using the byte-exact
        # preimage from the backup.
        new_doc = copy.deepcopy(current_doc)
        automation = dict(new_doc.get("automation") or {})
        b_automation = backup_doc.get("automation") or {}
        if "socketControlMode" in b_automation:
            automation["socketControlMode"] = b_automation["socketControlMode"]
        else:
            automation.pop("socketControlMode", None)
        if "socketPassword" in b_automation:
            automation["socketPassword"] = b_automation["socketPassword"]
        else:
            automation.pop("socketPassword", None)
        if automation:
            new_doc["automation"] = automation
        else:
            new_doc.pop("automation", None)

        rendered = render_jsonc(new_doc)

        pre_rollback_backup = _make_backup(
            cfg.settings_path, cfg.backup_dir, "pre-rollback"
        )
        pre_sha = sha256_bytes(cfg.settings_path)

        _atomic_write(cfg.settings_path, rendered, mode=0o600)
        post_sha = sha256_bytes(cfg.settings_path)
        owned_leaves_post = owned_state(new_doc)
    else:
        # Nothing was ever mutated: the prepared journal is reconciled as a clean no-op for the
        # settings file itself.
        pre_rollback_backup = None
        pre_sha = current_settings_sha
        post_sha = current_settings_sha
        owned_leaves_post = current_state

    # Candidate-store credential reconciliation (finding 2): never delete a pre-existing
    # credential; restore/remove only the exact operation-owned postimage. Reconcile EVERY
    # modeled file store (app-support, state-dir) -- whichever one the app actually populated
    # was recorded by cmd_observe_postimage. Keychain is metadata-only and never auto-deleted.
    store_actions: dict[str, str] = {}
    store_evidence: dict[str, dict] = {}
    for store_key, store_path, receipt_key in _candidate_file_stores(cfg):
        _reject_symlink(store_path, f"{receipt_key} store")
        action, evidence_now = _reconcile_store_credential(
            receipt, receipt_key, store_path, mutation_landed
        )
        store_actions[receipt_key] = action
        store_evidence[receipt_key] = evidence_now

    out_receipt = {
        "op": "rollback",
        "ts": _now_iso(),
        "settings_file": str(cfg.settings_path),
        "rolled_back_from_receipt": str(receipt_path),
        "restored_from_backup": str(backup_path),
        "mutation_had_landed": mutation_landed,
        "pre_sha256": pre_sha,
        "post_sha256": post_sha,
        "mode": "600",
        "pre_rollback_backup": (
            str(pre_rollback_backup) if pre_rollback_backup else None
        ),
        "owned_leaves_post": owned_leaves_post,
        # Backward-compatible single-store fields (app-support) plus the full per-store map.
        "app_saved_password_action": store_actions.get("app_saved_password_file"),
        "app_saved_password_file": store_evidence.get("app_saved_password_file"),
        "store_actions": store_actions,
        "store_evidence": store_evidence,
    }
    out_path = _write_receipt(cfg.receipt_dir, "rollback", out_receipt)
    return {"receipt_path": str(out_path), **out_receipt}


def cmd_observe_postimage(cfg: Config, receipt_path: Path | None = None) -> dict:
    """Observe which candidate credential store the app actually populated after a live
    activation + the app's supported reload, and record its non-secret postimage on the
    activation receipt so rollback can safely reconcile it.

    This is the observe-and-record CLI callsite required by the r3 activation plan. It performs
    NO settings mutation and NO app reload of its own: it reads the current on-disk state of
    each modeled candidate store and diffs it against the receipt's pre-activation baseline.

    To connect an observed store to THIS operation, the function does read secret material --
    the prepared secret (verified against its receipt hash) and a candidate store's bytes -- but
    only privately, in memory, for a constant-time comparison. No secret value is ever logged,
    returned, or written; every value that leaves this function (the observation report and the
    receipt amendment) is non-secret: sha256 / path / present-flag and Keychain presence
    metadata only.

    An absent -> present transition is NOT by itself proof this operation created the store
    (activation-observed-creation review). Attribution is deliberately conservative:
      - absent -> present AND the store's plaintext representation logically equals the prepared
        secret (private same-byte verification): CONNECTED to this operation; the sha256 of the
        exact bytes that passed the check is RECORDED as the operation postimage so rollback may
        reconcile it.
      - absent -> present but the representation does NOT match (an unrelated credential, or an
        opaque/unsupported representation), or the secret is unavailable to verify against:
        reported and PRESERVED, never recorded, never deleted on rollback.
      - present -> changed (a rotation of a preexisting credential): reported, never recorded --
        a changed preexisting credential stays protected.
      - unchanged: reported, nothing recorded.
      - Keychain presence transition: reported as metadata only; never recorded as a file
        postimage and never auto-deleted on rollback.
    If no modeled store changed, reports no_observed_store_change -- the app populated none of
    the modeled candidate stores (no reload occurred, or it uses an unmodeled/unsupported
    store), stated truthfully rather than guessed.
    """
    _reject_symlink(cfg.receipt_dir, "receipt dir")
    if receipt_path is None:
        receipt_path = _latest_receipt(
            cfg.receipt_dir, cfg.settings_path, op="activate"
        )
        if receipt_path is None:
            raise PreflightError(
                f"refusing: no activation receipt found for {cfg.settings_path}"
            )
    _reject_symlink(receipt_path, "receipt file")
    if not receipt_path.is_file():
        raise PreflightError(f"refusing: receipt file missing: {receipt_path}")
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("settings_file") != str(cfg.settings_path):
        raise PreflightError(
            "refusing: receipt settings_file does not match configured settings path"
        )

    # Resolve the secret this activation actually used, to CONNECT an observed store to this
    # operation. It is only usable as connection evidence if the on-disk secret still matches
    # the one recorded at activation time (same sha256). The value is held only in this local
    # and is never logged, returned, or written.
    # Read the prepared secret EXACTLY ONCE: verify its sha256 against the receipt AND decode it
    # for the private comparison from the same bytes (no separate sha-check + value read).
    verified_secret: str | None = None
    secret_info = receipt.get("secret_file")
    if (
        isinstance(secret_info, dict)
        and not cfg.secret_file.is_symlink()
        and cfg.secret_file.is_file()
    ):
        try:
            _secret_raw = cfg.secret_file.read_bytes()
        except OSError:
            _secret_raw = None
        if _secret_raw is not None and hashlib.sha256(
            _secret_raw
        ).hexdigest() == secret_info.get("sha256"):
            try:
                verified_secret = _secret_raw.decode("utf-8")
            except UnicodeDecodeError:
                verified_secret = None

    observations: list[dict] = []
    any_change = False
    recorded_any = False
    for store_key, store_path, receipt_key in _candidate_file_stores(cfg):
        info = receipt.get(receipt_key)
        # Read the store EXACTLY ONCE: the recorded hash and the connection check both derive
        # from these same immutable bytes (no snapshot-connection TOCTOU).
        current, raw = _read_store_snapshot(store_path)
        obs: dict = {
            "store": store_key,
            "receipt_key": receipt_key,
            "path": str(store_path),
            "current": current,
        }
        if not isinstance(info, dict) or "pre" not in info:
            obs["result"] = "unknown_receipt_shape"
            observations.append(obs)
            continue
        pre = info["pre"]
        same = pre.get("present") == current["present"] and pre.get(
            "sha256"
        ) == current.get("sha256")
        if same:
            obs["result"] = "unchanged"
        elif not pre.get("present") and current["present"]:
            any_change = True
            # Absent -> present is NOT proof this operation created it. Record the postimage as
            # operation-owned ONLY when the store's plaintext representation is CONNECTED to
            # this operation -- i.e. the very bytes we hash logically equal the prepared secret
            # (compared privately). Otherwise report unknown/unsupported and preserve it
            # (rollback then leaves it untouched, since no operation_postimage is recorded).
            if raw is None or not current.get("sha256"):
                obs["result"] = "present_but_unreadable_preserved"
            elif verified_secret is None:
                obs["result"] = (
                    "created_but_secret_unavailable_for_verification_preserved"
                )
            elif _bytes_plaintext_is_operation_secret(raw, verified_secret):
                # The recorded sha256 is derived from EXACTLY the bytes that passed the check.
                amend_receipt_with_verified_store_postimage(
                    receipt_path, receipt_key, current["sha256"]
                )
                obs["result"] = "created_operation_plaintext_verified_recorded"
                obs["operation_postimage_sha256"] = current["sha256"]
                recorded_any = True
            else:
                # An unrelated credential, or an opaque/unsupported representation we cannot
                # connect to this operation. Never attribute or delete.
                obs["result"] = "created_unconnected_representation_preserved"
        elif pre.get("present") and not current["present"]:
            obs["result"] = "removed_since_activation_reported_only"
            any_change = True
        else:
            # present -> changed: a rotation of a PREEXISTING credential. Never attribute or
            # record, even if the new content matches -- a changed preexisting credential stays
            # protected (rollback leaves it untouched).
            obs["result"] = "rotation_of_preexisting_observed_preserved"
            obs["observed_sha256"] = current.get("sha256")
            any_change = True
        observations.append(obs)

    kc_info = receipt.get("keychain_metadata")
    kc_now = _keychain_metadata()
    kc_obs: dict = {"store": "keychain", "current": kc_now}
    if isinstance(kc_info, dict) and isinstance(kc_info.get("pre"), dict):
        kc_pre = kc_info["pre"]
        if kc_pre.get("available") and kc_now.get("available"):
            if kc_pre.get("present") == kc_now.get("present"):
                kc_obs["result"] = "unchanged"
            elif not kc_pre.get("present") and kc_now.get("present"):
                kc_obs["result"] = "keychain_item_appeared_metadata_only"
                any_change = True
            elif kc_pre.get("present") and not kc_now.get("present"):
                kc_obs["result"] = "keychain_item_disappeared_metadata_only"
                any_change = True
            else:
                kc_obs["result"] = "keychain_presence_unknown"
        else:
            kc_obs["result"] = "keychain_unavailable"
    else:
        kc_obs["result"] = "unknown_receipt_shape"
    observations.append(kc_obs)

    return {
        "op": "observe-postimage",
        "ts": _now_iso(),
        "receipt_path": str(receipt_path),
        "settings_file": str(cfg.settings_path),
        "summary": (
            "observed_store_change" if any_change else "no_observed_store_change"
        ),
        "recorded_operation_postimage": recorded_any,
        "observations": observations,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_result(result: dict) -> None:
    print(json.dumps(result, indent=2, sort_keys=True))


def main(argv: list[str], cfg: Config | None = None) -> int:
    cfg = cfg or build_config_from_env()
    if not argv:
        argv = ["status"]
    cmd = argv[0]
    try:
        if cmd == "status":
            _print_result(cmd_status(cfg))
        elif cmd == "dry-run":
            _print_result(cmd_dry_run(cfg))
        elif cmd == "prepare-secret":
            _print_result(cmd_prepare_secret(cfg))
        elif cmd == "activate":
            if "--confirm" not in argv[1:]:
                print(
                    "refusing: activate requires --confirm after supervisor inspection",
                    file=sys.stderr,
                )
                return 2
            _print_result(cmd_activate(cfg))
        elif cmd == "rollback":
            if "--confirm" not in argv[1:]:
                print("refusing: rollback requires --confirm", file=sys.stderr)
                return 2
            receipt_arg = None
            rest = argv[1:]
            if "--receipt" in rest:
                idx = rest.index("--receipt")
                receipt_arg = Path(rest[idx + 1])
            _print_result(cmd_rollback(cfg, receipt_path=receipt_arg))
        elif cmd == "observe-postimage":
            receipt_arg = None
            rest = argv[1:]
            if "--receipt" in rest:
                idx = rest.index("--receipt")
                receipt_arg = Path(rest[idx + 1])
            _print_result(cmd_observe_postimage(cfg, receipt_path=receipt_arg))
        else:
            print(
                "usage: cmux_access_plan.py status|dry-run|prepare-secret|"
                "activate --confirm|rollback --confirm [--receipt PATH]|"
                "observe-postimage [--receipt PATH]",
                file=sys.stderr,
            )
            return 2
    except (PreflightError, ParseError, ConflictError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
