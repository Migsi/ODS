"""Fixed-root owner-private active-application record store.

Persists the canonical active-record objects produced and parsed by
``extension_application_observation``.  One canonical snapshot file lives in
``data/assistant-first/application-state``.  Cross-process exclusive root lock
prevents concurrent mutation.  The store is intentionally dormant: it does
not wire Docker, Compose, host probes, the Dashboard transaction executor,
installation, or runtime service mutation.

This module performs no network, subprocess, Docker, or environment operations.
It is Linux/POSIX-only with controlled platform-unsupported failure on import.

Custody model mirrors ``extension_resource_reservation_store``: the absolute
root value is validated without following it, every component is traversed
from the anchor with descriptor-relative ``O_DIRECTORY|O_NOFOLLOW`` opens,
custody is re-proven on every public call, and no ``Path.resolve``, ``stat``,
or path-open is ever used on the root or the state file.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from typing import Any

from extension_application_identity import (
    ApplicationIdentityError,
    produce_application_identity,
)
from extension_application_observation import (
    _no_duplicate_keys,
    parse_active_record,
    produce_active_record,
)
from extension_lifecycle_work import (
    LifecycleWorkCommand,
    LifecycleWorkError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STORE_SCHEMA = "ods.extension-application-records.v1"
MAX_RECORDS = 256
MAX_FILE_BYTES = 4 * 1024 * 1024  # 4 MiB

SNAPSHOT_NAME = "application-state.json"
TEMP_PREFIX = "tmp-"
TEMP_SUFFIX = ".app-state-tmp"

_ROOT_MODE = 0o700
_FILE_MODE = 0o600

# ---------------------------------------------------------------------------
# Platform guard (import-time)
# ---------------------------------------------------------------------------


class ApplicationRecordStoreError(LifecycleWorkError):
    """Store error with a stable public code. Value-free."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __repr__(self) -> str:
        return f"ApplicationRecordStoreError({self.code!r})"


def _platform_supported() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_NONBLOCK")
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and os.unlink in os.supports_dir_fd
    )


def _fail(code: str) -> None:
    raise ApplicationRecordStoreError(code) from None


# ---------------------------------------------------------------------------
# Frozen output types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActiveRecordEntry:
    """One persisted active record, keyed by service_id."""

    service_id: str
    record_sha256: str
    duplicate: bool = False


@dataclass(frozen=True)
class ReadResult:
    """Frozen read result for a single service."""

    found: bool
    entry: ActiveRecordEntry | None


@dataclass(frozen=True)
class CreateResult:
    """Frozen create result."""

    entry: ActiveRecordEntry
    duplicate: bool


@dataclass(frozen=True)
class CompareReplaceResult:
    """Frozen compare-and-replace result."""

    entry: ActiveRecordEntry
    duplicate: bool


@dataclass(frozen=True)
class CompareDeleteResult:
    """Frozen compare-and-delete result."""

    deleted: bool
    prior: ActiveRecordEntry | None


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_HASH64_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


def _validate_record_sha256(value: Any) -> str:
    if not isinstance(value, str) or _HASH64_RE.fullmatch(value) is None:
        _fail("store-binding-invalid")
    return value


def _validate_service_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or _SERVICE_ID_RE.fullmatch(value) is None
    ):
        _fail("store-binding-invalid")
    return value


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _canonical_store_json_bytes(value: Any) -> bytes:
    """Produce canonical JSON bytes for the store envelope."""
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("store-json-invalid")
    if not encoded or len(encoded) > MAX_FILE_BYTES:
        _fail("store-oversize")
    return encoded


# ---------------------------------------------------------------------------
# Snapshot parsing / validation
# ---------------------------------------------------------------------------


