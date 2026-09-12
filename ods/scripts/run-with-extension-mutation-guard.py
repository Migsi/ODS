#!/usr/bin/env python3
"""Exec one host mutation while holding ODS's canonical extension guard."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_API_DIR = ROOT / "extensions" / "services" / "dashboard-api"
if str(DASHBOARD_API_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_API_DIR))

from extension_operation_locks import (  # noqa: E402
    ServiceLockError,
    ServiceLockTimeout,
    exclusive_file_lock,
    mutation_guard_path,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hold the canonical ODS mutation guard and exec a command."
    )
    parser.add_argument("--lock-parent", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    args = _parser().parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        print("ERROR: mutation guard command is required", file=sys.stderr)
        return 64
    if os.name == "nt":
        print(
            "ERROR: Assistant First mutation coordination is not yet qualified "
            "for native Windows source updates.",
            file=sys.stderr,
        )
        return 69

    try:
        lock_parent = Path(args.lock_parent).resolve(strict=True)
        if not lock_parent.is_dir():
            raise ServiceLockError("mutation-guard-parent-not-directory")
        guard_path = mutation_guard_path(lock_parent, repair_mode=True)
        with exclusive_file_lock(guard_path, timeout=args.timeout) as lockfile:
            descriptor = lockfile.fileno()
            os.set_inheritable(descriptor, True)
            environment = dict(os.environ)
            environment["ODS_MUTATION_GUARD_FD"] = str(descriptor)
            os.execvpe(command[0], command, environment)
    except ServiceLockTimeout:
        print(
            "ERROR: Another ODS update or extension mutation is in progress. "
            "Wait for it to finish, then retry.",
            file=sys.stderr,
        )
        return 75
    except (OSError, ServiceLockError) as exc:
        code = str(exc) or type(exc).__name__
        print(f"ERROR: Could not acquire the safe ODS mutation guard: {code}", file=sys.stderr)
        return 74

    return 70


if __name__ == "__main__":
    raise SystemExit(main())
