"""Typed compact release summaries and full operation evidence helpers."""

from __future__ import annotations

from typing import Literal, cast

from dploydb.health import CandidateHealthResult
from dploydb.models import BackupArtifact, ReleaseHealthEvidence, ReleaseHookEvidence
from dploydb.redaction import JsonValue
from dploydb.runners.base import (
    ProductionCleanup,
    ProductionDiscovery,
    ProductionInspection,
    ProductionRestart,
    ProductionStart,
    ProductionStop,
)
from dploydb.traffic import TrafficHookResult


def hook_summary(result: TrafficHookResult) -> ReleaseHookEvidence:
    command = result.command
    return ReleaseHookEvidence(
        action=result.action.value,
        passed=result.passed,
        outcome=command.outcome.value,
        exit_code=command.exit_code,
        output_complete=not command.stdout.truncated and not command.stderr.truncated,
        duration_seconds=command.duration_seconds,
        termination_reason=(
            None if command.termination_reason is None else command.termination_reason.value
        ),
        forced_kill=command.forced_kill,
        cleanup_error=command.cleanup_error,
    )


def health_summary(
    result: CandidateHealthResult,
    *,
    role: Literal["new", "previous"],
    version: str,
) -> ReleaseHealthEvidence:
    return ReleaseHealthEvidence(
        role=role,
        version=version,
        url=result.readiness.url,
        readiness_attempts=result.readiness.attempt_count,
        smoke_outcome=None if result.smoke is None else "succeeded",
    )


def backup_evidence(artifact: BackupArtifact) -> dict[str, JsonValue]:
    return {
        "backup_id": artifact.metadata.backup_id,
        "sha256": artifact.metadata.sha256,
        "size_bytes": artifact.metadata.size_bytes,
        "database_path": str(artifact.database_path),
        "metadata_path": str(artifact.metadata_path),
        "sqlite": cast(JsonValue, artifact.metadata.sqlite.model_dump(mode="json")),
    }


def inspection_evidence(inspection: ProductionInspection) -> dict[str, JsonValue]:
    return {
        "handle": cast(JsonValue, inspection.handle.model_dump(mode="json")),
        "running": inspection.running,
        "mounts": [
            {
                "mount_type": mount.mount_type,
                "source": mount.source,
                "destination": mount.destination,
                "read_write": mount.read_write,
            }
            for mount in inspection.mounts
        ],
        "command": inspection.command.as_evidence(),
    }


def discovery_evidence(discovery: ProductionDiscovery) -> dict[str, JsonValue]:
    return {
        "query": discovery.query.as_evidence(),
        "inspection": inspection_evidence(discovery.inspection),
    }


def stop_evidence(stopped: ProductionStop) -> dict[str, JsonValue]:
    return {
        "handle": cast(JsonValue, stopped.handle.model_dump(mode="json")),
        "command": stopped.command.as_evidence(),
        "inspection": inspection_evidence(stopped.inspection),
    }


def start_evidence(started: ProductionStart) -> dict[str, JsonValue]:
    return {
        "handle": cast(JsonValue, started.handle.model_dump(mode="json")),
        "container_reference": started.container_reference,
        "command": started.command.as_evidence(),
        "inspection": inspection_evidence(started.inspection),
    }


def cleanup_evidence(cleanup: ProductionCleanup) -> dict[str, JsonValue]:
    return {
        "presence_query": cleanup.presence_query.as_evidence(),
        "remove_command": (
            None if cleanup.remove_command is None else cleanup.remove_command.as_evidence()
        ),
        "compose_down": cleanup.compose_down.as_evidence(),
        "proof": {
            "container_absent": cleanup.proof.container_absent,
            "networks_absent": cleanup.proof.networks_absent,
            "proven": cleanup.proof.proven,
            "container_query": cleanup.proof.container_query.as_evidence(),
            "network_query": cleanup.proof.network_query.as_evidence(),
        },
    }


def restart_evidence(restarted: ProductionRestart) -> dict[str, JsonValue]:
    return {
        "handle": cast(JsonValue, restarted.handle.model_dump(mode="json")),
        "command": restarted.command.as_evidence(),
        "inspection": inspection_evidence(restarted.inspection),
    }