def _validate_snapshot(data: Any) -> list[dict[str, Any]]:
    """Validate the complete snapshot state. Raises on any fault."""
    if not isinstance(data, dict):
        _fail("store-schema-invalid")
    if set(data.keys()) != {"schema", "records"}:
        _fail("store-schema-invalid")
    if data["schema"] != STORE_SCHEMA:
        _fail("store-schema-invalid")
    records = data["records"]
    if not isinstance(records, list):
        _fail("store-records-type")
    if len(records) > MAX_RECORDS:
        _fail("store-oversize-records")

    previous_sid: str | None = None
    for record in records:
        parse_active_record(
            _canonical_store_json_bytes(record)
        )
        sid = record["service_id"]
        if previous_sid is not None and sid <= previous_sid:
            _fail("store-record-order")
        previous_sid = sid
    return records


def _parse_snapshot(raw: bytes) -> list[dict[str, Any]]:
    """Decode strictly, validate, then require canonical byte equality."""
    if type(raw) is not bytes:
        _fail("store-bytes-required")
    if not raw:
        _fail("store-empty-snapshot")
    if len(raw) > MAX_FILE_BYTES:
        _fail("store-oversize")
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, TypeError, RecursionError):
        _fail("store-json-invalid")
    _validate_snapshot(value)
    if _canonical_store_json_bytes(value) != raw:
        _fail("store-noncanonical")
    return value["records"]


def _empty_snapshot_bytes() -> bytes:
    return _canonical_store_json_bytes({"schema": STORE_SCHEMA, "records": []})


# ---------------------------------------------------------------------------
# Platform and root custody (mirrors reservation store)
# ---------------------------------------------------------------------------


