"""Tests for the stub engine.

The stub is the walking skeleton's placeholder: it must reshape fetched chain
rows into `suggestions` rows and decide *nothing*. So the tests pin down exactly
that — a mechanical, order-preserving, lossless pass-through that stamps the
rows as stub and asserts no strategy output.
"""

from __future__ import annotations

from typing import Any

from src.engine import black_scholes as bs
from src.engine import stub

RUN_ID = "run-2026-07-26"


def chain_row(**overrides: Any) -> dict[str, Any]:
    """A chain row shaped like `src/chain.py`'s output."""
    row = {
        "ticker": "AAPL",
        "date": "2026-07-24",
        "expiration": "2026-08-21",
        "dte": 28,
        "right": bs.CALL,
        "strike": 230.0,
        "bid": 4.10,
        "ask": 4.30,
        "close": 4.20,
        "mid": 4.20,
        "volume": 1234,
        "open_interest": None,
        "iv": 0.294,
        "delta": 0.28,
        "spot": 228.5,
        "rate": 0.043,
        "method": "bs-mid-30dte-v2",
    }
    row.update(overrides)
    return row


def test_call_becomes_covered_call_and_put_becomes_csp():
    rows = stub.build_suggestions(
        [chain_row(right=bs.CALL), chain_row(right=bs.PUT)], RUN_ID
    )
    assert [r["strategy"] for r in rows] == ["covered_call", "cash_secured_put"]


def test_identifying_fields_pass_straight_through():
    [row] = stub.build_suggestions([chain_row()], RUN_ID)
    assert row["ticker"] == "AAPL"
    assert row["expiry"] == "2026-08-21"
    assert row["strike"] == 230.0
    assert row["delta"] == 0.28


def test_every_row_is_stamped_stub_and_with_the_run_id():
    rows = stub.build_suggestions([chain_row(), chain_row(right=bs.PUT)], RUN_ID)
    assert all(r["rule_version"] == "stub" for r in rows)
    assert all(r["run_id"] == RUN_ID for r in rows)


def test_strategy_outputs_are_left_null():
    """A stub makes no ranking: premium and yield must be absent, not guessed."""
    [row] = stub.build_suggestions([chain_row()], RUN_ID)
    assert row["premium"] is None
    assert row["annualized_yield"] is None


def test_the_whole_chain_row_is_kept_in_the_snapshot():
    """Nothing fetched is discarded — the snapshot is the raw row verbatim."""
    src = chain_row()
    [row] = stub.build_suggestions([src], RUN_ID)
    assert row["decision_snapshot"] == src


def test_rows_with_an_unrecognised_right_are_skipped():
    rows = stub.build_suggestions(
        [chain_row(right=None), chain_row(right="CALL"), chain_row(right=bs.PUT)],
        RUN_ID,
    )
    # Only the genuine put survives: `None` and the un-normalised "CALL" (chain
    # rows are already lower-cased) are not strategies the stub will label.
    assert [r["strategy"] for r in rows] == ["cash_secured_put"]


def test_input_order_is_preserved():
    strikes = [100.0, 105.0, 110.0]
    rows = stub.build_suggestions([chain_row(strike=s) for s in strikes], RUN_ID)
    assert [r["strike"] for r in rows] == strikes


def test_empty_input_gives_empty_output():
    assert stub.build_suggestions([], RUN_ID) == []
