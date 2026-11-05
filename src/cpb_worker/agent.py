import json
import logging
import os
import platform
import signal
import tempfile
from pathlib import Path
from threading import Event
from typing import Any

import httpx

from cpb_worker.client import WorkerClient
from cpb_worker.executor import (
    WorkerExecutionError,
    collect_artifacts,
    directory_size,
    execute_container,
    safe_extract_zip,
)

logger = logging.getLogger(__name__)


def memory_total_mb() -> int:
    if Path("/proc/meminfo").exists():
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    return 1024


class WorkerAgent:
    def __init__(
        self,
        client: WorkerClient,
        supported_images: list[str],
        poll_seconds: int,
        heartbeat_seconds: int,
        log_upload_seconds: int,
        max_artifact_bytes: int,
        state_file: Path,
    ) -> None:
        self.client = client
        self.supported_images = supported_images
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.log_upload_seconds = log_upload_seconds
        self.max_artifact_bytes = max_artifact_bytes
        self.state_file = state_file
        self.stop_event = Event()
        self.cpu_total = os.cpu_count() or 1
        self.memory_mb = memory_total_mb()

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

    def heartbeat(self) -> None:
        self.client.node_heartbeat(self.cpu_total, self.memory_mb, self.supported_images)

    def save_state(self, lease: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(lease), encoding="utf-8")

    def clear_state(self) -> None:
        self.state_file.unlink(missing_ok=True)

    def recover_interrupted_run(self) -> None:
        if not self.state_file.exists():
            return
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.client.fail(
                state["run_id"],
                state["lease_token"],
                "worker process restarted before the run finished",
                "infrastructure",
            )
        except Exception:
            logger.warning("could not report interrupted run; lease recovery will handle it")
        finally:
            self.clear_state()

    def run_forever(self) -> None:
        if platform.system().lower() != "linux":
            raise RuntimeError("cpb-worker supports Linux hosts only")
        self.install_signal_handlers()
        self.recover_interrupted_run()
        while not self.stop_event.is_set():
            try:
                self.heartbeat()
                lease = self.client.lease()
                if lease:
                    self.process_lease(lease)
            except (httpx.HTTPError, OSError, WorkerExecutionError) as exc:
                logger.error("worker iteration failed: %s", exc)
            self.stop_event.wait(self.poll_seconds)

    def process_lease(self, lease: dict[str, Any]) -> None:
        self.save_state(lease)
        run_id = lease["run_id"]
        lease_token = lease["lease_token"]
        spec = lease["specification"]
        failure_type = "infrastructure"
        logs_key = None
        try:
            with tempfile.TemporaryDirectory(prefix="cpb-worker-") as temporary:
                workspace_root = Path(temporary)
                bundle_path = workspace_root / "bundle.zip"
                workspace = workspace_root / "workspace"
                workspace.mkdir(mode=0o777)
                workspace.chmod(0o777)
                actual_sha = self.client.download(lease["bundle_url"], bundle_path)
                if actual_sha != lease["bundle_sha256"]:
                    raise WorkerExecutionError("source-bundle checksum mismatch")
                disk_limit = spec["resources"].get("disk_mb", 4096) * 1024 * 1024
                safe_extract_zip(bundle_path, workspace, disk_limit)
                if directory_size(workspace) > disk_limit:
                    raise WorkerExecutionError("source bundle exceeds task disk limit")
                self.client.start(run_id, lease_token)

                def run_heartbeat() -> None:
                    self.client.run_heartbeat(run_id, lease_token)

                def upload_logs(path: Path) -> str:
                    uploaded = self.client.upload(
                        run_id, lease_token, "logs", ".cpb-task.log", path
                    )
                    return uploaded["object_key"]

                result = execute_container(
                    workspace,
                    spec,
                    self.stop_event,
                    run_heartbeat,
                    upload_logs,
                    self.heartbeat_seconds,
                    self.log_upload_seconds,
                )
                logs_key = result.latest_logs_object_key
                if result.timed_out:
                    failure_type = "user_code"
                    raise WorkerExecutionError("task exceeded its execution timeout", "user_code")
                if directory_size(workspace) > disk_limit:
                    failure_type = "user_code"
                    raise WorkerExecutionError(
                        "task workspace exceeded its disk limit", "user_code"
                    )
                artifacts = []
                for name, path, sha256, size in collect_artifacts(
                    workspace, spec.get("artifacts", []), self.max_artifact_bytes
                ):
                    uploaded = self.client.upload(run_id, lease_token, "artifact", name, path)
                    if uploaded["sha256"] != sha256 or uploaded["size_bytes"] != size:
                        raise WorkerExecutionError("artifact upload integrity check failed")
                    artifacts.append(
                        {
                            "name": name,
                            "object_key": uploaded["object_key"],
                            "sha256": sha256,
                            "size_bytes": size,
                        }
                    )
                self.client.complete(run_id, lease_token, result.exit_code, logs_key, artifacts)
        except WorkerExecutionError as exc:
            failure_type = exc.failure_type or failure_type
            try:
                self.client.fail(
                    run_id,
                    lease_token,
                    str(exc),
                    failure_type,
                    logs_object_key=logs_key,
                )
            except httpx.HTTPError:
                logger.exception("failed to report task failure; lease recovery will handle it")
        except Exception as exc:
            logger.exception("unexpected task failure")
            try:
                self.client.fail(
                    run_id,
                    lease_token,
                    str(exc),
                    "infrastructure",
                    logs_object_key=logs_key,
                )
            except httpx.HTTPError:
                logger.exception("failed to report task failure; lease recovery will handle it")
        finally:
            self.clear_state()
