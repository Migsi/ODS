"""Resume one receipted library compensation from exact current state.

The driver has no Docker or filesystem authority of its own.  Its effect
consumer must remove one named, owner-verified resource at a time; every
transition is re-observed under the admitted transaction lease.  A partial
state is never reported as ABSENT or as a completed compensation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from extension_application_identity import produce_application_observation_identity
from extension_application_observation import (
    CompensationProgress,
    CurrentEvidence,
    MAX_CONTAINERS,
    observe_compensation_progress,
)
from extension_lifecycle_work import LifecycleWorkCommand, LifecycleWorkUncertainEffect


_PILOT_SERVICES = frozenset({"gitea", "miniflux", "ntfy", "ollama"})
_ACTIVE_FILES = frozenset(
    {"manifest.yaml", "compose.yaml", "configuration.json", "compose.override.yaml"}
)


class LibraryCompensationUncertain(LifecycleWorkUncertainEffect):
    """Current state or a possible mutation needs further reconciliation."""


class CompensationEffects(Protocol):
    def remove_container(self, command: LifecycleWorkCommand, name: str) -> None: ...

    def remove_file(self, command: LifecycleWorkCommand, name: str) -> None: ...

    def remove_record(
        self, command: LifecycleWorkCommand, record_sha256: str
    ) -> None: ...


class LibraryCompensationDriver:
    """Run strictly decreasing container, file, and record transitions."""

    def __init__(
        self,
        collect: Callable[[LifecycleWorkCommand], tuple[LifecycleWorkCommand, CurrentEvidence]],
        effects: CompensationEffects,
    ) -> None:
        if not callable(collect) or not all(
            callable(getattr(effects, name, None))
            for name in ("remove_container", "remove_file", "remove_record")
        ):
            raise LibraryCompensationUncertain("library-compensation-consumer-invalid")
        self._collect = collect
        self._effects = effects

    def _progress(
        self, command: LifecycleWorkCommand, expected_config_sha256: str
    ) -> tuple[CompensationProgress, str]:
        try:
            bound, evidence = self._collect(command)
            if type(bound) is not LifecycleWorkCommand or type(evidence) is not CurrentEvidence:
                raise ValueError("invalid collected evidence")
            identity = produce_application_observation_identity(bound)
            progress = observe_compensation_progress(
                bound, evidence, expected_config_sha256=expected_config_sha256
            )
            return progress, identity.identity_sha256
        except Exception as exc:
            raise LibraryCompensationUncertain(
                "library-compensation-observation-unavailable"
            ) from exc

    def observe_started(
        self, command: LifecycleWorkCommand, expected_config_sha256: str
    ) -> str | None:
        """Recover only a fully removed app; partial states require the driver."""
        self._validate_command(command)
        progress, identity_sha256 = self._progress(command, expected_config_sha256)
        return identity_sha256 if progress.state == "READY_TO_COMPLETE" else None

    @staticmethod
    def _validate_command(command: LifecycleWorkCommand) -> None:
        if (
            type(command) is not LifecycleWorkCommand
            or len(command.service_ids) != 1
            or command.service_ids[0] not in _PILOT_SERVICES
            or command.operation_key != f"apply:{command.service_ids[0]}"
            or command.payload
            != {"operation": {"serviceId": command.service_ids[0], "action": "install"}}
            or command.plan_material is None
            or command.plan_material.state != "reconciling"
        ):
            raise LibraryCompensationUncertain("library-compensation-command-invalid")

    @staticmethod
    def _require_step(
        before: CompensationProgress,
        after: CompensationProgress,
        kind: str,
        name: str | None,
    ) -> None:
        if kind == "container":
            expected_containers = tuple(
                item for item in before.remaining_containers if item != name
            )
            valid = (
                after.remaining_containers == expected_containers
                and after.remaining_files == before.remaining_files
                and after.record_sha256 == before.record_sha256
                and after.state
                == ("CONTAINERS_PRESENT" if expected_containers else "FILES_PRESENT")
            )
        elif kind == "file":
            expected_files = tuple(item for item in before.remaining_files if item != name)
            valid = (
                after.remaining_containers == ()
                and after.remaining_files == expected_files
                and after.record_sha256 == before.record_sha256
                and after.state == ("FILES_PRESENT" if expected_files else "RECORD_ONLY")
            )
        else:
            valid = (
                kind == "record"
                and after.state == "READY_TO_COMPLETE"
                and after.record_sha256 is None
                and after.remaining_containers == ()
                and after.remaining_files == ()
            )
        if not valid:
            raise LibraryCompensationUncertain("library-compensation-step-drift")

    def run(self, command: LifecycleWorkCommand, expected_config_sha256: str) -> str:
        """Resume from the exact started receipt; return only after total absence."""
        self._validate_command(command)
        for _ in range(MAX_CONTAINERS + len(_ACTIVE_FILES) + 1):
            before, identity_sha256 = self._progress(command, expected_config_sha256)
            if before.state == "READY_TO_COMPLETE":
                return identity_sha256
            if before.state == "CONTAINERS_PRESENT":
                kind = "container"
                name = before.remaining_containers[0]
                effect = lambda: self._effects.remove_container(command, name)
            elif before.state == "FILES_PRESENT":
                kind = "file"
                name = before.remaining_files[0]
                if name not in _ACTIVE_FILES:
                    raise LibraryCompensationUncertain("library-compensation-file-invalid")
                effect = lambda: self._effects.remove_file(command, name)
            elif before.state == "RECORD_ONLY":
                kind = "record"
                name = None
                if before.record_sha256 is None:
                    raise LibraryCompensationUncertain("library-compensation-record-invalid")
                effect = lambda: self._effects.remove_record(
                    command, before.record_sha256
                )
            else:
                raise LibraryCompensationUncertain("library-compensation-state-invalid")
            try:
                effect()
            except Exception as exc:
                raise LibraryCompensationUncertain(
                    "library-compensation-effect-uncertain"
                ) from exc
            after, after_identity = self._progress(command, expected_config_sha256)
            if after_identity != identity_sha256:
                raise LibraryCompensationUncertain("library-compensation-identity-drift")
            self._require_step(before, after, kind, name)
        raise LibraryCompensationUncertain("library-compensation-step-budget")


__all__ = ["LibraryCompensationDriver", "LibraryCompensationUncertain"]
