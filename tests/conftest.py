import hashlib
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cpb.credits import create_user_with_account, ensure_platform_user
from cpb.database import Base
from cpb.models import User
from cpb.storage import StoredObject


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}

    def add(self, key: str, data: bytes = b"data") -> StoredObject:
        stored = StoredObject(key, hashlib.sha256(data).hexdigest(), len(data))
        self.objects[key] = stored
        return stored

    def add_metadata(self, key: str, sha256: str, size_bytes: int) -> StoredObject:
        stored = StoredObject(key, sha256, size_bytes)
        self.objects[key] = stored
        return stored

    def stat(self, key: str) -> StoredObject:
        return self.objects[key]

    def download_url(self, key: str, expires_seconds: int = 900) -> str:
        return f"https://objects.invalid/{key}?expires={expires_seconds}"


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        ensure_platform_user(session)
        session.commit()
        yield session
        session.rollback()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def publisher(session: Session) -> User:
    user = create_user_with_account(
        session, "publisher-test", "publisher-test@example.invalid", signup_bonus=100
    )
    session.commit()
    return user


@pytest.fixture
def worker_owner(session: Session) -> User:
    user = create_user_with_account(
        session, "worker-test", "worker-test@example.invalid", signup_bonus=100
    )
    session.commit()
    return user


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()
