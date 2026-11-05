import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from cpb.config import get_settings
from cpb.credits import account_for_user, append_transaction, ensure_platform_user
from cpb.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    VerificationError,
)
from cpb.models import (
    Artifact,
    CreditAccount,
    Node,
    NodeStatus,
    RunStatus,
    Task,
    TaskRun,
    TaskStatus,
    TransactionType,
    User,
)
from cpb.schemas import (
    ContainerJobSpec,
    LeaseResponse,
    NodeHeartbeat,
    NodeRegister,
    RunComplete,
    RunFail,
    TaskCreate,
)
from cpb.security import generate_token, hash_token, token_matches
from cpb.storage import ObjectStorage
from cpb.transitions import transition_run, transition_task
from cpb.verification import ContainerJobVerifier


def now_utc() -> datetime:
    return datetime.now(UTC)


def is_expired(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= now_utc()


def estimated_credits(specification: ContainerJobSpec) -> int:
    return specification.resources.cpu * math.ceil(specification.resources.timeout_seconds / 60)


def owned_task(
    session: Session, task_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False
) -> Task:
    statement = select(Task).where(Task.id == task_id, Task.owner_user_id == user_id)
    if lock:
        statement = statement.with_for_update()
    task = session.scalar(statement)
    if not task:
        raise NotFoundError("task not found")
    return task


def create_task(session: Session, user: User, payload: TaskCreate, storage: ObjectStorage) -> Task:
    if not payload.bundle_object_key.startswith(f"users/{user.id}/bundles/"):
        raise AuthorizationError("bundle object does not belong to the current user")
    stored = storage.stat(payload.bundle_object_key)
    if stored.size_bytes > get_settings().max_bundle_bytes:
        raise VerificationError("source bundle exceeds the platform size limit")
    if not stored.sha256 or stored.sha256 != payload.bundle_sha256:
        raise VerificationError("source bundle hash does not match the uploaded object")
    spec = payload.specification
    task = Task(
        owner_user_id=user.id,
        status=TaskStatus.draft,
        bundle_object_key=payload.bundle_object_key,
        bundle_sha256=payload.bundle_sha256,
        runtime_image=spec.runtime.image,
        command_spec=spec.model_dump(mode="json"),
        cpu_required=spec.resources.cpu,
        memory_required_mb=spec.resources.memory_mb,
        timeout_seconds=spec.resources.timeout_seconds,
        estimated_credits=estimated_credits(spec),
        reserved_credits=0,
        max_retries=(
            get_settings().max_retries if payload.max_retries is None else payload.max_retries
        ),
        retry_count=0,
    )
    session.add(task)
    session.flush()
    return task


def submit_task(session: Session, task_id: uuid.UUID, user: User, storage: ObjectStorage) -> Task:
    task = owned_task(session, task_id, user.id, lock=True)
    if task.status != TaskStatus.draft:
        raise ConflictError("only a draft task can be submitted")
    ContainerJobSpec.model_validate(task.command_spec)
    stored = storage.stat(task.bundle_object_key)
    if stored.sha256 != task.bundle_sha256:
        raise VerificationError("source bundle is missing or its hash changed")
    account = account_for_user(session, user.id, lock=True)
    append_transaction(
        session,
        account,
        -task.estimated_credits,
        TransactionType.task_reservation,
        "task",
        str(task.id),
        f"task:{task.id}:reservation",
    )
    task.reserved_credits = task.estimated_credits
    transition_task(task, TaskStatus.queued)
    session.flush()
    return task


def refund_reservation(session: Session, task: Task, suffix: str = "refund") -> None:
    if task.reserved_credits <= 0:
        return
    refund = task.reserved_credits
    account = account_for_user(session, task.owner_user_id, lock=True)
    append_transaction(
        session,
        account,
        refund,
        TransactionType.task_refund,
        "task",
        str(task.id),
        f"task:{task.id}:{suffix}",
    )
    task.reserved_credits = 0


def cancel_task(session: Session, task_id: uuid.UUID, user: User) -> Task:
    task = owned_task(session, task_id, user.id, lock=True)
    if task.status == TaskStatus.queued:
        refund_reservation(session, task, "cancel-refund")
    transition_task(task, TaskStatus.cancelled)
    session.flush()
    return task


def register_node(session: Session, user: User, payload: NodeRegister) -> tuple[Node, str]:
    existing = session.scalar(
        select(Node).where(Node.owner_user_id == user.id, Node.name == payload.name)
    )
    if existing:
        raise ConflictError("a node with this name is already registered")
    token = generate_token("cpbw")
    node = Node(
        owner_user_id=user.id,
        name=payload.name,
        status=NodeStatus.online,
        platform=payload.platform,
        architecture=payload.architecture,
        cpu_total=payload.cpu_total,
        cpu_available=payload.cpu_total,
        memory_total_mb=payload.memory_total_mb,
        memory_available_mb=payload.memory_total_mb,
        supported_images=payload.supported_images,
        reputation=1.0,
        last_heartbeat_at=now_utc(),
        worker_token_hash=hash_token(token),
    )
    session.add(node)
    session.flush()
    return node, token


def heartbeat_node(session: Session, node: Node, payload: NodeHeartbeat) -> Node:
    node = session.scalar(select(Node).where(Node.id == node.id).with_for_update())
    if not node:
        raise NotFoundError("node not found")
    if payload.cpu_available > node.cpu_total:
        raise VerificationError("cpu_available exceeds cpu_total")
    if payload.memory_available_mb > node.memory_total_mb:
        raise VerificationError("memory_available_mb exceeds memory_total_mb")
    if payload.supported_images is not None:
        allowed = set(get_settings().runtime_images)
        if not set(payload.supported_images) <= allowed:
            raise VerificationError("supported_images contains a non-allow-listed image")
        node.supported_images = sorted(set(payload.supported_images))
    node.cpu_available = payload.cpu_available
    node.memory_available_mb = payload.memory_available_mb
    node.last_heartbeat_at = now_utc()
    if node.status != NodeStatus.busy:
        node.status = NodeStatus.online
    session.flush()
    return node


def lease_task(session: Session, node: Node, storage: ObjectStorage) -> LeaseResponse | None:
    node = session.scalar(select(Node).where(Node.id == node.id).with_for_update())
    if not node or node.status != NodeStatus.online:
        return None
    offline_cutoff = now_utc() - timedelta(seconds=get_settings().node_offline_seconds)
    if not node.last_heartbeat_at or (
        node.last_heartbeat_at.replace(tzinfo=node.last_heartbeat_at.tzinfo or UTC) < offline_cutoff
    ):
        node.status = NodeStatus.offline
        return None
    statement = (
        select(Task)
        .where(
            Task.status == TaskStatus.queued,
            Task.cpu_required <= node.cpu_available,
            Task.memory_required_mb <= node.memory_available_mb,
            Task.runtime_image.in_(node.supported_images),
            Task.retry_count <= Task.max_retries,
        )
        .order_by(Task.created_at, Task.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    task = session.scalar(statement)
    if not task:
        return None
    token = generate_token("lease")
    expires_at = now_utc() + timedelta(seconds=get_settings().lease_seconds)
    run = TaskRun(
        task_id=task.id,
        node_id=node.id,
        status=RunStatus.leased,
        lease_token_hash=hash_token(token),
        lease_expires_at=expires_at,
    )
    session.add(run)
    transition_task(task, TaskStatus.leased)
    node.status = NodeStatus.busy
    node.cpu_available -= task.cpu_required
    node.memory_available_mb -= task.memory_required_mb
    session.flush()
    return LeaseResponse(
        run_id=run.id,
        task_id=task.id,
        lease_token=token,
        lease_expires_at=expires_at,
        bundle_url=storage.download_url(task.bundle_object_key),
        bundle_sha256=task.bundle_sha256,
        specification=ContainerJobSpec.model_validate(task.command_spec),
    )


def locked_assigned_run(
    session: Session,
    run_id: uuid.UUID,
    node: Node,
    lease_token: str,
    *,
    allow_terminal: bool = False,
) -> TaskRun:
    run = session.scalar(select(TaskRun).where(TaskRun.id == run_id).with_for_update())
    if not run:
        raise NotFoundError("task run not found")
    if run.node_id != node.id:
        raise AuthorizationError("task run is assigned to another node")
    if not token_matches(lease_token, run.lease_token_hash):
        raise AuthorizationError("invalid lease token")
    if not allow_terminal and run.status in {RunStatus.succeeded, RunStatus.failed, RunStatus.lost}:
        raise ConflictError("task run is already terminal")
    return run


def start_run(session: Session, run_id: uuid.UUID, node: Node, lease_token: str) -> TaskRun:
    run = locked_assigned_run(session, run_id, node, lease_token)
    if is_expired(run.lease_expires_at):
        raise ConflictError("lease has expired")
    task = session.scalar(select(Task).where(Task.id == run.task_id).with_for_update())
    if not task:
        raise NotFoundError("task not found")
    transition_run(run, RunStatus.running)
    transition_task(task, TaskStatus.running)
    run.started_at = now_utc()
    run.lease_expires_at = now_utc() + timedelta(seconds=get_settings().lease_seconds)
    session.flush()
    return run


def heartbeat_run(session: Session, run_id: uuid.UUID, node: Node, lease_token: str) -> TaskRun:
    run = locked_assigned_run(session, run_id, node, lease_token)
    if run.status not in {RunStatus.leased, RunStatus.running}:
        raise ConflictError("heartbeats are accepted only for active execution leases")
    if is_expired(run.lease_expires_at):
        raise ConflictError("lease has expired")
    run.lease_expires_at = now_utc() + timedelta(seconds=get_settings().lease_seconds)
    session.flush()
    return run


def authorize_run_upload(
    session: Session, run_id: uuid.UUID, node: Node, lease_token: str
) -> TaskRun:
    run = locked_assigned_run(session, run_id, node, lease_token)
    if run.status not in {RunStatus.leased, RunStatus.running, RunStatus.uploading}:
        raise ConflictError("run is not accepting uploads")
    if is_expired(run.lease_expires_at):
        raise ConflictError("lease has expired")
    return run


def release_node_capacity(node: Node, task: Task) -> None:
    node.cpu_available = min(node.cpu_total, node.cpu_available + task.cpu_required)
    node.memory_available_mb = min(
        node.memory_total_mb, node.memory_available_mb + task.memory_required_mb
    )
    if node.status != NodeStatus.disabled:
        node.status = NodeStatus.online


def _lock_settlement_accounts(
    session: Session, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, CreditAccount]:
    accounts = session.scalars(
        select(CreditAccount)
        .where(CreditAccount.user_id.in_(user_ids))
        .order_by(CreditAccount.id)
        .with_for_update()
    ).all()
    result = {account.user_id: account for account in accounts}
    if set(result) != user_ids:
        raise NotFoundError("one or more settlement accounts are missing")
    return result


def settle_success(session: Session, task: Task, node: Node) -> None:
    platform = ensure_platform_user(session)
    accounts = _lock_settlement_accounts(
        session, {task.owner_user_id, node.owner_user_id, platform.id}
    )
    reward = Decimal(task.reserved_credits) * Decimal("0.80")
    fee = Decimal(task.reserved_credits) - reward
    append_transaction(
        session,
        accounts[node.owner_user_id],
        reward,
        TransactionType.worker_reward,
        "task",
        str(task.id),
        f"task:{task.id}:worker-reward",
    )
    append_transaction(
        session,
        accounts[platform.id],
        fee,
        TransactionType.platform_fee,
        "task",
        str(task.id),
        f"task:{task.id}:platform-fee",
    )
    task.reserved_credits = 0


def settle_user_failure(session: Session, task: Task, run: TaskRun, node: Node) -> None:
    platform = ensure_platform_user(session)
    accounts = _lock_settlement_accounts(
        session, {task.owner_user_id, node.owner_user_id, platform.id}
    )
    elapsed = 0.0
    if run.started_at and run.finished_at:
        started = run.started_at.replace(tzinfo=run.started_at.tzinfo or UTC)
        finished = run.finished_at.replace(tzinfo=run.finished_at.tzinfo or UTC)
        elapsed = max(0.0, (finished - started).total_seconds())
    charged = min(task.reserved_credits, task.cpu_required * max(1, math.ceil(elapsed / 60)))
    refund = task.reserved_credits - charged
    reward = Decimal(charged) * Decimal("0.80")
    fee = Decimal(charged) - reward
    if refund:
        append_transaction(
            session,
            accounts[task.owner_user_id],
            refund,
            TransactionType.task_refund,
            "task",
            str(task.id),
            f"task:{task.id}:unused-refund",
        )
    if reward:
        append_transaction(
            session,
            accounts[node.owner_user_id],
            reward,
            TransactionType.worker_reward,
            "task",
            str(task.id),
            f"task:{task.id}:failure-worker-reward",
        )
    if fee:
        append_transaction(
            session,
            accounts[platform.id],
            fee,
            TransactionType.platform_fee,
            "task",
            str(task.id),
            f"task:{task.id}:failure-platform-fee",
        )
    task.reserved_credits = 0


def complete_run(
    session: Session,
    run_id: uuid.UUID,
    node: Node,
    lease_token: str,
    payload: RunComplete,
    storage: ObjectStorage,
) -> TaskRun:
    run = locked_assigned_run(session, run_id, node, lease_token, allow_terminal=True)
    if run.status == RunStatus.succeeded:
        return run
    if run.status in {RunStatus.failed, RunStatus.lost}:
        raise ConflictError("task run has already failed")
    if run.status != RunStatus.running:
        raise ConflictError("only a running task can be completed")
    if is_expired(run.lease_expires_at):
        raise ConflictError("lease has expired")
    task = session.scalar(select(Task).where(Task.id == run.task_id).with_for_update())
    assigned_node = session.scalar(select(Node).where(Node.id == node.id).with_for_update())
    if not task or not assigned_node:
        raise NotFoundError("task or node not found")

    transition_run(run, RunStatus.uploading)
    transition_task(task, TaskStatus.uploading)
    run.exit_code = payload.exit_code
    run.logs_object_key = payload.logs_object_key
    run.result_object_key = payload.result_object_key
    transition_run(run, RunStatus.verifying)
    transition_task(task, TaskStatus.verifying)

    result = ContainerJobVerifier(storage).verify(task, run, payload.artifacts)
    if not result.valid:
        transition_run(run, RunStatus.failed)
        transition_task(task, TaskStatus.failed)
        run.failure_reason = result.reason
        run.finished_at = now_utc()
        settle_user_failure(session, task, run, assigned_node)
        release_node_capacity(assigned_node, task)
        session.flush()
        return run

    for metadata in payload.artifacts:
        session.add(
            Artifact(
                task_run_id=run.id,
                name=metadata.name,
                object_key=metadata.object_key,
                sha256=metadata.sha256,
                size_bytes=metadata.size_bytes,
            )
        )
    settle_success(session, task, assigned_node)
    transition_run(run, RunStatus.succeeded)
    transition_task(task, TaskStatus.completed)
    finished = now_utc()
    run.finished_at = finished
    task.completed_at = finished
    release_node_capacity(assigned_node, task)
    session.flush()
    return run


def fail_run(
    session: Session,
    run_id: uuid.UUID,
    node: Node,
    lease_token: str,
    payload: RunFail,
) -> TaskRun:
    run = locked_assigned_run(session, run_id, node, lease_token, allow_terminal=True)
    if run.status == RunStatus.failed:
        return run
    if run.status in {RunStatus.succeeded, RunStatus.lost}:
        raise ConflictError("task run is already terminal")
    if is_expired(run.lease_expires_at):
        raise ConflictError("lease has expired")
    task = session.scalar(select(Task).where(Task.id == run.task_id).with_for_update())
    assigned_node = session.scalar(select(Node).where(Node.id == node.id).with_for_update())
    if not task or not assigned_node:
        raise NotFoundError("task or node not found")
    transition_run(run, RunStatus.failed)
    run.failure_reason = payload.reason
    run.exit_code = payload.exit_code
    run.logs_object_key = payload.logs_object_key
    run.finished_at = now_utc()
    if payload.failure_type == "user_code":
        transition_task(task, TaskStatus.failed)
        settle_user_failure(session, task, run, assigned_node)
    else:
        task.retry_count += 1
        if task.retry_count <= task.max_retries:
            transition_task(task, TaskStatus.queued)
        else:
            transition_task(task, TaskStatus.failed)
            refund_reservation(session, task, "infrastructure-refund")
    release_node_capacity(assigned_node, task)
    session.flush()
    return run


def recover_expired_leases(session: Session, limit: int = 100) -> int:
    runs = session.scalars(
        select(TaskRun)
        .where(
            TaskRun.status.in_([RunStatus.leased, RunStatus.running]),
            TaskRun.lease_expires_at < now_utc(),
        )
        .order_by(TaskRun.lease_expires_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
    ).all()
    recovered = 0
    for run in runs:
        task = session.scalar(select(Task).where(Task.id == run.task_id).with_for_update())
        node = session.scalar(select(Node).where(Node.id == run.node_id).with_for_update())
        if not task or not node or run.status not in {RunStatus.leased, RunStatus.running}:
            continue
        transition_run(run, RunStatus.lost)
        run.failure_reason = "lease expired"
        run.finished_at = now_utc()
        task.retry_count += 1
        node.reputation = max(0.0, node.reputation - 0.05)
        if task.retry_count <= task.max_retries:
            transition_task(task, TaskStatus.queued)
        else:
            transition_task(task, TaskStatus.failed)
            refund_reservation(session, task, "lease-exhausted-refund")
        release_node_capacity(node, task)
        recovered += 1
    session.flush()
    return recovered