def _validate_platform() -> None:
    if not _platform_supported():
        _fail("platform-unsupported")


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_read_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _root_components(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or "\x00" in value:
        _fail("store-root-invalid")
    if not value.startswith("/") or value == "/":
        _fail("store-root-invalid")
    parts = value.split("/")[1:]
    if any(part in {"", ".", ".."} for part in parts):
        _fail("store-root-invalid")
    return tuple(parts)


def _open_root(parts: tuple[str, ...]) -> int:
    try:
        descriptor = os.open("/", _directory_flags())
    except OSError:
        _fail("store-root-missing")
    try:
        info = os.fstat(descriptor)
    except OSError:
        _close_quietly(descriptor)
        _fail("store-root-io-error")
    try:
        if not stat.S_ISDIR(info.st_mode):
            _close_quietly(descriptor)
            _fail("store-root-invalid")
        for component in parts:
            parent = descriptor
            descriptor = -1
            try:
                descriptor = os.open(component, _directory_flags(), dir_fd=parent)
            except FileNotFoundError:
                _fail("store-root-missing")
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    _fail("store-root-invalid")
                _fail("store-root-io-error")
            finally:
                _close_quietly(parent)
            try:
                info = os.fstat(descriptor)
            except OSError:
                _fail("store-root-io-error")
            if not stat.S_ISDIR(info.st_mode):
                _fail("store-root-invalid")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != _ROOT_MODE:
            _fail("store-root-custody-violation")
        return descriptor
    except BaseException:
        if descriptor >= 0:
            _close_quietly(descriptor)
        raise


# ---------------------------------------------------------------------------
# Snapshot file I/O
# ---------------------------------------------------------------------------


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _check_snapshot_file(descriptor: int) -> os.stat_result:
    try:
        info = os.fstat(descriptor)
    except OSError:
        _fail("store-snapshot-io-error")
    if not stat.S_ISREG(info.st_mode):
        _fail("store-corrupt-snapshot-file")
    if info.st_uid != os.geteuid():
        _fail("store-corrupt-snapshot-owner")
    if stat.S_IMODE(info.st_mode) != _FILE_MODE:
        _fail("store-corrupt-snapshot-mode")
    if info.st_nlink != 1:
        _fail("store-corrupt-snapshot-nlink")
    if info.st_size > MAX_FILE_BYTES:
        _fail("store-oversize")
    return info


def _read_all(descriptor: int, size: int) -> bytes:
    content = bytearray()
    try:
        while len(content) <= MAX_FILE_BYTES:
            remaining = MAX_FILE_BYTES + 1 - len(content)
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            content.extend(chunk)
    except OSError:
        _fail("store-snapshot-io-error")
    if len(content) > MAX_FILE_BYTES:
        _fail("store-oversize")
    if len(content) != size:
        _fail("store-snapshot-integrity")
    return bytes(content)


def _read_snapshot(root_fd: int) -> bytes:
    try:
        fd = os.open(SNAPSHOT_NAME, _file_read_flags(), dir_fd=root_fd)
    except FileNotFoundError:
        _fail("store-snapshot-missing")
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            _fail("store-snapshot-integrity")
        _fail("store-snapshot-io-error")
    try:
        before = _check_snapshot_file(fd)
        raw = _read_all(fd, before.st_size)
        after = _check_snapshot_file(fd)
        if _identity(before) != _identity(after):
            _fail("store-snapshot-integrity")
        try:
            path_info = os.stat(SNAPSHOT_NAME, dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            _fail("store-snapshot-integrity")
        if (path_info.st_dev, path_info.st_ino) != (after.st_dev, after.st_ino):
            _fail("store-snapshot-integrity")
        return raw
    finally:
        _close_quietly(fd)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    try:
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                _fail("store-snapshot-io-error")
            offset += written
    except OSError:
        _fail("store-snapshot-io-error")


def _cleanup_temp(root_fd: int, temp_name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(temp_name, dir_fd=root_fd, follow_symlinks=False)
    except OSError:
        return
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        return
    try:
        os.unlink(temp_name, dir_fd=root_fd)
    except OSError:
        pass


def _write_snapshot(root_fd: int, records: list[dict[str, Any]]) -> None:
    envelope = {"schema": STORE_SCHEMA, "records": records}
    _validate_snapshot(envelope)
    payload = _canonical_store_json_bytes(envelope)
    if len(payload) > MAX_FILE_BYTES:
        _fail("store-oversize")

    temp_name = TEMP_PREFIX + secrets.token_hex(16) + TEMP_SUFFIX
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        temp_fd = os.open(temp_name, flags, _FILE_MODE, dir_fd=root_fd)
    except OSError:
        _fail("store-snapshot-io-error")

    temp_info: os.stat_result | None = None
    replaced = False
    try:
        try:
            temp_info = os.fstat(temp_fd)
        except OSError:
            _fail("store-snapshot-integrity")
        if (
            not stat.S_ISREG(temp_info.st_mode)
            or temp_info.st_uid != os.geteuid()
            or stat.S_IMODE(temp_info.st_mode) != _FILE_MODE
            or temp_info.st_nlink != 1
        ):
            _fail("store-snapshot-integrity")

        _write_all(temp_fd, payload)
        try:
            os.fsync(temp_fd)
            sealed = os.fstat(temp_fd)
        except OSError:
            _fail("store-snapshot-io-error")
        if (
            (sealed.st_dev, sealed.st_ino) != (temp_info.st_dev, temp_info.st_ino)
            or not stat.S_ISREG(sealed.st_mode)
            or sealed.st_uid != os.geteuid()
            or sealed.st_size != len(payload)
            or stat.S_IMODE(sealed.st_mode) != _FILE_MODE
            or sealed.st_nlink != 1
        ):
            _fail("store-snapshot-integrity")

        try:
            named_temp = os.stat(temp_name, dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            _fail("store-snapshot-integrity")
        if _identity(named_temp) != _identity(sealed):
            _fail("store-snapshot-integrity")

        try:
            os.replace(
                temp_name,
                SNAPSHOT_NAME,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            replaced = True
        except OSError:
            _fail("store-snapshot-io-error")

        try:
            os.fsync(root_fd)
        except OSError:
            _fail("store-snapshot-io-error")

        # Reopen and require the exact payload bytes.
        if _read_snapshot(root_fd) != payload:
            _fail("store-snapshot-integrity")
    finally:
        _close_quietly(temp_fd)
        if not replaced and temp_info is not None:
            _cleanup_temp(root_fd, temp_name, temp_info)


def _read_records(root_fd: int) -> list[dict[str, Any]]:
    try:
        raw = _read_snapshot(root_fd)
    except ApplicationRecordStoreError as exc:
        if exc.code == "store-snapshot-missing":
            return []
        raise
    return _parse_snapshot(raw)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ApplicationRecordStore:
    """Snapshot-backed active-application record store.

    The root is re-validated and re-opened for every public call.  Exclusive
    ``flock`` is taken on the root descriptor and explicitly released in
    ``finally`` before close.

    Records are sorted by ``service_id``.  A duplicate ``service_id`` within
    the snapshot is rejected as corrupt state.
    """

    def __init__(self, root_path: str | os.PathLike[str]) -> None:
        _validate_platform()
        try:
            raw = os.fspath(root_path)
        except (TypeError, ValueError):
            _fail("store-root-invalid")
        self._root_parts = _root_components(raw)
        _close_quietly(_open_root(self._root_parts))

    def _run_locked(self, operation: Any) -> Any:
        root_fd = _open_root(self._root_parts)
        locked = False
        try:
            try:
                fcntl.flock(root_fd, fcntl.LOCK_EX)
            except OSError:
                _fail("store-lock-error")
            locked = True
            return operation(root_fd)
        finally:
            if locked:
                try:
                    fcntl.flock(root_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            _close_quietly(root_fd)

    # -- readers -------------------------------------------------------------

    def read(self, service_id: str) -> ReadResult:
        """Read the canonical active record for one service.

        Returns ``ReadResult(found=True, entry=...)`` or
        ``ReadResult(found=False, entry=None)``.
        """
        service_id = _validate_service_id(service_id)

        def operation(root_fd: int) -> ReadResult:
            for record in _read_records(root_fd):
                if record["service_id"] == service_id:
                    return ReadResult(
                        found=True,
                        entry=ActiveRecordEntry(
                            service_id=record["service_id"],
                            record_sha256=record["record_sha256"],
                            duplicate=False,
                        ),
                    )
            return ReadResult(found=False, entry=None)

        return self._run_locked(operation)

    def active(self) -> tuple[ActiveRecordEntry, ...]:
        """Return all persisted records sorted by service_id."""

        def operation(root_fd: int) -> tuple[ActiveRecordEntry, ...]:
            records = _read_records(root_fd)
            return tuple(
                ActiveRecordEntry(
                    service_id=r["service_id"],
                    record_sha256=r["record_sha256"],
                    duplicate=False,
                )
                for r in records
            )

        return self._run_locked(operation)

    # -- writers -------------------------------------------------------------

    def create(
        self,
        command: LifecycleWorkCommand,
        config_sha256: str,
        expected_containers: tuple[str, ...],
    ) -> CreateResult:
        """Create or exact-replay one active record.

        Accepts a plan-bound command and config digest.  Calls the existing
        ``produce_application_identity`` and ``produce_active_record`` from
        the observation module; never trusts a caller-fabricated dict.

        An exact replay (same record_sha256 already persisted for the same
        service_id) returns the persisted record with ``duplicate=True``.
        A conflicting active record for the same service_id with a different
        record_sha256 raises ``store-binding-conflict``.
        """
        if not isinstance(command, LifecycleWorkCommand):
            _fail("store-binding-invalid")
        if (
            not isinstance(config_sha256, str)
            or not re.fullmatch(r"^sha256:[0-9a-f]{64}$", config_sha256)
        ):
            _fail("store-binding-invalid")
        if type(expected_containers) is not tuple or not expected_containers:
            _fail("store-binding-invalid")

        def operation(root_fd: int) -> CreateResult:
            try:
                identity = produce_application_identity(command)
            except ApplicationIdentityError:
                _fail("store-identity-invalid")
            try:
                record_bytes = produce_active_record(
                    identity, config_sha256, expected_containers
                )
            except Exception as exc:  # noqa: BLE001
                code = (
                    exc.code if isinstance(exc, LifecycleWorkError) else "store-record-invalid"
                )
                _fail(code)

            record = parse_active_record(record_bytes)
            service_id = record["service_id"]
            record_sha256 = record["record_sha256"]
            records = _read_records(root_fd)

            for i, existing in enumerate(records):
                if existing["service_id"] == service_id:
                    if existing["record_sha256"] == record_sha256:
                        return CreateResult(
                            entry=ActiveRecordEntry(
                                service_id=service_id,
                                record_sha256=record_sha256,
                                duplicate=True,
                            ),
                            duplicate=True,
                        )
                    _fail("store-binding-conflict")

            if len(records) >= MAX_RECORDS:
                _fail("store-oversize-records")

            new_record = {
                k: v for k, v in record.items()
            }
            records.append(new_record)
            records.sort(key=lambda r: r["service_id"])
            _write_snapshot(root_fd, records)

            return CreateResult(
                entry=ActiveRecordEntry(
                    service_id=service_id,
                    record_sha256=record_sha256,
                    duplicate=False,
                ),
                duplicate=False,
            )

        return self._run_locked(operation)

    def compare_replace(
        self,
        command: LifecycleWorkCommand,
        config_sha256: str,
        expected_containers: tuple[str, ...],
        prior_record_sha256: str,
    ) -> CompareReplaceResult:
        """Compare-and-replace one active record.

        Requires that the current persisted record for the same service_id
        has exactly ``prior_record_sha256``.  Produces the replacement from
        the plan-bound command (never trusts a caller dict).

        An exact replay (new record matches what's already there) returns
        with ``duplicate=True``.  A stale compare value raises
        ``store-cas-stale``.  Missing target raises ``store-cas-absent``.
        """
        if not isinstance(command, LifecycleWorkCommand):
            _fail("store-binding-invalid")
        if (
            not isinstance(config_sha256, str)
            or not re.fullmatch(r"^sha256:[0-9a-f]{64}$", config_sha256)
        ):
            _fail("store-binding-invalid")
        if type(expected_containers) is not tuple or not expected_containers:
            _fail("store-binding-invalid")
        prior_record_sha256 = _validate_record_sha256(prior_record_sha256)

        def operation(root_fd: int) -> CompareReplaceResult:
            try:
                identity = produce_application_identity(command)
            except ApplicationIdentityError:
                _fail("store-identity-invalid")
            try:
                record_bytes = produce_active_record(
                    identity, config_sha256, expected_containers
                )
            except Exception as exc:  # noqa: BLE001
                code = (
                    exc.code if isinstance(exc, LifecycleWorkError) else "store-record-invalid"
                )
                _fail(code)

            record = parse_active_record(record_bytes)
            service_id = record["service_id"]
            record_sha256 = record["record_sha256"]
            records = _read_records(root_fd)

            target_idx: int | None = None
            for i, existing in enumerate(records):
                if existing["service_id"] == service_id:
                    target_idx = i
                    break

            if target_idx is None:
                _fail("store-cas-absent")
            if records[target_idx]["record_sha256"] != prior_record_sha256:
                _fail("store-cas-stale")

            # Exact replay: new record identical to what is persisted
            if records[target_idx]["record_sha256"] == record_sha256:
                return CompareReplaceResult(
                    entry=ActiveRecordEntry(
                        service_id=service_id,
                        record_sha256=record_sha256,
                        duplicate=True,
                    ),
                    duplicate=True,
                )

            records[target_idx] = {k: v for k, v in record.items()}
            records.sort(key=lambda r: r["service_id"])
            _write_snapshot(root_fd, records)

            return CompareReplaceResult(
                entry=ActiveRecordEntry(
                    service_id=service_id,
                    record_sha256=record_sha256,
                    duplicate=False,
                ),
                duplicate=False,
            )

        return self._run_locked(operation)

    def compare_delete(
        self,
        service_id: str,
        record_sha256: str,
    ) -> CompareDeleteResult:
        """Compare-and-delete one active record.

        Requires that the current persisted record has exactly
        ``record_sha256``.  A stale value raises ``store-cas-stale``.
        Missing target raises ``store-cas-absent``.
        """
        service_id = _validate_service_id(service_id)
        record_sha256 = _validate_record_sha256(record_sha256)

        def operation(root_fd: int) -> CompareDeleteResult:
            records = _read_records(root_fd)

            target_idx: int | None = None
            for i, existing in enumerate(records):
                if existing["service_id"] == service_id:
                    target_idx = i
                    break

            if target_idx is None:
                _fail("store-cas-absent")
            if records[target_idx]["record_sha256"] != record_sha256:
                _fail("store-cas-stale")

            prior = records[target_idx]
            del records[target_idx]
            _write_snapshot(root_fd, records)

            return CompareDeleteResult(
                deleted=True,
                prior=ActiveRecordEntry(
                    service_id=prior["service_id"],
                    record_sha256=prior["record_sha256"],
                    duplicate=False,
                ),
            )

        return self._run_locked(operation)

    def publish(
        self,
        command: LifecycleWorkCommand,
        config_sha256: str,
        expected_containers: tuple[str, ...],
    ) -> CreateResult:
        """Publish a new active record from a plan-bound command.

        This is the primary write path for the ``apply:<serviceId>``
        operation.  It accepts the exact plan-bound ``LifecycleWorkCommand``
        plus config digest and expected containers and calls existing
        application-identity/active-record producers.  It never trusts a
        caller-fabricated dict.

        An exact replay of the same record_sha256 for the same service_id
        returns the persisted record with ``duplicate=True``.  A conflicting
        record with a different sha256 raises ``store-publish-conflict``.
        """
        if not isinstance(command, LifecycleWorkCommand):
            _fail("store-binding-invalid")
        if (
            not isinstance(config_sha256, str)
            or not re.fullmatch(r"^sha256:[0-9a-f]{64}$", config_sha256)
        ):
            _fail("store-binding-invalid")
        if type(expected_containers) is not tuple or not expected_containers:
            _fail("store-binding-invalid")

        # Mutating inputs must be cloned/revalidated before locking.
        # command is a frozen dataclass (safe), config_sha256 is str (safe),
        # expected_containers is tuple (safe), but revalidate shape:
        for name in expected_containers:
            if not isinstance(name, str):
                _fail("store-binding-invalid")

        def operation(root_fd: int) -> CreateResult:
            try:
                identity = produce_application_identity(command)
            except ApplicationIdentityError:
                _fail("store-identity-invalid")
            try:
                record_bytes = produce_active_record(
                    identity, config_sha256, expected_containers
                )
            except Exception as exc:  # noqa: BLE001
                code = (
                    exc.code if isinstance(exc, LifecycleWorkError) else "store-record-invalid"
                )
                _fail(code)

            record = parse_active_record(record_bytes)
            sid = record["service_id"]
            rsha = record["record_sha256"]
            records = _read_records(root_fd)

            for existing in records:
                if existing["service_id"] == sid:
                    if existing["record_sha256"] == rsha:
                        return CreateResult(
                            entry=ActiveRecordEntry(
                                service_id=sid,
                                record_sha256=rsha,
                                duplicate=True,
                            ),
                            duplicate=True,
                        )
                    _fail("store-publish-conflict")

            if len(records) >= MAX_RECORDS:
                _fail("store-oversize-records")

            records.append({k: v for k, v in record.items()})
            records.sort(key=lambda r: r["service_id"])
            _write_snapshot(root_fd, records)

            return CreateResult(
                entry=ActiveRecordEntry(
                    service_id=sid,
                    record_sha256=rsha,
                    duplicate=False,
                ),
                duplicate=False,
            )

        return self._run_locked(operation)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "MAX_FILE_BYTES",
    "MAX_RECORDS",
    "SNAPSHOT_NAME",
    "STORE_SCHEMA",
    "ActiveRecordEntry",
    "ApplicationRecordStore",
    "ApplicationRecordStoreError",
    "CompareDeleteResult",
    "CompareReplaceResult",
    "CreateResult",
    "ReadResult",
]
