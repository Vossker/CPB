import hashlib
import subprocess
import zipfile
from pathlib import Path
from threading import Event

import pytest

from cpb_worker.agent import WorkerAgent
from cpb_worker.executor import (
    ExecutionResult,
    WorkerExecutionError,
    collect_artifacts,
    docker_command,
    execute_container,
    safe_extract_zip,
)


def worker_spec() -> dict:
    return {
        "runtime": {"image": "python:3.12-slim"},
        "resources": {"cpu": 2, "memory_mb": 512, "disk_mb": 512, "timeout_seconds": 5},
        "steps": [{"name": "test", "command": "python run_tests.py"}],
        "artifacts": ["result.txt"],
    }


def test_docker_command_has_isolation_controls(tmp_path: Path) -> None:
    command = docker_command(tmp_path, worker_spec())
    joined = " ".join(command)
    for fragment in [
        "--network none",
        "--read-only",
        "--cpus 2",
        "--memory 512m",
        "--memory-swap 512m",
        "--pids-limit 256",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 1000:1000",
    ]:
        assert fragment in joined
    assert "docker.sock" not in joined


def test_zip_path_traversal_rejected(tmp_path: Path) -> None:
    bundle = tmp_path / "bad.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("../outside.txt", "bad")
    with pytest.raises(WorkerExecutionError, match="traversal"):
        safe_extract_zip(bundle, tmp_path / "workspace", 1024)


def test_artifact_collection_hashes_and_rejects_escape(tmp_path: Path) -> None:
    (tmp_path / "result.txt").write_text("ok", encoding="utf-8")
    artifacts = collect_artifacts(tmp_path, ["result.txt"], 1024)
    assert artifacts[0][2] == hashlib.sha256(b"ok").hexdigest()
    with pytest.raises(WorkerExecutionError):
        collect_artifacts(tmp_path, ["../result.txt"], 1024)


class ImmediateProcess:
    returncode = 0

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def poll(self):
        return self.returncode


@pytest.mark.parametrize("returncode", [0, 2])
def test_container_success_and_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    class Process(ImmediateProcess):
        def __init__(self, *_args, **_kwargs) -> None:
            self.returncode = returncode

    monkeypatch.setattr(subprocess, "Popen", Process)
    result = execute_container(
        tmp_path, worker_spec(), Event(), lambda: None, lambda _path: "logs", 20, 30
    )
    assert result.exit_code == returncode
    assert result.timed_out is False


def test_container_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class RunningProcess:
        returncode = None

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    values = iter([0.0, 10.0])
    monkeypatch.setattr(subprocess, "Popen", RunningProcess)
    monkeypatch.setattr("cpb_worker.executor.time.monotonic", lambda: next(values))
    monkeypatch.setattr("cpb_worker.executor.time.sleep", lambda _value: None)
    spec = worker_spec()
    spec["resources"]["timeout_seconds"] = 1
    result = execute_container(tmp_path, spec, Event(), lambda: None, lambda _path: "logs", 20, 30)
    assert result.timed_out is True
    assert result.exit_code == 124


class FakeWorkerClient:
    def __init__(self, bundle_sha: str, *, checksum_mismatch: bool = False) -> None:
        self.bundle_sha = bundle_sha
        self.checksum_mismatch = checksum_mismatch
        self.failures = []
        self.completions = []

    def download(self, _url, destination: Path) -> str:
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("run_tests.py", "print('ok')")
        return "0" * 64 if self.checksum_mismatch else self.bundle_sha

    def start(self, *_args):
        return None

    def run_heartbeat(self, *_args):
        return None

    def upload(self, _run_id, _token, _kind, _name, path: Path):
        data = path.read_bytes()
        return {
            "object_key": "uploaded/object",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }

    def complete(self, *args):
        self.completions.append(args)

    def fail(self, *args, **kwargs):
        self.failures.append((args, kwargs))


def make_agent(client, state_file: Path) -> WorkerAgent:
    return WorkerAgent(client, ["python:3.12-slim"], 1, 5, 5, 1024, state_file)


def lease_payload() -> dict:
    return {
        "run_id": "run-id",
        "task_id": "task-id",
        "lease_token": "lease-token",
        "bundle_url": "https://objects.invalid/bundle",
        "bundle_sha256": "a" * 64,
        "specification": worker_spec(),
    }


def test_checksum_mismatch_is_reported_and_state_cleared(tmp_path: Path) -> None:
    client = FakeWorkerClient("a" * 64, checksum_mismatch=True)
    state_file = tmp_path / "state.json"
    make_agent(client, state_file).process_lease(lease_payload())
    assert client.failures
    assert "checksum mismatch" in client.failures[0][0][2]
    assert not state_file.exists()


def test_workspace_cleanup_after_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeWorkerClient("a" * 64)
    seen_workspace: list[Path] = []

    def fake_execute(workspace: Path, *_args, **_kwargs):
        seen_workspace.append(workspace)
        artifact = workspace / "result.txt"
        artifact.write_text("result", encoding="utf-8")
        log = workspace / ".cpb-task.log"
        log.write_text("ok", encoding="utf-8")
        return ExecutionResult(0, log, False, "logs/object")

    monkeypatch.setattr("cpb_worker.agent.execute_container", fake_execute)
    make_agent(client, tmp_path / "state.json").process_lease(lease_payload())
    assert client.completions
    assert seen_workspace and not seen_workspace[0].exists()
