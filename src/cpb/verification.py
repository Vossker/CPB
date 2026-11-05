from dataclasses import dataclass
from typing import Protocol

from cpb.config import get_settings
from cpb.errors import VerificationError
from cpb.models import Task, TaskRun
from cpb.schemas import ArtifactMetadata, ContainerJobSpec
from cpb.storage import ObjectStorage


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    reason: str | None = None


class TaskVerifier(Protocol):
    def verify(
        self, task: Task, run: TaskRun, artifacts: list[ArtifactMetadata]
    ) -> VerificationResult: ...


class ContainerJobVerifier:
    def __init__(self, storage: ObjectStorage) -> None:
        self.storage = storage

    def verify(
        self, task: Task, run: TaskRun, artifacts: list[ArtifactMetadata]
    ) -> VerificationResult:
        specification = ContainerJobSpec.model_validate(task.command_spec)
        if run.logs_object_key:
            if not run.logs_object_key.startswith(f"runs/{run.id}/logs/"):
                raise VerificationError("log object does not belong to this run")
            self.storage.stat(run.logs_object_key)
        if run.result_object_key:
            if not run.result_object_key.startswith(f"runs/{run.id}/result/"):
                raise VerificationError("result object does not belong to this run")
            self.storage.stat(run.result_object_key)
        if run.exit_code != specification.verification.required_exit_code:
            return VerificationResult(
                False,
                "expected exit code "
                f"{specification.verification.required_exit_code}, got {run.exit_code}",
            )

        by_name: dict[str, ArtifactMetadata] = {}
        declared = set(specification.artifacts)
        for artifact in artifacts:
            if artifact.name in by_name:
                raise VerificationError(f"duplicate artifact metadata for {artifact.name}")
            if artifact.name not in declared:
                raise VerificationError(f"undeclared artifact: {artifact.name}")
            expected_prefix = f"runs/{run.id}/artifact/"
            if not artifact.object_key.startswith(expected_prefix):
                raise VerificationError("artifact object does not belong to this run")
            if artifact.size_bytes > get_settings().max_artifact_bytes:
                return VerificationResult(False, f"artifact exceeds size limit: {artifact.name}")
            stored = self.storage.stat(artifact.object_key)
            if stored.size_bytes != artifact.size_bytes:
                return VerificationResult(False, f"artifact size mismatch: {artifact.name}")
            if not stored.sha256 or stored.sha256 != artifact.sha256:
                return VerificationResult(False, f"artifact hash mismatch: {artifact.name}")
            by_name[artifact.name] = artifact

        missing = set(specification.verification.required_files) - set(by_name)
        if missing:
            return VerificationResult(False, f"missing required artifacts: {sorted(missing)}")
        return VerificationResult(True)
