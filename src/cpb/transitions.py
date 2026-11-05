from cpb.errors import ConflictError
from cpb.models import RunStatus, Task, TaskRun, TaskStatus

TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.draft: {TaskStatus.queued, TaskStatus.cancelled},
    TaskStatus.queued: {TaskStatus.leased, TaskStatus.cancelled, TaskStatus.failed},
    TaskStatus.leased: {TaskStatus.running, TaskStatus.queued, TaskStatus.failed},
    TaskStatus.running: {TaskStatus.uploading, TaskStatus.queued, TaskStatus.failed},
    TaskStatus.uploading: {TaskStatus.verifying, TaskStatus.failed},
    TaskStatus.verifying: {TaskStatus.completed, TaskStatus.queued, TaskStatus.failed},
    TaskStatus.completed: set(),
    TaskStatus.failed: set(),
    TaskStatus.cancelled: set(),
}

RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.leased: {RunStatus.running, RunStatus.failed, RunStatus.lost},
    RunStatus.running: {RunStatus.uploading, RunStatus.failed, RunStatus.lost},
    RunStatus.uploading: {RunStatus.verifying, RunStatus.failed},
    RunStatus.verifying: {RunStatus.succeeded, RunStatus.failed},
    RunStatus.succeeded: set(),
    RunStatus.failed: set(),
    RunStatus.lost: set(),
}


def transition_task(task: Task, target: TaskStatus) -> None:
    if target not in TASK_TRANSITIONS[task.status]:
        raise ConflictError(f"invalid task transition: {task.status.value} -> {target.value}")
    task.status = target


def transition_run(run: TaskRun, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS[run.status]:
        raise ConflictError(f"invalid run transition: {run.status.value} -> {target.value}")
    run.status = target
