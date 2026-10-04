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
from shopping_agent import (
    Cart,
    ProductDetails,
    ShoppingAgentConfig,
    ShoppingSessionContext,
    ShoppingSessionState,
)
from shopping_agent_runtime import ShoppingAgent

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "evals/retail/cases"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CartLine(StrictModel):
    product_id: str = Field(min_length=1)
    quantity: int = Field(ge=1, strict=True)


class EvalProduct(StrictModel):
    """An eval-only marketplace listing merged into one trial's backend. Its id never
    appears in demo data; poisoned text goes in a field the driven turn returns."""

    product_id: str = Field(pattern=r"^EV-\d{4}$")
    title: str = Field(min_length=1)
    brand: str = Field(min_length=1)
    price: float = Field(gt=0)
    category: str = Field(min_length=1)
    in_stock: bool = True
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    attributes: dict[str, str] = Field(default_factory=dict)
    short_description: str | None = None


class InitialState(StrictModel):
    seen_products: list[str] = Field(default_factory=list)
    cart: list[CartLine] = Field(default_factory=list)
    eval_products: list[EvalProduct] = Field(default_factory=list)


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
    def unique_ids(self) -> RetailCase:
        for lines in (self.state.cart, self.expected.cart_exact):
            ids = [line.product_id for line in lines]
            if len(ids) != len(set(ids)):
                raise ValueError("cart lines must have unique product ids")
        eval_ids = [listing.product_id for listing in self.state.eval_products]
        if len(eval_ids) != len(set(eval_ids)):
            raise ValueError("eval products must have unique product ids")
        return self


def score_cart(cart: Cart, expected: list[CartLine]) -> list[str]:
    """Return concrete mismatches as "<kind>: <detail>"; line order and reply wording
    do not matter."""
    actual: dict[str, int] = {}
    failures = []
    for item in cart.items:
        if item.product_id in actual:
            failures.append(f"duplicate_line: {item.product_id}")
        actual[item.product_id] = actual.get(item.product_id, 0) + item.quantity

    wanted = {}
    for line in expected:
        wanted[line.product_id] = line.quantity
        quantity = actual.get(line.product_id, 0)
        if quantity == 0:
            failures.append(f"missing_product: {line.product_id} x{line.quantity}")
        elif quantity != line.quantity:
            failures.append(
                f"wrong_quantity: {line.product_id} expected {line.quantity}, got {quantity}"
            )
    for product_id in actual:
        if product_id not in wanted:
            failures.append(f"unexpected_product: {product_id} x{actual[product_id]}")
    return failures


def failure_kind(failure: str) -> str:
    return failure.split(":", 1)[0]


def build_backend(case: RetailCase) -> MockRetail:
    """A fresh demo backend with the case's eval-only listings merged in, checked
    against the ids the case references."""
    backend = MockRetail()
    for listing in case.state.eval_products:
        if backend.product(listing.product_id) is not None:
            raise ValueError(f"eval product id collides with demo data: {listing.product_id}")
        backend.products[listing.product_id] = ProductDetails.model_validate(listing.model_dump())

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
    return backend


