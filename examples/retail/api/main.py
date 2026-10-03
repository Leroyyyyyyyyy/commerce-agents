# Copyright 2026 Anthropic PBC
# SPDX-License-Identifier: Apache-2.0

"""ACME retail example API: the mock retailer behind the shared storefront routes, the
merchant router under /api/merchant, and the retail-only routes below.

    uvicorn retail.api.main:app --app-dir examples --reload --port 8000

Memory here is file-backed (``data/.memory-store.json``, gitignored) and seeded once per
user, so what a shopper asks the store to remember, or to forget, survives a restart.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from commerce_common.memory import InMemoryMemoryStore, JsonFileMemoryStore
from demo_common import (
    REPO_ROOT,
    MemorySeeder,
    build_storefront_host,
    load_demo_env,
)
from shopping_agent import ProductDetails, ShoppingSessionState
from shopping_agent_runtime import ShoppingAgent

from .agent_config import build_shopping_config
from .cart_add import RetailCartAddRequest, RetailCartAdds
from .merchant import create_merchant_router
from .mock_retail import DATA_DIR, MockRetail

load_demo_env(DATA_DIR.parent)
PRODUCT_IMAGES = DATA_DIR.parent / "storefront-web" / "public" / "products"

shopping_config = build_shopping_config()
sessions = None
startup = []
shutdown = []
if database_url := os.environ.get("COMMERCE_DATABASE_URL"):
    # Optional imports: the in-memory demo needs no PostgreSQL dependencies or server.
    from demo_common.postgres import PostgresDatabase, PostgresSessionStore

    from .postgres_retail import PostgresRetail

    database = PostgresDatabase(database_url)
    backend = PostgresRetail(database, config=shopping_config)
    sessions = PostgresSessionStore(ShoppingSessionState, database)

    async def start_database() -> None:
        await asyncio.to_thread(database.open)
        await asyncio.to_thread(
            database.migrate,
            [
                REPO_ROOT / "examples/demo_common/migrations/001_sessions.sql",
                Path(__file__).parent / "migrations/002_carts.sql",
                Path(__file__).parent / "migrations/003_cart_add_operations.sql",
            ],
        )

    async def close_database() -> None:
        await asyncio.to_thread(database.close)

    startup.append(start_database)
    shutdown.append(close_database)
else:
    backend = MockRetail()

agent = ShoppingAgent(
    backend=backend,
    skills_dir=REPO_ROOT / "shopping-agent" / "skills",
    config=shopping_config,
    memory_store=JsonFileMemoryStore(DATA_DIR / ".memory-store.json"),
)


def product_detail(product: ProductDetails) -> dict:
    # Detail-panel enrichment only; the agent's tool results never carry it.
    return product.model_dump() | {
        "price_intelligence": backend.price_intelligence(product.product_id),
        "review_aspects": backend.review_aspects(product.product_id),
    }


host = build_storefront_host(
    title="ACME Retail demo API",
    example_root=DATA_DIR.parent,
    backend=backend,
    agent=agent,
    memory_seeder=MemorySeeder(
        DATA_DIR / "memory-seed.json", marker=DATA_DIR / ".memory-seeded.json"
    ),
    product_detail=product_detail,
    sessions=sessions,
    on_startup=startup,
    on_shutdown=shutdown,
)
app = host.app
app.include_router(create_merchant_router(backend, InMemoryMemoryStore()), prefix="/api/merchant")
# The merchant portal shows the storefront's listing photos, so the API serves them to both apps.
app.mount("/products", StaticFiles(directory=PRODUCT_IMAGES, check_dir=False), name="products")


cart_adds = RetailCartAdds(host)


@app.post("/api/cart/add")
async def cart_add(request: RetailCartAddRequest, record: host.CurrentSession) -> dict:
    return await cart_adds.add(record, request)
