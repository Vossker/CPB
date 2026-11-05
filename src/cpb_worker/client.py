from pathlib import Path
from typing import Any

import httpx


class WorkerClient:
    def __init__(self, server: str, token: str, timeout: float = 30.0) -> None:
        self.http = httpx.Client(
            base_url=server.rstrip("/"),
            headers={"X-Worker-Token": token},
            timeout=timeout,
        )

    def close(self) -> None:
        self.http.close()

    def node_heartbeat(
        self, cpu_available: int, memory_available_mb: int, supported_images: list[str]
    ) -> dict[str, Any]:
        response = self.http.post(
            "/api/v1/nodes/heartbeat",
            json={
                "cpu_available": cpu_available,
                "memory_available_mb": memory_available_mb,
                "supported_images": supported_images,
            },
        )
        response.raise_for_status()
        return response.json()

    def lease(self) -> dict[str, Any] | None:
        response = self.http.post("/api/v1/worker/lease", json={})
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()

    def start(self, run_id: str, lease_token: str) -> None:
        response = self.http.post(
            f"/api/v1/runs/{run_id}/start", headers={"X-Lease-Token": lease_token}
        )
        response.raise_for_status()

    def run_heartbeat(self, run_id: str, lease_token: str) -> None:
        response = self.http.post(
            f"/api/v1/runs/{run_id}/heartbeat",
            headers={"X-Lease-Token": lease_token},
        )
        response.raise_for_status()

    def download(self, url: str, destination: Path) -> str:
        import hashlib

        digest = hashlib.sha256()
        with self.http.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    output.write(chunk)
        return digest.hexdigest()

    def upload(
        self,
        run_id: str,
        lease_token: str,
        kind: str,
        name: str,
        path: Path,
    ) -> dict[str, Any]:
        with path.open("rb") as stream:
            response = self.http.post(
                "/api/v1/uploads/artifact",
                headers={"X-Lease-Token": lease_token},
                data={"run_id": run_id, "kind": kind, "name": name},
                files={"file": (path.name, stream, "application/octet-stream")},
                timeout=120,
            )
        response.raise_for_status()
        return response.json()

    def complete(
        self,
        run_id: str,
        lease_token: str,
        exit_code: int,
        logs_object_key: str | None,
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        response = self.http.post(
            f"/api/v1/runs/{run_id}/complete",
            headers={"X-Lease-Token": lease_token},
            json={
                "exit_code": exit_code,
                "logs_object_key": logs_object_key,
                "artifacts": artifacts,
            },
            timeout=120,
        )
        response.raise_for_status()
        return response.json()

    def fail(
        self,
        run_id: str,
        lease_token: str,
        reason: str,
        failure_type: str,
        *,
        exit_code: int | None = None,
        logs_object_key: str | None = None,
    ) -> dict[str, Any]:
        response = self.http.post(
            f"/api/v1/runs/{run_id}/fail",
            headers={"X-Lease-Token": lease_token},
            json={
                "reason": reason[:4000],
                "failure_type": failure_type,
                "exit_code": exit_code,
                "logs_object_key": logs_object_key,
            },
        )
        response.raise_for_status()
        return response.json()
