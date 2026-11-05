import argparse
import io
import json
import time
import zipfile
from pathlib import Path

import httpx


def make_bundle(example: Path) -> bytes:
    output = io.BytesIO()
    excluded = {"task.yaml", "task.json"}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in example.rglob("*"):
            if path.is_file() and path.name not in excluded and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(example).as_posix())
    return output.getvalue()


def request(client: httpx.Client, method: str, path: str, **kwargs):
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--credentials", type=Path, default=Path("seed-credentials.json"))
    parser.add_argument("--wait-seconds", type=int, default=300)
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
    example = Path("examples/python-pytest-job")
    specification = json.loads((example / "task.json").read_text(encoding="utf-8"))
    bundle = make_bundle(example)

    with httpx.Client(
        base_url=args.server,
        headers={"X-API-Key": credentials["publisher_api_key"]},
        timeout=60,
    ) as publisher:
        initial_wallet = request(publisher, "GET", "/api/v1/wallet")
        uploaded = request(
            publisher,
            "POST",
            "/api/v1/uploads/task-bundle",
            files={"file": ("python-pytest-job.zip", bundle, "application/zip")},
        )
        task = request(
            publisher,
            "POST",
            "/api/v1/tasks",
            json={
                "bundle_object_key": uploaded["object_key"],
                "bundle_sha256": uploaded["sha256"],
                "specification": specification,
                "max_retries": 2,
            },
        )
        task = request(publisher, "POST", f"/api/v1/tasks/{task['id']}/submit")
        print(f"Submitted task {task['id']} with {task['reserved_credits']} reserved credits")
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            task = request(publisher, "GET", f"/api/v1/tasks/{task['id']}")
            if task["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(2)
        if task["status"] != "completed":
            raise SystemExit(f"Task did not complete successfully: {task['status']}")
        final_wallet = request(publisher, "GET", "/api/v1/wallet")
        artifacts = request(publisher, "GET", f"/api/v1/tasks/{task['id']}/artifacts")

    with httpx.Client(
        base_url=args.server,
        headers={"X-API-Key": credentials["worker_owner_api_key"]},
    ) as worker_owner:
        worker_wallet = request(worker_owner, "GET", "/api/v1/wallet")

    print(f"Task status: {task['status']}")
    print(f"Publisher balance: {initial_wallet['balance']} -> {final_wallet['balance']}")
    print(f"Worker-owner balance: {worker_wallet['balance']}")
    for artifact in artifacts:
        print(f"Artifact {artifact['name']}: {artifact['download_url']}")


if __name__ == "__main__":
    main()
