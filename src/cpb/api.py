import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, Response, UploadFile, status
from sqlalchemy import select

from cpb.config import get_settings
from cpb.credits import account_for_user, balance_for_account
from cpb.dependencies import CurrentNode, CurrentUser, DbSession, LeaseToken
from cpb.models import Artifact, CreditTransaction, Task, TaskRun
from cpb.schemas import (
    DownloadItem,
    LeaseRequest,
    LeaseResponse,
    Message,
    NodeHeartbeat,
    NodeRead,
    NodeRegister,
    NodeRegisterResponse,
    RunComplete,
    RunFail,
    RunHeartbeatResponse,
    TaskCreate,
    TaskRead,
    TaskRunRead,
    TransactionRead,
    UploadResponse,
    WalletRead,
    validate_relative_path,
)
from cpb.services import (
    authorize_run_upload,
    cancel_task,
    complete_run,
    create_task,
    fail_run,
    heartbeat_node,
    heartbeat_run,
    lease_task,
    owned_task,
    register_node,
    start_run,
    submit_task,
)
from cpb.storage import bundle_key, get_storage, run_upload_key

router = APIRouter(prefix="/api/v1")


@router.post("/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task_endpoint(payload: TaskCreate, session: DbSession, user: CurrentUser) -> Task:
    return create_task(session, user, payload, get_storage())


@router.get("/tasks", response_model=list[TaskRead])
def list_tasks(session: DbSession, user: CurrentUser) -> list[Task]:
    return list(
        session.scalars(
            select(Task).where(Task.owner_user_id == user.id).order_by(Task.created_at.desc())
        )
    )


@router.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(task_id: uuid.UUID, session: DbSession, user: CurrentUser) -> Task:
    return owned_task(session, task_id, user.id)


@router.post("/tasks/{task_id}/submit", response_model=TaskRead)
def submit_task_endpoint(task_id: uuid.UUID, session: DbSession, user: CurrentUser) -> Task:
    return submit_task(session, task_id, user, get_storage())


@router.post("/tasks/{task_id}/cancel", response_model=TaskRead)
def cancel_task_endpoint(task_id: uuid.UUID, session: DbSession, user: CurrentUser) -> Task:
    return cancel_task(session, task_id, user)


@router.get("/tasks/{task_id}/runs", response_model=list[TaskRunRead])
def task_runs(task_id: uuid.UUID, session: DbSession, user: CurrentUser) -> list[TaskRun]:
    task = owned_task(session, task_id, user.id)
    return list(
        session.scalars(
            select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.created_at.desc())
        )
    )


@router.get("/tasks/{task_id}/logs", response_model=list[DownloadItem])
def task_logs(task_id: uuid.UUID, session: DbSession, user: CurrentUser) -> list[DownloadItem]:
    task = owned_task(session, task_id, user.id)
    storage = get_storage()
    runs = session.scalars(
        select(TaskRun)
        .where(TaskRun.task_id == task.id, TaskRun.logs_object_key.is_not(None))
        .order_by(TaskRun.created_at.desc())
    )
    items = []
    for run in runs:
        assert run.logs_object_key is not None
        stored = storage.stat(run.logs_object_key)
        items.append(
            DownloadItem(
                name=f"run-{run.id}.log",
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                download_url=storage.download_url(stored.key),
            )
        )
    return items


@router.get("/tasks/{task_id}/artifacts", response_model=list[DownloadItem])
def task_artifacts(task_id: uuid.UUID, session: DbSession, user: CurrentUser) -> list[DownloadItem]:
    task = owned_task(session, task_id, user.id)
    artifacts = session.scalars(
        select(Artifact)
        .join(TaskRun, TaskRun.id == Artifact.task_run_id)
        .where(TaskRun.task_id == task.id)
        .order_by(Artifact.created_at)
    )
    storage = get_storage()
    return [
        DownloadItem(
            name=item.name,
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            download_url=storage.download_url(item.object_key),
        )
        for item in artifacts
    ]


@router.get("/wallet", response_model=WalletRead)
def wallet(session: DbSession, user: CurrentUser) -> WalletRead:
    account = account_for_user(session, user.id)
    return WalletRead(user_id=user.id, balance=balance_for_account(session, account.id))


