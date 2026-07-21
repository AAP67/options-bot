"""Unit tests for the thin DB layer. The Supabase client is faked — no network,
no supabase package needed at runtime. We assert both the returned data and the
exact calls made against the client (table name, verb, args)."""

from __future__ import annotations

import pytest

from src.db import DB


class FakeQuery:
    """Records the chain of calls and returns canned rows on execute()."""

    def __init__(self, log: list[tuple], rows: list[dict]):
        self._log = log
        self._rows = rows

    def insert(self, rows):
        self._log.append(("insert", rows))
        return self

    def upsert(self, rows, on_conflict=None):
        self._log.append(("upsert", rows, on_conflict))
        return self

    def select(self, columns):
        self._log.append(("select", columns))
        return self

    def delete(self):
        self._log.append(("delete",))
        return self

    def eq(self, column, value):
        self._log.append(("eq", column, value))
        return self

    def order(self, column, desc=False):
        self._log.append(("order", column, desc))
        return self

    def limit(self, n):
        self._log.append(("limit", n))
        return self

    def execute(self):
        return type("Resp", (), {"data": self._rows})()


class FakeClient:
    """Minimal stand-in for a Supabase client."""

    def __init__(self, rows=None):
        self.calls: list[tuple] = []
        self._rows = rows if rows is not None else []
        self.last_table: str | None = None

    def table(self, name):
        self.last_table = name
        self.calls.append(("table", name))
        return FakeQuery(self.calls, self._rows)


# -- generic primitives -----------------------------------------------------


def test_insert_returns_data_and_hits_right_table():
    client = FakeClient(rows=[{"id": 1, "symbol": "AAPL"}])
    db = DB(client)

    out = db.insert("positions", [{"symbol": "AAPL"}])

    assert out == [{"id": 1, "symbol": "AAPL"}]
    assert ("table", "positions") in client.calls
    assert ("insert", [{"symbol": "AAPL"}]) in client.calls


def test_insert_empty_is_noop():
    client = FakeClient()
    db = DB(client)

    assert db.insert("positions", []) == []
    assert client.calls == []  # never touched the client


def test_select_applies_eq_filters():
    client = FakeClient(rows=[{"ticker": "AAPL"}])
    db = DB(client)

    out = db.select("iv_history", filters={"ticker": "AAPL"})

    assert out == [{"ticker": "AAPL"}]
    assert ("eq", "ticker", "AAPL") in client.calls


def test_delete_applies_eq_filters():
    client = FakeClient(rows=[{"id": 1}])
    db = DB(client)

    out = db.delete("heartbeat", filters={"note": "test"})

    assert out == [{"id": 1}]
    assert ("delete",) in client.calls
    assert ("eq", "note", "test") in client.calls


def test_delete_without_filters_is_refused():
    client = FakeClient()
    db = DB(client)

    with pytest.raises(ValueError, match="at least one filter"):
        db.delete("heartbeat", filters={})
    assert client.calls == []  # never touched the client


# -- table-specific helpers -------------------------------------------------


def test_upsert_iv_uses_ticker_date_conflict_key():
    client = FakeClient(rows=[{"ticker": "AAPL", "date": "2026-07-21"}])
    db = DB(client)

    db.upsert_iv([{"ticker": "AAPL", "date": "2026-07-21", "atm_iv": 0.3}])

    upserts = [c for c in client.calls if c[0] == "upsert"]
    assert upserts and upserts[0][2] == "ticker,date"


def test_upsert_outcome_uses_suggestion_id_conflict_key():
    client = FakeClient(rows=[{"suggestion_id": 7}])
    db = DB(client)

    db.upsert_outcome({"suggestion_id": 7, "realized_pnl": 42})

    upserts = [c for c in client.calls if c[0] == "upsert"]
    assert upserts and upserts[0][2] == "suggestion_id"


def test_suggestions_for_run_filters_by_run_id():
    client = FakeClient(rows=[{"run_id": "abc"}])
    db = DB(client)

    out = db.suggestions_for_run("abc")

    assert out == [{"run_id": "abc"}]
    assert ("eq", "run_id", "abc") in client.calls


def test_latest_positions_filters_by_newest_synced_at():
    client = FakeClient(rows=[{"synced_at": "2026-07-21T00:00:00Z", "symbol": "AAPL"}])
    db = DB(client)

    out = db.latest_positions()

    assert out and out[0]["symbol"] == "AAPL"
    assert ("order", "synced_at", True) in client.calls


# -- from_env ---------------------------------------------------------------


def test_from_env_fails_loudly_without_secrets(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        DB.from_env()
