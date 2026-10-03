"""Run isolated retail Messages API trials and grade the authoritative final cart."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ConfigDict, Field, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from commerce_common.memory import InMemoryMemoryStore
from demo_common import load_demo_env
from retail.api.agent_config import build_shopping_config
from retail.api.mock_retail import DATA_DIR, MockRetail
from shopping_agent import Cart, ShoppingAgentConfig, ShoppingSessionContext, ShoppingSessionState
from shopping_agent_runtime import ShoppingAgent

ROOT = Path(__file__).resolve().parents[1]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CartLine(StrictModel):
    product_id: str = Field(min_length=1)
    quantity: int = Field(ge=1, strict=True)


class InitialState(StrictModel):
    seen_products: list[str] = Field(default_factory=list)
    cart: list[CartLine] = Field(default_factory=list)


class Expected(StrictModel):
    # An exact cart, not just a subset of ids or a total unit count.
    cart_exact: list[CartLine]


class RetailCase(StrictModel):
    id: str = Field(min_length=1)
    priority: Literal["critical", "high", "medium", "low"]
    difficulty: Literal["easy", "medium", "hard"]
    tags: list[str]
    state: InitialState
    turns: list[str] = Field(min_length=1)
    expected: Expected
    notes: str

    @model_validator(mode="after")
    def unique_cart_ids(self) -> RetailCase:
        for lines in (self.state.cart, self.expected.cart_exact):
            ids = [line.product_id for line in lines]
            if len(ids) != len(set(ids)):
                raise ValueError("cart lines must have unique product ids")
        return self


def score_cart(cart: Cart, expected: list[CartLine]) -> list[str]:
    """Return concrete mismatches; line order and reply wording do not matter."""
    actual: dict[str, int] = {}
    failures = []
    for item in cart.items:
        if item.product_id in actual:
            failures.append(f"duplicate cart line: {item.product_id}")
        actual[item.product_id] = actual.get(item.product_id, 0) + item.quantity

    wanted = {}
    for line in expected:
        wanted[line.product_id] = line.quantity
        quantity = actual.get(line.product_id, 0)
        if quantity != line.quantity:
            failures.append(f"{line.product_id}: expected quantity {line.quantity}, got {quantity}")
    for product_id in actual:
        if product_id not in wanted:
            failures.append(f"unexpected product: {product_id}")
    return failures


def validate_fixture(case: RetailCase, backend: MockRetail) -> None:
    ids = list(case.state.seen_products)
    for line in (*case.state.cart, *case.expected.cart_exact):
        ids.append(line.product_id)
    for product_id in ids:
        if backend.product(product_id) is None:
            raise ValueError(f"case references missing fixture product: {product_id}")
    for line in case.state.cart:
        product = backend.product(line.product_id)
        if product.has_options or not product.in_stock:
            raise ValueError(f"initial cart product is not purchasable: {line.product_id}")


async def run_trial(
    case: RetailCase,
    config: ShoppingAgentConfig,
    *,
    client: AsyncAnthropic,
    timeout_s: float = 180,
) -> dict[str, Any]:
    """Use a fresh backend, session, transcript and memory store on every trial."""
    backend = MockRetail()
    validate_fixture(case, backend)
    session = ShoppingSessionContext(session_id=uuid4().hex, user_id="eval-shopper")
    state = ShoppingSessionState()
    for product_id in case.state.seen_products:
        state.remember_products([backend.product(product_id)])
    for line in case.state.cart:
        await backend.add_to_cart(session, line.product_id, line.quantity)
    agent = ShoppingAgent(
        backend=backend,
        skills_dir=ROOT / "shopping-agent" / "skills",
        config=config,
        memory_store=InMemoryMemoryStore(),
        client=client,
    )
    # Capture the effective catalog, including the demo's date-stamped attributes.
    catalog = {}
    for product_id, product in (backend.products | backend.variants).items():
        catalog[product_id] = product.model_dump(mode="json")
    skills = {}
    for name in agent.skills.names:
        skills[name] = agent.skills.get_instructions(name)
    deployment = {
        "system": agent._static_system,
        "tools": agent._tools,
        "skills": skills,
        "catalog": catalog,
    }
    events: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    completed = 0
    error_type = None
    started = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            for turn_index, text in enumerate(case.turns):
                messages.append({"role": "user", "content": text})
                turn_completed = False
                async for event in agent.stream_turn(messages, session, state):
                    events.append({"turn": turn_index, **event.model_dump(mode="json")})
                    if event.type == "turn_complete":
                        turn_completed = True
                completed += int(turn_completed)
    except Exception as exc:
        # Do not serialize exception text: SDK errors may contain endpoint or request data.
        error_type = type(exc).__name__

    final_cart = await backend.get_cart(session)
    failures = score_cart(final_cart, case.expected.cart_exact)
    if completed != len(case.turns):
        failures.append(f"expected {len(case.turns)} completed turns, got {completed}")
    status = "error" if error_type else ("fail" if failures else "pass")
    return {
        "status": status,
        "error_type": error_type,
        "failures": failures,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "final_cart": final_cart.model_dump(mode="json"),
        "events": events,
        "deployment_fingerprint": fingerprint(deployment),
        "deployment_snapshot": deployment,
    }


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"pass": 0, "fail": 0, "error": 0}
    for result in results:
        counts[result["status"]] += 1
    return {
        **counts,
        "trials": len(results),
        "pass_rate": counts["pass"] / len(results) if results else None,
    }


async def run(args: argparse.Namespace) -> int:
    case = RetailCase.model_validate_json(args.case.read_text())
    validate_fixture(case, MockRetail())
    config = build_shopping_config()
    if args.model:
        config = config.model_copy(update={"model": args.model})
    # Match demo credential loading, without importing the demo app or its file memory.
    load_demo_env(DATA_DIR.parent)
    fixtures = {}
    for name in (
        "catalog.json",
        "users.json",
        "orders.json",
        "policies.json",
        "merchant_inventory.json",
    ):
        fixtures[name] = hashlib.sha256((DATA_DIR / name).read_bytes()).hexdigest()
    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "live",
        "created_at": datetime.now(UTC).isoformat(),
        "case": case.model_dump(mode="json"),
        "config": config.model_dump(mode="json"),
        "fixture_fingerprints": fixtures,
        "runner_fingerprint": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "memory_extraction": False,
        "results": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the path: never silently overwrite an existing baseline.
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2))
    async with AsyncAnthropic(timeout=config.request_timeout_s, max_retries=0) as client:
        for trial in range(1, args.trials + 1):
            result = await run_trial(case, config, client=client, timeout_s=args.timeout)
            result["trial"] = trial
            report["results"].append(result)
            report["summary"] = summarize(report["results"])
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            print(f"trial {trial}: {result['status']} {result['failures']}")
    print(json.dumps(report["summary"]))
    print(f"report: {args.output}")
    return 0 if report["summary"]["pass"] == args.trials else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", type=Path, default=ROOT / "evals/retail/cases/add-known-product.json"
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--model", help="Override the retail deployment model")
    parser.add_argument("--timeout", type=float, default=180, help="Seconds per whole trial")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    parser.add_argument("--output", type=Path, default=ROOT / f"evals/retail/reports/{stamp}.json")
    args = parser.parse_args()
    if args.trials < 1 or args.timeout <= 0:
        parser.error("--trials and --timeout must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
