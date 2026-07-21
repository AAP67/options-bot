"""Live SnapTrade checks that the unit tests structurally cannot make.

`test_sync.py` proves the normalizer is self-consistent with its fixtures. It
would keep passing if SnapTrade changed what a field *means* — and the unit
quirk these tests guard is exactly that kind of change: option `cost_basis` is
reported per contract while `price` is per share. Silently flipping to
per-share would not break any unit test, but would understate option cost basis
100x and corrupt position sizing in Sprint 5.

Read-only: lists accounts and holdings, writes nothing to SnapTrade or Supabase.

    RUN_INTEGRATION=1 uv run pytest tests/test_sync_integration.py -v
"""

from __future__ import annotations

import datetime as dt

import pytest

from src import sync


@pytest.fixture(scope="session")
def live_accounts(live_snaptrade):
    accounts = sync.fetch_accounts(live_snaptrade)
    if not accounts:
        pytest.skip("no brokerage accounts linked to this SnapTrade key")
    return accounts


@pytest.fixture(scope="session")
def live_rows(live_snaptrade, live_accounts):
    """Every normalized row for one live sync, without writing it anywhere."""
    now = dt.datetime.now(dt.UTC)
    positions, balances = {}, {}
    for account in live_accounts:
        account_id = str(account["id"])
        positions[account_id] = sync.fetch_positions(live_snaptrade, account_id)
        balances[account_id] = sync.fetch_balances(live_snaptrade, account_id)
    return sync.build_rows(live_accounts, positions, balances, now.isoformat())


@pytest.fixture(scope="session")
def live_option_positions(live_snaptrade, live_accounts):
    """Raw (unnormalized) option rows straight from the unified endpoint."""
    found = []
    for account in live_accounts:
        for row in sync.fetch_positions(live_snaptrade, str(account["id"])):
            instrument = dict(row.get("instrument") or {})
            if sync.asset_type_for(instrument.get("kind")) == "option":
                found.append((row, instrument))
    if not found:
        pytest.skip("no open option positions to check unit semantics against")
    return found


def test_live_connection_is_fresh(live_accounts):
    """The staleness check should pass on a healthy connection."""
    assert sync.check_freshness(live_accounts, dt.datetime.now(dt.UTC)) == []


def test_option_cost_basis_is_still_per_contract(live_option_positions):
    """The invariant the normalizer depends on — asserted against live data.

    Per contract, cost_basis / price lands near the multiplier (~100). If
    SnapTrade switched to per-share, the ratio would collapse to order 1. The
    bound is deliberately loose (>= multiplier/10) so a merely profitable or
    losing position never trips it — only a semantics change does.
    """
    for row, instrument in live_option_positions:
        price = float(row["price"])
        if price == 0:
            continue  # a worthless contract tells us nothing about units
        multiplier = float(instrument.get("multiplier") or sync.DEFAULT_OPTION_MULTIPLIER)
        ratio = abs(float(row["cost_basis"]) / price)
        assert ratio >= multiplier / 10, (
            f"{instrument.get('symbol')}: cost_basis/price = {ratio:.2f}, expected "
            f"~{multiplier:.0f}. SnapTrade may have switched cost_basis to per-share — "
            "normalize_position() would now understate option cost basis 100x."
        )


def test_option_multiplier_is_reported(live_option_positions):
    """We fall back to 100, but a missing multiplier should be a known state."""
    for _row, instrument in live_option_positions:
        assert instrument.get("multiplier"), (
            f"{instrument.get('symbol')}: no multiplier reported; the normalizer is "
            "silently assuming 100 for a contract that may not be standard-sized."
        )


def test_short_positions_carry_negative_market_value(live_rows):
    """Sign conventions must survive normalization — a short is not an asset."""
    for row in live_rows:
        if row["quantity"] < 0:
            assert row["market_value"] is not None and row["market_value"] < 0, (
                f"{row['symbol']}: quantity {row['quantity']} but market_value "
                f"{row['market_value']}"
            )


def test_every_row_is_writable(live_rows):
    """Whatever the live account holds must satisfy the `positions` schema."""
    required = {"synced_at", "account_id", "symbol", "asset_type", "quantity", "currency"}
    for row in live_rows:
        assert required <= row.keys()
        assert all(row[column] is not None for column in required)
        assert isinstance(row["quantity"], float)


def test_unmapped_instrument_kinds_are_visible(live_rows):
    """Not a failure — a heads-up that a new asset class appeared."""
    known = {"equity", "option", "crypto", "cash"}
    unmapped = {row["asset_type"] for row in live_rows} - known
    assert not unmapped, (
        f"new asset_type(s) from SnapTrade: {sorted(unmapped)}. They synced fine "
        "(pass-through by design) — decide whether the engine should see them, and "
        "update ASSET_TYPE_BY_KIND plus the 0003 column comment."
    )
