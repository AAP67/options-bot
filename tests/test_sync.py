"""Unit tests for the SnapTrade sync. All I/O is faked.

Fixture payloads mirror the shapes the live SnapTrade Personal API actually
returns (unified positions endpoint, account balances, account sync_status),
including its unit quirks: numerics arrive as strings, and option `cost_basis`
is per contract while `price` is per share.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src import sync

NOW = dt.datetime(2026, 7, 21, 18, 0, tzinfo=dt.UTC)
SYNCED_AT = NOW.isoformat()


def account(name: str = "Robinhood Individual", **overrides):
    """A linked account with a fresh holdings sync unless told otherwise."""
    base = {
        "id": "acct-1",
        "name": name,
        "status": "open",
        "sync_status": {
            "holdings": {
                "last_successful_sync": "2026-07-21T17:17:40.750897+00:00",
                "initial_sync_completed": True,
            }
        },
    }
    base.update(overrides)
    return base


EQUITY_ROW = {
    "units": "14.53947",
    "price": "251.34",
    "cost_basis": "202.11",
    "currency": "USD",
    "instrument": {
        "kind": "stock",
        "symbol": "AAPL",
        "raw_symbol": "AAPL",
        "currency": "USD",
        "description": "Apple Inc.",
    },
}

# A short call: one contract sold for $1.48/share -> cost_basis 148 per contract.
OPTION_ROW = {
    "units": "-1",
    "price": "2.3",
    "cost_basis": "148",
    "instrument": {
        "kind": "option",
        "symbol": "AAPL  260821C00260000",
        "multiplier": 100,
        "option_type": "CALL",
        "strike_price": 260.0,
        "expiration_date": "2026-08-21",
        "underlying": "AAPL",
    },
}

BALANCE = {"cash": 15234.56, "buying_power": 15234.56, "currency": {"code": "USD"}}


# -- normalization ----------------------------------------------------------


def test_equity_position_uses_per_unit_cost_and_plain_market_value():
    row = sync.normalize_position("acct-1", EQUITY_ROW, SYNCED_AT)
    assert row["symbol"] == "AAPL"
    assert row["asset_type"] == "equity"
    assert row["quantity"] == pytest.approx(14.53947)
    assert row["avg_cost"] == pytest.approx(202.11)
    assert row["market_value"] == pytest.approx(14.53947 * 251.34)
    assert row["currency"] == "USD"
    assert row["raw"] is EQUITY_ROW
    assert row["synced_at"] == SYNCED_AT


def test_short_option_applies_multiplier_and_keeps_cost_basis_per_contract():
    row = sync.normalize_position("acct-1", OPTION_ROW, SYNCED_AT)
    assert row["asset_type"] == "option"
    assert row["quantity"] == -1
    # cost_basis is already per contract — must NOT be multiplied again.
    assert row["avg_cost"] == pytest.approx(148.0)
    # price is per share, so market value needs the 100x multiplier.
    assert row["market_value"] == pytest.approx(-230.0)


def test_option_without_multiplier_falls_back_to_100():
    payload = {**OPTION_ROW, "instrument": {"kind": "option", "symbol": "X"}}
    assert sync.normalize_position("a", payload, SYNCED_AT)["market_value"] == pytest.approx(-230.0)


def test_unknown_instrument_kind_passes_through_rather_than_being_dropped():
    payload = {
        "units": "1",
        "price": "5",
        "cost_basis": "4",
        "instrument": {"kind": "future", "symbol": "ESZ6"},
    }
    assert sync.normalize_position("a", payload, SYNCED_AT)["asset_type"] == "future"


def test_missing_price_yields_null_market_value_not_zero():
    payload = {**EQUITY_ROW, "price": None}
    assert sync.normalize_position("a", payload, SYNCED_AT)["market_value"] is None


def test_cash_becomes_a_position_row_keyed_by_currency():
    row = sync.normalize_cash("acct-1", BALANCE, SYNCED_AT)
    assert row["symbol"] == "USD"
    assert row["asset_type"] == "cash"
    assert row["quantity"] == pytest.approx(15234.56)
    assert row["market_value"] == pytest.approx(15234.56)
    assert row["avg_cost"] is None


def test_balance_without_cash_is_skipped():
    assert sync.normalize_cash("acct-1", {"currency": {"code": "USD"}}, SYNCED_AT) is None


def test_build_rows_covers_every_account():
    accounts = [account(), account("Robinhood Crypto", id="acct-2")]
    rows = sync.build_rows(
        accounts,
        {"acct-1": [EQUITY_ROW, OPTION_ROW], "acct-2": []},
        {"acct-1": [BALANCE], "acct-2": [BALANCE]},
        SYNCED_AT,
    )
    assert len(rows) == 4
    assert {r["account_id"] for r in rows} == {"acct-1", "acct-2"}
    assert sum(r["asset_type"] == "cash" for r in rows) == 2


# -- freshness --------------------------------------------------------------


def test_fresh_account_reports_no_problems():
    assert sync.check_freshness([account()], NOW) == []


def test_stale_holdings_are_reported():
    stale = account(
        sync_status={
            "holdings": {
                "last_successful_sync": "2026-07-18T00:00:00+00:00",
                "initial_sync_completed": True,
            }
        }
    )
    problems = sync.check_freshness([stale], NOW)
    assert len(problems) == 1
    assert "last synced" in problems[0]


def test_incomplete_initial_sync_is_reported():
    never = account(
        sync_status={
            "holdings": {
                "last_successful_sync": None,
                "initial_sync_completed": False,
            }
        }
    )
    assert "no completed holdings sync" in sync.check_freshness([never], NOW)[0]


def test_closed_account_is_reported():
    assert "status" in sync.check_freshness([account(status="closed")], NOW)[0]


def test_naive_timestamp_is_treated_as_utc():
    naive = account(
        sync_status={
            "holdings": {
                "last_successful_sync": "2026-07-21T17:17:40",
                "initial_sync_completed": True,
            }
        }
    )
    assert sync.check_freshness([naive], NOW) == []


def test_unparseable_timestamp_is_reported_not_ignored():
    bad = account(
        sync_status={
            "holdings": {
                "last_successful_sync": "not-a-date",
                "initial_sync_completed": True,
            }
        }
    )
    assert sync.check_freshness([bad], NOW) != []


# -- orchestration ----------------------------------------------------------


class FakeClient:
    """Stands in for the SnapTrade SDK, mimicking its `.body` responses."""

    def __init__(self, accounts, positions, balances):
        self._accounts = accounts
        self._positions = positions
        self._balances = balances
        self.account_information = self

    class _Response:
        def __init__(self, body):
            self.body = body

    def list_user_accounts(self, **_):
        return self._Response(self._accounts)

    def get_all_account_positions(self, account_id, **_):
        return self._Response({"results": self._positions.get(account_id, [])})

    def get_user_account_balance(self, account_id, **_):
        return self._Response(self._balances.get(account_id, []))


class FakeDB:
    def __init__(self):
        self.written: list[dict] = []

    def insert_positions(self, rows):
        self.written = rows
        return rows


def test_sync_writes_one_snapshot_under_a_single_timestamp():
    client = FakeClient([account()], {"acct-1": [EQUITY_ROW, OPTION_ROW]}, {"acct-1": [BALANCE]})
    db = FakeDB()
    written = sync.sync(client, db, now=NOW)
    assert len(written) == 3
    assert {r["synced_at"] for r in written} == {SYNCED_AT}


def test_sync_refuses_to_write_stale_data():
    stale = account(
        sync_status={
            "holdings": {
                "last_successful_sync": "2026-07-01T00:00:00+00:00",
                "initial_sync_completed": True,
            }
        }
    )
    db = FakeDB()
    with pytest.raises(sync.SyncError, match="stale brokerage data"):
        sync.sync(FakeClient([stale], {}, {}), db, now=NOW)
    assert db.written == []


def test_sync_fails_when_no_accounts_are_linked():
    with pytest.raises(sync.SyncError, match="no linked accounts"):
        sync.sync(FakeClient([], {}, {}), FakeDB(), now=NOW)


def test_sync_refuses_to_record_an_empty_snapshot():
    client = FakeClient([account()], {"acct-1": []}, {"acct-1": []})
    with pytest.raises(sync.SyncError, match="zero rows"):
        sync.sync(client, FakeDB(), now=NOW)
