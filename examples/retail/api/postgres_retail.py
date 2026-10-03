"""The fixture retail backend with shared, transactionally capped PostgreSQL carts."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

from commerce_common.streaming import ToolOutcome
from demo_common.postgres import PostgresDatabase
from demo_common.storefront import add_button_note, direct_add_error
from demo_common.storefront_fixtures import cart_line, unavailable_detail
from shopping_agent import (
    Cart,
    CartAddition,
    CartItem,
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
    Unavailable,
)
from shopping_agent.fencing import STOREFRONT_FENCE
from shopping_agent.gates import gated_add_to_cart

from .mock_retail import MockRetail


class _TransactionalAdd:
    """Run the shared gate against an already open short transaction."""

    def __init__(self, backend: PostgresRetail, connection: Connection) -> None:
        self.backend = backend
        self.connection = connection
        self.quantity_added = 0

    async def try_atomic_add_to_cart(
        self,
        session: ShoppingSessionContext,
        product_id: str,
        quantity: int,
        *,
        max_quantity: int,
        max_lines: int,
    ) -> CartAddition:
        self.backend._validate_limits(max_quantity, max_lines)
        result = self.backend._mutate_cart(
            self.connection, session.session_id, product_id, quantity, "add"
        )
        self.quantity_added = result.quantity_added
        return result


class PostgresRetail(MockRetail):
    def __init__(self, database: PostgresDatabase, *, config: ShoppingAgentConfig) -> None:
        super().__init__()
        self.database = database
        self.config = config

    @staticmethod
    def _cart(connection: Connection, session_id: str) -> Cart:
        rows = connection.execute(
            "SELECT product, quantity FROM retail_cart_items WHERE session_id = %s ORDER BY product_id",
            (session_id,),
        ).fetchall()
        items = []
        for row in rows:
            items.append(CartItem.model_validate(row["product"] | {"quantity": row["quantity"]}))
        return Cart(items=items)

    def _read_cart(self, session_id: str) -> Cart:
        with self.database.pool.connection() as connection:
            return self._cart(connection, session_id)

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        return await asyncio.to_thread(self._read_cart, session.session_id)

    def _mutate_cart(
        self,
        connection: Connection,
        session_id: str,
        product_id: str,
        quantity: int,
        action: Literal["add", "update", "remove"],
    ) -> CartAddition:
        product = self.product(product_id)
        if action == "add":
            if product is None or product.has_options:
                raise KeyError(product_id)
            if not product.in_stock:
                raise Unavailable(unavailable_detail(product, self.listing_of(product_id)))
        max_quantity = self.config.max_quantity_per_item
        max_lines = self.config.max_cart_lines
        # Lock the parent even for an empty cart and for writes to different products.
        connection.execute(
            "INSERT INTO retail_carts (session_id, max_quantity, max_lines) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (session_id, max_quantity, max_lines),
        )
        limits = connection.execute(
            "SELECT max_quantity, max_lines FROM retail_carts WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if limits["max_quantity"] != max_quantity or limits["max_lines"] != max_lines:
            raise ValueError("cart limits differ from its persisted deployment limits")
        current = self._cart(connection, session_id)
        existing = next((item for item in current.items if item.product_id == product_id), None)
        added = 0
        refused = None
        if action == "add":
            previous = existing.quantity if existing else 0
            if existing is None and len(current.items) >= max_lines:
                refused = "full"
            else:
                added = min(max(1, quantity), max(0, max_quantity - previous))
                if added == 0:
                    refused = "limit"
                else:
                    line = cart_line(product, previous + added)
                    connection.execute(
                        "INSERT INTO retail_cart_items "
                        "(session_id, product_id, quantity, max_quantity, product) VALUES (%s, %s, %s, %s, %s) "
                        "ON CONFLICT (session_id, product_id) DO UPDATE SET "
                        "quantity = EXCLUDED.quantity, product = EXCLUDED.product",
                        (
                            session_id,
                            product_id,
                            line.quantity,
                            max_quantity,
                            Jsonb(line.model_dump(mode="json")),
                        ),
                    )
        elif action == "update" and existing is not None:
            connection.execute(
                "UPDATE retail_cart_items SET quantity = %s WHERE session_id = %s AND product_id = %s",
                (min(max(1, quantity), max_quantity), session_id, product_id),
            )
        elif action == "remove":
            connection.execute(
                "DELETE FROM retail_cart_items WHERE session_id = %s AND product_id = %s",
                (session_id, product_id),
            )
        return CartAddition(
            cart=self._cart(connection, session_id), quantity_added=added, refused=refused
        )

    def _mutate(
        self,
        session_id: str,
        product_id: str,
        quantity: int,
        action: Literal["add", "update", "remove"],
    ) -> CartAddition:
        with self.database.pool.connection() as connection:
            result = self._mutate_cart(connection, session_id, product_id, quantity, action)
        # Publish only after the connection context has committed.
        return result

    def _validate_limits(self, max_quantity: int, max_lines: int) -> None:
        if (
            max_quantity != self.config.max_quantity_per_item
            or max_lines != self.config.max_cart_lines
        ):
            raise ValueError("gate and backend cart limits must match")

    async def try_atomic_add_to_cart(
        self,
        session: ShoppingSessionContext,
        product_id: str,
        quantity: int,
        *,
        max_quantity: int,
        max_lines: int,
    ) -> CartAddition:
        self._validate_limits(max_quantity, max_lines)
        return await asyncio.to_thread(
            self._mutate, session.session_id, product_id, quantity, "add"
        )

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        result = await self.try_atomic_add_to_cart(
            session,
            product_id,
            quantity,
            max_quantity=self.config.max_quantity_per_item,
            max_lines=self.config.max_cart_lines,
        )
        return result.cart

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        result = await asyncio.to_thread(
            self._mutate, session.session_id, product_id, quantity, "update"
        )
        return result.cart

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        result = await asyncio.to_thread(self._mutate, session.session_id, product_id, 0, "remove")
        return result.cart

    @staticmethod
    def _complete_direct_add(
        connection: Connection,
        session_id: str,
        request: dict[str, Any],
        payload: dict[str, Any],
        status: int,
        response: dict[str, Any],
        document: dict[str, Any],
        note: str | None,
    ) -> None:
        connection.execute(
            "INSERT INTO retail_cart_add_operations "
            "(session_id, operation_id, payload, status, response) VALUES (%s, %s, %s, %s, %s)",
            (
                session_id,
                request["operation_id"],
                Jsonb(payload),
                status,
                Jsonb(response),
            ),
        )
        if note is not None:
            document["pending_app_events"].append(note)
            connection.execute(
                "UPDATE commerce_sessions SET version = version + 1, document = %s WHERE session_id = %s",
                (Jsonb(document), session_id),
            )

    def _direct_add_once(
        self, session_id: str, request: dict[str, Any], note_template: str
    ) -> tuple[int, dict[str, Any]]:
        with self.database.pool.connection() as connection:
            # Lock order: session then cart. No model/network wait while either is held.
            row = connection.execute(
                "SELECT document FROM commerce_sessions WHERE session_id = %s FOR UPDATE",
                (session_id,),
            ).fetchone()
            if row is None:
                return 401, {"detail": "Unknown session"}
            payload = {"product_id": request["product_id"], "quantity": request["quantity"]}
            completed = connection.execute(
                "SELECT payload, status, response FROM retail_cart_add_operations "
                "WHERE session_id = %s AND operation_id = %s",
                (session_id, request["operation_id"]),
            ).fetchone()
            if completed:
                if completed["payload"] != payload:
                    return 409, {
                        "detail": "Operation ID was already used with different parameters"
                    }
                return completed["status"], completed["response"]
            document = row["document"]
            state = ShoppingSessionState.model_validate(document["state"])
            transaction = _TransactionalAdd(self, connection)
            if not self.config.enable_cart:
                execution = ToolOutcome.error("This store does not offer a cart.")
            else:
                try:
                    execution = asyncio.run(
                        gated_add_to_cart(
                            backend=transaction,
                            config=self.config,
                            session=ShoppingSessionContext(
                                session_id=session_id, user_id=document["user_id"]
                            ),
                            state=state,
                            product_id=request["product_id"],
                            quantity=request["quantity"],
                        )
                    )
                except Unavailable as error:
                    execution = ToolOutcome.error(
                        STOREFRONT_FENCE.sanitize_text(str(error), max_chars=200)
                    )
            note = None
            if detail := direct_add_error(execution):
                status, response = 400, {"detail": detail}
            else:
                status = 200
                cart = next(
                    event.data["cart"] for event in execution.events if event.type == "cart_update"
                )
                response = {"ok": True, "cart": cart}
                note = add_button_note(
                    state.seen_products[request["product_id"]],
                    transaction.quantity_added,
                    note_template,
                )
            self._complete_direct_add(
                connection, session_id, request, payload, status, response, document, note
            )
        return status, response

    async def direct_add_once(
        self, session_id: str, request: dict[str, Any], note_template: str
    ) -> tuple[int, dict[str, Any]]:
        """Commit the direct button's cart, replay result and note in one transaction."""
        return await asyncio.to_thread(self._direct_add_once, session_id, request, note_template)

    def reset_session(self, session_id: str) -> None:
        with self.database.pool.connection() as connection:
            connection.execute("DELETE FROM retail_carts WHERE session_id = %s", (session_id,))
