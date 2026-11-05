import argparse
import json
from pathlib import Path

from sqlalchemy import select

from cpb.credits import create_user_with_account, ensure_platform_user
from cpb.database import SessionLocal
from cpb.models import ApiKey, Node, User
from cpb.schemas import NodeRegister
from cpb.security import generate_token, hash_token
from cpb.services import recover_expired_leases, register_node


def _upsert_api_key(session, user: User, name: str) -> str:
    token = generate_token("cpbu")
    key = session.scalar(select(ApiKey).where(ApiKey.user_id == user.id, ApiKey.name == name))
    if key:
        key.token_hash = hash_token(token)
        key.revoked_at = None
    else:
        session.add(ApiKey(user_id=user.id, name=name, token_hash=hash_token(token)))
    return token


def seed() -> None:
    with SessionLocal.begin() as session:
        ensure_platform_user(session)
        publisher = session.scalar(select(User).where(User.username == "publisher"))
        if not publisher:
            publisher = create_user_with_account(session, "publisher", "publisher@cpb.local")
        worker_owner = session.scalar(select(User).where(User.username == "worker-owner"))
        if not worker_owner:
            worker_owner = create_user_with_account(
                session, "worker-owner", "worker-owner@cpb.local"
            )
        publisher_key = _upsert_api_key(session, publisher, "development")
        worker_owner_key = _upsert_api_key(session, worker_owner, "development")
        node = session.scalar(
            select(Node).where(Node.owner_user_id == worker_owner.id, Node.name == "dev-node")
        )
        if node:
            worker_token = generate_token("cpbw")
            node.worker_token_hash = hash_token(worker_token)
        else:
            node, worker_token = register_node(
                session,
                worker_owner,
                NodeRegister(
                    name="dev-node",
                    platform="linux",
                    architecture="x86_64",
                    cpu_total=4,
                    memory_total_mb=8192,
                    supported_images=["python:3.11-slim", "python:3.12-slim"],
                ),
            )
        credentials = {
            "publisher_api_key": publisher_key,
            "worker_owner_api_key": worker_owner_key,
            "worker_token": worker_token,
            "node_id": str(node.id),
            "publisher_user_id": str(publisher.id),
            "worker_owner_user_id": str(worker_owner.id),
        }
    path = Path("seed-credentials.json")
    path.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    print(json.dumps(credentials, indent=2))
    print(f"Development credentials written to {path}; the file is git-ignored.")


def recover() -> None:
    with SessionLocal.begin() as session:
        count = recover_expired_leases(session)
    print(f"Recovered {count} expired lease(s).")


def main() -> None:
    parser = argparse.ArgumentParser(prog="cpb")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="create development users, credits, keys, and node")
    subparsers.add_parser("recover-leases", help="recover expired task leases once")
    args = parser.parse_args()
    if args.command == "seed":
        seed()
    elif args.command == "recover-leases":
        recover()


if __name__ == "__main__":
    main()
