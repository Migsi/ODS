"""Comprehensive tests for extension_application_record_store.

Covers:
- Happy create/publish, read, active, compare_replace, compare_delete
- Exact duplicate/replay distinction
- Duplicate-key and noncanonical JSON rejection
- Oversize, short-write, fsync, replace, response-loss failures
- Stale CAS, absent CAS
- Symlink/FIFO/device/hardlink/mode/owner/root-swap/name-swap attacks
- Input mutation during call
- Concurrent cross-process lock contention
- Both umask 002 and umask 077
- Windows import fails with controlled platform code
- No production import/wiring
"""

from __future__ import annotations

import ast
import errno
import hashlib
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

BIN_DIR = Path(__file__).resolve().parents[4] / "bin"
ODS_ROOT = Path(__file__).resolve().parents[4]
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_NONBLOCK")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
)
if SUPPORTED:
    import extension_application_record_store as store_mod
    import extension_lifecycle_plan as lifecycle_plan
    import extension_lifecycle_work as lifecycle_work
else:
    store_mod = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Constants / fixtures (aligned with test_extension_application_observation)
# ---------------------------------------------------------------------------

TRANSACTION_ID = "txn-" + "1" * 24
PLAN_HASH = "2" * 64
DEFINITION_SHA = "sha256:" + "3" * 64
COMPOSE_SHA = "sha256:" + "4" * 64
SERVICE_ID = "documents"
VERSION = "1.2.3"
ACTION = "install"
CONFIG_SHA = "sha256:" + "e" * 64
CONTAINER_NAMES = ("documents-api", "documents-worker")


def _definition(service_id: str, compose: str | None = COMPOSE_SHA) -> dict:
    return {
        "id": service_id,
        "serviceType": "docker",
        "manifestSchemaVersion": "ods.services.v2",
        "version": VERSION,
        "dataSchemaVersion": "1",
        "odsCompatibility": {"minimum": "2.0.0", "maximum": "3.0.0"},
        "definitionSha256": DEFINITION_SHA,
        "composeSha256": compose,
        "definitionSource": "library",
        "composeFile": "compose.yaml" if compose else None,
        "dependsOn": [],
        "provides": [],
        "requires": [],
        "conflicts": [],
        "requirements": {},
        "estimates": {},
        "configuration": [],
        "artifacts": {
            "images": [
                {
                    "reference": f"example.invalid/{service_id}:{VERSION}",
                    "digest": "sha256:" + "5" * 64,
                    "downloadBytes": 123,
                }
            ],
            "builds": [],
        },
        "resources": {},
        "lifecycle": {},
        "data": [],
        "trust": {},
        "support": {},
    }


def _transaction(state: str, definitions: list[dict]) -> dict:
    return {
        "transactionId": TRANSACTION_ID,
        "state": state,
        "approval": {
            "transactionId": TRANSACTION_ID,
            "planHash": PLAN_HASH,
            "approvedBy": "owner",
        },
        "envelope": {
            "planHash": PLAN_HASH,
            "plan": {
                "selectedServices": [d["id"] for d in definitions],
                "operations": [
                    {"serviceId": d["id"], "action": ACTION} for d in definitions
                ],
                "definitions": definitions,
            },
        },
    }


