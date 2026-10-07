"""Fixture tests for scripts/cmux_access_plan.py (SIMULATED ONLY).

Never touches the real socket, the real ~/.config/cmux/cmux.json, or the real
~/.config/cmux/settings.json. Every Config below points entirely into tmp_path; the CLI binary
used for the live-mode probe is a fake no-output script, never /Applications/cmux.app/....

Run: cd bridge && python3 -m pytest tests/test_activation.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import cmux_access_plan as cap  # noqa: E402

STRING_NEIGHBOR = "literal,} and ,] text"


def make_fixture_text(mode_value: str | None = None, extra_top_level: str = "") -> str:
    """A JSONC fixture with a template comment, a trailing comma, and a comma/brace-bearing
    string value that a naive trailing-comma regex would corrupt."""
    mode_line = f'    "socketControlMode": "{mode_value}",\n' if mode_value else ""
    return (
        "{\n"
        "  // cmux settings template\n"
        '  "$schema": "https://example.invalid/schema.json",\n'
        '  "schemaVersion": 1,\n'
        '  "automation": {\n'
        f"{mode_line}"
        "  },\n"
        '  "actions": {\n'
        f'    "fixture": "{STRING_NEIGHBOR}"\n'
        "  },\n"
        f"{extra_top_level}"
        "}\n"
    )


def write_secret(
    path: Path, content: str = "s3cr3t-plaintext-value", mode: int = 0o600
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)


def make_cfg(tmp_path: Path, **overrides) -> cap.Config:
    fake_cli = tmp_path / "fake-cmux-cli"
    fake_cli.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(fake_cli, 0o700)
    defaults = dict(
        settings_path=tmp_path / "cmux.json",
        legacy_settings_path=tmp_path / "settings.json",
        secret_file=tmp_path / "bridge" / "cmux-socket-password",
        backup_dir=tmp_path / "backups",
        receipt_dir=tmp_path / "access-receipts",
        app_saved_password_file=tmp_path / "AppSupport" / "socket-control-password",
        state_dir_password_file=tmp_path / "state" / "cmux" / "socket-control-password",
        cli_path=fake_cli,
    )
    defaults.update(overrides)
    return cap.Config(**defaults)


# ---------------------------------------------------------------------------
# JSONC scanner
# ---------------------------------------------------------------------------


def test_string_neighbor_value_not_corrupted_by_parse():
    doc = cap.parse_jsonc(make_fixture_text())
    assert doc["actions"]["fixture"] == STRING_NEIGHBOR


def test_trailing_comma_and_comments_parse_correctly():
    text = (
        "{\n"
        "  /* block comment\n"
        "     spanning lines */\n"
        '  "$schema": "s", // trailing line comment\n'
        '  "automation": {\n'
        '    "socketControlMode": "cmuxOnly",\n'
        "  },\n"
        "}\n"
    )
    doc = cap.parse_jsonc(text)
    assert doc["$schema"] == "s"
    assert doc["automation"]["socketControlMode"] == "cmuxOnly"


def test_malformed_jsonc_refuses_without_writing(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text('{"a": "unterminated\n')
    write_secret(cfg.secret_file)
    with pytest.raises(cap.ParseError):
        cap.cmd_activate(cfg)
    assert cfg.settings_path.read_text() == '{"a": "unterminated\n'
    assert not cfg.backup_dir.exists() or not any(cfg.backup_dir.iterdir())
    assert not cfg.receipt_dir.exists() or not any(cfg.receipt_dir.iterdir())


# ---------------------------------------------------------------------------
# secret preflight
# ---------------------------------------------------------------------------


def test_missing_secret_refuses(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    with pytest.raises(cap.PreflightError, match="secret file absent"):
        cap.cmd_activate(cfg)
    assert cfg.settings_path.read_text() == make_fixture_text()


def test_empty_secret_refuses(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    write_secret(cfg.secret_file, content="")
    with pytest.raises(cap.PreflightError, match="empty"):
        cap.cmd_activate(cfg)


def test_wrong_mode_secret_refuses(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    write_secret(cfg.secret_file, mode=0o644)
    with pytest.raises(cap.PreflightError, match="owner-only"):
        cap.cmd_activate(cfg)


# ---------------------------------------------------------------------------
# symlink preflight
# ---------------------------------------------------------------------------


def test_symlinked_destination_refuses(tmp_path):
    real = tmp_path / "elsewhere.json"
    real.write_text(make_fixture_text())
    cfg = make_cfg(tmp_path)
    cfg.settings_path.symlink_to(real)
    write_secret(cfg.secret_file)
    with pytest.raises(cap.PreflightError, match="symlink"):
        cap.cmd_activate(cfg)
    assert real.read_text() == make_fixture_text()  # untouched


def test_symlinked_secret_refuses(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    real_secret = tmp_path / "real-secret"
    write_secret(real_secret)
    cfg.secret_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.secret_file.symlink_to(real_secret)
    with pytest.raises(cap.PreflightError, match="symlink"):
        cap.cmd_activate(cfg)
    assert cfg.settings_path.read_text() == make_fixture_text()


def test_symlinked_backup_dir_refuses(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    write_secret(cfg.secret_file)
    real_backup_target = tmp_path / "real-backups"
    real_backup_target.mkdir()
    cfg.backup_dir.symlink_to(real_backup_target)
    with pytest.raises(cap.PreflightError, match="symlink"):
        cap.cmd_activate(cfg)
    assert cfg.settings_path.read_text() == make_fixture_text()
    assert not any(real_backup_target.iterdir())


# ---------------------------------------------------------------------------
# activate + rollback: preimage correctness
# ---------------------------------------------------------------------------


def test_string_neighbor_survives_activate_and_rollback(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    write_secret(cfg.secret_file)

    cap.cmd_activate(cfg)
    activated = json.loads(cfg.settings_path.read_text())
    assert activated["actions"]["fixture"] == STRING_NEIGHBOR
    assert activated["automation"]["socketControlMode"] == "password"

    cap.cmd_rollback(cfg)
    rolled_back = json.loads(cfg.settings_path.read_text())
    assert rolled_back["actions"]["fixture"] == STRING_NEIGHBOR


def test_secret_value_never_appears_in_receipts_or_backups(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    secret_value = "the-actual-secret-do-not-leak"
    write_secret(cfg.secret_file, content=secret_value)

    result = cap.cmd_activate(cfg)
    receipt_text = Path(result["receipt_path"]).read_text()
    assert secret_value not in receipt_text
    assert json.dumps(result).find(secret_value) == -1

    for p in cfg.backup_dir.iterdir():
        # the pre-activate backup is a literal copy of the OLD file, which never had the
        # secret in it yet; confirm that too, plus confirm the receipt carries no plaintext.
        assert secret_value not in p.read_text()


def test_rollback_restores_preimage_not_delete(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="automation"))
    write_secret(cfg.secret_file)

    cap.cmd_activate(cfg)
    cap.cmd_rollback(cfg)

    final = json.loads(cfg.settings_path.read_text())
    assert final["automation"]["socketControlMode"] == "automation"
    assert "socketPassword" not in final["automation"]
    assert final["actions"]["fixture"] == STRING_NEIGHBOR


def test_rollback_preserves_unrelated_later_edit(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    write_secret(cfg.secret_file)

    cap.cmd_activate(cfg)

    doc = json.loads(cfg.settings_path.read_text())
    doc["actions"]["addedLater"] = "unrelated-edit"
    cfg.settings_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")

    cap.cmd_rollback(cfg)

    final = json.loads(cfg.settings_path.read_text())
    assert final["actions"]["addedLater"] == "unrelated-edit"
    assert "automation" not in final or "socketControlMode" not in final.get(
        "automation", {}
    )


def test_rollback_refuses_on_conflicting_owned_leaf_change(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    write_secret(cfg.secret_file)

    cap.cmd_activate(cfg)

    doc = json.loads(cfg.settings_path.read_text())
    doc["automation"]["socketControlMode"] = (
        "allowAll"  # simulate an external Settings-UI change
    )
    conflicting_text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    cfg.settings_path.write_text(conflicting_text)

    with pytest.raises(cap.ConflictError):
        cap.cmd_rollback(cfg)

    assert cfg.settings_path.read_text() == conflicting_text  # untouched


def test_immediate_rollback_backup_does_not_collide(tmp_path):
    """Two backups written in the same activate/rollback pair must not overwrite each other."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    write_secret(cfg.secret_file)

    cap.cmd_activate(cfg)
    cap.cmd_rollback(cfg)

    backups = list(cfg.backup_dir.iterdir())
    assert len(backups) == 2
    assert len({b.name for b in backups}) == 2


