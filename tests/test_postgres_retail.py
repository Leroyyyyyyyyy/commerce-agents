"""Real PostgreSQL integration tests, enabled only by COMMERCE_TEST_DATABASE_URL."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from commerce_common.testing import FakeClient, text_message, tool_use_message
from demo_common import MemorySeeder, build_storefront_host
from demo_common.host import _save_streamed_turn
from demo_common.sessions import SessionConflictError, UnknownSessionError
from retail.api.agent_config import build_shopping_config
from shopping_agent import ShoppingAgentConfig, ShoppingSessionContext, ShoppingSessionState
from shopping_agent.gates import gated_add_to_cart
from shopping_agent_runtime import ShoppingAgent

try:
    import psycopg
    from psycopg import sql

    from demo_common.postgres import PostgresDatabase, PostgresSessionStore
    from retail.api.postgres_retail import PostgresRetail
except ModuleNotFoundError as error:
    if error.name not in {"psycopg", "psycopg_pool"}:
        raise
    pytest.skip("install requirements-postgres.txt for PostgreSQL tests", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = [
    ROOT / "examples/demo_common/migrations/001_sessions.sql",
    ROOT / "examples/retail/api/migrations/002_carts.sql",
]


@pytest.fixture
def database():
    dsn = os.environ.get("COMMERCE_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("set COMMERCE_TEST_DATABASE_URL to run real PostgreSQL integration tests")
    schema = "commerce_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database = PostgresDatabase(dsn, schema=schema)
    try:
        database.open()
        database.migrate(MIGRATIONS)
        yield database, dsn, schema
    finally:
        database.close()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def store(database):
    return PostgresSessionStore(ShoppingSessionState, database[0])


@pytest.fixture
def backend(database):
    return PostgresRetail(database[0], config=build_shopping_config())


def context(record):
    return ShoppingSessionContext(session_id=record.session_id, user_id=record.user_id)


def test_state_and_transcript_round_trip_and_compaction(store, backend):
    record = store.start("demo-user")
    assert record.version == 1
    record.state.remember_products([backend.product("AR-1202")])
    record.messages = [{"role": "user", "content": "add it"}]
    record.pending_app_events = ["tapped"]
    store.save(record)
    assert record.version == 2
    loaded = store.require(record.session_id)
    # Provenance is typed as Product; reloading need not retain a ProductDetails subclass.
    assert loaded.state_document() == record.state_document()
    assert loaded.messages == record.messages and loaded.version == record.version
    store.save(record)
    assert record.version == 2
    record.messages[0]["content"] = "[cleared]"
    record.messages.append({"role": "assistant", "content": "done"})
    record.stored_messages = 0
    store.save(record)
    loaded = store.require(record.session_id)
    assert loaded.messages == record.messages
    assert "AR-1202" in loaded.state.seen_products
    assert store.session_ids_for_user("demo-user") == [record.session_id]


def test_stale_writer_cannot_write_state_or_transcript(store, backend):
    record = store.start("demo-user")
    winner = store.require(record.session_id)
    loser = store.require(record.session_id)
    winner.pending_app_events.append("button note")
    store.save(winner)
    loser.state.remember_products([backend.product("AR-1202")])
    loser.messages.append({"role": "user", "content": "stale"})
    with pytest.raises(SessionConflictError):
        store.save(loser)
    loaded = store.require(record.session_id)
    assert loaded.state.seen_products == {}
    assert loaded.messages == []
    assert loaded.pending_app_events == ["button note"]
    assert loser.version == 1


def test_failed_save_does_not_move_state_or_callers_version(store, backend):
    record = store.start("demo-user")
    record.state.remember_products([backend.product("AR-1202")])
    record.messages.append({"role": "user", "content": object()})
    with pytest.raises(TypeError):
        store.save(record)
    assert record.version == 1
    loaded = store.require(record.session_id)
    assert loaded.messages == [] and loaded.state.seen_products == {}


async def test_cart_survives_new_store_and_pool(database, store, backend):
    record = store.start("demo-user")
    record.messages.append({"role": "user", "content": "tent"})
    record.state.remember_products([backend.product("AR-1202")])
    store.save(record)
    await backend.add_to_cart(context(record), "AR-1202", 1)
    other = PostgresDatabase(database[1], schema=database[2])
    other.open()
    try:
        loaded = PostgresSessionStore(ShoppingSessionState, other).require(record.session_id)
        cart = await PostgresRetail(other, config=build_shopping_config()).get_cart(context(loaded))
        assert loaded.messages == record.messages
        assert cart.items[0].product_id == "AR-1202" and cart.item_count == 1
    finally:
        other.close()


async def test_reset_cascades_cart_and_cannot_resurrect_session(store, backend, database):
    record = store.start("demo-user")
    stale = store.require(record.session_id)
    await backend.add_to_cart(context(record), "AR-1202", 1)
    store.reset(record)
    store.save(record)
    with pytest.raises(UnknownSessionError):
        store.require(record.session_id)
    stale.messages.append({"role": "user", "content": "late"})
    with pytest.raises(SessionConflictError):
        store.save(stale)
    with database[0].pool.connection() as connection:
        assert connection.execute("SELECT * FROM retail_carts").fetchall() == []
        assert connection.execute("SELECT * FROM retail_cart_items").fetchall() == []


async def test_db_constraint_refuses_excess_quantity(store, backend, database):
    record = store.start("demo-user")
    await backend.add_to_cart(context(record), "AR-1202", 1)
    with pytest.raises(psycopg.errors.CheckViolation), database[0].pool.connection() as connection:
        connection.execute(
            "UPDATE retail_cart_items SET quantity = 25 WHERE session_id = %s", (record.session_id,)
        )
    assert (await backend.get_cart(context(record))).item_count == 1


async def test_gate_reports_actual_increment_and_limit(store, backend):
    record = store.start("demo-user")
    record.state.remember_products([backend.product("AR-1202")])
    await backend.add_to_cart(context(record), "AR-1202", 20)
    result = await gated_add_to_cart(
        backend=backend,
        config=backend.config,
        session=context(record),
        state=record.state,
        product_id="AR-1202",
        quantity=20,
    )
    assert "x4" in result.result_text and "capped" in result.result_text
    assert (await backend.get_cart(context(record))).item_count == 24
    refused = await gated_add_to_cart(
        backend=backend,
        config=backend.config,
        session=context(record),
        state=record.state,
        product_id="AR-1202",
        quantity=1,
    )
    assert refused.is_error


async def test_provenance_is_checked_before_database_write(store, backend):
    record = store.start("demo-user")
    result = await gated_add_to_cart(
        backend=backend,
        config=backend.config,
        session=context(record),
        state=record.state,
        product_id="AR-1202",
        quantity=1,
    )
    assert result.blocked == "provenance"
    assert (await backend.get_cart(context(record))).items == []


async def test_update_remove_and_deployment_limit_mismatch(store, backend, database):
    record = store.start("demo-user")
    session = context(record)
    await backend.add_to_cart(session, "AR-1202", 1)
    assert (await backend.update_cart_item(session, "AR-1202", 99)).item_count == 24
    assert (await backend.update_cart_item(session, "AR-1201", 1)).item_count == 24
    changed = PostgresRetail(database[0], config=ShoppingAgentConfig(max_quantity_per_item=10))
    with pytest.raises(ValueError, match="persisted deployment limits"):
        await changed.add_to_cart(session, "AR-1202", 1)
    assert (await backend.remove_from_cart(session, "AR-1202")).items == []


def test_stream_save_preserves_concurrent_button_note(store):
    record = store.start("demo-user")
    record.messages.append({"role": "user", "content": "hello"})
    store.save(record)  # The request dependency's pre-stream save.
    button = store.require(record.session_id)
    button.pending_app_events.append("button during turn")
    store.save(button)
    record.messages.append({"role": "assistant", "content": "done"})
    _save_streamed_turn(store, record)
    loaded = store.require(record.session_id)
    assert loaded.messages == record.messages
    assert loaded.pending_app_events == ["button during turn"]


def test_stream_save_does_not_overwrite_competing_turn(store):
    record = store.start("demo-user")
    record.messages.append({"role": "user", "content": "first"})
    store.save(record)
    rival = store.require(record.session_id)
    rival.messages.append({"role": "user", "content": "second"})
    store.save(rival)
    record.messages.append({"role": "assistant", "content": "first reply"})
    _save_streamed_turn(store, record)
    assert store.require(record.session_id).messages == rival.messages


async def test_messages_api_turn_uses_postgres_cart_and_can_be_saved(store, backend):
    record = store.start("demo-user")
    record.state.remember_products([backend.product("AR-1202")])
    record.messages.append({"role": "user", "content": "Add AR-1202."})
    client = FakeClient(
        [
            tool_use_message("add_to_cart", {"product_id": "AR-1202", "quantity": 1}),
            text_message("Added."),
        ]
    )
    agent = ShoppingAgent(backend=backend, config=backend.config, client=client)
    events = [
        event async for event in agent.stream_turn(record.messages, context(record), record.state)
    ]
    store.save(record)
    assert events[-1].type == "turn_complete"
    assert (await backend.get_cart(context(record))).item_count == 1
    assert store.require(record.session_id).messages == record.messages


async def test_failure_before_commit_rolls_back_cart_write(store, backend, monkeypatch):
    record = store.start("demo-user")
    original = backend._cart
    calls = 0

    def fail_after_write(connection, session_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("failure before commit")
        return original(connection, session_id)

    monkeypatch.setattr(backend, "_cart", fail_after_write)
    with pytest.raises(RuntimeError, match="before commit"):
        await backend.add_to_cart(context(record), "AR-1202", 1)
    monkeypatch.setattr(backend, "_cart", original)
    assert (await backend.get_cart(context(record))).items == []


async def test_cancelling_thread_wait_does_not_cancel_its_database_write(
    store, backend, monkeypatch
):
    record = store.start("demo-user")
    original = backend._mutate
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed_write(*args):
        started.set()
        if not release.wait(5):
            raise TimeoutError("test release")
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(backend, "_mutate", delayed_write)
    task = asyncio.create_task(backend.add_to_cart(context(record), "AR-1202", 1))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert (await backend.get_cart(context(record))).item_count == 1


async def test_replayed_add_is_not_yet_idempotent(store, backend):
    record = store.start("demo-user")
    await backend.add_to_cart(context(record), "AR-1202", 1)
    await backend.add_to_cart(context(record), "AR-1202", 1)
    assert (await backend.get_cart(context(record))).item_count == 2


def test_http_session_chat_cart_and_reset_use_injected_store(store, backend, tmp_path):
    config = backend.config.model_copy(update={"enable_memory": False})
    agent = ShoppingAgent(
        backend=backend,
        config=config,
        client=FakeClient(
            [
                tool_use_message("add_to_cart", {"product_id": "AR-1202", "quantity": 1}),
                text_message("Added."),
            ]
        ),
    )
    host = build_storefront_host(
        title="ACME persistence test",
        example_root=tmp_path,
        backend=backend,
        agent=agent,
        sessions=store,
        memory_seeder=MemorySeeder(tmp_path / "empty-seed.json"),
    )
    assert host.sessions is store
    with TestClient(host.app, base_url="http://localhost") as client:
        session_id = client.post("/api/session", json={"user_id": "demo-user"}).json()["session_id"]
        headers = {"X-Session-Id": session_id}
        record = store.require(session_id)
        record.state.remember_products([backend.product("AR-1202")])
        store.save(record)
        response = client.post("/api/chat", json={"message": "Add AR-1202."}, headers=headers)
        assert response.status_code == 200 and "turn_complete" in response.text
        assert client.get("/api/cart", headers=headers).json()["item_count"] == 1
        assert store.require(session_id).messages[-1]["role"] == "assistant"
        response = client.post("/api/reset", json={}, headers=headers)
        assert response.status_code == 200
        assert client.get("/api/cart", headers=headers).status_code == 401
        fresh_id = response.json()["session_id"]
        assert client.get("/api/cart", headers={"X-Session-Id": fresh_id}).json()["item_count"] == 0


def test_migrations_are_repeatable_but_applied_sql_cannot_be_edited(database, tmp_path):
    database[0].migrate(MIGRATIONS)
    changed = tmp_path / "001_sessions.sql"
    changed.write_text(MIGRATIONS[0].read_text() + "\n-- changed\n")
    with pytest.raises(ValueError, match="applied migration changed"):
        database[0].migrate([changed])


# Independent interpreter processes, each with its own pool and no shared asyncio.Lock.
WORKER = """
import asyncio, json, os, sys, time
from pathlib import Path
sys.path.insert(0, "examples")
from demo_common.postgres import PostgresDatabase, PostgresSessionStore
from retail.api.postgres_retail import PostgresRetail
from shopping_agent import ShoppingAgentConfig, ShoppingSessionContext, ShoppingSessionState
schema, session_id, product_id, max_lines, ready, go = sys.argv[1:]
database = PostgresDatabase(os.environ["COMMERCE_TEST_DATABASE_URL"], schema=schema)
database.open()
try:
    backend = PostgresRetail(database, config=ShoppingAgentConfig(max_cart_lines=int(max_lines)))
    record = PostgresSessionStore(ShoppingSessionState, database).require(session_id)
    session = ShoppingSessionContext(session_id=session_id, user_id=record.user_id)
    if product_id == "read":
        cart = asyncio.run(backend.get_cart(session))
        print(json.dumps({"messages": record.messages, "seen": list(record.state.seen_products), "cart": cart.model_dump()}))
    else:
        Path(ready).touch()
        deadline = time.monotonic() + 15
        while not Path(go).exists():
            if time.monotonic() > deadline:
                raise TimeoutError("worker barrier")
            time.sleep(0.01)
        result = asyncio.run(backend.try_atomic_add_to_cart(session, product_id, 20, max_quantity=24, max_lines=int(max_lines)))
        print(result.model_dump_json())
