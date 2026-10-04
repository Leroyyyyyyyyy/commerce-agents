"""Deterministic tests for the eval harness, not model behavior trials."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from pydantic import ValidationError
from scripts.eval_retail import (
    CASES_DIR,
    CartLine,
    RetailCase,
    build_backend,
    count_failure_kinds,
    load_cases,
    run_trial,
    score_cart,
    summarize,
)

from commerce_common.streaming import AgentEvent
from commerce_common.testing import FakeClient, text_message, tool_use_message
from retail.api.agent_config import build_shopping_config
from shopping_agent import Cart, CartItem

CASE_PATH = CASES_DIR / "cart-001-add-known-product.json"


@pytest.fixture
def case() -> RetailCase:
    return RetailCase.model_validate_json(CASE_PATH.read_text())


def cart_of(lines: list[tuple[str, int]]) -> Cart:
    items = []
    for product_id, quantity in lines:
        items.append(
            CartItem(product_id=product_id, title="ACME item", price=10, quantity=quantity)
        )
    return Cart(items=items)


@pytest.mark.parametrize(
    ("lines", "passed"),
    [
        ([("AR-1202", 1)], True),
        ([], False),
        ([("AR-1201", 1)], False),  # Same total count, wrong product (also a tent).
        ([("AR-1202", 2)], False),
        ([("AR-1202", 1), ("AR-1201", 1)], False),
        ([("AR-1202", 1), ("AR-1202", 1)], False),
    ],
)
def test_exact_cart_grader(case, lines, passed):
    assert (score_cart(cart_of(lines), case.expected.cart_exact) == []) is passed


def test_cart_order_does_not_matter():
    expected = [
        CartLine(product_id="AR-1201", quantity=2),
        CartLine(product_id="AR-1202", quantity=1),
    ]
    assert score_cart(cart_of([("AR-1202", 1), ("AR-1201", 2)]), expected) == []


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True, "1"])
def test_invalid_expected_quantity_is_rejected(quantity):
    with pytest.raises(ValidationError):
        CartLine(product_id="AR-1202", quantity=quantity)


def test_unknown_expectation_is_rejected(case):
    data = case.model_dump()
    data["expected"]["cart_excat"] = []
    with pytest.raises(ValidationError):
        RetailCase.model_validate(data)


def test_duplicate_expected_ids_are_rejected(case):
    data = case.model_dump()
    data["expected"]["cart_exact"] *= 2
    with pytest.raises(ValidationError):
        RetailCase.model_validate(data)


async def test_scripted_turn_executes_real_gate_and_backend(case):
    original = copy.deepcopy(case)
    client = FakeClient(
        [
            tool_use_message("add_to_cart", {"product_id": "AR-1202", "quantity": 1}),
            text_message("Added."),
        ]
    )
    result = await run_trial(case, build_shopping_config(), client=cast(Any, client))
    assert result["status"] == "pass"
    assert result["final_cart"]["items"][0]["product_id"] == "AR-1202"
    assert len(client.calls) == 2
    assert any(event["type"] == "cart_update" for event in result["events"])
    assert case == original
    # A new trial must not inherit the prior cart or transcript.
    second = await run_trial(
        case, build_shopping_config(), client=cast(Any, FakeClient([text_message("Done.")]))
    )
    assert second["status"] == "fail"
    assert second["final_cart"]["items"] == []
    assert second["deployment_fingerprint"] == result["deployment_fingerprint"]
    json.dumps(result)  # Reports are JSON serializable.


async def test_correct_events_do_not_rescue_wrong_final_cart(case):
    client = FakeClient(
        [
            tool_use_message("add_to_cart", {"product_id": "AR-1202", "quantity": 2}),
            text_message("Added one."),
        ]
    )
    result = await run_trial(case, build_shopping_config(), client=cast(Any, client))
    types = {event["type"] for event in result["events"]}
    assert {"tool_call", "cart_update", "turn_complete"} <= types
    assert result["status"] == "fail"
    assert result["failures"] == ["wrong_quantity: AR-1202 expected 1, got 2"]


async def test_partial_success_with_stream_error_is_not_a_pass(case):
    # The add commits, then the scripted client runs out before turn_complete.
    client = FakeClient([tool_use_message("add_to_cart", {"product_id": "AR-1202", "quantity": 1})])
    result = await run_trial(case, build_shopping_config(), client=cast(Any, client))
    assert result["final_cart"]["items"][0]["quantity"] == 1
    assert result["status"] == "error"
    assert result["error_type"] == "AssertionError"
    assert "incomplete_turns: expected 1, got 0" in result["failures"]


async def test_unknown_fixture_id_fails_before_model_call(case):
    data = case.model_dump()
    data["state"]["seen_products"] = ["MISSING-ID"]
    client = FakeClient([])
    with pytest.raises(ValueError, match="missing fixture product"):
        await run_trial(
            RetailCase.model_validate(data), build_shopping_config(), client=cast(Any, client)
        )
    assert client.calls == []


async def test_fabricated_cart_event_cannot_pass(case, monkeypatch):
    from scripts import eval_retail

    async def fake_turn(self, messages, session, state):
        yield AgentEvent.cart_update(cart_of([("AR-1202", 1)]).model_dump())
        yield AgentEvent.turn_complete("end_turn", {}, 0, 0)

    monkeypatch.setattr(eval_retail.ShoppingAgent, "stream_turn", fake_turn)
    result = await run_trial(case, build_shopping_config(), client=cast(Any, FakeClient([])))
    assert result["status"] == "fail"
    assert result["final_cart"]["items"] == []


async def test_timeout_is_an_execution_error(case, monkeypatch):
    from scripts import eval_retail

    async def slow_turn(self, messages, session, state):
        await asyncio.sleep(1)
        yield AgentEvent.turn_complete("end_turn", {}, 0, 0)

    monkeypatch.setattr(eval_retail.ShoppingAgent, "stream_turn", slow_turn)
    result = await run_trial(
        case, build_shopping_config(), client=cast(Any, FakeClient([])), timeout_s=0.01
    )
    assert result["status"] == "error"
    assert result["error_type"] == "TimeoutError"


async def test_report_is_saved_and_baseline_is_not_overwritten(tmp_path, monkeypatch):
    from scripts import eval_retail

    client = FakeClient(
        [
            tool_use_message("add_to_cart", {"product_id": "AR-1202", "quantity": 1}),
            text_message("Added."),
            text_message("Nothing added."),
        ]
    )

    @asynccontextmanager
    async def fake_client(**kwargs):
        assert kwargs["max_retries"] == 0
        yield client

    monkeypatch.setattr(eval_retail, "AsyncAnthropic", fake_client)
    monkeypatch.setattr(eval_retail, "load_demo_env", lambda _: None)
    args = argparse.Namespace(
        case=[CASE_PATH], model=None, trials=2, timeout=10, output=tmp_path / "report.json"
    )
    assert await eval_retail.run(args) == 1
    report = json.loads(args.output.read_text())
    assert report["summary"] == {"pass": 1, "fail": 1, "error": 0, "trials": 2, "pass_rate": 0.5}
    assert report["failure_kinds"] == {"missing_product": 1}
    assert report["memory_extraction"] is False
    results = report["cases"][0]["results"]
    # Both trials share one deployment, stored once rather than per trial.
    assert len(report["deployments"]) == 1
    assert report["deployments"][results[0]["deployment_fingerprint"]]["skills"]
    assert "deployment_snapshot" not in results[0]
    baseline = args.output.read_bytes()
    with pytest.raises(FileExistsError):
        await eval_retail.run(args)
    assert args.output.read_bytes() == baseline


def test_report_keeps_execution_errors_separate():
    results = [{"status": "pass"}, {"status": "fail"}, {"status": "error"}]
    assert summarize(results) == {"pass": 1, "fail": 1, "error": 1, "trials": 3, "pass_rate": 1 / 3}
    assert summarize([])["pass_rate"] is None


def test_shipped_cases_load_and_reference_real_fixtures():
    cases = load_cases([CASES_DIR])
    assert len(cases) >= 10
    for case in cases:
        assert case.id.startswith("cart-")
        assert (CASES_DIR / f"{case.id}.json").exists()


def test_every_failure_has_a_kind():
    expected = [
        CartLine(product_id="AR-1202", quantity=1),
        CartLine(product_id="AR-1206", quantity=2),
    ]
    cart = cart_of([("AR-1206", 3), ("AR-1201", 1), ("AR-1201", 1)])
    failures = score_cart(cart, expected)
    kinds = [failure.split(":", 1)[0] for failure in failures]
    assert sorted(kinds) == [
        "duplicate_line",
        "missing_product",
        "unexpected_product",
        "wrong_quantity",
    ]


def test_failure_kinds_count_trials_not_lines():
    results = [
        {"failures": ["unexpected_product: A x1", "unexpected_product: B x1"], "error_type": None},
        {"failures": ["incomplete_turns: expected 1, got 0"], "error_type": "TimeoutError"},
        {"failures": [], "error_type": None},
    ]
    # The errored trial's cart diff is not counted as a model failure.
    assert count_failure_kinds(results) == {"unexpected_product": 1, "error:TimeoutError": 1}


def eval_listing(product_id: str = "EV-9001") -> dict[str, Any]:
    return {
        "product_id": product_id,
        "title": "Camp Lantern",
        "brand": "ACME Marketplace seller 1",
        "price": 20.0,
        "category": "outdoor-camping",
        "short_description": "A lantern.",
    }


def test_eval_products_join_one_trial_backend_only(case):
    data = case.model_dump()
    data["state"]["eval_products"] = [eval_listing()]
    data["expected"]["cart_exact"] = [{"product_id": "EV-9001", "quantity": 1}]
    backend = build_backend(RetailCase.model_validate(data))
    assert backend.product("EV-9001").title == "Camp Lantern"
    # The next trial's backend starts from demo data again.
    assert build_backend(case).product("EV-9001") is None


@pytest.mark.parametrize("product_id", ["AR-1202", "ar-1202"])
def test_eval_product_cannot_shadow_demo_data(case, product_id):
    data = case.model_dump()
    listing = eval_listing()
    data["state"]["eval_products"] = [listing]
    validated = RetailCase.model_validate(data)
    # The id pattern keeps eval ids apart; a forced collision still fails the build.
    validated.state.eval_products[0].product_id = product_id
    with pytest.raises(ValueError, match="collides with demo data"):
        build_backend(validated)


def test_eval_product_id_pattern_is_enforced(case):
    data = case.model_dump()
    data["state"]["eval_products"] = [eval_listing("AR-9001")]
    with pytest.raises(ValidationError):
        RetailCase.model_validate(data)


async def test_tool_trace_pairs_calls_with_gate_outcomes(case):
    client = FakeClient(
        [
            tool_use_message("add_to_cart", {"product_id": "AR-1403", "quantity": 1}),
            tool_use_message("add_to_cart", {"product_id": "AR-1202", "quantity": 1}),
            text_message("Added."),
        ]
    )
    result = await run_trial(case, build_shopping_config(), client=cast(Any, client))
    trace = result["tool_trace"]
    assert [call["input"]["product_id"] for call in trace] == ["AR-1403", "AR-1202"]
    assert trace[0]["status"] == "blocked"
    assert trace[0]["reason"] == "provenance"
    assert trace[1]["status"] == "ok"


async def test_multi_case_report_has_per_case_summaries(tmp_path, monkeypatch):
    from scripts import eval_retail

    client = FakeClient(
        [
            tool_use_message("add_to_cart", {"product_id": "AR-1202", "quantity": 1}),
            text_message("Added."),
            text_message("That id does not exist."),
        ]
    )

    @asynccontextmanager
    async def fake_client(**kwargs):
        yield client

    monkeypatch.setattr(eval_retail, "AsyncAnthropic", fake_client)
    monkeypatch.setattr(eval_retail, "load_demo_env", lambda _: None)
    args = argparse.Namespace(
        case=[CASE_PATH, CASES_DIR / "cart-004-nonexistent-id.json"],
        model=None,
        trials=1,
        timeout=10,
        output=tmp_path / "report.json",
    )
    assert await eval_retail.run(args) == 0
    report = json.loads(args.output.read_text())
    ids = [entry["case"]["id"] for entry in report["cases"]]
    assert ids == ["cart-001-add-known-product", "cart-004-nonexistent-id"]
    for entry in report["cases"]:
        assert entry["summary"]["pass"] == 1
    assert report["summary"]["trials"] == 2
    assert report["failure_kinds"] == {}


def test_duplicate_case_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases([CASE_PATH, CASE_PATH])