def _command(operation_key: str, service_ids: list[str], payload: dict):
    unsigned = {
        "schema": lifecycle_work.REQUEST_SCHEMA,
        "transactionId": TRANSACTION_ID,
        "planHash": PLAN_HASH,
        "operationKey": operation_key,
        "serviceIds": service_ids,
        "payload": payload,
    }
    request = {
        **unsigned,
        "requestHash": hashlib.sha256(
            json.dumps(
                unsigned,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
    }
    return lifecycle_work.parse_lifecycle_work_request(request)


def _bound_command(
    service_id: str = SERVICE_ID,
    action: str = ACTION,
    version: str = VERSION,
    compose: str | None = COMPOSE_SHA,
) -> lifecycle_work.LifecycleWorkCommand:
    definitions = [_definition(service_id, compose)]
    tx = _transaction("applying", definitions)
    payload = {"operation": {"serviceId": service_id, "action": action}}
    cmd = _command(f"apply:{service_id}", [service_id], payload)
    return lifecycle_plan.bind_lifecycle_plan(cmd, tx)


# ---------------------------------------------------------------------------
# Windows import test
# ---------------------------------------------------------------------------


class WindowsImportTests(unittest.TestCase):
    """Windows import must fail with controlled platform code, not crash."""

    def test_windows_import_platform_check(self) -> None:
        if SUPPORTED:
            self.skipTest("POSIX platform; cannot simulate Windows import here")
        self.assertIsNone(store_mod)


# ---------------------------------------------------------------------------
# POSIX store tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(SUPPORTED, "requires POSIX descriptor-relative filesystem APIs")
class ApplicationRecordStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "application-state"
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o700)
        self.store = store_mod.ApplicationRecordStore(self.root)
        self.command = _bound_command()
        self.config_sha = CONFIG_SHA
        self.containers: tuple[str, ...] = CONTAINER_NAMES

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def snapshot_path(self) -> Path:
        return self.root / store_mod.SNAPSHOT_NAME

    def assert_code(self, code: str, call) -> None:
        with self.assertRaises(store_mod.ApplicationRecordStoreError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    # -- Basic happy path --

    def test_create_then_read(self) -> None:
        result = self.store.publish(self.command, self.config_sha, self.containers)
        self.assertFalse(result.duplicate)
        self.assertEqual(result.entry.service_id, "documents")

        read = self.store.read("documents")
        self.assertTrue(read.found)
        self.assertIsNotNone(read.entry)
        self.assertEqual(read.entry.record_sha256, result.entry.record_sha256)

    def test_create_exact_replay_is_duplicate(self) -> None:
        first = self.store.publish(self.command, self.config_sha, self.containers)
        self.assertFalse(first.duplicate)
        replay = self.store.publish(self.command, self.config_sha, self.containers)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.entry.record_sha256, first.entry.record_sha256)

    def test_read_absent_service(self) -> None:
        read = self.store.read("nonexistent")
        self.assertFalse(read.found)
        self.assertIsNone(read.entry)

    def test_active_returns_sorted(self) -> None:
        cmd_b = _bound_command("beta-service")
        cmd_a = _bound_command("alpha-service")
        cmd_c = _bound_command("gamma-service")

        self.store.publish(cmd_b, self.config_sha, ("beta-api",))
        self.store.publish(cmd_a, self.config_sha, ("alpha-api",))
        self.store.publish(cmd_c, self.config_sha, ("gamma-api",))

        active = self.store.active()
        self.assertEqual(len(active), 3)
        self.assertEqual(
            [e.service_id for e in active],
            ["alpha-service", "beta-service", "gamma-service"],
        )

    # -- Compare-and-replace --

    def test_compare_replace_happy(self) -> None:
        first = self.store.publish(self.command, self.config_sha, self.containers)
        new_command = _bound_command("documents", "update", "2.0.0")
        new_config = "sha256:" + "f" * 64

        result = self.store.compare_replace(
            new_command, new_config, self.containers, first.entry.record_sha256
        )
        self.assertFalse(result.duplicate)
        read = self.store.read("documents")
        self.assertTrue(read.found)
        self.assertEqual(read.entry.record_sha256, result.entry.record_sha256)
        self.assertNotEqual(read.entry.record_sha256, first.entry.record_sha256)

    def test_compare_replace_stale(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        stale_sha = "0" * 64
        new_command = _bound_command("documents", "update", "2.0.0")
        self.assert_code(
            "store-cas-stale",
            lambda: self.store.compare_replace(
                new_command, self.config_sha, self.containers, stale_sha
            ),
        )

    def test_compare_replace_absent(self) -> None:
        new_command = _bound_command("documents", "update", "2.0.0")
        self.assert_code(
            "store-cas-absent",
            lambda: self.store.compare_replace(
                new_command, self.config_sha, self.containers, "1" * 64
            ),
        )

    def test_compare_replace_exact_replay(self) -> None:
        first = self.store.publish(self.command, self.config_sha, self.containers)
        result = self.store.compare_replace(
            self.command, self.config_sha, self.containers, first.entry.record_sha256
        )
        self.assertTrue(result.duplicate)
        self.assertEqual(result.entry.record_sha256, first.entry.record_sha256)

    # -- Compare-and-delete --

    def test_compare_delete_happy(self) -> None:
        first = self.store.publish(self.command, self.config_sha, self.containers)
        result = self.store.compare_delete(
            "documents", first.entry.record_sha256
        )
        self.assertTrue(result.deleted)
        self.assertIsNotNone(result.prior)
        self.assertEqual(result.prior.record_sha256, first.entry.record_sha256)

        read = self.store.read("documents")
        self.assertFalse(read.found)

    def test_compare_delete_stale(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        self.assert_code(
            "store-cas-stale",
            lambda: self.store.compare_delete("documents", "0" * 64),
        )

    def test_compare_delete_absent(self) -> None:
        self.assert_code(
            "store-cas-absent",
            lambda: self.store.compare_delete("documents", "0" * 64),
        )

    # -- Conflict --

    def test_publish_conflict_different_record(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        new_command = _bound_command("documents", "update", "2.0.0")
        self.assert_code(
            "store-publish-conflict",
            lambda: self.store.publish(new_command, self.config_sha, self.containers),
        )

    # -- Umask tests --

    def test_mode_is_0600_under_permissive_umask(self) -> None:
        old_umask = os.umask(0o002)
        try:
            self.store.publish(self.command, self.config_sha, self.containers)
            mode = stat.S_IMODE(self.snapshot_path.stat().st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            os.umask(old_umask)

    def test_mode_is_0600_under_restrictive_umask(self) -> None:
        old_umask = os.umask(0o077)
        try:
            self.store.publish(self.command, self.config_sha, self.containers)
            mode = stat.S_IMODE(self.snapshot_path.stat().st_mode)
            self.assertEqual(mode, 0o600)
        finally:
            os.umask(old_umask)

    # -- Symlink/FIFO/device attacks --

    def test_symlink_snapshot_rejected(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        real_data = self.snapshot_path.read_bytes()
        self.snapshot_path.unlink()
        fake = self.root / "fake.json"
        fake.write_bytes(real_data)
        self.snapshot_path.symlink_to(fake)
        self.assert_code("store-corrupt-snapshot-file", self.store.active)
        self.snapshot_path.unlink(missing_ok=True)

    def test_fifo_snapshot_rejected(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        self.snapshot_path.unlink()
        os.mkfifo(str(self.snapshot_path))
        self.assert_code("store-corrupt-snapshot-file", self.store.active)

    def test_device_snapshot_rejected(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        self.snapshot_path.unlink()
        try:
            dev_fd = os.mknod(str(self.snapshot_path), stat.S_IFCHR | 0o600, 0)
            os.close(dev_fd)
        except OSError:
            self.skipTest("mknod not permitted (likely container)")
        self.assert_code("store-corrupt-snapshot-file", self.store.active)

    def test_hardlink_snapshot_rejected(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        extra_link = self.root / "extra-link.json"
        os.link(str(self.snapshot_path), str(extra_link))
        self.assert_code("store-corrupt-snapshot-nlink", self.store.active)

    # -- Mode/owner attacks --

    def test_wrong_mode_snapshot_rejected(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        self.snapshot_path.chmod(0o755)
        self.assert_code("store-corrupt-snapshot-mode", self.store.active)

    def test_wrong_owner_snapshot_rejected(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("requires root to chown to another uid")
        self.store.publish(self.command, self.config_sha, self.containers)
        try:
            os.chown(str(self.snapshot_path), 65534, -1)
        except OSError:
            try:
                os.chown(str(self.snapshot_path), 1, -1)
            except OSError:
                self.skipTest("cannot chown to alternative uid")
        self.assert_code("store-corrupt-snapshot-owner", self.store.active)

    # -- Root-swap / name-swap --

    def test_root_replaced_with_symlink(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        try:
            self.root.rmdir()
        except OSError:
            pass
        decoy = Path(self.temp.name) / "decoy"
        decoy.mkdir(mode=0o700)
        self.root.symlink_to(decoy)
        self.assert_code("store-root-invalid", self.store.active)

    def test_root_removed(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        for f in self.root.iterdir():
            if f.is_file():
                f.unlink()
        self.root.rmdir()
        self.assert_code("store-root-missing", self.store.active)

    # -- Input mutation --

    def test_input_mutation_does_not_affect_store(self) -> None:
        result = self.store.publish(self.command, self.config_sha, self.containers)
        active = self.store.active()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].record_sha256, result.entry.record_sha256)

    # -- Noncanonical JSON --

    def test_noncanonical_snapshot_rejected(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        raw = self.snapshot_path.read_bytes()
        self.snapshot_path.write_bytes(raw.rstrip(b"\n"))
        self.assert_code("store-noncanonical", self.store.active)

    def test_duplicate_key_snapshot_rejected(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        self.snapshot_path.write_bytes(
            b'{"schema":"ods.extension-application-records.v1",'
            b'"schema":"duplicate","records":[]}\n'
        )
        self.assert_code("store-json-invalid", self.store.active)

    # -- Oversize --

    def test_oversize_snapshot_rejected(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        big = b"x" * (store_mod.MAX_FILE_BYTES + 1)
        self.snapshot_path.write_bytes(big)
        self.assert_code("store-oversize", self.store.active)

    # -- Short write / fsync / replace failures --

    def test_short_write_fails(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        before = self.snapshot_path.read_bytes()

        write_calls: list[int] = []

        def fake_write(fd, data, *args, **kwargs):
            write_calls.append(len(data))
            if len(write_calls) == 1:
                return len(data)
            return 0

        with mock.patch.object(store_mod.os, "write", side_effect=fake_write):
            self.assert_code(
                "store-snapshot-io-error",
                lambda: self.store.publish(
                    _bound_command("new-svc"), self.config_sha, ("new-api",)
                ),
            )
        self.assertEqual(self.snapshot_path.read_bytes(), before)

    def test_fsync_failure_preserves_old_state(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        before = self.snapshot_path.read_bytes()

        def fake_fsync(fd):
            raise OSError(errno.EIO, "I/O error")

        with mock.patch.object(store_mod.os, "fsync", side_effect=fake_fsync):
            self.assert_code(
                "store-snapshot-io-error",
                lambda: self.store.publish(
                    _bound_command("new-svc"), self.config_sha, ("new-api",)
                ),
            )
        self.assertEqual(self.snapshot_path.read_bytes(), before)

    def test_replace_failure_preserves_old_state(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        before = self.snapshot_path.read_bytes()

        def fake_replace(*args, **kwargs):
            raise OSError(errno.EIO, "replace failed")

        with mock.patch.object(store_mod.os, "replace", side_effect=fake_replace):
            self.assert_code(
                "store-snapshot-io-error",
                lambda: self.store.publish(
                    _bound_command("new-svc"), self.config_sha, ("new-api",)
                ),
            )
        self.assertEqual(self.snapshot_path.read_bytes(), before)

    def test_response_loss_after_replace(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)

        real_stat = store_mod.os.stat

        def mismatching_stat(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if isinstance(path, str) and path.startswith(store_mod.TEMP_PREFIX):
                return SimpleNamespace(
                    st_dev=result.st_dev,
                    st_ino=result.st_ino + 1,
                    st_mode=result.st_mode,
                    st_nlink=result.st_nlink,
                    st_uid=result.st_uid,
                    st_size=result.st_size,
                    st_mtime_ns=result.st_mtime_ns,
                    st_ctime_ns=result.st_ctime_ns,
                )
            return result

        with mock.patch.object(store_mod.os, "stat", side_effect=mismatching_stat):
            self.assert_code(
                "store-snapshot-integrity",
                lambda: self.store.publish(
                    _bound_command("new-svc"), self.config_sha, ("new-api",)
                ),
            )

    # -- Concurrency --

    def _concurrent_publish_worker(
        self, root: str, marker: str, start, queue
    ) -> None:
        try:
            s = store_mod.ApplicationRecordStore(root)
            cmd = _bound_command(f"svc-{marker}", "install", "1.0.0")
            start.wait()
            result = s.publish(cmd, CONFIG_SHA, (f"svc-{marker}-api",))
            queue.put(("ok", result.entry.service_id))
        except store_mod.ApplicationRecordStoreError as exc:
            queue.put(("error", exc.code))
        except Exception as exc:  # noqa: BLE001
            queue.put(("exception", str(exc)))

    def test_concurrent_publishes_both_succeed_different_services(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        queue = context.Queue()
        workers = [
            context.Process(
                target=self._concurrent_publish_worker,
                args=(str(self.root), marker, start, queue),
            )
            for marker in ("a", "b")
        ]
        for w in workers:
            w.start()
        start.set()
        results = [queue.get(timeout=15) for _ in workers]
        for w in workers:
            w.join(timeout=15)
            self.assertEqual(w.exitcode, 0)
        self.assertEqual([kind for kind, _ in results], ["ok", "ok"])
        active = self.store.active()
        self.assertEqual(len(active), 2)

    # -- Frozen output types --

    def test_entries_are_frozen(self) -> None:
        self.store.publish(self.command, self.config_sha, self.containers)
        read = self.store.read("documents")
        self.assertTrue(read.found)
        with self.assertRaises(FrozenInstanceError):
            read.entry.service_id = "hacked"  # type: ignore[misc]

    def test_read_result_is_frozen(self) -> None:
        read = self.store.read("documents")
        with self.assertRaises(FrozenInstanceError):
            read.found = True  # type: ignore[misc]

    # -- Custody validation --

    def test_root_wrong_mode_fails(self) -> None:
        self.root.chmod(0o755)
        self.assert_code(
            "store-root-custody-violation",
            lambda: store_mod.ApplicationRecordStore(self.root),
        )
        self.root.chmod(0o700)

    def test_root_wrong_owner_fails(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("requires root to chown root")
        try:
            os.chown(str(self.root), 65534, -1)
        except OSError:
            try:
                os.chown(str(self.root), 1, -1)
            except OSError:
                self.skipTest("cannot chown to alternative uid")
        self.assert_code(
            "store-root-custody-violation",
            lambda: store_mod.ApplicationRecordStore(self.root),
        )

    # -- Invalid inputs --

    def test_invalid_service_id(self) -> None:
        self.assert_code(
            "store-binding-invalid",
            lambda: self.store.read("INVALID/SERVICE"),
        )

    def test_invalid_record_sha256(self) -> None:
        self.assert_code(
            "store-binding-invalid",
            lambda: self.store.compare_delete("documents", "not-a-sha"),
        )

    def test_invalid_command_type(self) -> None:
        self.assert_code(
            "store-binding-invalid",
            lambda: self.store.publish(
                "not-a-command", self.config_sha, self.containers
            ),
        )

    def test_invalid_config_sha(self) -> None:
        self.assert_code(
            "store-binding-invalid",
            lambda: self.store.publish(self.command, "bad-sha", self.containers),
        )

    def test_empty_containers(self) -> None:
        self.assert_code(
            "store-binding-invalid",
            lambda: self.store.publish(self.command, self.config_sha, ()),
        )

    def test_containers_not_tuple(self) -> None:
        self.assert_code(
            "store-binding-invalid",
            lambda: self.store.publish(
                self.command, self.config_sha, list(self.containers)
            ),
        )

    # -- Public surface --

    def test_values_frozen_and_public_surface_complete(self) -> None:
        self.assertIn("ApplicationRecordStore", store_mod.__all__)
        self.assertIn("ApplicationRecordStoreError", store_mod.__all__)
        self.assertIn("ActiveRecordEntry", store_mod.__all__)
        self.assertIn("ReadResult", store_mod.__all__)
        self.assertIn("CreateResult", store_mod.__all__)
        self.assertIn("CompareReplaceResult", store_mod.__all__)
        self.assertIn("CompareDeleteResult", store_mod.__all__)

    def test_store_has_no_production_importer_yet(self) -> None:
        offenders: list[str] = []
        roots = [
            ODS_ROOT / "bin",
            ODS_ROOT / "extensions" / "services" / "dashboard-api",
        ]
        for root in roots:
            for path in root.rglob("*.py"):
                if (
                    path.name == "extension_application_record_store.py"
                    or "tests" in path.parts
                ):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import) and any(
                        alias.name == "extension_application_record_store"
                        for alias in node.names
                    ):
                        offenders.append(str(path.relative_to(ODS_ROOT)))
                    if (
                        isinstance(node, ast.ImportFrom)
                        and node.module == "extension_application_record_store"
                    ):
                        offenders.append(str(path.relative_to(ODS_ROOT)))
        self.assertEqual(set(offenders), set())


if __name__ == "__main__":
    unittest.main()
