"""Linux source-only stage custody tests; no live restore effect."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parents[4] / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import extension_data_stream_restore as restores  # noqa: E402
from extension_lifecycle_work import LifecycleWorkExecutionError  # noqa: E402
from test_extension_data_restore_journal import _ready  # noqa: E402


linux_effect = pytest.mark.skipif(os.name != "posix", reason="descriptor-relative Linux restore stage")


def _read_noatime(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME)
    try:
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


@linux_effect
def test_staged_verified_bytes_leave_live_target_untouched_and_complete_replay(tmp_path: Path):
    install, _backup, alpha, store, command, _root, journal = _ready(tmp_path)
    source = alpha / "note"
    before = (alpha.stat().st_ino, alpha.stat().st_mtime_ns,
              source.stat().st_ino, source.stat().st_atime_ns,
              source.stat().st_mtime_ns, _read_noatime(source))
    stager = restores.StreamRestoreStager(install, store, journal)
    stage_name = stager.stage(command, "alpha", 0)
    assert stage_name is not None
    staged = install / "data" / stage_name
    assert staged.is_dir() and _read_noatime(staged / "note") == before[-1]
    assert (staged / "note").stat().st_mtime_ns == before[4]
    stage_before = (staged.stat().st_ino, staged.stat().st_mtime_ns)
    assert stager.stage(command, "alpha", 0) == stage_name
    assert (staged.stat().st_ino, staged.stat().st_mtime_ns) == stage_before
    assert (alpha.stat().st_ino, alpha.stat().st_mtime_ns,
            source.stat().st_ino, source.stat().st_atime_ns,
            source.stat().st_mtime_ns, _read_noatime(source)) == before


@linux_effect
def test_absent_source_publishes_intent_but_does_not_create_stage(tmp_path: Path):
    install, _backup, _alpha, store, command, root, journal = _ready(tmp_path)
    stager = restores.StreamRestoreStager(install, store, journal)
    assert stager.stage(command, "beta", 0) is None
    assert len(list(root.iterdir())) == 1
    assert not list((install / "data").glob(".ods-restore-stage-*"))


@linux_effect
def test_partial_stage_is_retained_and_refused_on_retry(tmp_path: Path, monkeypatch):
    install, _backup, alpha, store, command, _root, journal = _ready(tmp_path)
    with store.open_verified(command) as (_archive, document, receipt):
        path = document["services"][0]["paths"][0]
        intent = journal.begin(command, receipt, "alpha", 0, path)
    original = os.write
    writes = 0

    def interrupt(fd, content):
        nonlocal writes
        writes += 1
        if writes == 1:
            return original(fd, content[:max(1, len(content) // 2)])
        raise OSError("simulated interrupted stage copy")

    stager = restores.StreamRestoreStager(install, store, journal)
    with monkeypatch.context() as patcher:
        patcher.setattr(restores.os, "write", interrupt)
        with pytest.raises(LifecycleWorkExecutionError):
            stager.stage(command, "alpha", 0)
    stage = install / "data" / intent["stageName"]
    assert stage.is_dir() and (stage / "note").exists()
    assert (stage / "note").read_bytes() != (alpha / "note").read_bytes()
    with pytest.raises(LifecycleWorkExecutionError) as caught:
        stager.stage(command, "alpha", 0)
    assert caught.value.code == "lifecycle-work-data-restore-stage-readback-invalid"
    assert (alpha / "note").read_bytes() == b"private source"
    assert stage.is_dir()


@linux_effect
def test_symlink_stage_collision_after_intent_refuses_without_following(tmp_path: Path):
    install, _backup, alpha, store, command, _root, journal = _ready(tmp_path)
    with store.open_verified(command) as (_archive, document, receipt):
        path = document["services"][0]["paths"][0]
        intent = journal.begin(command, receipt, "alpha", 0, path)
    stage = install / "data" / intent["stageName"]
    stage.symlink_to(alpha, target_is_directory=True)
    with pytest.raises(LifecycleWorkExecutionError) as caught:
        restores.StreamRestoreStager(install, store, journal).stage(command, "alpha", 0)
    assert caught.value.code == "lifecycle-work-data-restore-stage-collision"
    assert stage.is_symlink() and (alpha / "note").read_bytes() == b"private source"
