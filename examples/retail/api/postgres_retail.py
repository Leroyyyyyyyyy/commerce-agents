"""The fixture retail backend with shared, transactionally capped PostgreSQL carts."""

from __future__ import annotations

import asyncio
from typing import Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

from demo_common.postgres import PostgresDatabase
from demo_common.storefront_fixtures import cart_line, unavailable_detail
from shopping_agent import (
    Cart,
    CartAddition,
    CartItem,
    ShoppingAgentConfig,
    ShoppingSessionContext,
    Unavailable,
)

from .mock_retail import MockRetail


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

    def _mutate(
        self,
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
        with self.database.pool.connection() as connection:
            # The parent exists even for an empty cart: locking a missing item would
            # not serialize two first inserts or two different products at the line cap.
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
            result = CartAddition(
                cart=self._cart(connection, session_id), quantity_added=added, refused=refused
            )
        # The connection context has committed before returning the confirmation.
        return result

    async def try_atomic_add_to_cart(
        self,
        session: ShoppingSessionContext,
        product_id: str,
        quantity: int,
        *,
        max_quantity: int,
        max_lines: int,
    ) -> CartAddition:
        if (
            max_quantity != self.config.max_quantity_per_item
            or max_lines != self.config.max_cart_lines
        ):
            raise ValueError("gate and backend cart limits must match")
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

    def reset_session(self, session_id: str) -> None:
        with self.database.pool.connection() as connection:
            connection.execute("DELETE FROM retail_carts WHERE session_id = %s", (session_id,))
