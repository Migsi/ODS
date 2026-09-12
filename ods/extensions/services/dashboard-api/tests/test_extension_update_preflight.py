from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import assistant_first_planner as planner
import extension_lockfile as lockfile
import extension_update_preflight as preflight


REVISION = "a" * 40


def _lockfile_envelope(ods_version: str = "2.6.0") -> dict:
    document = {
        "schema": "ods.extensions.lockfile.v1",
        "odsVersion": ods_version,
        "catalogRevision": "b" * 64,
        "platform": "linux",
        "architecture": "amd64",
        "containerRuntime": "docker",
        "runtimeMode": "assistant-first",
        "postCommitObservedStateRevision": "c" * 64,
        "extensions": [],
        "lastCommittedTransaction": {
            "transactionId": "txn-" + "d" * 24,
            "planHash": "e" * 64,
            "sequence": 1,
            "plannedObservedStateRevision": "f" * 64,
        },
        "priorLockfileHash": None,
        "backupReference": "backup-v1-20260912T200000Z",
    }
    return lockfile.lockfile_envelope(document)


def _write_lockfile(install_dir: Path, *, canonical: bool = True) -> Path:
    root = install_dir / "data/assistant-first/desired-state"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "extensions.lock.json"
    envelope = _lockfile_envelope()
    if canonical:
        path.write_bytes(lockfile.canonical_lockfile_bytes(envelope))
    else:
        path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    if os.name == "posix":
        root.chmod(0o700)
        path.chmod(0o600)
    return path


def _candidate(candidate_dir: Path) -> tuple[bytes, bytes]:
    repository_ods = Path(__file__).resolve().parents[4]
    manifest = (repository_ods / "manifest.json").read_bytes()
    catalog = (repository_ods / "config/extensions-catalog.json").read_bytes()
    (candidate_dir / "config").mkdir(parents=True)
    (candidate_dir / "manifest.json").write_bytes(manifest)
    (candidate_dir / "config/extensions-catalog.json").write_bytes(catalog)
    return manifest, catalog


def _fixture(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    install_dir = tmp_path / "installed"
    candidate_dir = tmp_path / "candidate"
    install_dir.mkdir()
    candidate_dir.mkdir()
    _write_lockfile(install_dir)
    (install_dir / ".env").write_text("ODS_VERSION=2.6.0\n", encoding="utf-8")
    manifest, catalog = _candidate(candidate_dir)
    return install_dir, candidate_dir, manifest, catalog


def test_exact_candidate_tree_is_bound_and_deterministic(tmp_path: Path) -> None:
    install_dir, candidate_dir, manifest, catalog = _fixture(tmp_path)

    first = preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)
    second = preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)

    assert planner.canonical_json_bytes(first) == planner.canonical_json_bytes(second)
    assert first["schema"] == "ods.extensions.update-preflight.v1"
    assert first["candidateSourceRevision"] == REVISION
    assert first["candidateManifestSha256"] == hashlib.sha256(manifest).hexdigest()
    assert first["candidateCatalogFileSha256"] == hashlib.sha256(catalog).hexdigest()
    assert first["assessmentEnvelope"]["assessment"]["canUpdate"] is True
    assert first["assessmentEnvelope"]["assessment"]["requiresExtensionPlan"] is False
    material = {key: value for key, value in first.items() if key != "preflightHash"}
    assert (
        first["preflightHash"]
        == hashlib.sha256(planner.canonical_json_bytes(material)).hexdigest()
    )


def test_exact_candidate_accepts_canonical_empty_bootstrap_lockfile(
    tmp_path: Path,
) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    document = _lockfile_envelope()["lockfile"]
    document["lastCommittedTransaction"] = None
    document["backupReference"] = None
    bootstrap = lockfile.lockfile_envelope(document)
    path = install_dir / "data/assistant-first/desired-state/extensions.lock.json"
    path.write_bytes(lockfile.canonical_lockfile_bytes(bootstrap))

    result = preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)

    assert result["assessmentEnvelope"]["assessment"]["canUpdate"] is True
    assert (
        result["assessmentEnvelope"]["assessment"]["requiresExtensionPlan"]
        is False
    )


def test_preflight_reads_without_mutating_either_tree(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)

    def inventory(root: Path) -> list[tuple[str, bytes]]:
        return sorted(
            (path.relative_to(root).as_posix(), path.read_bytes())
            for path in root.rglob("*")
            if path.is_file()
        )

    before = (inventory(install_dir), inventory(candidate_dir))
    preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)
    after = (inventory(install_dir), inventory(candidate_dir))

    assert after == before


def test_installed_version_precedence_and_normalization(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    (install_dir / ".env").write_text('ODS_VERSION="v2.6.0"\n', encoding="utf-8")
    (install_dir / ".version").write_text(
        json.dumps({"version": "9.9.9"}), encoding="utf-8"
    )

    result = preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)

    assert result["assessmentEnvelope"]["assessment"]["installedOdsVersion"] == "2.6.0"


