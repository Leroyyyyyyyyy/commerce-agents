"""The retail direct-add contract, exercised against a real database."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from commerce_common.testing import FakeClient
from demo_common import MemorySeeder, build_storefront_host
from shopping_agent_runtime import ShoppingAgent
from tests import test_postgres_retail as pg_tests

# Reuse the isolated real-database fixtures, without importing their test functions.
database = pg_tests.database
store = pg_tests.store
backend = pg_tests.backend


@pytest.fixture
def direct_app(store, backend, tmp_path):
    from retail.api.cart_add import RetailCartAddRequest, RetailCartAdds

    agent = ShoppingAgent(
        backend=backend,
        config=backend.config.model_copy(update={"enable_memory": False}),
        client=FakeClient([]),
    )
    host = build_storefront_host(
        title="ACME direct-add test",
        example_root=tmp_path,
        backend=backend,
        agent=agent,
        sessions=store,
        memory_seeder=MemorySeeder(tmp_path / "empty.json"),
    )
    adds = RetailCartAdds(host)

    @host.app.post("/api/cart/add")
    async def add(request: RetailCartAddRequest, record: host.CurrentSession):
        return await adds.add(record, request)

    record = store.start("demo-user")
    record.state.remember_products([backend.product("AR-1202"), backend.product("AR-1201")])
    store.save(record)
    with TestClient(host.app, base_url="http://localhost", raise_server_exceptions=False) as client:
        yield client, {"X-Session-Id": record.session_id}, host


def payload(operation_id=None, **changes):
    return {
        "operation_id": operation_id or str(uuid4()),
        "product_id": "AR-1202",
        "quantity": 1,
        **changes,
    }


def test_same_id_replays_original_result_even_after_cart_changes(direct_app, store):
    client, headers, _ = direct_app
    body = payload()
    first = client.post("/api/cart/add", json=body, headers=headers)
    assert first.status_code == 200
    assert client.post("/api/cart/add", json=body, headers=headers).json() == first.json()
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 1
    assert client.post("/api/cart/add", json=payload(), headers=headers).status_code == 200
    replay = client.post("/api/cart/add", json=body, headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 2
    assert len(store.require(headers["X-Session-Id"]).pending_app_events) == 2


def test_different_ids_with_identical_parameters_are_new_adds(direct_app):
    client, headers, _ = direct_app
    for _ in range(2):
        assert client.post("/api/cart/add", json=payload(), headers=headers).status_code == 200
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 2


@pytest.mark.parametrize("changes", [{"quantity": 2}, {"product_id": "AR-1201"}])
def test_same_id_with_changed_parameters_is_conflict(direct_app, changes):
    client, headers, _ = direct_app
    body = payload()
    assert client.post("/api/cart/add", json=body, headers=headers).status_code == 200
    assert client.post("/api/cart/add", json=body | changes, headers=headers).status_code == 409
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 1


def test_response_lost_after_commit_retries_without_another_add(
    direct_app, backend, monkeypatch, store
):
    client, headers, _ = direct_app
    original = backend.direct_add_once

    async def lose_response(*args):
        await original(*args)
        raise RuntimeError("response lost after commit")

    body = payload()
    monkeypatch.setattr(backend, "direct_add_once", lose_response)
    assert client.post("/api/cart/add", json=body, headers=headers).status_code == 500
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 1
    monkeypatch.setattr(backend, "direct_add_once", original)
    retry = client.post("/api/cart/add", json=body, headers=headers)
    assert retry.status_code == 200 and retry.json()["cart"]["item_count"] == 1
    assert len(store.require(headers["X-Session-Id"]).pending_app_events) == 1


def test_failure_before_commit_rolls_back_cart_result_and_note(
    direct_app, backend, database, monkeypatch, store
):
    client, headers, _ = direct_app
    original = backend._complete_direct_add

    def fail(*args):
        original(*args)
        raise RuntimeError("failure before commit")

    body = payload()
    monkeypatch.setattr(backend, "_complete_direct_add", fail)
    assert client.post("/api/cart/add", json=body, headers=headers).status_code == 500
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 0
    assert store.require(headers["X-Session-Id"]).pending_app_events == []
    with database[0].pool.connection() as connection:
        assert connection.execute("SELECT * FROM retail_cart_add_operations").fetchall() == []
    monkeypatch.setattr(backend, "_complete_direct_add", original)
    assert client.post("/api/cart/add", json=body, headers=headers).status_code == 200
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 1


def test_concurrent_retries_replay_one_result(direct_app, store):
    client, headers, _ = direct_app
    body = payload()
    with ThreadPoolExecutor(max_workers=4) as workers:
        responses = list(
            workers.map(
                lambda _: client.post("/api/cart/add", json=body, headers=headers), range(4)
            )
        )
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 1
    assert len(store.require(headers["X-Session-Id"]).pending_app_events) == 1


def test_id_is_scoped_to_session(direct_app, store, backend):
    client, headers, _ = direct_app
    other = store.start("demo-user")
    other.state.remember_products([backend.product("AR-1202")])
    store.save(other)
    body = payload()
    for session_headers in [headers, {"X-Session-Id": other.session_id}]:
        assert client.post("/api/cart/add", json=body, headers=session_headers).status_code == 200
        assert client.get("/api/cart", headers=session_headers).json()["item_count"] == 1


@pytest.mark.parametrize("operation_id", [None, "", "not-a-uuid"])
def test_operation_id_is_required_and_validated(direct_app, operation_id):
    client, headers, _ = direct_app
    body = payload()
    if operation_id is None:
        del body["operation_id"]
    else:
        body["operation_id"] = operation_id
    assert client.post("/api/cart/add", json=body, headers=headers).status_code == 422
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 0


def test_completed_operation_survives_new_pool_and_gate_state_change(
    direct_app, store, backend, database
):
    import asyncio

    from demo_common.postgres import PostgresDatabase
    from retail.api.postgres_retail import PostgresRetail

    client, headers, _ = direct_app
    body = payload()
    first = client.post("/api/cart/add", json=body, headers=headers)
    record = store.require(headers["X-Session-Id"])
    record.state.seen_products.clear()
    store.save(record)
    other = PostgresDatabase(database[1], schema=database[2])
    other.open()
    try:
        fresh = PostgresRetail(other, config=backend.config)
        status, response = asyncio.run(fresh.direct_add_once(record.session_id, body, "test note"))
        assert status == 200 and response == first.json()
    finally:
        other.close()


def test_cart_result_and_button_note_preserve_latest_session(
    direct_app, store, backend, monkeypatch
):
    client, headers, _ = direct_app
    original = backend.direct_add_once

    async def concurrent_session_write(*args):
        rival = store.require(headers["X-Session-Id"])
        rival.messages.append({"role": "user", "content": "concurrent turn"})
        rival.pending_app_events.append("existing note")
        store.save(rival)
        return await original(*args)

    monkeypatch.setattr(backend, "direct_add_once", concurrent_session_write)
    assert client.post("/api/cart/add", json=payload(), headers=headers).status_code == 200
    loaded = store.require(headers["X-Session-Id"])
    assert loaded.messages == [{"role": "user", "content": "concurrent turn"}]
    assert loaded.pending_app_events[0] == "existing note"
    assert len(loaded.pending_app_events) == 2


def test_unique_constraint_and_reset_cascade(direct_app, database, store):
    import psycopg

    client, headers, _ = direct_app
    assert client.post("/api/cart/add", json=payload(), headers=headers).status_code == 200
    with pytest.raises(psycopg.errors.UniqueViolation), database[0].pool.connection() as connection:
        connection.execute(
            "INSERT INTO retail_cart_add_operations SELECT * FROM retail_cart_add_operations"
        )
    store.delete(headers["X-Session-Id"])
    with database[0].pool.connection() as connection:
        assert connection.execute("SELECT * FROM retail_cart_add_operations").fetchall() == []


@pytest.mark.parametrize(
    "product_id, detail", [("AR-1902", "options"), ("AR-1902-FULL", "out of stock")]
)
def test_options_and_availability_are_not_bypassed(direct_app, store, backend, product_id, detail):
    client, headers, _ = direct_app
    record = store.require(headers["X-Session-Id"])
    record.state.remember_products([backend.product(product_id)])
    store.save(record)
    body = payload(product_id=product_id)
    first = client.post("/api/cart/add", json=body, headers=headers)
    assert first.status_code == 400 and detail in first.json()["detail"]
    assert client.post("/api/cart/add", json=body, headers=headers).json() == first.json()
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 0


def test_provenance_refusal_is_a_completed_result(direct_app, store):
    client, headers, _ = direct_app
    body = payload(product_id="AR-1203")
    first = client.post("/api/cart/add", json=body, headers=headers)
    assert first.status_code == 400 and "not in this session" in first.json()["detail"]
    record = store.require(headers["X-Session-Id"])
    # An unrelated provenance change must not turn this ID into a later write.
    record.state.seen_products["AR-1203"] = record.state.seen_products["AR-1202"].model_copy(
        update={"product_id": "AR-1203"}
    )
    store.save(record)
    replay = client.post("/api/cart/add", json=body, headers=headers)
    assert replay.status_code == 400 and replay.json() == first.json()
    assert client.get("/api/cart", headers=headers).json()["item_count"] == 0


def test_caps_and_actual_increment_in_note(direct_app, store):
    client, headers, _ = direct_app
    assert (
        client.post("/api/cart/add", json=payload(quantity=20), headers=headers).status_code == 200
    )
    body = payload(quantity=20)
    capped = client.post("/api/cart/add", json=body, headers=headers)
    assert capped.status_code == 200 and capped.json()["cart"]["item_count"] == 24
    assert "quantity 4." in store.require(headers["X-Session-Id"]).pending_app_events[-1]
    assert client.post("/api/cart/add", json=body, headers=headers).json() == capped.json()
    refused = client.post("/api/cart/add", json=payload(), headers=headers)
    assert refused.status_code == 400 and "limit" in refused.json()["detail"]


def test_default_quantity_and_uuid_spelling_have_same_identity(direct_app):
    client, headers, _ = direct_app
    body = payload()
    del body["quantity"]
    first = client.post("/api/cart/add", json=body, headers=headers)
    body["quantity"] = 1
    body["operation_id"] = body["operation_id"].upper()
    replay = client.post("/api/cart/add", json=body, headers=headers)
    assert first.status_code == replay.status_code == 200 and first.json() == replay.json()
    assert (
        client.post("/api/cart/add", json=body | {"extra": True}, headers=headers).status_code
        == 422
    )


IDEMPOTENT_WORKER = """
import asyncio, json, os, sys, time
from pathlib import Path
sys.path.insert(0, "examples")
from demo_common.postgres import PostgresDatabase
from retail.api.postgres_retail import PostgresRetail
from retail.api.agent_config import build_shopping_config
schema, session_id, operation_id, ready, go = sys.argv[1:]
database = PostgresDatabase(os.environ["COMMERCE_TEST_DATABASE_URL"], schema=schema)
database.open()
try:
    backend = PostgresRetail(database, config=build_shopping_config())
    Path(ready).touch()
    deadline = time.monotonic() + 15
    while not Path(go).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("worker barrier")
        time.sleep(0.01)
    result = asyncio.run(backend.direct_add_once(session_id, {"operation_id": operation_id, "product_id": "AR-1202", "quantity": 1}, "test note"))
    print(json.dumps(result))
