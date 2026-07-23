"""Tests for the weekly run's persistence path (Sprint 4, step 2).

The engine's reshape is tested in test_engine_stub.py; here we pin the
orchestration around it: a stable run id, that the stub rows reach the DB
exactly once under one run id, and that an empty fetch writes nothing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from src.engine import black_scholes as bs
from src.engine import stub
from src import weekly

NOW = dt.datetime(2026, 7, 26, 14, 0, 0, tzinfo=dt.UTC)


class FakeDB:
    """Records what would be written, mirroring test_sync.py's double."""

    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.insert_calls = 0

    def insert_suggestions(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.insert_calls += 1
        self.written = rows
        return rows


def chain_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "ticker": "AAPL",
        "expiration": "2026-08-21",
        "right": bs.CALL,
        "strike": 230.0,
        "delta": 0.28,
        "mid": 4.20,
    }
    row.update(overrides)
    return row


# -- run id -----------------------------------------------------------------


def test_run_id_is_a_sortable_utc_stamp():
    assert weekly.new_run_id(NOW) == "weekly-20260726T140000Z"


def test_run_id_is_deterministic_for_a_given_time():
    assert weekly.new_run_id(NOW) == weekly.new_run_id(NOW)


def test_run_id_normalises_to_utc():
    """A non-UTC instant yields the same id as its UTC equivalent."""
    eastern = dt.timezone(dt.timedelta(hours=-4))
    same_instant = NOW.astimezone(eastern)
    assert weekly.new_run_id(same_instant) == weekly.new_run_id(NOW)


def test_run_ids_sort_in_time_order():
    later = NOW + dt.timedelta(minutes=1)
    assert weekly.new_run_id(NOW) < weekly.new_run_id(later)


# -- persistence ------------------------------------------------------------


def test_stub_rows_are_written_once_under_one_run_id():
    db = FakeDB()
    written = weekly.write_stub_suggestions(
        db, [chain_row(), chain_row(right=bs.PUT)], now=NOW
    )

    assert db.insert_calls == 1
    assert len(db.written) == 2
    run_ids = {r["run_id"] for r in db.written}
    assert run_ids == {"weekly-20260726T140000Z"}
    assert all(r["rule_version"] == stub.RULE_VERSION for r in db.written)
    # what's returned is exactly what was persisted
    assert written == db.written


def test_returned_rows_carry_the_run_id_for_the_delivery_steps():
    db = FakeDB()
    written = weekly.write_stub_suggestions(db, [chain_row()], now=NOW)
    assert written[0]["run_id"] == "weekly-20260726T140000Z"


def test_empty_chains_write_nothing_and_issue_no_insert():
    db = FakeDB()
    written = weekly.write_stub_suggestions(db, [], now=NOW)
    assert written == []
    assert db.insert_calls == 0
    assert db.written == []