finally:
    database.close()
"""


def test_new_interpreter_restores_saved_session_and_cart(database, store, backend):
    record = store.start("demo-user")
    record.state.remember_products([backend.product("AR-1202")])
    record.messages.append({"role": "user", "content": "saved"})
    store.save(record)
    asyncio.run(backend.add_to_cart(context(record), "AR-1202", 1))
    output = subprocess.run(
        [sys.executable, "-c", WORKER, database[2], record.session_id, "read", "100", "", ""],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    restored = json.loads(output.stdout)
    assert restored["messages"] == record.messages
    assert restored["seen"] == ["AR-1202"]
    assert restored["cart"]["items"][0]["quantity"] == 1


@pytest.mark.parametrize("different_products", [False, True])
def test_two_processes_cannot_exceed_quantity_or_line_cap(
    database, store, tmp_path, different_products
):
    record = store.start("demo-user")
    max_lines = 1 if different_products else 100
    product_ids = ["AR-1202", "AR-1201" if different_products else "AR-1202"]
    go = tmp_path / "go"
    ready_files = []
    workers = []
    try:
        for index, product_id in enumerate(product_ids):
            ready = tmp_path / f"ready-{index}"
            ready_files.append(ready)
            workers.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        WORKER,
                        database[2],
                        record.session_id,
                        product_id,
                        str(max_lines),
                        str(ready),
                        str(go),
                    ],
                    cwd=ROOT,
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
        if different_products:
            assert sorted(result["quantity_added"] for result in results) == [0, 20]
            assert any(result["refused"] == "full" for result in results)
        else:
            assert sorted(result["quantity_added"] for result in results) == [4, 20]
        backend = PostgresRetail(database[0], config=ShoppingAgentConfig(max_cart_lines=max_lines))
        cart = asyncio.run(backend.get_cart(context(record)))
        assert len(cart.items) == 1
        assert cart.item_count == (20 if different_products else 24)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
            worker.communicate()
