# Retail PostgreSQL persistence

The retail Messages API app can share shopping sessions and carts across processes.
The catalog, profiles, orders and policies still come from fixture files. Merchant
state and long-term memory are not migrated by this mode. With no
`COMMERCE_DATABASE_URL`, the app keeps its original in-memory sessions and carts.
Enabling SQL does not import existing in-memory records; start a new session in SQL mode.

## Run

Install the optional driver after the root requirements:

```bash
.venv/bin/python -m pip install -r requirements-postgres.txt
```

Use an existing PostgreSQL server, or start the local-only Compose service. Set a
password suitable for a URI, or percent-encode reserved characters in the connection URI.
The example values below are placeholders, not credentials.

```bash
export COMMERCE_PG_PASSWORD='<local-password>'
docker compose -f compose.postgres.yaml up -d --wait
export COMMERCE_DATABASE_URL="postgresql://commerce:${COMMERCE_PG_PASSWORD}@127.0.0.1:5432/commerce"
.venv/bin/uvicorn retail.api.main:app --app-dir examples --port 8000 --workers 2
```

The Compose service binds to loopback and uses a named data volume. `COMMERCE_PG_PORT`
changes its host port; use the same port in the database URL. Stopping the service does
not remove the volume. Do not use development credentials or demo authentication in a
public deployment. Avoid placing URLs with passwords in command logs or committed files.

At API startup the pool opens and applies the two SQL migrations in one transaction,
serialized with a database advisory lock. Applied migration hashes are checked; a changed
migration is rejected rather than silently reapplied. The deployment role needs DDL rights
for this demo; a production deployment should run migrations separately with its own role.
The pool closes at shutdown, including startup failure. Each worker owns its own pool.

## Interfaces

- `examples/demo_common/postgres.py`: `PostgresDatabase` and `PostgresSessionStore`.
  `require` reads state/version/history in one snapshot. `save` locks and checks the
  session version, commits state and transcript together, then updates the caller's
  baseline. A stale writer writes nothing. The public start/require/save/reset API is
  supported; separate low-level `write_state` and `write_messages` calls are refused
  because splitting them would lose the transaction guarantee.
- `examples/demo_common/migrations/001_sessions.sql`: sessions, their versioned state
  documents and transcripts. This small implementation stores each transcript as one
  JSONB array rather than normalized per-message rows.
- `examples/retail/api/postgres_retail.py`: `PostgresRetail`, a fixture backend with SQL
  add/update/remove/read operations. Database calls are offloaded from async handlers
  to threads. No database transaction spans a model call.
- `examples/retail/api/migrations/002_carts.sql`: parent carts and product lines, cascading
  from sessions. Primary keys prevent duplicate lines. A CHECK and composite foreign
  key bind each line's quantity to its parent's persisted per-item limit. The cart-line
  count is enforced under the parent lock in the backend, not by a SQL CHECK.
- `shopping-agent/core/shopping_agent/backend.py:try_atomic_add_to_cart`: the optional shared-store capability.
  `CartAddition` returns the actual increment or a full/limit refusal. The default method
  returns `None` without writing; existing backends keep the process-local gate fallback.
- `examples/demo_common/storefront.py`: accepts an injected session store and startup/shutdown
  hooks. Sync start/reset operations run in the thread pool, as the session dependency
  and streaming BackgroundTask already do.

The provenance and options gates still run before any cart write. On the SQL path,
`gated_add_to_cart` passes the deployment caps to the backend without a separate
read/check/write. The backend creates or locks the parent cart, reads current lines,
computes the allowed increment, writes, and returns only after commit. Locking the parent
also protects two first inserts and competing different products at the line-count cap.
The confirmation uses the committed increment, not the requested amount. Updates and
removals take the same parent lock. A deployment limit mismatch is refused; changing limits
on existing carts requires an explicit migration or reset.

The stream-save conflict handler rebases a pending-note-only change and preserves the
queued note. A competing transcript or state mutation is logged as non-mergeable and is
not overwritten. It does not serialize overlapping chat turns or guarantee recovery of
the losing turn. An SSE response that already streamed cannot be changed into HTTP 409.

## Guarantees and limits

Committed sessions, provenance and carts remain readable by a new process. Two worker
transactions cannot jointly exceed the configured item or line caps. A failed transaction
before commit leaves no partial cart write. Reset deletes the session and cascades its cart.
State/history persistence and cart mutation use separate short transactions; they are not
one transaction covering an entire Agent turn. Cancelling a coroutine waiting on a thread
does not stop already running SQL: an interrupted call may still commit, so its side effect
must be checked against the authoritative cart.

There is no operation ledger or request idempotency yet. If a write commits but its response
is lost, repeating the add can add again. Same parameters do not identify the same user
operation. A later idempotency layer must carry a stable host-controlled operation ID,
uniquely constrain it, and commit its receipt with the cart mutation. Model-generated tool
IDs must not be treated as stable across regenerated turns. Different user operations need
different IDs even when their parameters match.

The host still saves the finished transcript at stream end. This mode does not checkpoint
an in-flight Agent loop, resume interrupted model generation, provide a durable background
memory queue, or persist merchant catalog changes. The cart backend still relies on the
host's authenticated session context and the core's provenance gate.

## Verify

The integration suite creates a random schema in the explicitly selected test database
and drops only that schema afterward. Never point it at a production database. Without
the optional driver or `COMMERCE_TEST_DATABASE_URL`, these tests are skipped, not counted
as database verification.

```bash
export COMMERCE_TEST_DATABASE_URL='<dedicated-test-database-uri>'
.venv/bin/python -m pytest tests/test_postgres_retail.py -q
.venv/bin/python -m pytest tests/test_persistence_seams.py -q
```

The SQL suite checks version conflicts, atomic save failure, transcript compaction, a new
interpreter reading saved state/cart, reset cleanup, database quantity constraints,
pre-commit rollback, cancellation with a surviving SQL write, actual capped confirmations,
HTTP session/chat/cart/reset wiring,
and two independent processes adding concurrently. One test deliberately confirms that
replaying an add is not yet idempotent. Agent-turn tests use `FakeClient`; this suite makes
no model API calls and does not measure model decision quality.
