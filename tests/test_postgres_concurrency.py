import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from cpb.credits import create_user_with_account, ensure_platform_user
from cpb.database import Base
from cpb.schemas import NodeRegister
from cpb.services import create_task, lease_task, register_node, submit_task
from tests.conftest import FakeStorage
from tests.helpers import task_payload


@pytest.mark.postgres
def test_two_workers_cannot_lease_the_same_task() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    schema = f"cpb_test_{uuid.uuid4().hex}"
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        storage = FakeStorage()
        with Session(engine) as session:
            ensure_platform_user(session)
            publisher = create_user_with_account(
                session, f"publisher-{uuid.uuid4()}", f"p-{uuid.uuid4()}@example.invalid"
            )
            owner1 = create_user_with_account(
                session, f"owner-{uuid.uuid4()}", f"o1-{uuid.uuid4()}@example.invalid"
            )
            owner2 = create_user_with_account(
                session, f"owner-{uuid.uuid4()}", f"o2-{uuid.uuid4()}@example.invalid"
            )
            nodes = []
            for index, owner in enumerate([owner1, owner2]):
                node, _ = register_node(
                    session,
                    owner,
                    NodeRegister(
                        name=f"node-{index}",
                        platform="linux",
                        architecture="x86_64",
                        cpu_total=2,
                        memory_total_mb=2048,
                        supported_images=["python:3.12-slim"],
                    ),
                )
                nodes.append(node.id)
            bundle = storage.add(f"users/{publisher.id}/bundles/job.zip", b"bundle")
            task = create_task(session, publisher, task_payload(publisher, bundle), storage)
            submit_task(session, task.id, publisher, storage)
            session.commit()
            task_id = task.id

        barrier = threading.Barrier(2)
        results = []

        def claim(node_id):
            with Session(engine) as session:
                node = session.get(__import__("cpb.models", fromlist=["Node"]).Node, node_id)
                barrier.wait()
                lease = lease_task(session, node, storage)
                session.commit()
                results.append(lease.task_id if lease else None)

        threads = [threading.Thread(target=claim, args=(node_id,)) for node_id in nodes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert results.count(task_id) == 1
        assert results.count(None) == 1
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()
