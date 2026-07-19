"""Narrow injectable boundaries and configured adapters for deployment."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dploydb.candidate import (
    CandidateStageObserver,
    CandidateValidationResult,
    HealthChecker,
    run_candidate_stage,
)
from dploydb.config import LoadedConfiguration, ProductionTopology
from dploydb.cutover import (
    create_final_backup,
    migrate_production_database,
    restore_final_backup,
)
from dploydb.health import ApplicationHealthChecker, CandidateHealthResult
from dploydb.locking import DeploymentLock
from dploydb.models import (
    BackupArtifact,
    MigrationCommandEvidence,
    ProductionMigrationResult,
    VerifiedDatabaseRestoreResult,
)
from dploydb.runners.base import (
    ApplicationRunner,
    ProductionApplicationRunner,
    ProductionStop,
)
from dploydb.runners.docker_compose_production import DockerComposeProductionRunner
from dploydb.state import StateStore
from dploydb.subprocesses import SubprocessRunner
from dploydb.traffic import CommandTrafficController, TrafficController


class PreCutoverStage(Protocol):
    """Caller-owned rehearsal and candidate boundary."""

    def run(
        self,
        loaded: LoadedConfiguration,
        *,
        version: str,
        config_path: Path,
        operation_id: str,
        store: StateStore,
        lock: DeploymentLock,
        cancellation_event: threading.Event | None,
        stage_observer: CandidateStageObserver,
    ) -> CandidateValidationResult: ...


class ProductionHealthBoundary(Protocol):
    """Final and rollback application health boundary."""

    def check_application(
        self,
        *,
        version: str,
        database_path: Path,
        cancellation_event: threading.Event | None = None,
    ) -> CandidateHealthResult: ...


class CutoverDatabase(Protocol):
    """Database transaction boundary owned by the deployment operation."""

    def create_final(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
    ) -> BackupArtifact: ...

    def migrate(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
        final_backup: BackupArtifact,
        traffic_activated: bool,
        evidence_sink: Callable[[MigrationCommandEvidence], None],
        cancellation_event: threading.Event | None,
        log_path: Path,
    ) -> ProductionMigrationResult: ...

    def restore(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
        final_backup: BackupArtifact,
        traffic_activated: bool,
    ) -> VerifiedDatabaseRestoreResult: ...


@dataclass(frozen=True, slots=True)
class DeploymentDependencies:
    """Injectable operational boundaries used by the coordinator."""

    pre_cutover: PreCutoverStage
    production: ProductionApplicationRunner
    traffic: TrafficController
    database: CutoverDatabase
    health: ProductionHealthBoundary


class ConfiguredPreCutover:
    def __init__(
        self,
        *,
        command_environment: Mapping[str, str],
        command_runner: SubprocessRunner | None,
        application_runner: ApplicationRunner | None,
        health_checker: HealthChecker | None,
    ) -> None:
        self.command_environment = command_environment
        self.command_runner = command_runner
        self.application_runner = application_runner
        self.health_checker = health_checker

    def run(
        self,
        loaded: LoadedConfiguration,
        *,
        version: str,
        config_path: Path,
        operation_id: str,
        store: StateStore,
        lock: DeploymentLock,
        cancellation_event: threading.Event | None,
        stage_observer: CandidateStageObserver,
    ) -> CandidateValidationResult:
        return run_candidate_stage(
            loaded,
            version=version,
            config_path=config_path,
            operation_id=operation_id,
            store=store,
            lock=lock,
            command_environment=self.command_environment,
            command_runner=self.command_runner,
            application_runner=self.application_runner,
            health_checker=self.health_checker,
            cancellation_event=cancellation_event,
            complete_operation=False,
            stage_observer=stage_observer,
        )


class ConfiguredCutoverDatabase:
    def __init__(
        self,
        loaded: LoadedConfiguration,
        *,
        config_path: Path,
        command_environment: Mapping[str, str],
        command_runner: SubprocessRunner | None,
    ) -> None:
        self.loaded = loaded
        self.config_path = config_path
        self.command_environment = command_environment
        self.command_runner = command_runner

    def create_final(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
    ) -> BackupArtifact:
        return create_final_backup(
            self.loaded,
            operation_id=operation_id,
            stopped=stopped,
        )

    def migrate(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
        final_backup: BackupArtifact,
        traffic_activated: bool,
        evidence_sink: Callable[[MigrationCommandEvidence], None],
        cancellation_event: threading.Event | None,
        log_path: Path,
    ) -> ProductionMigrationResult:
        return migrate_production_database(
            self.loaded,
            operation_id=operation_id,
            stopped=stopped,
            final_backup=final_backup,
            config_path=self.config_path,
            traffic_activated=traffic_activated,
            evidence_sink=evidence_sink,
            command_environment=self.command_environment,
            command_runner=self.command_runner,
            cancellation_event=cancellation_event,
            log_path=log_path,
        )

    def restore(
        self,
        *,
        operation_id: str,
        stopped: ProductionStop,
        final_backup: BackupArtifact,
        traffic_activated: bool,
    ) -> VerifiedDatabaseRestoreResult:
        return restore_final_backup(
            self.loaded,
            operation_id=operation_id,
            stopped=stopped,
            final_backup=final_backup,
            traffic_activated=traffic_activated,
        )


def default_dependencies(
    loaded: LoadedConfiguration,
    *,
    topology: ProductionTopology,
    config_path: Path,
    working_directory: Path,
    command_environment: Mapping[str, str],
    candidate_command_runner: SubprocessRunner | None,
    candidate_application_runner: ApplicationRunner | None,
    candidate_health_checker: HealthChecker | None,
) -> tuple[DeploymentDependencies, ApplicationHealthChecker]:
    production = DockerComposeProductionRunner(
        project=loaded.config.project,
        application=loaded.config.application,
        topology=topology,
        database_environment_name=loaded.config.database.path_env,
        production_database_path=loaded.config.database.path,
        secrets=loaded.secrets,
        working_directory=working_directory,
        command_environment=command_environment,
    )
    traffic = CommandTrafficController(
        traffic=loaded.config.traffic,
        secrets=loaded.secrets,
        working_directory=working_directory,
        command_environment=command_environment,
    )
    health = ApplicationHealthChecker(
        application=loaded.config.application,
        health_url=topology.health_url,
        database_environment_name=loaded.config.database.path_env,
        secrets=loaded.secrets,
        working_directory=working_directory,
        command_environment=command_environment,
    )
    return (
        DeploymentDependencies(
            pre_cutover=ConfiguredPreCutover(
                command_environment=command_environment,
                command_runner=candidate_command_runner,
                application_runner=candidate_application_runner,
                health_checker=candidate_health_checker,
            ),
            production=production,
            traffic=traffic,
            database=ConfiguredCutoverDatabase(
                loaded,
                config_path=config_path,
                command_environment=command_environment,
                command_runner=None,
            ),
            health=health,
        ),
        health,
    )
