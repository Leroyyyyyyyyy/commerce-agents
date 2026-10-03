"""Optional PostgreSQL connection pool and atomic versioned session store."""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .sessions import SessionConflictError, SessionRecord, SessionStore, StateT, UnknownSessionError


class PostgresDatabase:
    def __init__(self, dsn: str, *, schema: str = "public") -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", schema):
            raise ValueError("invalid database schema")
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=8,
            open=False,
            timeout=10,
            kwargs={"row_factory": dict_row, "options": f"-c search_path={schema}"},
        )

    def open(self) -> None:
        self.pool.open(wait=True, timeout=10)

    def close(self) -> None:
        self.pool.close()

    def migrate(self, paths: list[Path]) -> None:
        """Serialize migrations; refuse edits to SQL that was already applied."""
        with self.pool.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(773420019)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS commerce_schema_migrations "
                "(name text PRIMARY KEY, digest text NOT NULL)"
            )
            for path in paths:
                body = path.read_text()
                digest = hashlib.sha256(body.encode()).hexdigest()
                applied = connection.execute(
                    "SELECT digest FROM commerce_schema_migrations WHERE name = %s", (path.name,)
                ).fetchone()
                if applied:
                    if applied["digest"] != digest:
                        raise ValueError(f"applied migration changed: {path.name}")
                    continue
                connection.execute(body)
                connection.execute(
                    "INSERT INTO commerce_schema_migrations (name, digest) VALUES (%s, %s)",
                    (path.name, digest),
                )


class PostgresSessionStore(SessionStore[StateT]):
    """The public start/require/save/reset API; save commits state and history together."""

    def __init__(self, state_type: type[StateT], database: PostgresDatabase) -> None:
        super().__init__(state_type)
        self.database = database

    def require(self, session_id: str) -> SessionRecord[StateT]:
        # One SELECT observes a coherent version, state and transcript snapshot.
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT version, document, messages FROM commerce_sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            raise UnknownSessionError(session_id)
        document = row["document"]
        messages = row["messages"]
        return SessionRecord(
            session_id=session_id,
            user_id=document["user_id"],
            state=self._state_type.model_validate(document["state"]),
            messages=messages,
            pending_app_events=list(document["pending_app_events"]),
            version=row["version"],
            stored_state=copy.deepcopy(document),
            stored_messages=len(messages),
        )

    def save(self, record: SessionRecord[StateT]) -> None:
        if record.ended:
            return
        document = record.state_document()
        grew = record.stored_messages < len(record.messages)
        if document == record.stored_state and not grew:
            return
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT version, messages FROM commerce_sessions WHERE session_id = %s FOR UPDATE",
                (record.session_id,),
            ).fetchone()
            if row is None:
                if record.version != 0:
                    raise SessionConflictError(record.session_id)
                connection.execute(
                    "INSERT INTO commerce_sessions (session_id, version, document, messages) "
                    "VALUES (%s, 1, %s, %s)",
                    (record.session_id, Jsonb(document), Jsonb(record.messages)),
                )
            else:
                if row["version"] != record.version:
                    raise SessionConflictError(record.session_id)
                messages = row["messages"]
                if grew:
                    messages = (
                        messages[: record.stored_messages]
                        + record.messages[record.stored_messages :]
                    )
                updated = connection.execute(
                    "UPDATE commerce_sessions SET version = version + 1, document = %s, messages = %s "
                    "WHERE session_id = %s AND version = %s RETURNING version",
                    (Jsonb(document), Jsonb(messages), record.session_id, record.version),
                ).fetchone()
                if updated is None:
                    raise SessionConflictError(record.session_id)
        # Only update the caller's baseline after COMMIT succeeded.
        record.version += 1
        record.stored_state = document
        if grew:
            record.stored_messages = len(record.messages)

    def read_state(self, session_id: str) -> tuple[int, dict[str, Any]] | None:
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT version, document FROM commerce_sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        return (row["version"], row["document"]) if row else None

    def read_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self.database.pool.connection() as connection:
            row = connection.execute(
                "SELECT messages FROM commerce_sessions WHERE session_id = %s", (session_id,)
            ).fetchone()
        return row["messages"] if row else []

    def write_state(self, session_id: str, document: dict[str, Any], version: int) -> None:
        raise NotImplementedError("Use save(record): state and history must commit together")

    def write_messages(self, session_id: str, messages: list[dict[str, Any]], start: int) -> None:
        raise NotImplementedError("Use save(record): state and history must commit together")

    def delete(self, session_id: str) -> None:
        with self.database.pool.connection() as connection:
            # Cart rows cascade from the session; deletion cannot leave an orphan cart.
            connection.execute("DELETE FROM commerce_sessions WHERE session_id = %s", (session_id,))

    def session_ids_for_user(self, user_id: str) -> list[str]:
        with self.database.pool.connection() as connection:
            rows = connection.execute(
                "SELECT session_id FROM commerce_sessions "
                "WHERE document->>'user_id' = %s ORDER BY session_id",
                (user_id,),
            ).fetchall()
        return [row["session_id"] for row in rows]
