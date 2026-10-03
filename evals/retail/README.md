# Retail cart eval

A minimal behavioral eval for the retail shopping agent on the Messages API.
`scripts/eval_retail.py` drives the agent directly, executes real core gates against
`MockRetail`, and grades `backend.get_cart(session)` after the turns. It does not
exercise HTTP, SSE transport, session persistence, or browser rendering.

## Run

Install the root Python requirements and configure Anthropic credentials as for the
retail demo. The runner uses the demo environment loader; existing process environment
variables take precedence over `.env` files. It does not import `retail.api.main`.

```bash
.venv/bin/python scripts/eval_retail.py --trials 3
.venv/bin/python scripts/eval_retail.py --trials 3 --model <model-id> \
  --output evals/retail/reports/baseline.json
```

Each live trial costs model tokens. `--timeout` bounds the entire trial in seconds;
SDK retries are disabled. Reports are written after each trial, and an existing output
file is never overwritten. Exit code 0 means every trial passed; 1 means at least one
trial failed or errored. Reports are gitignored. Review model output before sharing it.

## Interfaces

- `cases/add-known-product.json`: the first case, adding only AR-1202 x1 to an empty cart.
- `scripts/eval_retail.py:RetailCase`: a validated subset of the commerce-evals case shape.
  Unsupported fields fail validation rather than being silently ignored. The additional
  `expected.cart_exact` key requires the complete set of product IDs and quantities.
- `scripts/eval_retail.py:score_cart`: exact cart comparison, independent of line order.
  Wrong IDs, missing items, wrong quantities, extra items and duplicate lines fail.
- `scripts/eval_retail.py:run_trial`: injects seen products and initial cart lines, appends
  each user turn to the transcript, consumes agent events, then reads the final cart.
- `tests/test_retail_eval.py`: deterministic grader and runner checks with `FakeClient`.
  These tests are not evidence of real model decision quality.

Every trial starts with a new backend, session, transcript and empty in-memory memory
store. The runner neither reads demo memory files nor schedules post-turn extraction.
This case cannot measure memory extraction behavior. Product provenance is injected into
session state, not fabricated as conversation history.

A pass requires the exact final cart and completion of every turn. A reply claiming
success and a `cart_update` event are not authoritative. A runtime exception or timeout
is reported separately as `error`, even if a cart write already committed. Exception
class names, not raw exception text, are stored in reports. Tool soft errors remain in
the events; the case is graded on its final state, not a required tool sequence.

Each report contains the case, model/config, fixture hashes and runner hash. Each trial
contains the effective catalog, static prompt/tools and skill bodies with their fingerprint,
events, final cart, elapsed time, failure reasons and verdict. The demo stamps delivery
attributes using its boot date; a changed deployment fingerprint requires a new baseline.
Token usage is available in the `turn_complete` events. `elapsed_ms` is trial wall time,
not first-token or first-card latency. No price estimate or judge is included.

Run the deterministic checks without credentials:

```bash
.venv/bin/python -m pytest tests/test_retail_eval.py -q
```
