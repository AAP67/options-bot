"""Thin Supabase I/O layer.

No business logic lives here — just create/read helpers over the four tables.
The Supabase client is injected so unit tests can mock it; `DB.from_env()`
builds a real client from the environment for production use.
"""

from __future__ import annotations

import os
from typing import Any


class DB:
    """Wraps a Supabase client with typed create/read helpers per table."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_env(cls) -> DB:
        """Build a DB from SUPABASE_URL and SUPABASE_SERVICE_KEY.

        Imported lazily so tests never need the supabase package installed.
        Fails loudly (iron rule #4) if either secret is missing.
        """
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set "
                "(see .env.example)."
            )
        from supabase import create_client  # lazy import

        return cls(create_client(url, key))

    # -- generic primitives -------------------------------------------------

    def insert(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert one or more rows; return the inserted rows (with ids)."""
        if not rows:
            return []
        resp = self._client.table(table).insert(rows).execute()
        return resp.data

    def upsert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        on_conflict: str,
    ) -> list[dict[str, Any]]:
        """Insert rows, updating on conflict of `on_conflict` column(s)."""
        if not rows:
            return []
        resp = (
            self._client.table(table)
            .upsert(rows, on_conflict=on_conflict)
            .execute()
        )
        return resp.data

    def select(
        self,
        table: str,
        columns: str = "*",
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Select rows, optionally filtered by equality on each key/value."""
        query = self._client.table(table).select(columns)
        for column, value in (filters or {}).items():
            query = query.eq(column, value)
        return query.execute().data

    def delete(self, table: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """Delete rows matching equality on each key/value; return deleted rows.

        Filters are required — a delete with no predicate would truncate a table.
        """
        if not filters:
            raise ValueError("delete requires at least one filter")
        query = self._client.table(table).delete()
        for column, value in filters.items():
            query = query.eq(column, value)
        return query.execute().data

    # -- table-specific convenience helpers ---------------------------------

    def insert_positions(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return self.insert("positions", rows)

    def upsert_iv(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Idempotent daily IV write; one row per (ticker, date)."""
        return self.upsert("iv_history", rows, on_conflict="ticker,date")

    def insert_suggestions(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return self.insert("suggestions", rows)

    def upsert_outcome(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """One outcome per suggestion; re-scoring updates in place."""
        return self.upsert("outcomes", [row], on_conflict="suggestion_id")

    def latest_positions(self) -> list[dict[str, Any]]:
        """Rows from the most recent sync (by synced_at)."""
        resp = (
            self._client.table("positions")
            .select("*")
            .order("synced_at", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return []
        latest = resp.data[0]["synced_at"]
        return self.select("positions", filters={"synced_at": latest})

    def iv_for_ticker(self, ticker: str) -> list[dict[str, Any]]:
        return self.select("iv_history", filters={"ticker": ticker})

    def latest_iv_date(self, method: str | None = None) -> str | None:
        """The newest session stored in iv_history, as an ISO string, or None.

        `method` scopes the query to one derivation, so a stale-method row does
        not count as "we already have this session" — the caller parses and
        compares. Thin I/O only; the date arithmetic lives in `src.iv`.
        """
        query = (
            self._client.table("iv_history")
            .select("date")
            .order("date", desc=True)
            .limit(1)
        )
        if method is not None:
            query = query.eq("method", method)
        rows = query.execute().data
        return str(rows[0]["date"])[:10] if rows else None

    def suggestions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self.select("suggestions", filters={"run_id": run_id})

    def insert_sync_run(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Record one sync attempt (Sprint 2) — what happened, not what we own."""
        return self.insert("sync_runs", [row])

    def latest_sync_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        """Most recent sync attempts, newest first."""
        resp = (
            self._client.table("sync_runs")
            .select("*")
            .order("synced_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data

    def insert_heartbeat(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Write one proof-of-life row (Sprint 0 cron -> DB check)."""
        return self.insert("heartbeat", [row])
