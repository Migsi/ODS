"""Bind plan-bound release operations to one atomic batch transition.

This dormant adapter is the release boundary for 5G-V.  It validates the
complete ``release`` command against the plan material, re-proves every
reservation record under a single store lock via typed expectations, and
transitions the exact batch from active to released with deterministic
evidence and idempotent exact replay.  A mid-batch failure cannot strand an
untracked partial release because the store performs one atomic snapshot
write under a single exclusive lock.

The adapter constructs ``ReleaseExpectation`` records from plan-bound
operations and definitions, passing them into ``batch_release`` so that
action, claims, transaction, plan, and service bindings are all re-proved
under the same lock and snapshot.  This eliminates TOCTOU between pre-load
and mutation.

It does not create roots, discover definitions, evaluate Compose, or
operate services and containers.  It writes only the owner-private
reservation records through ``ResourceReservationStore.batch_release``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from extension_lifecycle_plan import (
    PLAN_MATERIAL_SCHEMA,
    LifecyclePlanMaterial,
    PlannedDefinition,
    PlannedHostPort,
    PlannedOperation,
)
from extension_lifecycle_work import (
    LifecycleWorkCommand,
    LifecycleWorkExecutionError,
    LifecycleWorkValidationError,
)
from extension_resource_reservation_store import (
    BATCH_RELEASE_SCHEMA,
    RELEASED,
    ReleaseExpectation,
    ReservationClaims,
    ReservationRecord,
    ReservationStoreError,
    ResourceReservationStore,
)

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ResourceReleaseAdapterError(LifecycleWorkExecutionError):
    """Stable, value-free failure after command binding was accepted."""


def _execution_error(code: str, cause: BaseException | None = None) -> None:
    if cause is None:
        raise ResourceReleaseAdapterError(code) from None
    raise ResourceReleaseAdapterError(code) from cause


def _validation_error(code: str) -> None:
    raise LifecycleWorkValidationError(code) from None


def _map_store_error(exc: ReservationStoreError) -> None:
    """Map every ReservationStoreError code to one fixed lifecycle code."""
    _execution_error("lifecycle-work-release-failed", exc)


def _now_utc_second() -> str:
    """Strict UTC-second wall clock for release timestamps."""
    dt = datetime.now(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_store(store: Any) -> ResourceReservationStore:
    if type(store) is not ResourceReservationStore:
        _execution_error("lifecycle-work-release-adapter-invalid")
    return store


# -------------------------------------------------------------------
# Canonical evidence helpers
# -------------------------------------------------------------------


def _canonical_json_bytes(value: Any) -> bytes:
    """Produce canonical JSON bytes (same canonical form as the store)."""
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            .encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _execution_error("lifecycle-work-release-evidence-failed")


def _sha256hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -------------------------------------------------------------------
# Release evidence construction
# -------------------------------------------------------------------


def _build_release_evidence(
    transaction_id: str,
    plan_hash: str,
    service_ids: tuple[str, ...],
    record_sha256s: tuple[str, ...],
) -> str:
    """Build canonical SHA-256 evidence for a batch release result.

    The document binds transaction, plan, ordered service IDs (mutable
    service order from the plan), and per-record hashes.
    """
    document = {
        "schema": BATCH_RELEASE_SCHEMA,
        "transactionId": transaction_id,
        "planHash": plan_hash,
        "serviceIds": list(service_ids),
        "recordSha256s": list(record_sha256s),
        "outcome": "released",
    }
    return _sha256hex(_canonical_json_bytes(document))


# -------------------------------------------------------------------
# Plan-to-store port conversion
# -------------------------------------------------------------------


def _plan_ports_to_store(
    ports: tuple[PlannedHostPort, ...],
) -> tuple[Any, ...]:
    """Convert plan ports to store HostPort form, sorted by (port, protocol)."""
    from extension_resource_reservation_store import HostPort

    converted: list[Any] = []
    for hp in ports:
        if type(hp) is not PlannedHostPort:
            _validation_error("lifecycle-work-plan-mismatch")
        converted.append(HostPort(port=hp.port, protocol=hp.protocol))
    converted.sort(key=lambda hp: (hp.port, hp.protocol))
    return tuple(converted)


# -------------------------------------------------------------------
# Adapter
# -------------------------------------------------------------------


class ResourceReleaseAdapter:
    """Callable dispatcher for only the plan-bound ``release`` operation."""

    def __init__(self, store: ResourceReservationStore) -> None:
        _validate_store(store)
        self._store = store

    def __call__(self, command: LifecycleWorkCommand) -> str:
        """Re-prove plan, build exact expectations, batch-release atomically.

        Returns the batch-release evidence hash (lowercase hex SHA-256).

        All pre-mutation validation of action, claims, and record binding
        happens inside ``batch_release`` under a single lock via the typed
        expectation surface, eliminating TOCTOU between load and mutation.
        """
        # -- Command type ------------------------------------------------
        if type(command) is not LifecycleWorkCommand:
            _validation_error("lifecycle-work-command-invalid")

        # -- Plan material presence + type -------------------------------
        plan_material = command.plan_material
        if plan_material is None:
            _validation_error("lifecycle-work-plan-missing")
        if type(plan_material) is not LifecyclePlanMaterial:
            _validation_error("lifecycle-work-plan-mismatch")
        if plan_material.schema != PLAN_MATERIAL_SCHEMA:
            _validation_error("lifecycle-work-plan-mismatch")
        if plan_material.transaction_id != command.transaction_id:
            _validation_error("lifecycle-work-plan-mismatch")
        if plan_material.plan_hash != command.plan_hash:
            _validation_error("lifecycle-work-plan-mismatch")

        # -- Plan state must be verifying or reconciling -----------------
        if plan_material.state not in ("verifying", "reconciling"):
            _validation_error("lifecycle-work-plan-mismatch")

        # -- Operations / definitions structural proof -------------------
        if (
            type(plan_material.operations) is not tuple
            or type(plan_material.definitions) is not tuple
        ):
            _validation_error("lifecycle-work-plan-mismatch")
        if not plan_material.operations or not plan_material.definitions:
            _validation_error("lifecycle-work-plan-mismatch")
        if len(plan_material.operations) != len(plan_material.definitions):
            _validation_error("lifecycle-work-plan-mismatch")
        for op in plan_material.operations:
            if type(op) is not PlannedOperation:
                _validation_error("lifecycle-work-plan-mismatch")
        for d in plan_material.definitions:
            if type(d) is not PlannedDefinition:
                _validation_error("lifecycle-work-plan-mismatch")

        # -- Positional alignment: op[i].service_id == def[i].service_id --
        for op, d in zip(plan_material.operations, plan_material.definitions):
            if op.service_id != d.service_id:
                _validation_error("lifecycle-work-plan-mismatch")

        # -- operation_key must be exactly "release" ---------------------
        if command.operation_key != "release":
            _validation_error("lifecycle-work-operation-mismatch")

        # -- Mutable service order (exact) --------------------------------
        # The mutable service order is the canonical release target set:
        # every non-noop operation in the plan, in plan order.
        mutable = tuple(
            op for op in plan_material.operations if op.action != "noop"
        )
        if not mutable:
            _validation_error("lifecycle-work-plan-mismatch")
        mutable_service_ids = tuple(op.service_id for op in mutable)

        # -- service_ids must match mutable service order exactly ---------
        if (
            type(command.service_ids) is not tuple
            or command.service_ids != mutable_service_ids
        ):
            _validation_error("lifecycle-work-command-invalid")

        # -- payload must match batch release expectation -----------------
        expected_payload = {"serviceIds": list(mutable_service_ids)}
        if command.payload != expected_payload:
            _validation_error("lifecycle-work-plan-mismatch")

        # -- Build typed expectations: one per mutable service ------------
        # Each expectation binds service_id, action, and claims from the
        # plan-bound material.  These are passed into batch_release where
        # they are re-proved atomically under the store lock.
        mutable_index: dict[str, tuple[PlannedOperation, PlannedDefinition]] = {}
        for op, d in zip(plan_material.operations, plan_material.definitions):
            if op.action != "noop":
                if op.service_id in mutable_index:
                    _validation_error("lifecycle-work-plan-mismatch")
                mutable_index[op.service_id] = (op, d)

        expectations: list[ReleaseExpectation] = []
        for sid in mutable_service_ids:
            op, defn = mutable_index[sid]
            if defn.host_ports is None or defn.exclusive is None:
                _validation_error("lifecycle-work-plan-mismatch")
            if (
                type(defn.host_ports) is not tuple
                or type(defn.exclusive) is not tuple
            ):
                _validation_error("lifecycle-work-plan-mismatch")

            # Convert plan ports to store form (sorted by port, protocol)
            try:
                store_ports = _plan_ports_to_store(defn.host_ports)
            except (TypeError, ValueError):
                _validation_error("lifecycle-work-plan-mismatch")

            try:
                claims = ReservationClaims(
                    host_ports=store_ports,
                    exclusive=defn.exclusive,
                )
            except (TypeError, ValueError):
                _validation_error("lifecycle-work-plan-mismatch")

            expectations.append(
                ReleaseExpectation(
                    service_id=sid,
                    action=op.action,
                    claims=claims,
                )
            )

        # -- Atomic batch release under one lock --------------------------
        now = _now_utc_second()
        try:
            released = self._store.batch_release(
                command.transaction_id,
                command.plan_hash,
                tuple(expectations),
                now,
            )
        except ReservationStoreError as exc:
            _map_store_error(exc)
        except Exception:  # noqa: BLE001 -- stable value-free fail-closed mapping; raw exception text is never exposed in the error code
            _execution_error("lifecycle-work-release-failed")

        # -- Validate all returned records --------------------------------
        if type(released) is not tuple or len(released) != len(mutable_service_ids):
            _execution_error("lifecycle-work-release-evidence-mismatch")

        for pos, sid in enumerate(mutable_service_ids):
            rec = released[pos]
            if type(rec) is not ReservationRecord:
                _execution_error("lifecycle-work-release-evidence-mismatch")
            if rec.transaction_id != command.transaction_id:
                _execution_error("lifecycle-work-release-evidence-mismatch")
            if rec.plan_hash != command.plan_hash:
                _execution_error("lifecycle-work-release-evidence-mismatch")
            if rec.service_id != sid:
                _execution_error("lifecycle-work-release-evidence-mismatch")
            if rec.status != RELEASED:
                _execution_error("lifecycle-work-release-evidence-mismatch")
            if type(rec.duplicate) is not bool:
                _execution_error("lifecycle-work-release-evidence-mismatch")
            if (
                not isinstance(rec.record_sha256, str)
                or _HASH_RE.fullmatch(rec.record_sha256) is None
            ):
                _execution_error("lifecycle-work-release-evidence-mismatch")

        # -- Build evidence hash ------------------------------------------
        record_sha256s = tuple(rec.record_sha256 for rec in released)
        evidence_hash = _build_release_evidence(
            command.transaction_id,
            command.plan_hash,
            mutable_service_ids,
            record_sha256s,
        )
        return evidence_hash


__all__ = [
    "ResourceReleaseAdapter",
    "ResourceReleaseAdapterError",
]