# ---------------------------------------------------------------------------
# dry-run must never mutate, and must mask the secret
# ---------------------------------------------------------------------------


def test_dry_run_does_not_mutate_and_masks_secret(tmp_path):
    cfg = make_cfg(tmp_path)
    original = make_fixture_text()
    cfg.settings_path.write_text(original)
    secret_value = "should-never-appear-unmasked"
    write_secret(cfg.secret_file, content=secret_value)

    result = cap.cmd_dry_run(cfg)

    assert cfg.settings_path.read_text() == original
    assert not cfg.backup_dir.exists()
    assert not cfg.receipt_dir.exists()
    assert secret_value not in json.dumps(result)
    assert "<masked>" in result["would_activate"]


# ---------------------------------------------------------------------------
# status probe against a fake no-output CLI
# ---------------------------------------------------------------------------


def test_status_handles_unreachable_cli(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    result = cap.cmd_status(cfg)
    assert result["live_access_mode"] == "unreachable"
    assert result["settings_file"]["exists"] is True


# ---------------------------------------------------------------------------
# CLI --confirm gate
# ---------------------------------------------------------------------------


def test_cli_activate_refuses_without_confirm(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    write_secret(cfg.secret_file)
    rc = cap.main(["activate"], cfg=cfg)
    assert rc == 2
    assert cfg.settings_path.read_text() == make_fixture_text()


def test_cli_rollback_refuses_without_confirm(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text())
    rc = cap.main(["rollback"], cfg=cfg)
    assert rc == 2


# ---------------------------------------------------------------------------
# finding 1 (activation-followup review): persist a recoverable transaction intent BEFORE
# changing live settings; recovery must reconcile a prepared record after interruption.
# ---------------------------------------------------------------------------


def test_injected_receipt_persistence_failure_leaves_preimage_intact(
    tmp_path, monkeypatch
):
    """RED repro (work/probe_activation_transaction.py): injecting a `_write_receipt` failure
    used to leave socketControlMode=password with no receipt on disk. The journal must now be
    persisted+verified BEFORE the settings mutation, so a synchronous failure here must never
    touch the live settings file at all."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)

    def fail_write_receipt(*a, **k):
        raise OSError("injected receipt persistence failure")

    monkeypatch.setattr(cap, "_write_receipt", fail_write_receipt)
    with pytest.raises(OSError, match="injected receipt persistence failure"):
        cap.cmd_activate(cfg)

    after = cap.parse_jsonc(cfg.settings_path.read_text())
    assert after["automation"]["socketControlMode"] == "cmuxOnly"
    assert after["actions"]["fixture"] == STRING_NEIGHBOR
    assert not cfg.receipt_dir.exists() or not any(cfg.receipt_dir.iterdir())


def test_rollback_reconciles_prepared_record_after_interrupted_settings_replace(
    tmp_path, monkeypatch
):
    """Simulates process interruption between the (already-persisted, verified) prepared
    journal and the settings replacement itself. The owned preimage must already be intact
    (nothing was mutated), and `rollback` must reconcile the orphaned prepared record instead
    of refusing with 'no activation receipt found'."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)

    real_atomic_write = cap._atomic_write

    def fail_settings_replace(path, content, mode=0o600):
        if path == cfg.settings_path:
            raise OSError("injected settings replace interruption")
        return real_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(cap, "_atomic_write", fail_settings_replace)
    with pytest.raises(OSError, match="injected settings replace interruption"):
        cap.cmd_activate(cfg)

    # The prepared journal survived (persist-before-mutate); settings are untouched.
    receipts = [p for p in cfg.receipt_dir.iterdir() if p.is_file()]
    assert len(receipts) == 1
    assert (
        cap.parse_jsonc(cfg.settings_path.read_text())["automation"][
            "socketControlMode"
        ]
        == "cmuxOnly"
    )

    monkeypatch.setattr(cap, "_atomic_write", real_atomic_write)
    result = cap.cmd_rollback(cfg)
    assert result["mutation_had_landed"] is False
    assert (
        cap.parse_jsonc(cfg.settings_path.read_text())["automation"][
            "socketControlMode"
        ]
        == "cmuxOnly"
    )


# ---------------------------------------------------------------------------
# finding 2 (activation-followup review): account for the app's own saved credential --
# capture before-state before the mutation; restore/remove only an exact operation-owned
# postimage on rollback; never delete a pre-existing credential.
# ---------------------------------------------------------------------------


## Absent-before/present-after timing, or a changed preexisting hash, is NOT proof that a
## credential change belongs to this operation: acting on it could delete or overwrite an
## UNRELATED credential. The three regressions below cover those negative scenarios and require
## the credential be left untouched + reported in every case. The positive fixture after them
## models the only thing that IS sufficient evidence: a VERIFIED operation
## postimage hash, recorded via amend_receipt_with_verified_app_password_postimage the way a
## caller that actually performed the app's supported reload during a controlled, live
## activation would record it -- not an arbitrary string.


def test_rollback_leaves_unrelated_credential_untouched_when_absent_before(tmp_path):
    """Regression for conflict-probe scenario 1: credential absent before activation; an
    UNRELATED credential appears later (no verified operation postimage exists on the
    receipt). Rollback must never delete it."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)
    assert not cfg.app_saved_password_file.exists()

    result = cap.cmd_activate(cfg)

    cfg.app_saved_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.app_saved_password_file.write_text("unrelated-later-owner-credential")
    os.chmod(cfg.app_saved_password_file, 0o600)

    rollback_result = cap.cmd_rollback(cfg, Path(result["receipt_path"]))

    assert cfg.app_saved_password_file.exists()
    assert cfg.app_saved_password_file.read_text() == "unrelated-later-owner-credential"
    assert (
        rollback_result["app_saved_password_action"]
        == "no_verified_operation_postimage_untouched"
    )


def test_rollback_never_overwrites_unrelated_rotation_of_preexisting_credential(
    tmp_path,
):
    """Regression for conflict-probe scenario 2: credential preexisted; a later UNRELATED
    rotation occurs (no verified operation postimage). Rollback must never overwrite it from
    backup."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)
    cfg.app_saved_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.app_saved_password_file.write_text("preexisting-password")
    os.chmod(cfg.app_saved_password_file, 0o600)

    result = cap.cmd_activate(cfg)

    cfg.app_saved_password_file.write_text("unrelated-later-owner-credential")
    os.chmod(cfg.app_saved_password_file, 0o600)

    rollback_result = cap.cmd_rollback(cfg, Path(result["receipt_path"]))

    assert cfg.app_saved_password_file.exists()
    assert cfg.app_saved_password_file.read_text() == "unrelated-later-owner-credential"
    assert (
        rollback_result["app_saved_password_action"]
        == "no_verified_operation_postimage_untouched"
    )


def test_rollback_never_touches_credential_when_activation_never_landed(
    tmp_path, monkeypatch
):
    """Regression for conflict-probe scenario 3: inject a failure BEFORE the settings
    replacement (prepared journal persisted, activation never landed), then an unrelated
    credential is created. Rollback must prove `mutation_had_landed is False` and never touch
    the credential -- activation had no side effect to reconcile."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)

    real_atomic_write = cap._atomic_write

    def fail_settings_replace(path, content, mode=0o600):
        if path == cfg.settings_path:
            raise OSError("injected before settings mutation")
        return real_atomic_write(path, content, mode=mode)

    monkeypatch.setattr(cap, "_atomic_write", fail_settings_replace)
    with pytest.raises(OSError, match="injected before settings mutation"):
        cap.cmd_activate(cfg)
    monkeypatch.setattr(cap, "_atomic_write", real_atomic_write)

    cfg.app_saved_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.app_saved_password_file.write_text("unrelated-later-owner-credential")
    os.chmod(cfg.app_saved_password_file, 0o600)

    rollback_result = cap.cmd_rollback(cfg)

    assert rollback_result["mutation_had_landed"] is False
    assert cfg.app_saved_password_file.exists()
    assert cfg.app_saved_password_file.read_text() == "unrelated-later-owner-credential"
    assert rollback_result["app_saved_password_action"] == "not_landed_untouched"


def test_rollback_removes_verified_operation_owned_app_password(tmp_path):
    """Corrected positive fixture: the ONLY thing that licenses rollback to remove a
    newly-present credential is a VERIFIED operation postimage -- the actual hash of the
    credential this operation is independently known to have produced, as a caller who
    performed the app's supported reload during a controlled, live activation would record via
    `amend_receipt_with_verified_app_password_postimage` after reading back the app's real
    on-disk representation. (No caller in this codebase does that yet -- we are not doing live
    activation; this test models the recording step directly to prove the verification-gated
    removal logic in isolation. The actual app-watcher behavior must be recorded during a
    bounded, real, controlled activation before this path is claimed proven end-to-end.)
    """
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)
    assert not cfg.app_saved_password_file.exists()

    result = cap.cmd_activate(cfg)

    cfg.app_saved_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.app_saved_password_file.write_text("app-produced-credential-for-this-operation")
    os.chmod(cfg.app_saved_password_file, 0o600)
    verified_sha256 = cap.sha256_bytes(cfg.app_saved_password_file)
    cap.amend_receipt_with_verified_app_password_postimage(
        Path(result["receipt_path"]), verified_sha256
    )
    # The verified hash is expected on the amended receipt (paths + hashes only); the raw
    # credential VALUE must never appear there.
    amended_text = Path(result["receipt_path"]).read_text()
    assert verified_sha256 in amended_text
    assert "app-produced-credential-for-this-operation" not in amended_text

    rollback_result = cap.cmd_rollback(cfg, Path(result["receipt_path"]))

    assert not cfg.app_saved_password_file.exists()
    assert rollback_result["app_saved_password_action"] == "removed_operation_owned"


def test_rollback_leaves_unverified_change_untouched_even_with_a_postimage_on_file(
    tmp_path,
):
    """A verified operation postimage is recorded, but the file present at rollback time does
    NOT match it (something else changed it afterward). Rollback must not guess -- leave it
    untouched and report the conflict distinctly from the no-evidence-at-all case."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)
    assert not cfg.app_saved_password_file.exists()

    result = cap.cmd_activate(cfg)

    cfg.app_saved_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.app_saved_password_file.write_text("app-produced-credential-for-this-operation")
    os.chmod(cfg.app_saved_password_file, 0o600)
    verified_sha256 = cap.sha256_bytes(cfg.app_saved_password_file)
    cap.amend_receipt_with_verified_app_password_postimage(
        Path(result["receipt_path"]), verified_sha256
    )

    # Something else overwrites it after the verified postimage was recorded.
    cfg.app_saved_password_file.write_text("a-third-party-changed-this")
    os.chmod(cfg.app_saved_password_file, 0o600)

    rollback_result = cap.cmd_rollback(cfg, Path(result["receipt_path"]))

    assert cfg.app_saved_password_file.exists()
    assert cfg.app_saved_password_file.read_text() == "a-third-party-changed-this"
    assert (
        rollback_result["app_saved_password_action"] == "unrelated_credential_untouched"
    )


# ---------------------------------------------------------------------------
# Keychain hermeticity: never issue a real `security` query during unit tests.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    monkeypatch.setenv("CMUX_SKIP_KEYCHAIN_PROBE", "1")


# ---------------------------------------------------------------------------
# cmd_activate post-write conflict handling (activation-postwrite-conflict review):
# the post-write hash-mismatch path must NOT blindly restore the whole backup.
# ---------------------------------------------------------------------------
def test_activate_postwrite_unrelated_edit_preserved_not_blindly_restored(
    tmp_path, monkeypatch
):
    """An unrelated edit (e.g. a theme change) that lands AFTER our atomic settings replace but
    before the readback makes post_sha mismatch while our OWNED leaves are still correct. The
    old handler blindly restored the entire pre-activation backup, erasing that edit. Now the
    activation must accept, preserve the unrelated edit, and record the drift durably."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)

    real_atomic_write = cap._atomic_write

    def inject_unrelated_edit(path, content, mode=0o600):
        real_atomic_write(path, content, mode=mode)
        if path == cfg.settings_path:
            # Simulate an unrelated concurrent editor writing a new top-level key AFTER our
            # atomic replace, before cmd_activate's readback.
            doc = cap.parse_jsonc(path.read_text())
            doc["theme"] = "midnight"
            real_atomic_write(path, cap.render_jsonc(doc), mode=mode)

    monkeypatch.setattr(cap, "_atomic_write", inject_unrelated_edit)
    result = cap.cmd_activate(cfg)
    monkeypatch.setattr(cap, "_atomic_write", real_atomic_write)

    final = json.loads(cfg.settings_path.read_text())
    # Our activation landed AND the unrelated edit survived (was NOT restored away).
    assert final["automation"]["socketControlMode"] == "password"
    assert final["theme"] == "midnight"
    assert final["actions"]["fixture"] == STRING_NEIGHBOR
    # Drift recorded both on the returned result and durably on the receipt.
    assert "post_write_unrelated_drift" in result
    receipt_on_disk = json.loads(Path(result["receipt_path"]).read_text())
    assert "post_write_unrelated_drift" in receipt_on_disk

    # Rollback still reconciles owned leaves back to the pre-activation preimage (cmuxOnly)
    # and preserves the unrelated edit.
    cap.cmd_rollback(cfg, Path(result["receipt_path"]))
    rolled = json.loads(cfg.settings_path.read_text())
    assert rolled["theme"] == "midnight"
    assert rolled["automation"]["socketControlMode"] == "cmuxOnly"
    assert "socketPassword" not in rolled.get("automation", {})


def test_activate_postwrite_owned_leaf_divergence_reports_conflict_and_leaves_intact(
    tmp_path, monkeypatch
):
    """If the OWNED leaves themselves diverge post-write (our write overwritten, or the app
    migrated an owned leaf), the handler must NOT guess/overwrite them and must NOT restore the
    backup. It leaves the current file intact and reports a conflict; the durable receipt
    supports a later conflict-aware rollback."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)

    real_atomic_write = cap._atomic_write

    def inject_owned_leaf_change(path, content, mode=0o600):
        real_atomic_write(path, content, mode=mode)
        if path == cfg.settings_path:
            doc = cap.parse_jsonc(path.read_text())
            # Something rewrites the owned mode leaf to an unexpected value after our replace.
            doc["automation"]["socketControlMode"] = "someMigratedMode"
            real_atomic_write(path, cap.render_jsonc(doc), mode=mode)

    monkeypatch.setattr(cap, "_atomic_write", inject_owned_leaf_change)
    with pytest.raises(cap.ConflictError, match="post-write owned leaves diverged"):
        cap.cmd_activate(cfg)
    monkeypatch.setattr(cap, "_atomic_write", real_atomic_write)

    # The current file is left EXACTLY as the divergent writer left it -- not restored to the
    # pre-activation cmuxOnly backup.
    final = json.loads(cfg.settings_path.read_text())
    assert final["automation"]["socketControlMode"] == "someMigratedMode"
    # A durable activation receipt exists for a conflict-aware rollback to use.
    receipts = list(cfg.receipt_dir.glob("activate.*.json"))
    assert len(receipts) == 1


# ---------------------------------------------------------------------------
# cmd_observe_postimage: observe-and-record with CONNECTION-gated attribution
# (activation-observed-creation review). Absent->present is not proof of ownership.
# ---------------------------------------------------------------------------
def test_observe_no_store_change_reports_truthfully(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file)
    result = cap.cmd_activate(cfg)

    obs = cap.cmd_observe_postimage(cfg, Path(result["receipt_path"]))
    assert obs["summary"] == "no_observed_store_change"
    assert obs["recorded_operation_postimage"] is False
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert receipt["state_dir_password_file"]["operation_postimage"] is None
    assert receipt["app_saved_password_file"]["operation_postimage"] is None


def test_observe_does_not_record_unrelated_created_credential(tmp_path):
    """NEGATIVE 1: a DIFFERENT credential file appears after activation (content != the prepared
    secret). It must NOT be recorded as operation-owned, and rollback must NOT delete it."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file, content="the-operation-secret")
    result = cap.cmd_activate(cfg)

    # An unrelated credential (NOT our secret) appears in the state-dir store.
    cfg.state_dir_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_dir_password_file.write_text("an-unrelated-credential")
    os.chmod(cfg.state_dir_password_file, 0o600)

    obs = cap.cmd_observe_postimage(cfg, Path(result["receipt_path"]))
    assert obs["recorded_operation_postimage"] is False
    state_obs = next(o for o in obs["observations"] if o.get("store") == "state_dir")
    assert state_obs["result"] == "created_unconnected_representation_preserved"
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    assert receipt["state_dir_password_file"]["operation_postimage"] is None

    rb = cap.cmd_rollback(cfg, Path(result["receipt_path"]))
    assert cfg.state_dir_password_file.exists()
    assert cfg.state_dir_password_file.read_text() == "an-unrelated-credential"
    assert (
        rb["store_actions"]["state_dir_password_file"]
        == "no_verified_operation_postimage_untouched"
    )


def test_observe_does_not_record_rotation_of_preexisting_credential(tmp_path):
    """NEGATIVE 2: a PREEXISTING credential is rotated after activation -- even to a value that
    equals the prepared secret. A changed preexisting credential stays protected: never
    recorded, never overwritten/removed on rollback."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    write_secret(cfg.secret_file, content="the-operation-secret")
    cfg.state_dir_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_dir_password_file.write_text("preexisting-credential")
    os.chmod(cfg.state_dir_password_file, 0o600)

    result = cap.cmd_activate(cfg)

    # Rotate the preexisting credential -- deliberately to the operation secret's plaintext, to
    # prove even a content match on a PREEXISTING store is not attributed.
    cfg.state_dir_password_file.write_text("the-operation-secret")
    os.chmod(cfg.state_dir_password_file, 0o600)

    obs = cap.cmd_observe_postimage(cfg, Path(result["receipt_path"]))
    assert obs["recorded_operation_postimage"] is False
    state_obs = next(o for o in obs["observations"] if o.get("store") == "state_dir")
    assert state_obs["result"] == "rotation_of_preexisting_observed_preserved"

    rb = cap.cmd_rollback(cfg, Path(result["receipt_path"]))
    assert cfg.state_dir_password_file.exists()
    assert cfg.state_dir_password_file.read_text() == "the-operation-secret"
    assert (
        rb["store_actions"]["state_dir_password_file"]
        == "no_verified_operation_postimage_untouched"
    )


def test_observe_records_operation_owned_when_representation_matches_secret(tmp_path):
    """TRUE POSITIVE: the app creates a NEW (absent-before) state-dir credential whose plaintext
    representation logically equals the prepared secret. That connection licenses recording the
    operation postimage; rollback may then remove exactly this operation-owned credential."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    secret = "connected-operation-secret-value"
    write_secret(cfg.secret_file, content=secret)
    assert not cfg.state_dir_password_file.exists()

    result = cap.cmd_activate(cfg)

    # The app writes its store as the plaintext of the secret we activated with (with a trailing
    # newline, a common file-write artifact -- the private compare strips it).
    cfg.state_dir_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_dir_password_file.write_text(secret + "\n")
    os.chmod(cfg.state_dir_password_file, 0o600)

    obs = cap.cmd_observe_postimage(cfg, Path(result["receipt_path"]))
    assert obs["recorded_operation_postimage"] is True
    state_obs = next(o for o in obs["observations"] if o.get("store") == "state_dir")
    assert state_obs["result"] == "created_operation_plaintext_verified_recorded"

    # The receipt carries only the non-secret sha256 -- never the secret value.
    receipt_text = Path(result["receipt_path"]).read_text()
    assert secret not in receipt_text
    receipt = json.loads(receipt_text)
    op_pi = receipt["state_dir_password_file"]["operation_postimage"]
    assert op_pi["sha256"] == cap.sha256_bytes(cfg.state_dir_password_file)

    rb = cap.cmd_rollback(cfg, Path(result["receipt_path"]))
    assert not cfg.state_dir_password_file.exists()
    assert rb["store_actions"]["state_dir_password_file"] == "removed_operation_owned"


def test_observe_true_positive_never_leaks_secret_even_when_recording(tmp_path):
    """The connection compare is private: neither the observe result nor the amended receipt
    ever contains the secret value, only its sha256."""
    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    secret = "do-not-leak-this-observed-secret"
    write_secret(cfg.secret_file, content=secret)
    result = cap.cmd_activate(cfg)

    cfg.app_saved_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.app_saved_password_file.write_text(secret)
    os.chmod(cfg.app_saved_password_file, 0o600)

    obs = cap.cmd_observe_postimage(cfg, Path(result["receipt_path"]))
    assert json.dumps(obs).find(secret) == -1
    app_obs = next(o for o in obs["observations"] if o.get("store") == "app_support")
    assert app_obs["result"] == "created_operation_plaintext_verified_recorded"
    assert secret not in Path(result["receipt_path"]).read_text()


def test_read_store_snapshot_hash_matches_the_exact_returned_bytes(tmp_path):
    """Invariant behind the snapshot-connection fix: the evidence sha256 is always sha256 of the
    SAME bytes returned, from a single read -- there is no window to hash one read and verify
    another."""
    p = tmp_path / "store"
    p.write_bytes(b"some-credential-bytes\n")
    ev, raw = cap._read_store_snapshot(p)
    import hashlib

    assert raw == b"some-credential-bytes\n"
    assert ev["present"] is True
    assert ev["sha256"] == hashlib.sha256(raw).hexdigest()


def test_observe_records_hash_of_verified_bytes_not_a_separate_read(tmp_path, monkeypatch):
    """PIN for activation-snapshot-connection: the OLD code hashed bytes A via _file_evidence but
    verified bytes B via a separate read, then recorded sha256(A) as 'verified' -- so a later
    unrelated rotation to A was deleted by rollback. The fix reads once, so the recorded hash is
    sha256 of exactly the bytes that passed the connection check. Here the snapshot yields the
    operation bytes B (== secret) while the on-disk file holds a DIFFERENT content A; the recorded
    hash must be sha256(B), and a rollback that re-reads A must NOT delete it."""
    import hashlib

    cfg = make_cfg(tmp_path)
    cfg.settings_path.write_text(make_fixture_text(mode_value="cmuxOnly"))
    secret = "the-connected-operation-secret"
    write_secret(cfg.secret_file, content=secret)
    result = cap.cmd_activate(cfg)

    B = (secret + "\n").encode()  # operation's real credential bytes (match the secret)
    A = "an-unrelated-rotation-value"  # a DIFFERENT on-disk content
    cfg.state_dir_password_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_dir_password_file.write_text(A)
    os.chmod(cfg.state_dir_password_file, 0o600)

    real_snapshot = cap._read_store_snapshot

    def snapshot_returns_B(store_path):
        if store_path == cfg.state_dir_password_file:
            return (
                {
                    "path": str(store_path),
                    "present": True,
                    "sha256": hashlib.sha256(B).hexdigest(),
                },
                B,
            )
        return real_snapshot(store_path)

    monkeypatch.setattr(cap, "_read_store_snapshot", snapshot_returns_B)
    obs = cap.cmd_observe_postimage(cfg, Path(result["receipt_path"]))
    monkeypatch.setattr(cap, "_read_store_snapshot", real_snapshot)

    assert obs["recorded_operation_postimage"] is True
    receipt = json.loads(Path(result["receipt_path"]).read_text())
    op = receipt["state_dir_password_file"]["operation_postimage"]
    # Recorded hash is sha256(B) -- the verified bytes -- NOT sha256(A) from a separate read.
    assert op["sha256"] == hashlib.sha256(B).hexdigest()
    assert op["sha256"] != cap.sha256_bytes(cfg.state_dir_password_file)

    # Rollback re-reads the REAL file (A). Since op_sha == sha256(B) != sha256(A), the unrelated
    # A is preserved, not deleted -- the exact loss the old separate-read bug caused.
    rb = cap.cmd_rollback(cfg, Path(result["receipt_path"]))
    assert cfg.state_dir_password_file.exists()
    assert cfg.state_dir_password_file.read_text() == A
    assert (
        rb["store_actions"]["state_dir_password_file"]
        == "unrelated_credential_untouched"
    )