@router.get("/wallet/transactions", response_model=list[TransactionRead])
def wallet_transactions(session: DbSession, user: CurrentUser) -> list[CreditTransaction]:
    account = account_for_user(session, user.id)
    return list(
        session.scalars(
            select(CreditTransaction)
            .where(CreditTransaction.account_id == account.id)
            .order_by(CreditTransaction.created_at.desc())
        )
    )


@router.post(
    "/nodes/register", response_model=NodeRegisterResponse, status_code=status.HTTP_201_CREATED
)
def register_node_endpoint(
    payload: NodeRegister, session: DbSession, user: CurrentUser
) -> NodeRegisterResponse:
    node, token = register_node(session, user, payload)
    return NodeRegisterResponse(node_id=node.id, worker_token=token)


@router.post("/nodes/heartbeat", response_model=NodeRead)
def node_heartbeat(payload: NodeHeartbeat, session: DbSession, node: CurrentNode) -> NodeRead:
    return NodeRead.model_validate(heartbeat_node(session, node, payload))


@router.post("/worker/lease", response_model=LeaseResponse | None)
def worker_lease(
    _payload: LeaseRequest,
    session: DbSession,
    node: CurrentNode,
    response: Response,
) -> LeaseResponse | None:
    result = lease_task(session, node, get_storage())
    if result is None:
        response.status_code = status.HTTP_204_NO_CONTENT
    return result


@router.post("/runs/{run_id}/start", response_model=TaskRunRead)
def run_start(
    run_id: uuid.UUID, session: DbSession, node: CurrentNode, lease_token: LeaseToken
) -> TaskRun:
    return start_run(session, run_id, node, lease_token)


@router.post("/runs/{run_id}/heartbeat", response_model=RunHeartbeatResponse)
def run_heartbeat(
    run_id: uuid.UUID, session: DbSession, node: CurrentNode, lease_token: LeaseToken
) -> RunHeartbeatResponse:
    run = heartbeat_run(session, run_id, node, lease_token)
    return RunHeartbeatResponse(lease_expires_at=run.lease_expires_at)


@router.post("/runs/{run_id}/complete", response_model=TaskRunRead)
def run_complete(
    run_id: uuid.UUID,
    payload: RunComplete,
    session: DbSession,
    node: CurrentNode,
    lease_token: LeaseToken,
) -> TaskRun:
    return complete_run(session, run_id, node, lease_token, payload, get_storage())


@router.post("/runs/{run_id}/fail", response_model=TaskRunRead)
def run_fail(
    run_id: uuid.UUID,
    payload: RunFail,
    session: DbSession,
    node: CurrentNode,
    lease_token: LeaseToken,
) -> TaskRun:
    return fail_run(session, run_id, node, lease_token, payload)


@router.post(
    "/uploads/task-bundle", response_model=UploadResponse, status_code=status.HTTP_201_CREATED
)
def upload_task_bundle(
    session: DbSession,
    user: CurrentUser,
    file: Annotated[UploadFile, File()],
) -> UploadResponse:
    del session
    storage = get_storage()
    key = bundle_key(user.id, file.filename or "bundle.zip")
    stored = storage.put_stream(key, file.file, get_settings().max_bundle_bytes)
    storage.tag_sha256(stored)
    return UploadResponse(**stored.__dict__)


@router.post(
    "/uploads/artifact", response_model=UploadResponse, status_code=status.HTTP_201_CREATED
)
def upload_artifact(
    session: DbSession,
    node: CurrentNode,
    lease_token: LeaseToken,
    run_id: Annotated[uuid.UUID, Form()],
    kind: Annotated[Literal["artifact", "logs", "result"], Form()],
    name: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> UploadResponse:
    authorize_run_upload(session, run_id, node, lease_token)
    if kind == "artifact":
        validate_relative_path(name)
    storage = get_storage()
    key = run_upload_key(run_id, kind, name)
    stored = storage.put_stream(key, file.file, get_settings().max_artifact_bytes)
    storage.tag_sha256(stored)
    return UploadResponse(**stored.__dict__)


@router.get("/health", response_model=Message, include_in_schema=False)
def health() -> Message:
    return Message(detail="ok")