def test_version_file_and_manifest_fallbacks(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    (install_dir / ".env").unlink()
    (install_dir / ".version").write_text('{"version":"v2.6.0"}', encoding="utf-8")
    assert preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)

    (install_dir / ".version").unlink()
    (install_dir / "manifest.json").write_bytes(
        (candidate_dir / "manifest.json").read_bytes()
    )
    assert preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)


@pytest.mark.parametrize(
    ("revision", "code"),
    [
        ("A" * 40, "invalid-candidate-revision"),
        ("a" * 39, "invalid-candidate-revision"),
    ],
)
def test_candidate_revision_must_be_an_exact_lowercase_object_id(
    tmp_path: Path, revision: str, code: str
) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)

    with pytest.raises(preflight.ExtensionUpdatePreflightError, match=code):
        preflight.assess_candidate_tree(install_dir, candidate_dir, revision)


def test_candidate_manifest_versions_must_agree(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    manifest_path = candidate_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release"]["version"] = "2.6.1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        preflight.ExtensionUpdatePreflightError, match="manifest-version-mismatch"
    ):
        preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)


def test_duplicate_version_or_json_keys_fail_closed(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    (install_dir / ".env").write_text(
        "ODS_VERSION=2.6.0\nODS_VERSION=2.6.0\n", encoding="utf-8"
    )
    with pytest.raises(
        preflight.ExtensionUpdatePreflightError, match="duplicate-installed-version"
    ):
        preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)

    (install_dir / ".env").write_text("ODS_VERSION=2.6.0\n", encoding="utf-8")
    (candidate_dir / "manifest.json").write_text(
        '{"ods_version":"2.6.0","ods_version":"2.6.0"}', encoding="utf-8"
    )
    with pytest.raises(
        preflight.ExtensionUpdatePreflightError, match="json-duplicate-key"
    ):
        preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)


def test_noncanonical_lockfile_is_rejected(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    _write_lockfile(install_dir, canonical=False)

    with pytest.raises(
        preflight.ExtensionUpdatePreflightError,
        match="source-lockfile-noncanonical",
    ):
        preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)


def test_symlinked_candidate_file_is_rejected(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    catalog_path = candidate_dir / "config/extensions-catalog.json"
    target = candidate_dir / "catalog-target.json"
    target.write_bytes(catalog_path.read_bytes())
    catalog_path.unlink()
    try:
        catalog_path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(preflight.ExtensionUpdatePreflightError, match="file-symlink"):
        preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)


def test_symlinked_root_component_is_rejected(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    linked = tmp_path / "linked-candidate"
    try:
        linked.symlink_to(candidate_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(
        preflight.ExtensionUpdatePreflightError, match="root-component-symlink"
    ):
        preflight.assess_candidate_tree(install_dir, linked, REVISION)


def test_cli_errors_are_bounded_and_do_not_expose_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    install_dir = tmp_path / "private-install-name"
    candidate_dir = tmp_path / "private-candidate-name"
    install_dir.mkdir()
    candidate_dir.mkdir()

    code = preflight.main(
        [
            "--install-dir",
            str(install_dir),
            "--candidate-dir",
            str(candidate_dir),
            "--candidate-revision",
            REVISION,
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert code == preflight.EXIT_INVALID_INPUT
    assert output == {
        "schema": "ods.extensions.update-preflight-error.v1",
        "error": {
            "code": "file-missing",
            "details": {"field": "installed.lockfile"},
        },
    }
    assert "private-install-name" not in json.dumps(output)
    assert "private-candidate-name" not in json.dumps(output)


def test_installed_cli_wrapper_emits_the_bound_preflight(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    repository_ods = Path(__file__).resolve().parents[4]

    completed = subprocess.run(
        [
            sys.executable,
            str(repository_ods / "scripts/assess-extension-update.py"),
            "--install-dir",
            str(install_dir),
            "--candidate-dir",
            str(candidate_dir),
            "--candidate-revision",
            REVISION,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == preflight.EXIT_READY
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["schema"] == preflight.PREFLIGHT_SCHEMA


@pytest.mark.skipif(os.name != "posix", reason="POSIX custody contract")
def test_lockfile_must_retain_owner_only_custody(tmp_path: Path) -> None:
    install_dir, candidate_dir, _, _ = _fixture(tmp_path)
    lock_path = install_dir / "data/assistant-first/desired-state/extensions.lock.json"
    lock_path.chmod(0o644)

    with pytest.raises(preflight.ExtensionUpdatePreflightError, match="file-custody"):
        preflight.assess_candidate_tree(install_dir, candidate_dir, REVISION)


@pytest.mark.parametrize(
    ("can_update", "requires_plan", "expected"),
    [
        (True, False, preflight.EXIT_READY),
        (True, True, preflight.EXIT_EXTENSION_PLAN_REQUIRED),
        (False, False, preflight.EXIT_BLOCKED),
    ],
)
def test_exit_codes_are_stable(
    can_update: bool, requires_plan: bool, expected: int
) -> None:
    result = {
        "assessmentEnvelope": {
            "assessment": {
                "canUpdate": can_update,
                "requiresExtensionPlan": requires_plan,
            }
        }
    }

    assert preflight.exit_code_for_preflight(result) == expected
