from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from cpb.api import router
from cpb.database import get_db
from cpb.errors import DomainError
from cpb.main import domain_error_handler
from cpb.models import ApiKey, User
from cpb.security import hash_token
from tests.conftest import FakeStorage
from tests.helpers import specification


def test_create_submit_and_wallet_api(
    session: Session,
    publisher: User,
    storage: FakeStorage,
    monkeypatch,
) -> None:
    token = "cpbu_test-token"
    session.add(ApiKey(user_id=publisher.id, name="test", token_hash=hash_token(token)))
    session.commit()
    bundle = storage.add(f"users/{publisher.id}/bundles/example/job.zip", b"bundle")
    monkeypatch.setattr("cpb.api.get_storage", lambda: storage)

    def override_db() -> Iterator[Session]:
        yield session
        session.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    headers = {"X-API-Key": token}
    created = client.post(
        "/api/v1/tasks",
        headers=headers,
        json={
            "bundle_object_key": bundle.key,
            "bundle_sha256": bundle.sha256,
            "specification": specification().model_dump(mode="json"),
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    submitted = client.post(f"/api/v1/tasks/{task_id}/submit", headers=headers)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "queued"
    wallet = client.get("/api/v1/wallet", headers=headers)
    assert wallet.status_code == 200
    assert wallet.json()["balance"] == "98.00"


def test_worker_route_rejects_user_api_key(session: Session) -> None:
    def override_db() -> Iterator[Session]:
        yield session

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(DomainError, domain_error_handler)
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    response = client.post("/api/v1/worker/lease", headers={"X-API-Key": "user"}, json={})
    assert response.status_code == 401
