from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cpb.config import get_settings
from cpb.credits import account_for_user, balance_for_account, ensure_platform_user
from cpb.errors import AuthorizationError
from cpb.models import (
    CreditTransaction,
    Node,
    RunStatus,
    TaskStatus,
    User,
)
from cpb.schemas import ArtifactMetadata, NodeRegister, RunComplete
from cpb.services import (
    complete_run,
    create_task,
    lease_task,
    now_utc,
    recover_expired_leases,
    register_node,
    start_run,
    submit_task,
)
from tests.conftest import FakeStorage
from tests.helpers import specification, task_payload


def make_node(session: Session, owner: User, *, images: list[str] | None = None) -> Node:
    node, _ = register_node(
        session,
        owner,
        NodeRegister(
            name=f"node-{owner.id}",
            platform="linux",
            architecture="x86_64",
            cpu_total=4,
            memory_total_mb=4096,
            supported_images=images or ["python:3.12-slim"],
        ),
    )
    return node


def queued_task(
    session: Session,
    publisher: User,
    storage: FakeStorage,
    *,
    spec=None,
    max_retries: int = 2,
):
    bundle = storage.add(f"users/{publisher.id}/bundles/example/job.zip", b"bundle")
    task = create_task(
        session,
        publisher,
        task_payload(publisher, bundle, spec or specification(), max_retries=max_retries),
        storage,
    )
    submit_task(session, task.id, publisher, storage)
    return task


def test_compatible_worker_leases_once_and_token_is_validated(
    session: Session, publisher: User, worker_owner: User, storage: FakeStorage
) -> None:
    task = queued_task(session, publisher, storage)
    node = make_node(session, worker_owner)
    lease = lease_task(session, node, storage)
    assert lease is not None and lease.task_id == task.id
    assert task.status == TaskStatus.leased
    assert lease_task(session, node, storage) is None
    with pytest.raises(AuthorizationError):
        start_run(session, lease.run_id, node, "wrong-token")
    run = start_run(session, lease.run_id, node, lease.lease_token)
    assert run.status == RunStatus.running


def test_incompatible_worker_gets_no_task(
    session: Session, publisher: User, worker_owner: User, storage: FakeStorage
) -> None:
    task = queued_task(session, publisher, storage)
    node = make_node(session, worker_owner, images=["python:3.11-slim"])
    assert lease_task(session, node, storage) is None
    assert task.status == TaskStatus.queued


def test_expired_lease_is_recovered_and_retry_limit_respected(
    session: Session, publisher: User, worker_owner: User, storage: FakeStorage
) -> None:
    task = queued_task(session, publisher, storage, max_retries=0)
    node = make_node(session, worker_owner)
    lease = lease_task(session, node, storage)
    assert lease is not None
    run = start_run(session, lease.run_id, node, lease.lease_token)
    run.lease_expires_at = now_utc() - timedelta(seconds=1)
    assert recover_expired_leases(session) == 1
    assert run.status == RunStatus.lost
    assert task.status == TaskStatus.failed
    assert task.retry_count == 1
    assert task.reserved_credits == 0
    assert node.reputation < 1.0


def test_success_verification_and_exactly_once_settlement(
    session: Session, publisher: User, worker_owner: User, storage: FakeStorage
) -> None:
    spec = specification(
        timeout_seconds=300,
        artifacts=["test-results.txt"],
        required=["test-results.txt"],
    )
    task = queued_task(session, publisher, storage, spec=spec)
    node = make_node(session, worker_owner)
    lease = lease_task(session, node, storage)
    assert lease is not None
    run = start_run(session, lease.run_id, node, lease.lease_token)
    key = f"runs/{run.id}/artifact/random/test-results.txt"
    stored = storage.add(key, b"all passed")
    payload = RunComplete(
        exit_code=0,
        artifacts=[
            ArtifactMetadata(
                name="test-results.txt",
                object_key=key,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
            )
        ],
    )
    completed = complete_run(session, run.id, node, lease.lease_token, payload, storage)
    assert completed.status == RunStatus.succeeded
    assert task.status == TaskStatus.completed
    assert task.reserved_credits == 0
    worker_account = account_for_user(session, worker_owner.id)
    assert balance_for_account(session, worker_account.id) == 104
    platform = ensure_platform_user(session)
    platform_account = account_for_user(session, platform.id)
    assert balance_for_account(session, platform_account.id) == 1

    duplicate = complete_run(session, run.id, node, lease.lease_token, payload, storage)
    assert duplicate.id == run.id
    reward_count = session.scalar(
        select(func.count(CreditTransaction.id)).where(
            CreditTransaction.idempotency_key == f"task:{task.id}:worker-reward"
        )
    )
    assert reward_count == 1


@pytest.mark.parametrize(
    ("exit_code", "include_artifact", "hash_matches"),
    [(2, True, True), (0, False, True), (0, True, False)],
)
def test_verification_failures(
    session: Session,
    publisher: User,
    worker_owner: User,
    storage: FakeStorage,
    exit_code: int,
    include_artifact: bool,
    hash_matches: bool,
) -> None:
    spec = specification(artifacts=["result.txt"], required=["result.txt"])
    task = queued_task(session, publisher, storage, spec=spec)
    node = make_node(session, worker_owner)
    lease = lease_task(session, node, storage)
    assert lease is not None
    run = start_run(session, lease.run_id, node, lease.lease_token)
    metadata = []
    if include_artifact:
        key = f"runs/{run.id}/artifact/random/result.txt"
        stored = storage.add(key, b"result")
        metadata = [
            ArtifactMetadata(
                name="result.txt",
                object_key=key,
                sha256=stored.sha256 if hash_matches else "0" * 64,
                size_bytes=stored.size_bytes,
            )
        ]
    result = complete_run(
        session,
        run.id,
        node,
        lease.lease_token,
        RunComplete(exit_code=exit_code, artifacts=metadata),
        storage,
    )
    assert result.status == RunStatus.failed
    assert task.status == TaskStatus.failed


def test_oversized_artifact_is_rejected(
    session: Session, publisher: User, worker_owner: User, storage: FakeStorage
) -> None:
    spec = specification(artifacts=["large.bin"], required=["large.bin"])
    queued_task(session, publisher, storage, spec=spec)
    node = make_node(session, worker_owner)
    lease = lease_task(session, node, storage)
    assert lease is not None
    run = start_run(session, lease.run_id, node, lease.lease_token)
    key = f"runs/{run.id}/artifact/random/large.bin"
    size = get_settings().max_artifact_bytes + 1
    stored = storage.add_metadata(key, "a" * 64, size)
    result = complete_run(
        session,
        run.id,
        node,
        lease.lease_token,
        RunComplete(
            exit_code=0,
            artifacts=[
                ArtifactMetadata(
                    name="large.bin",
                    object_key=key,
                    sha256=stored.sha256,
                    size_bytes=size,
                )
            ],
        ),
        storage,
    )
    assert result.status == RunStatus.failed
