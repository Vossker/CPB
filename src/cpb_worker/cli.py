import argparse
import logging
from pathlib import Path

from cpb_worker.agent import WorkerAgent
from cpb_worker.client import WorkerClient

DEFAULT_IMAGES = [
    "python:3.11-slim",
    "python:3.12-slim",
    "node:20-slim",
    "ubuntu:24.04",
    "gcc:14",
]


def main() -> None:
    parser = argparse.ArgumentParser(prog="cpb-worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run the Linux worker loop")
    run.add_argument("--server", required=True)
    run.add_argument("--token", required=True)
    run.add_argument("--image", action="append", dest="images")
    run.add_argument("--poll-seconds", type=int, default=5)
    run.add_argument("--heartbeat-seconds", type=int, default=20)
    run.add_argument("--log-upload-seconds", type=int, default=30)
    run.add_argument("--max-artifact-bytes", type=int, default=100 * 1024 * 1024)
    run.add_argument("--state-file", type=Path, default=Path(".cpb-worker-state.json"))
    run.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    client = WorkerClient(args.server, args.token)
    try:
        WorkerAgent(
            client=client,
            supported_images=args.images or DEFAULT_IMAGES,
            poll_seconds=max(1, args.poll_seconds),
            heartbeat_seconds=max(5, args.heartbeat_seconds),
            log_upload_seconds=max(5, args.log_upload_seconds),
            max_artifact_bytes=args.max_artifact_bytes,
            state_file=args.state_file,
        ).run_forever()
    finally:
        client.close()


if __name__ == "__main__":
    main()
