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
.venv/bin/python scripts/eval_retail.py --trials 5 \
  --case evals/retail/cases/cart-009-options-unspecified.json
.venv/bin/python scripts/eval_retail.py --trials 3 --model <model-id> \
  --output evals/retail/reports/baseline.json
```

`--case` takes files or directories and defaults to every case in `cases/`; `--trials`
is per case. Trials run one at a time.

Each live trial costs model tokens. `--timeout` bounds the entire trial in seconds;
SDK retries are disabled. Reports are written after each trial, and an existing output
file is never overwritten. Exit code 0 means every trial passed; 1 means at least one
trial failed or errored. Reports are gitignored. Review model output before sharing it.

## Interfaces

- `cases/`: one case per file, named by its `id`. Each pins one cart behavior by its final
  cart: a plain add, provenance resolution, a nonexistent id, a budget pick, the per-item
  cap, an out-of-stock refusal, an unsettled option, an option settled on the next turn,
  edits to an existing cart, and a poisoned marketplace listing with its should-serve
  counterpart. Each case's `notes` names the fixture fact that decides it.
- `scripts/eval_retail.py:RetailCase`: a validated subset of the commerce-evals case shape.
  Unsupported fields fail validation rather than being silently ignored. The additional
  `expected.cart_exact` key requires the complete set of product IDs and quantities.
  `state.eval_products` adds eval-only `EV-` listings under an ACME marketplace seller to
  one trial's backend; an id that matches demo data fails the build.
- `scripts/eval_retail.py:score_cart`: exact cart comparison, independent of line order.
  Each failure starts with its kind: `missing_product`, `wrong_quantity`,
  `unexpected_product`, `duplicate_line`; the runner adds `incomplete_turns`.
- `scripts/eval_retail.py:run_trial`: injects seen products and initial cart lines, appends
  each user turn to the transcript, consumes agent events, then reads the final cart.
- `tests/test_retail_eval.py`: deterministic grader and runner checks with `FakeClient`.
  These tests are not evidence of real model decision quality.

Every trial starts with a new backend, session, transcript and empty in-memory memory
store. The runner neither reads demo memory files nor schedules post-turn extraction.
These cases cannot measure memory extraction behavior. Product provenance is injected into
session state, not fabricated as conversation history.

A pass requires the exact final cart and completion of every turn. A reply claiming
success and a `cart_update` event are not authoritative. A runtime exception or timeout
is reported separately as `error`, even if a cart write already committed. Exception
class names, not raw exception text, are stored in reports. Tool soft errors remain in
the events; the case is graded on its final state, not a required tool sequence.

Each report contains model/config, fixture hashes, runner hash, the overall summary and
failure-kind counts, and per case its definition, summary and trials. Each trial contains
events, a tool trace (each call's input and gate status), final cart, elapsed time, failure
reasons, verdict and a deployment fingerprint; `deployments` stores the effective catalog,
static prompt/tools and skill bodies once per fingerprint. A failure-kind count is the
number of trials with that kind. The demo stamps delivery
attributes using its boot date; a changed deployment fingerprint requires a new baseline.
Token usage is available in the `turn_complete` events. `elapsed_ms` is trial wall time,
not first-token or first-card latency. No price estimate or judge is included.

Run the deterministic checks without credentials:

```bash
.venv/bin/python -m pytest tests/test_retail_eval.py -q
```
