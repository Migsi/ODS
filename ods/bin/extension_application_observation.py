"""Pure, dormant current-application observation evidence contract.

A future host ``ObservationAdapter`` must classify each plan-bound
``apply:<serviceId>`` operation as ``ABSENT`` or ``APPLIED`` from current
evidence.  Any partial, contradictory, drifted, malformed, unavailable, or
unsupported evidence raises a stable value-free ``ApplicationObservationError``.

Receipts alone and labels alone are never sufficient.

This module performs no filesystem, os/environment, subprocess, Docker, network,
clock, thread, executor, dispatcher, or mutation operations.  It imports only
stdlib and the existing extension types.

The contract operates on an already plan-bound ``LifecycleWorkCommand`` whose
exact ``operation_key`` is ``apply:<serviceId>``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from extension_application_identity import (
    ApplicationIdentity,
    ApplicationIdentityError,
    parse_observed_labels,
    produce_application_identity,
)
from extension_lifecycle_plan import LifecyclePlanMaterial
from extension_lifecycle_receipts import LifecycleSnapshot
from extension_lifecycle_work import (
    LifecycleWorkCommand,
    LifecycleWorkError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RECORD_SCHEMA = "ods.extension-application-active-record.v1"
RECORD_KEYS = frozenset({
    "schema",
    "service_id",
    "version",
    "action",
    "transaction_id",
    "plan_sha256",
    "request_sha256",
    "definition_sha256",
    "compose_sha256",
    "identity_sha256",
    "config_sha256",
    "expected_containers",
    "record_sha256",
})

MAX_INPUT_BYTES = 256 * 1024  # 256 KB hard input limit
MAX_CONTAINERS = 32
_MAX_CONTAINER_NAME = 128

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HASH64_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,255}$")
_VALID_STATES = frozenset({
    "running", "created", "paused", "restarting", "removing",
    "exited", "dead", "not_found",
})
_VALID_HEALTH = frozenset({"healthy", "unhealthy", "starting", "no_healthcheck"})

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ApplicationObservationError(LifecycleWorkError):
    """Observation failure carrying only a stable public code; never values."""


# ---------------------------------------------------------------------------
# Frozen output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerStateSummary:
    """Summary of one observed container (no secrets, no raw labels)."""

    name: str
    state: str
    health: str
    identity_match: bool


@dataclass(frozen=True)
class ObservationResult:
    """Frozen public observation result.

    Exposes only service_id, classification, identity/record digests, and
    container state/health summaries.  Never exposes configuration values,
    secret values/references, host paths, raw labels, process output, or
    supplied bad values.
    """

    service_id: str
    classification: str  # "ABSENT" | "APPLIED"
    identity_sha256: str
    record_sha256: str | None
    containers: tuple[ContainerStateSummary, ...]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bad(code: str = "application-observation-error") -> None:
    raise ApplicationObservationError(code) from None


# ---------------------------------------------------------------------------
# 1. Canonical active-evidence record schema
# ---------------------------------------------------------------------------


def _validate_canonical_record(record: Any) -> dict[str, Any]:
    """Validate and return a canonical active-evidence record.

    Rejects unknown/missing fields, bool/type confusion, non-canonical names,
    oversized input, and mismatched record digest.
    """
    if not isinstance(record, dict):
        _bad("record-must-be-mapping")
    if set(record) != RECORD_KEYS:
        _bad("record-keys-mismatch")

    schema = record.get("schema")
    if not isinstance(schema, str) or schema != RECORD_SCHEMA:
        _bad("record-schema-invalid")

    service_id = record.get("service_id")
    version = record.get("version")
    action = record.get("action")
    transaction_id = record.get("transaction_id")
    plan_sha256 = record.get("plan_sha256")
    request_sha256 = record.get("request_sha256")
    definition_sha256 = record.get("definition_sha256")
    compose_sha256 = record.get("compose_sha256")
    identity_sha256 = record.get("identity_sha256")
    config_sha256 = record.get("config_sha256")
    expected_containers = record.get("expected_containers")
    record_sha256 = record.get("record_sha256")

    # Type confusion: reject bools masquerading as strings/int
    for label, val in (
        ("service_id", service_id), ("version", version), ("action", action),
        ("transaction_id", transaction_id), ("plan_sha256", plan_sha256),
        ("request_sha256", request_sha256), ("definition_sha256",
                                             definition_sha256),
        ("compose_sha256", compose_sha256), ("identity_sha256", identity_sha256),
        ("config_sha256", config_sha256), ("record_sha256", record_sha256),
    ):
        if isinstance(val, bool):
            _bad(f"record-field-invalid-{label}")

    # Validate identity fields match produce_application_identity output
    if _HASH64_RE.fullmatch(identity_sha256) is None:
        _bad("record-field-invalid-identity_sha256")
    if _DIGEST_RE.fullmatch(definition_sha256) is None:
        _bad("record-field-invalid-definition_sha256")

    # compose_sha256: either digest or "absent" sentinel
    if compose_sha256 != "absent" and _DIGEST_RE.fullmatch(compose_sha256) is None:
        _bad("record-field-invalid-compose_sha256")

    if _DIGEST_RE.fullmatch(config_sha256) is None:
        _bad("record-field-invalid-config_sha256")

    # expected_containers: sorted, unique, bounded list of valid names
    if (
        not isinstance(expected_containers, list)
        or not expected_containers
        or len(expected_containers) > MAX_CONTAINERS
    ):
        _bad("record-field-invalid-expected_containers")
    seen: set[str] = set()
    for idx, name in enumerate(expected_containers):
        if isinstance(name, bool) or not isinstance(name, str):
            _bad("record-field-invalid-expected_containers")
        if not name or len(name) > _MAX_CONTAINER_NAME:
            _bad("record-field-invalid-expected_containers")
        if _CONTAINER_NAME_RE.fullmatch(name) is None:
            _bad("record-field-invalid-expected_containers")
        if name in seen:
            _bad("record-field-invalid-expected_containers")
        seen.add(name)
    # Must be sorted
    if expected_containers != sorted(expected_containers):
        _bad("record-field-invalid-expected_containers")

    # record_sha256 must be sha256 of canonical bytes of the preceding fields
    if _HASH64_RE.fullmatch(record_sha256) is None:
        _bad("record-field-invalid-record_sha256")

    # Recompute record SHA-256
    record_payload: dict[str, Any] = {
        "compose_sha256": compose_sha256,
        "config_sha256": config_sha256,
        "definition_sha256": definition_sha256,
        "expected_containers": expected_containers,
        "identity_sha256": identity_sha256,
        "plan_sha256": plan_sha256,
        "request_sha256": request_sha256,
        "schema": RECORD_SCHEMA,
        "service_id": service_id,
        "transaction_id": transaction_id,
        "version": version,
        "action": action,
    }
    canonical = (
        json.dumps(
            record_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(canonical) > MAX_INPUT_BYTES:
        _bad("record-oversize")

    computed = hashlib.sha256(canonical).hexdigest()
    if computed != record_sha256:
        _bad("record-digest-mismatch")

    return record


# ---------------------------------------------------------------------------
# 2. Current evidence inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerObservation:
    """Strict observation of a single container.

    Must contain exact name, state, health, and label map.
    """

    name: str
    state: str
    health: str
    labels: dict[str, str]


@dataclass(frozen=True)
class CurrentEvidence:
    """Current evidence inputs for observation classification.

    - ``active_record``: canonical active-evidence record or ``None`` (absent).
    - ``active_definition_digest``: actual definition sha256 or ``None`` (absent).
    - ``active_compose_digest``: actual Compose sha256 or ``None`` (absent).
    - ``active_config_digest``: actual configuration sha256 or ``None`` (absent).
    - ``container_observations``: zero or more strict container observations.
    - ``receipt_snapshot``: ``LifecycleSnapshot`` for the exact apply operation.
    - ``docker_available``: whether Docker/probe was reachable.
    """

    active_record: dict[str, Any] | None
    active_definition_digest: str | None
    active_compose_digest: str | None
    active_config_digest: str | None
    container_observations: tuple[ContainerObservation, ...]
    receipt_snapshot: LifecycleSnapshot
    docker_available: bool


def _validate_current_evidence(evidence: Any) -> CurrentEvidence:
    """Validate and return a CurrentEvidence object.

    Rejects type confusion, non-canonical digests, oversized inputs, and
    malformed container observations.
    """
    if not isinstance(evidence, CurrentEvidence):
        _bad("evidence-type-invalid")

    # active_record validation (may be None for absent)
    if evidence.active_record is not None:
        _validate_canonical_record(evidence.active_record)

    # active_definition_digest: must be None or valid digest
    if evidence.active_definition_digest is not None:
        if isinstance(evidence.active_definition_digest, bool):
            _bad("digest-type-confused")
        if not isinstance(evidence.active_definition_digest, str):
            _bad("digest-type-confused")
        if _DIGEST_RE.fullmatch(evidence.active_definition_digest) is None:
            _bad("digest-format-invalid")

    # active_compose_digest: must be None or valid digest
    if evidence.active_compose_digest is not None:
        if isinstance(evidence.active_compose_digest, bool):
            _bad("digest-type-confused")
        if not isinstance(evidence.active_compose_digest, str):
            _bad("digest-type-confused")
        if _DIGEST_RE.fullmatch(evidence.active_compose_digest) is None:
            _bad("digest-format-invalid")

    # active_config_digest: must be None or valid digest
    if evidence.active_config_digest is not None:
        if isinstance(evidence.active_config_digest, bool):
            _bad("digest-type-confused")
        if not isinstance(evidence.active_config_digest, str):
            _bad("digest-type-confused")
        if _DIGEST_RE.fullmatch(evidence.active_config_digest) is None:
            _bad("digest-format-invalid")

    # container_observations validation
    if not isinstance(evidence.container_observations, tuple):
        _bad("containers-type-invalid")
    for obs in evidence.container_observations:
        if not isinstance(obs, ContainerObservation):
            _bad("container-observation-type-invalid")
        if not isinstance(obs.name, str) or isinstance(obs.name, bool):
            _bad("container-name-invalid")
        if not obs.name:
            _bad("container-name-invalid")
        if obs.state not in _VALID_STATES:
            _bad("container-state-invalid")
        if obs.health not in _VALID_HEALTH:
            _bad("container-health-invalid")
        if not isinstance(obs.labels, dict):
            _bad("container-labels-invalid")
        for k, v in obs.labels.items():
            if not isinstance(k, str) or not isinstance(v, str):
                _bad("container-labels-invalid")

    # receipt_snapshot validation
    if not isinstance(evidence.receipt_snapshot, LifecycleSnapshot):
        _bad("receipt-snapshot-type-invalid")

    # docker_available: must be bool (strict)
    if type(evidence.docker_available) is not bool:
        _bad("docker-available-type-invalid")

    return evidence


# ---------------------------------------------------------------------------
# 3. Classification logic
# ---------------------------------------------------------------------------


def _validate_command(command: Any) -> ApplicationIdentity:
    """Validate the command and return the expected ApplicationIdentity.

    Re-proves command, transaction, plan, request, service, action, version,
    definition digest, optional Compose digest, and deterministic identity.
    """
    if not isinstance(command, LifecycleWorkCommand):
        _bad("command-type-invalid")
    if command.plan_material is None:
        _bad("command-plan-not-bound")
    if not isinstance(command.plan_material, LifecyclePlanMaterial):
        _bad("command-plan-type-invalid")

    try:
        identity = produce_application_identity(command)
    except ApplicationIdentityError:
        _bad("command-identity-invalid")

    return identity


def _all_mutations_absent(evidence: CurrentEvidence) -> bool:
    """Check if every current mutation source is absent."""
    return (
        evidence.active_record is None
        and evidence.active_definition_digest is None
        and evidence.active_compose_digest is None
        and evidence.active_config_digest is None
        and len(evidence.container_observations) == 0
    )


def _any_mutation_present(evidence: CurrentEvidence) -> bool:
    """Check if any current mutation source is present."""
    return (
        evidence.active_record is not None
        or evidence.active_definition_digest is not None
        or evidence.active_compose_digest is not None
        or evidence.active_config_digest is not None
        or len(evidence.container_observations) > 0
    )


def _classify_absent(
    command: LifecycleWorkCommand,
    identity: ApplicationIdentity,
    evidence: CurrentEvidence,
) -> ObservationResult:
    """Classify as ABSENT.

    Valid when every current mutation source is absent.
    A started-only exact receipt may coexist (recovery retry path).
    A failed terminal receipt with no current mutation is also ABSENT.
    A completed terminal receipt with no current mutation is drift -> error.
    """
    snapshot = evidence.receipt_snapshot

    if snapshot.state == "absent":
        # No receipts, no mutations: clean ABSENT
        pass
    elif snapshot.state == "started":
        # Started-only receipt with no mutations: safe retry path
        pass
    elif snapshot.state == "failed":
        # Failed terminal with no mutations: ABSENT (effects cleaned)
        pass
    elif snapshot.state == "completed":
        # Completed terminal but no mutations: drift/ambiguous -> error
        _bad("completed-receipt-no-mutation-drift")
    else:
        _bad("receipt-state-invalid")

    return ObservationResult(
        service_id=identity.service_id,
        classification="ABSENT",
        identity_sha256=identity.identity_sha256,
        record_sha256=None,
        containers=(),
    )


def _classify_applied(
    command: LifecycleWorkCommand,
    identity: ApplicationIdentity,
    evidence: CurrentEvidence,
) -> ObservationResult:
    """Classify as APPLIED.

    Requires:
    - Exact active record exists with matching identity fields and digests.
    - Observed definition/Compose/config digests exactly match record and plan.
    - Observed containers exactly match the record's expected container-name set.
    - Every container has a complete parsed identity equal to plan-bound identity.
    - Receipt is exact started-only or exact completed.
    - If completed, terminal evidence_hash == application identity SHA-256.
    """
    snapshot = evidence.receipt_snapshot

    # Must have active record
    if evidence.active_record is None:
        _bad("applied-record-required")

    record = evidence.active_record

    # Record identity fields must match plan-bound identity
    if record["identity_sha256"] != identity.identity_sha256:
        _bad("record-identity-mismatch")
    if record["definition_sha256"] != identity.definition_sha256:
        _bad("record-definition-mismatch")
    if record["compose_sha256"] != identity.compose_sha256:
        _bad("record-compose-mismatch")

    # Active definition digest must match plan
    if evidence.active_definition_digest is None:
        _bad("applied-definition-required")
    if evidence.active_definition_digest != identity.definition_sha256:
        _bad("definition-drift")

    # Active Compose digest must match plan (if plan has compose)
    if identity.compose_sha256 != "absent":
        if evidence.active_compose_digest is None:
            _bad("applied-compose-required")
        if evidence.active_compose_digest != identity.compose_sha256:
            _bad("compose-drift")
    else:
        # Plan has no Compose; active compose must be absent
        if evidence.active_compose_digest is not None:
            _bad("compose-should-be-absent")

    # Active config digest must match record
    if evidence.active_config_digest is None:
        _bad("applied-config-required")
    if evidence.active_config_digest != record["config_sha256"]:
        _bad("config-drift")

    # Container observations must exactly match expected containers
    expected_names = set(record["expected_containers"])
    observed_names: set[str] = set()
    summaries: list[ContainerStateSummary] = []

    if len(evidence.container_observations) != len(expected_names):
        _bad("container-count-mismatch")

    for obs in evidence.container_observations:
        if obs.name in observed_names:
            _bad("container-duplicate-name")
        observed_names.add(obs.name)

        if obs.name not in expected_names:
            _bad("container-name-mismatch")

        # Parse container identity labels and verify they match plan identity
        try:
            container_identity = parse_observed_labels(obs.labels)
        except ApplicationIdentityError:
            _bad("container-label-identity-invalid")

        if container_identity != identity:
            _bad("container-identity-mismatch")

        summaries.append(
            ContainerStateSummary(
                name=obs.name,
                state=obs.state,
                health=obs.health,
                identity_match=True,
            )
        )

    if observed_names != expected_names:
        _bad("container-set-mismatch")

    # Sort summaries by name for deterministic output
    summaries.sort(key=lambda s: s.name)

    # Receipt validation
    if snapshot.state not in ("started", "completed"):
        _bad("applied-receipt-state-invalid")

    if snapshot.state == "completed":
        # No receipt is ambiguous
        if snapshot.terminal_receipt is None:
            _bad("completed-no-terminal-receipt")
        # Terminal evidence_hash must equal application identity SHA-256
        if snapshot.terminal_receipt.evidence_hash != identity.identity_sha256:
            _bad("completed-evidence-hash-mismatch")

    # No receipt plus effects is ambiguous
    if snapshot.started_receipt is None:
        _bad("applied-receipt-required")

    return ObservationResult(
        service_id=identity.service_id,
        classification="APPLIED",
        identity_sha256=identity.identity_sha256,
        record_sha256=record["record_sha256"],
        containers=tuple(summaries),
    )


# ---------------------------------------------------------------------------
# 4. Main public entry point
# ---------------------------------------------------------------------------


def observe_application(
    command: LifecycleWorkCommand,
    evidence: CurrentEvidence,
) -> ObservationResult:
    """Classify current application state as ABSENT or APPLIED.

    Accepts one plan-bound ``LifecycleWorkCommand`` for ``apply:<serviceId>``
    and current evidence.  Returns a frozen ``ObservationResult``.

    Raises ``ApplicationObservationError`` on any partial, contradictory,
    drifted, malformed, unavailable, or unsupported evidence.

    Container state/health is validated and surfaced but does NOT gate APPLIED:
    stopped, exited, restarting, unhealthy, or no-healthcheck containers are
    still mutations that recovery must compensate.  Actual health success
    belongs to verify_all, not applied-prefix discovery.
    """
    validated_evidence = _validate_current_evidence(evidence)
    identity = _validate_command(command)

    # Docker unavailable is always an error
    if not validated_evidence.docker_available:
        _bad("docker-unavailable")

    all_absent = _all_mutations_absent(validated_evidence)
    any_present = _any_mutation_present(validated_evidence)

    if all_absent:
        return _classify_absent(command, identity, validated_evidence)
    elif any_present:
        return _classify_applied(command, identity, validated_evidence)
    else:
        _bad("impossible-evidence-state")


__all__ = [
    "ApplicationObservationError",
    "ContainerObservation",
    "ContainerStateSummary",
    "CurrentEvidence",
    "MAX_CONTAINERS",
    "MAX_INPUT_BYTES",
    "ObservationResult",
    "RECORD_KEYS",
    "RECORD_SCHEMA",
    "observe_application",
    "_validate_canonical_record",
    "_validate_current_evidence",
]
