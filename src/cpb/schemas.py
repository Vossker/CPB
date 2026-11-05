import re
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cpb.config import get_settings
from cpb.models import NodeStatus, RunStatus, TaskStatus, TransactionType

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_relative_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("paths must use forward slashes")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("path must be a normalized relative workspace path")
    return value


class RuntimeSpec(BaseModel):
    image: str

    @field_validator("image")
    @classmethod
    def image_is_allowed(cls, value: str) -> str:
        if value not in get_settings().runtime_images:
            raise ValueError("runtime image is not allow-listed")
        return value


class ResourceSpec(BaseModel):
    cpu: int = Field(ge=1)
    memory_mb: int = Field(ge=128)
    disk_mb: int = Field(default=4096, ge=128)
    timeout_seconds: int = Field(ge=1)

    @model_validator(mode="after")
    def within_platform_limits(self) -> "ResourceSpec":
        settings = get_settings()
        if self.cpu > settings.max_cpu:
            raise ValueError(f"cpu exceeds platform maximum of {settings.max_cpu}")
        if self.memory_mb > settings.max_memory_mb:
            raise ValueError(f"memory_mb exceeds platform maximum of {settings.max_memory_mb}")
        if self.timeout_seconds > settings.max_timeout_seconds:
            raise ValueError(
                f"timeout_seconds exceeds platform maximum of {settings.max_timeout_seconds}"
            )
        return self


class NetworkSpec(BaseModel):
    enabled: Literal[False] = False


class StepSpec(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1, max_length=4096)

    @field_validator("command")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("command contains a NUL byte")
        return value


class VerificationSpec(BaseModel):
    required_exit_code: int = 0
    required_files: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("required_files")
    @classmethod
    def required_paths_are_safe(cls, value: list[str]) -> list[str]:
        paths = [validate_relative_path(item) for item in value]
        if len(set(paths)) != len(paths):
            raise ValueError("required_files contains duplicates")
        return paths


class ContainerJobSpec(BaseModel):
    version: Literal[1] = 1
    runtime: RuntimeSpec
    resources: ResourceSpec
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    steps: list[StepSpec] = Field(min_length=1, max_length=20)
    artifacts: list[str] = Field(default_factory=list, max_length=100)
    verification: VerificationSpec = Field(default_factory=VerificationSpec)

    @field_validator("artifacts")
    @classmethod
    def artifact_paths_are_safe(cls, value: list[str]) -> list[str]:
        paths = [validate_relative_path(item) for item in value]
        if len(set(paths)) != len(paths):
            raise ValueError("artifacts contains duplicates")
        return paths

    @model_validator(mode="after")
    def required_files_are_declared(self) -> "ContainerJobSpec":
        missing = set(self.verification.required_files) - set(self.artifacts)
        if missing:
            raise ValueError(f"required files are not declared artifacts: {sorted(missing)}")
        return self


class TaskCreate(BaseModel):
    bundle_object_key: str = Field(min_length=1, max_length=1024)
    bundle_sha256: str
    specification: ContainerJobSpec
    max_retries: int | None = Field(default=None, ge=0)

    @field_validator("bundle_sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        value = value.lower()
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("bundle_sha256 must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("max_retries")
    @classmethod
    def retry_limit(cls, value: int | None) -> int | None:
        if value is not None and value > get_settings().max_retries:
            raise ValueError(
                f"max_retries exceeds platform maximum of {get_settings().max_retries}"
            )
        return value


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_user_id: uuid.UUID
    status: TaskStatus
    bundle_object_key: str
    bundle_sha256: str
    runtime_image: str
    command_spec: dict
    cpu_required: int
    memory_required_mb: int
    timeout_seconds: int
    estimated_credits: int
    reserved_credits: int
    max_retries: int
    retry_count: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class TaskRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    node_id: uuid.UUID
    status: RunStatus
    lease_expires_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class NodeRegister(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    platform: Literal["linux"]
    architecture: Literal["x86_64", "amd64", "aarch64", "arm64"]
    cpu_total: int = Field(ge=1, le=1024)
    memory_total_mb: int = Field(ge=128, le=4_194_304)
    supported_images: list[str] = Field(min_length=1)

    @field_validator("supported_images")
    @classmethod
    def supported_images_are_allowed(cls, values: list[str]) -> list[str]:
        allowed = set(get_settings().runtime_images)
        if not set(values) <= allowed:
            raise ValueError("supported_images contains a non-allow-listed image")
        return sorted(set(values))


class NodeRegisterResponse(BaseModel):
    node_id: uuid.UUID
    worker_token: str


class NodeHeartbeat(BaseModel):
    cpu_available: int = Field(ge=0)
    memory_available_mb: int = Field(ge=0)
    supported_images: list[str] | None = None


class NodeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_user_id: uuid.UUID
    name: str
    status: NodeStatus
    platform: str
    architecture: str
    cpu_total: int
    cpu_available: int
    memory_total_mb: int
    memory_available_mb: int
    supported_images: list[str]
    reputation: float
    last_heartbeat_at: datetime | None


class LeaseRequest(BaseModel):
    pass


class LeaseResponse(BaseModel):
    run_id: uuid.UUID
    task_id: uuid.UUID
    lease_token: str
    lease_expires_at: datetime
    bundle_url: str
    bundle_sha256: str
    specification: ContainerJobSpec


class RunHeartbeatResponse(BaseModel):
    lease_expires_at: datetime


class ArtifactMetadata(BaseModel):
    name: str
    object_key: str = Field(min_length=1, max_length=1024)
    sha256: str
    size_bytes: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def name_is_safe(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("sha256")
    @classmethod
    def sha_is_valid(cls, value: str) -> str:
        value = value.lower()
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class RunComplete(BaseModel):
    exit_code: int
    logs_object_key: str | None = Field(default=None, max_length=1024)
    result_object_key: str | None = Field(default=None, max_length=1024)
    artifacts: list[ArtifactMetadata] = Field(default_factory=list, max_length=100)


class RunFail(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    failure_type: Literal["user_code", "infrastructure"]
    exit_code: int | None = None
    logs_object_key: str | None = Field(default=None, max_length=1024)


class UploadResponse(BaseModel):
    object_key: str
    sha256: str
    size_bytes: int


class DownloadItem(BaseModel):
    name: str
    sha256: str
    size_bytes: int
    download_url: str


class WalletRead(BaseModel):
    user_id: uuid.UUID
    balance: Decimal


class TransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    amount: Decimal
    transaction_type: TransactionType
    reference_type: str
    reference_id: str
    idempotency_key: str
    created_at: datetime


class Message(BaseModel):
    detail: str
