import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cpb.database import Base


class StrEnum(enum.StrEnum):
    pass


class UserStatus(StrEnum):
    active = "active"
    disabled = "disabled"
    platform = "platform"


class NodeStatus(StrEnum):
    offline = "offline"
    online = "online"
    busy = "busy"
    disabled = "disabled"


class TaskStatus(StrEnum):
    draft = "draft"
    queued = "queued"
    leased = "leased"
    running = "running"
    uploading = "uploading"
    verifying = "verifying"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class RunStatus(StrEnum):
    leased = "leased"
    running = "running"
    uploading = "uploading"
    verifying = "verifying"
    succeeded = "succeeded"
    failed = "failed"
    lost = "lost"


class TransactionType(StrEnum):
    signup_bonus = "signup_bonus"
    task_reservation = "task_reservation"
    task_refund = "task_refund"
    worker_reward = "worker_reward"
    platform_fee = "platform_fee"
    manual_adjustment = "manual_adjustment"


def utcnow() -> datetime:
    return datetime.now().astimezone()


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    status: Mapped[UserStatus] = mapped_column(Enum(UserStatus), default=UserStatus.active)

    account: Mapped["CreditAccount"] = relationship(back_populates="user", uselist=False)


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Node(TimestampMixin, Base):
    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[NodeStatus] = mapped_column(Enum(NodeStatus), default=NodeStatus.offline)
    platform: Mapped[str] = mapped_column(String(32))
    architecture: Mapped[str] = mapped_column(String(32))
    cpu_total: Mapped[int] = mapped_column(Integer)
    cpu_available: Mapped[int] = mapped_column(Integer)
    memory_total_mb: Mapped[int] = mapped_column(Integer)
    memory_available_mb: Mapped[int] = mapped_column(Integer)
    supported_images: Mapped[list[str]] = mapped_column(JSON)
    reputation: Mapped[float] = mapped_column(Float, default=1.0)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    __table_args__ = (UniqueConstraint("owner_user_id", "name", name="uq_node_owner_name"),)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.draft)
    bundle_object_key: Mapped[str] = mapped_column(String(1024))
    bundle_sha256: Mapped[str] = mapped_column(String(64))
    runtime_image: Mapped[str] = mapped_column(String(255))
    command_spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    cpu_required: Mapped[int] = mapped_column(Integer)
    memory_required_mb: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    estimated_credits: Mapped[int] = mapped_column(Integer)
    reserved_credits: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    runs: Mapped[list["TaskRun"]] = relationship(back_populates="task")

    __table_args__ = (Index("ix_tasks_queue", "status", "created_at"),)


class TaskRun(TimestampMixin, Base):
    __tablename__ = "task_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"))
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="RESTRICT"))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.leased)
    lease_token_hash: Mapped[str] = mapped_column(String(64), index=True)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    logs_object_key: Mapped[str | None] = mapped_column(String(1024))
    result_object_key: Mapped[str | None] = mapped_column(String(1024))
    failure_reason: Mapped[str | None] = mapped_column(Text)

    task: Mapped[Task] = relationship(back_populates="runs")
    artifacts: Mapped[list["Artifact"]] = relationship(back_populates="task_run")

    __table_args__ = (Index("ix_task_runs_active_lease", "status", "lease_expires_at"),)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(512))
    object_key: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task_run: Mapped[TaskRun] = relationship(back_populates="artifacts")

    __table_args__ = (UniqueConstraint("task_run_id", "name", name="uq_artifact_run_name"),)


class CreditAccount(Base):
    __tablename__ = "credit_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="account")
    transactions: Mapped[list["CreditTransaction"]] = relationship(back_populates="account")


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("credit_accounts.id", ondelete="RESTRICT"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    transaction_type: Mapped[TransactionType] = mapped_column(Enum(TransactionType))
    reference_type: Mapped[str] = mapped_column(String(64))
    reference_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped[CreditAccount] = relationship(back_populates="transactions")