finally:
    database.close()
"""


def test_two_processes_retry_one_operation(database, store, backend, tmp_path):
    import asyncio
    import json
    import subprocess
    import sys
    import time

    record = store.start("demo-user")
    record.state.remember_products([backend.product("AR-1202")])
    store.save(record)
    operation_id = str(uuid4())
    go = tmp_path / "go"
    workers, ready_files = [], []
    try:
        for index in range(2):
            ready = tmp_path / f"ready-{index}"
            ready_files.append(ready)
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        IDEMPOTENT_WORKER,
                        database[2],
                        record.session_id,
                        operation_id,
                        str(ready),
                        str(go),
                    ],
                    cwd=pg_tests.ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 20
        while not all(path.exists() for path in ready_files):
            if time.monotonic() > deadline or any(worker.poll() is not None for worker in workers):
                pytest.fail("workers did not reach the barrier")
            time.sleep(0.01)
        go.touch()
        results = []
        for worker in workers:
            output, error = worker.communicate(timeout=20)
            assert worker.returncode == 0, error
            results.append(json.loads(output))
        assert results[0] == results[1] and results[0][0] == 200
        assert results[0][1]["cart"]["item_count"] == 1
        assert asyncio.run(backend.get_cart(pg_tests.context(record))).item_count == 1
        assert store.require(record.session_id).pending_app_events == ["test note"]
        with database[0].pool.connection() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) AS n FROM retail_cart_add_operations"
                ).fetchone()["n"]
                == 1
            )
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate()
