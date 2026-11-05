import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cpb.credits import account_for_user, balance_for_account, create_user_with_account
from cpb.errors import ConflictError, InsufficientCreditsError
from cpb.models import CreditTransaction, TaskStatus, User
from cpb.services import cancel_task, create_task, submit_task
from tests.conftest import FakeStorage
from tests.helpers import specification, task_payload


def uploaded_bundle(storage: FakeStorage, user: User):
    return storage.add(f"users/{user.id}/bundles/example/job.zip", b"bundle")


def test_create_submit_reserves_credits(
    session: Session, publisher: User, storage: FakeStorage
) -> None:
    bundle = uploaded_bundle(storage, publisher)
    task = create_task(
        session,
        publisher,
        task_payload(publisher, bundle, specification(cpu=2, timeout_seconds=61)),
        storage,
    )
    assert task.status == TaskStatus.draft
    assert task.estimated_credits == 4
    submit_task(session, task.id, publisher, storage)
    assert task.status == TaskStatus.queued
    assert task.reserved_credits == 4
    account = account_for_user(session, publisher.id)
    assert balance_for_account(session, account.id) == 96


def test_task_cannot_be_submitted_twice(
    session: Session, publisher: User, storage: FakeStorage
) -> None:
    bundle = uploaded_bundle(storage, publisher)
    task = create_task(session, publisher, task_payload(publisher, bundle), storage)
    submit_task(session, task.id, publisher, storage)
    with pytest.raises(ConflictError):
        submit_task(session, task.id, publisher, storage)


def test_insufficient_credit_rejection(session: Session, storage: FakeStorage) -> None:
    poor = create_user_with_account(
        session,
        f"poor-{uuid.uuid4()}",
        f"poor-{uuid.uuid4()}@example.invalid",
        signup_bonus=0,
    )
    bundle = uploaded_bundle(storage, poor)
    task = create_task(session, poor, task_payload(poor, bundle), storage)
    with pytest.raises(InsufficientCreditsError):
        submit_task(session, task.id, poor, storage)
    assert task.status == TaskStatus.draft


def test_cancel_refunds_once(session: Session, publisher: User, storage: FakeStorage) -> None:
    bundle = uploaded_bundle(storage, publisher)
    task = create_task(session, publisher, task_payload(publisher, bundle), storage)
    submit_task(session, task.id, publisher, storage)
    cancel_task(session, task.id, publisher)
    assert task.status == TaskStatus.cancelled
    assert task.reserved_credits == 0
    account = account_for_user(session, publisher.id)
    assert balance_for_account(session, account.id) == 100
    with pytest.raises(ConflictError):
        cancel_task(session, task.id, publisher)


def test_ledger_reconciles_to_transaction_sum(
    session: Session, publisher: User, storage: FakeStorage
) -> None:
    bundle = uploaded_bundle(storage, publisher)
    task = create_task(session, publisher, task_payload(publisher, bundle), storage)
    submit_task(session, task.id, publisher, storage)
    account = account_for_user(session, publisher.id)
    ledger_sum = session.scalar(
        select(func.sum(CreditTransaction.amount)).where(CreditTransaction.account_id == account.id)
    )
    assert balance_for_account(session, account.id) == ledger_sum
