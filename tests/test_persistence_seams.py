"""Persistence extension and safe stream-save behavior, with no database dependency."""

from __future__ import annotations

from typing import cast

import pytest

from demo_common.host import _save_streamed_turn, build_app
from demo_common.sessions import SessionStore
from shopping_agent import (
    Cart,
    CartAddition,
    CartItem,
    Product,
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
    StorefrontBackend,
)
from shopping_agent.gates import gated_add_to_cart


class AtomicBackend:
    calls = 0

    async def try_atomic_add_to_cart(self, session, product_id, quantity, **limits):
        self.calls += 1
        assert quantity == 20 and limits == {"max_quantity": 24, "max_lines": 100}
        cart = Cart(items=[CartItem(product_id="p-1", title="ACME item", price=1, quantity=24)])
        return CartAddition(cart=cart, quantity_added=4)

    async def get_cart(self, session):
        pytest.fail("atomic add must not do a separate read/check/write")

    async def add_to_cart(self, session, product_id, quantity):
        pytest.fail("atomic add must not also call the fallback write")


async def test_atomic_backend_confirmation_uses_actual_increment():
    backend = AtomicBackend()
    state = ShoppingSessionState()
    state.remember_products([Product(product_id="p-1", title="ACME item", price=1)])
    result = await gated_add_to_cart(
        backend=cast(StorefrontBackend, backend),
        config=ShoppingAgentConfig(),
        session=ShoppingSessionContext(session_id="s-1", user_id="demo-user"),
        state=state,
        product_id="p-1",
        quantity=20,
    )
    assert backend.calls == 1
    assert "x4" in result.result_text and "capped" in result.result_text


async def test_atomic_backend_is_not_called_without_provenance():
    backend = AtomicBackend()
    result = await gated_add_to_cart(
        backend=cast(StorefrontBackend, backend),
        config=ShoppingAgentConfig(),
        session=ShoppingSessionContext(session_id="s-1", user_id="demo-user"),
        state=ShoppingSessionState(),
        product_id="p-1",
        quantity=20,
    )
    assert result.blocked == "provenance" and backend.calls == 0


def test_stream_save_rebases_only_concurrent_notes():
    store = SessionStore(ShoppingSessionState)
    record = store.start("demo-user")
    record.messages.append({"role": "user", "content": "hello"})
    store.save(record)
    button = store.require(record.session_id)
    button.pending_app_events.append("button")
    store.save(button)
    record.state.remember_products([Product(product_id="p-1", title="ACME item", price=1)])
    record.messages.append({"role": "assistant", "content": "done"})
    _save_streamed_turn(store, record)
    loaded = store.require(record.session_id)
    assert loaded.messages == record.messages
    assert loaded.pending_app_events == ["button"]
    assert "p-1" in loaded.state.seen_products


def test_stream_save_does_not_overwrite_another_states_write():
    store = SessionStore(ShoppingSessionState)
    record = store.start("demo-user")
    rival = store.require(record.session_id)
    rival.state.remember_products([Product(product_id="p-2", title="ACME item", price=1)])
    store.save(rival)
    record.messages.append({"role": "assistant", "content": "done"})
    _save_streamed_turn(store, record)
    loaded = store.require(record.session_id)
    assert "p-2" in loaded.state.seen_products and loaded.messages == []


async def test_app_lifespan_runs_shutdown_on_success_and_startup_failure():
    calls = []

    async def start():
        calls.append("start")

    async def close():
        calls.append("close")

    app = build_app("ACME test", [start], [close])
    async with app.router.lifespan_context(app):
        assert calls == ["start"]
    assert calls == ["start", "close"]

    async def broken_start():
        raise RuntimeError("startup failed")

    app = build_app("ACME test", [start, broken_start], [close])
    with pytest.raises(RuntimeError, match="startup failed"):
        async with app.router.lifespan_context(app):
            pytest.fail("startup must not succeed")
    assert calls == ["start", "close", "start", "close"]
