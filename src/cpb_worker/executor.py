import hashlib
import shlex
import shutil
import stat
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event
from typing import Any


class WorkerExecutionError(Exception):
    def __init__(self, message: str, failure_type: str = "infrastructure") -> None:
        super().__init__(message)
        self.failure_type = failure_type


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    logs_path: Path
    timed_out: bool
    latest_logs_object_key: str | None


def directory_size(path: Path) -> int:
    return sum(
        item.stat().st_size for item in path.rglob("*") if not item.is_symlink() and item.is_file()
    )


def safe_extract_zip(bundle: Path, workspace: Path, disk_limit_bytes: int) -> None:
    root = workspace.resolve()
    total = 0
    try:
        archive = zipfile.ZipFile(bundle)
    except zipfile.BadZipFile as exc:
        raise WorkerExecutionError("source bundle is not a valid ZIP archive") from exc
    with archive:
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise WorkerExecutionError("source bundle contains a path traversal")
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                raise WorkerExecutionError("source bundle contains a symbolic link")
            total += info.file_size
            if total > disk_limit_bytes:
                raise WorkerExecutionError("expanded source bundle exceeds the task disk limit")
            target = (workspace / Path(*path.parts)).resolve()
            if not target.is_relative_to(root):
                raise WorkerExecutionError("source bundle path escapes the workspace")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def write_task_script(workspace: Path, specification: dict[str, Any]) -> Path:
    script = workspace / ".cpb-run.sh"
    lines = ["#!/bin/sh", "set -eu"]
    for step in specification["steps"]:
        safe_name = step["name"].replace("\n", " ").replace("\r", " ")
        lines.append(f"printf '%s\\n' {shlex.quote(f'==> {safe_name}')}")
        lines.append(step["command"])
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def docker_command(workspace: Path, specification: dict[str, Any]) -> list[str]:
    resources = specification["resources"]
    memory = f"{resources['memory_mb']}m"
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cpus",
        str(resources["cpu"]),
        "--memory",
        memory,
        "--memory-swap",
        memory,
        "--pids-limit",
        "256",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "1000:1000",
        "--tmpfs",
        "/tmp:size=512m",
        "--volume",
        f"{workspace.resolve()}:/workspace:rw",
        "--workdir",
        "/workspace",
        specification["runtime"]["image"],
        "/bin/sh",
        "/workspace/.cpb-run.sh",
    ]


def execute_container(
    workspace: Path,
    specification: dict[str, Any],
    stop_event: Event,
    heartbeat_callback,
    log_upload_callback,
    heartbeat_seconds: int,
    log_upload_seconds: int,
) -> ExecutionResult:
    write_task_script(workspace, specification)
    log_path = workspace / ".cpb-task.log"
    latest_log_key: str | None = None
    timeout_seconds = specification["resources"]["timeout_seconds"]
    started = time.monotonic()
    next_heartbeat = started + heartbeat_seconds
    next_log_upload = started + log_upload_seconds
    timed_out = False
    process = None
    try:
        with log_path.open("wb", buffering=0) as log_stream:
            process = subprocess.Popen(
                docker_command(workspace, specification),
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                shell=False,
            )
            try:
                while process.poll() is None:
                    now = time.monotonic()
                    if stop_event.is_set():
                        raise WorkerExecutionError("worker is shutting down")
                    if now - started >= timeout_seconds:
                        timed_out = True
                        process.kill()
                        process.wait(timeout=10)
                        break
                    if now >= next_heartbeat:
                        heartbeat_callback()
                        next_heartbeat = now + heartbeat_seconds
                    if now >= next_log_upload and log_path.exists() and log_path.stat().st_size:
                        latest_log_key = log_upload_callback(log_path)
                        next_log_upload = now + log_upload_seconds
                    time.sleep(0.5)
                exit_code = 124 if timed_out else int(process.returncode or 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
    except FileNotFoundError as exc:
        raise WorkerExecutionError("Docker CLI is not installed or not on PATH") from exc
    if log_path.exists() and log_path.stat().st_size:
        latest_log_key = log_upload_callback(log_path)
    return ExecutionResult(exit_code, log_path, timed_out, latest_log_key)


def collect_artifacts(
    workspace: Path, declared_paths: list[str], max_artifact_bytes: int
) -> list[tuple[str, Path, str, int]]:
    root = workspace.resolve()
    results: list[tuple[str, Path, str, int]] = []
    for name in declared_paths:
        posix_path = PurePosixPath(name)
        if posix_path.is_absolute() or ".." in posix_path.parts or "\\" in name:
            raise WorkerExecutionError("artifact path escapes the workspace", "user_code")
        path = workspace.joinpath(*posix_path.parts)
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise WorkerExecutionError("artifact must be a regular file", "user_code")
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise WorkerExecutionError("artifact path escapes the workspace", "user_code")
        size = resolved.stat().st_size
        if size > max_artifact_bytes:
            raise WorkerExecutionError("artifact exceeds the platform size limit", "user_code")
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        results.append((name, resolved, digest.hexdigest(), size))
    return results
