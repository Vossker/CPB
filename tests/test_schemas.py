import pytest
from pydantic import ValidationError

from cpb.schemas import ContainerJobSpec, validate_relative_path
from tests.helpers import specification


def test_valid_task_specification() -> None:
    spec = specification(artifacts=["results/test.txt"], required=["results/test.txt"])
    assert spec.runtime.image == "python:3.12-slim"
    assert spec.network.enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("cpu", 5), ("memory_mb", 4097), ("timeout_seconds", 901)],
)
def test_rejects_resource_limits(field: str, value: int) -> None:
    payload = specification().model_dump()
    payload["resources"][field] = value
    with pytest.raises(ValidationError):
        ContainerJobSpec.model_validate(payload)


def test_rejects_unapproved_image() -> None:
    with pytest.raises(ValidationError, match="allow-listed"):
        specification(image="evil/latest")


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../b", "a\\b", ""])
def test_rejects_unsafe_artifact_paths(path: str) -> None:
    with pytest.raises(ValueError):
        validate_relative_path(path)


def test_required_artifact_must_be_declared() -> None:
    with pytest.raises(ValidationError, match="not declared"):
        specification(required=["missing.xml"])
