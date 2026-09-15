"""Real Linux streaming snapshot tests; generic restore/host execution stay disabled."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parents[4] / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import extension_data_stream_snapshot as snapshots  # noqa: E402
from extension_data_scope_contract import bind_data_scope  # noqa: E402
from extension_lifecycle_work import LifecycleWorkExecutionError  # noqa: E402
from test_extension_data_scope_contract import command  # noqa: E402


linux_effect = pytest.mark.skipif(os.name != "posix", reason="descriptor-relative Linux snapshot")


def _command():
    return command(
        actions=("install", "install"),
        selected_paths=(["data/alpha"], ["data/beta"]),
        prior_paths=[],
    )


def _roots(tmp_path: Path):
    install = tmp_path / "install"
    data = tmp_path / "data"
    backup = data / "assistant-first" / "stream-backups"
    alpha = install / "data" / "alpha"
    alpha.mkdir(parents=True)
    backup.mkdir(parents=True)
    for directory in (install, install / "data", alpha, data, data / "assistant-first", backup):
        directory.chmod(0o700)
    return install, data, backup, alpha


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)
    path.chmod(0o600)


def test_stream_scope_is_only_the_attested_old_new_union():
    bound = bind_data_scope(_command())
    index = snapshots._scope_index(bound)
    assert [(service["serviceId"], [path["path"] for path in service["paths"]])
            for service in index] == [("alpha", ["data/alpha"]), ("beta", ["data/beta"])]


@linux_effect
def test_large_file_streams_without_base64_and_absent_path_is_explicit(tmp_path: Path):
    install, data, backup, alpha = _roots(tmp_path)
    large = alpha / "large.bin"
    with large.open("wb") as stream:
        for _ in range(9):
            stream.write(b"a" * 1024 * 1024)
    large.chmod(0o600)
    _write(alpha / "small.txt", b"private value\n")
    value = _command()
    store = snapshots.StreamSnapshotStore(install, data, backup)
    receipt = store.backup(value)
    assert receipt.file_count == 2
    assert receipt.content_bytes == 9 * 1024 * 1024 + len(b"private value\n")
    assert store.verify(value) == receipt
    archive = backup / f"{value.transaction_id}.{value.plan_hash}.tar"
    assert archive.stat().st_mode & 0o777 == 0o400
    assert archive.stat().st_size < 10 * 1024 * 1024
    assert b"private value" not in snapshots._canonical({"receipt": receipt.archive_sha256})
    assert len(list(backup.iterdir())) == 1


@linux_effect
def test_published_first_snapshot_wins_after_live_data_changes(tmp_path: Path):
    install, data, backup, alpha = _roots(tmp_path)
    source = alpha / "note.txt"
    _write(source, b"first\n")
    value = _command()
    store = snapshots.StreamSnapshotStore(install, data, backup)
    initial = store.backup(value)
    _write(source, b"second\n")
    assert store.backup(value) == initial


@linux_effect
def test_source_symlink_fails_without_published_archive(tmp_path: Path):
    install, data, backup, alpha = _roots(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"not in scope")
    (alpha / "link").symlink_to(outside)
    value = _command()
    store = snapshots.StreamSnapshotStore(install, data, backup)
    with pytest.raises(LifecycleWorkExecutionError):
        store.backup(value)
    assert not (backup / f"{value.transaction_id}.{value.plan_hash}.tar").exists()


@linux_effect
def test_file_budget_refuses_snapshot_before_publication(tmp_path: Path, monkeypatch):
    install, data, backup, alpha = _roots(tmp_path)
    _write(alpha / "too-large", b"abcdef")
    monkeypatch.setattr(snapshots, "_MAX_FILE_BYTES", 4)
    value = _command()
    store = snapshots.StreamSnapshotStore(install, data, backup)
    with pytest.raises(LifecycleWorkExecutionError):
        store.backup(value)
    assert not (backup / f"{value.transaction_id}.{value.plan_hash}.tar").exists()