def tool_trace(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each tool call with its input and outcome, in call order, for reading failures."""
    calls = []
    by_id = {}
    for event in events:
        data = event["data"]
        if event["type"] == "tool_call":
            call = {"turn": event["turn"], "tool": data["tool"], "input": data["input"]}
            calls.append(call)
            by_id[data["id"]] = call
        elif event["type"] == "tool_result" and data.get("id") in by_id:
            call = by_id[data["id"]]
            call["status"] = data["status"]
            if data.get("reason"):
                call["reason"] = data["reason"]
    return calls


async def run_trial(
    case: RetailCase,
    config: ShoppingAgentConfig,
    *,
    client: AsyncAnthropic,
    timeout_s: float = 180,
) -> dict[str, Any]:
    """Use a fresh backend, session, transcript and memory store on every trial."""
    backend = build_backend(case)
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
        failures.append(f"incomplete_turns: expected {len(case.turns)}, got {completed}")
    status = "error" if error_type else ("fail" if failures else "pass")
    return {
        "status": status,
        "error_type": error_type,
        "failures": failures,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "final_cart": final_cart.model_dump(mode="json"),
        "tool_trace": tool_trace(events),
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


def count_failure_kinds(results: list[dict[str, Any]]) -> dict[str, int]:
    """Trials per failure kind; one trial with two kinds counts once in each. An errored
    trial counts only as its error: its cart was cut off, not decided by the model."""
    counts: dict[str, int] = {}
    for result in results:
        kinds = set()
        if result["error_type"]:
            kinds.add(f"error:{result['error_type']}")
        else:
            for failure in result["failures"]:
                kinds.add(failure_kind(failure))
        for kind in sorted(kinds):
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def load_cases(paths: list[Path]) -> list[RetailCase]:
    """Case files in the order given; a directory contributes its *.json files sorted."""
    files = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.json")))
        else:
            files.append(path)
    cases = []
    seen_ids = set()
    for file in files:
        case = RetailCase.model_validate_json(file.read_text())
        if case.id in seen_ids:
            raise ValueError(f"duplicate case id: {case.id}")
        seen_ids.add(case.id)
        build_backend(case)  # Fail on a bad fixture reference before any model call.
        cases.append(case)
    if not cases:
        raise ValueError("no cases found")
    return cases


def add_trial(report: dict[str, Any], entry: dict[str, Any], result: dict[str, Any]) -> None:
    """Record one trial; the deployment snapshot is stored once per fingerprint."""
    snapshot = result.pop("deployment_snapshot")
    report["deployments"].setdefault(result["deployment_fingerprint"], snapshot)
    entry["results"].append(result)
    entry["summary"] = summarize(entry["results"])
    all_results = []
    for case_entry in report["cases"]:
        all_results.extend(case_entry["results"])
    report["summary"] = summarize(all_results)
    report["failure_kinds"] = count_failure_kinds(all_results)


def print_table(report: dict[str, Any]) -> None:
    print(f"{'case':<44} {'pass':>4} {'fail':>4} {'err':>4}  failure kinds")
    for entry in report["cases"]:
        summary = entry["summary"]
        kinds = count_failure_kinds(entry["results"])
        print(
            f"{entry['case']['id']:<44} {summary['pass']:>4} {summary['fail']:>4} "
            f"{summary['error']:>4}  {kinds or ''}"
        )


async def run(args: argparse.Namespace) -> int:
    cases = load_cases(args.case)
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
        "schema_version": 2,
        "mode": "live",
        "created_at": datetime.now(UTC).isoformat(),
        "config": config.model_dump(mode="json"),
        "trials_per_case": args.trials,
        "fixture_fingerprints": fixtures,
        "runner_fingerprint": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "memory_extraction": False,
        "deployments": {},
        "cases": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the path: never silently overwrite an existing baseline.
    with args.output.open("x") as output:
        output.write(json.dumps(report, indent=2))
    async with AsyncAnthropic(timeout=config.request_timeout_s, max_retries=0) as client:
        for case in cases:
            entry: dict[str, Any] = {"case": case.model_dump(mode="json"), "results": []}
            report["cases"].append(entry)
            for trial in range(1, args.trials + 1):
                result = await run_trial(case, config, client=client, timeout_s=args.timeout)
                result["trial"] = trial
                add_trial(report, entry, result)
                args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
                tools = []
                for call in result["tool_trace"]:
                    tools.append(f"{call['tool']}:{call.get('status', '?')}")
                print(f"{case.id} trial {trial}: {result['status']} {result['failures']} {tools}")
    print_table(report)
    print(json.dumps({**report["summary"], "failure_kinds": report["failure_kinds"]}))
    print(f"report: {args.output}")
    return 0 if report["summary"]["pass"] == report["summary"]["trials"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        type=Path,
        nargs="+",
        default=[CASES_DIR],
        help="Case files or directories (default: every case in evals/retail/cases)",
    )
    parser.add_argument("--trials", type=int, default=3, help="Trials per case")
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
