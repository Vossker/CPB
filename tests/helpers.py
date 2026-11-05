from cpb.models import User
from cpb.schemas import ContainerJobSpec, TaskCreate
from cpb.storage import StoredObject


def specification(
    *,
    image: str = "python:3.12-slim",
    cpu: int = 1,
    memory_mb: int = 256,
    timeout_seconds: int = 120,
    artifacts: list[str] | None = None,
    required: list[str] | None = None,
) -> ContainerJobSpec:
    return ContainerJobSpec.model_validate(
        {
            "version": 1,
            "runtime": {"image": image},
            "resources": {
                "cpu": cpu,
                "memory_mb": memory_mb,
                "disk_mb": 512,
                "timeout_seconds": timeout_seconds,
            },
            "network": {"enabled": False},
            "steps": [{"name": "test", "command": "python run_tests.py"}],
            "artifacts": artifacts or [],
            "verification": {
                "required_exit_code": 0,
                "required_files": required or [],
            },
        }
    )


def task_payload(
    user: User,
    stored: StoredObject,
    spec: ContainerJobSpec | None = None,
    max_retries: int = 2,
) -> TaskCreate:
    return TaskCreate(
        bundle_object_key=stored.key,
        bundle_sha256=stored.sha256,
        specification=spec or specification(),
        max_retries=max_retries,
    )
