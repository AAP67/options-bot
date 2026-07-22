"""CRUD tests against the real Supabase project (Sprint 1 exit criteria).

Gated behind RUN_INTEGRATION=1 — the default `pytest` run and CI skip these, so
only a deliberate local run touches the live database. Every row written is
tagged with a per-run uuid and deleted in teardown; nothing is left behind.

    RUN_INTEGRATION=1 uv run pytest tests/test_db_integration.py -v
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest


@pytest.fixture()
def tag() -> str:
    """A unique marker so this run's rows can never collide with real data."""
    return f"test-{uuid.uuid4().hex[:12]}"


@pytest.fixture()
def cleanup(integration_db, tag):
    """Delete every row this test wrote, pass or fail."""
    yield
    # outcomes cascade from suggestions, so deleting suggestions is enough there.
    integration_db.delete("suggestions", {"run_id": tag})
    integration_db.delete("positions", {"account_id": tag})
    integration_db.delete("iv_history", {"ticker": tag})
    integration_db.delete("heartbeat", {"note": tag})
    integration_db.delete("sync_runs", {"error": tag})


def test_positions_insert_and_read_back(integration_db, tag, cleanup):
    synced_at = dt.datetime.now(dt.UTC).isoformat()
    written = integration_db.insert_positions(
        [
            {
                "synced_at": synced_at,
                "account_id": tag,
                "symbol": "AAPL",
                "asset_type": "equity",
                "quantity": 100,
                "avg_cost": 190.25,
                "market_value": 21000,
            },
            {
                "synced_at": synced_at,
                "account_id": tag,
                "symbol": "USD",
                "asset_type": "cash",
                "quantity": 5000,
            },
        ]
    )

    assert len(written) == 2
    assert all(row["id"] for row in written)

    read_back = integration_db.select("positions", filters={"account_id": tag})
    assert {row["symbol"] for row in read_back} == {"AAPL", "USD"}
    # numerics come back as strings or floats depending on driver; compare as float
    aapl = next(r for r in read_back if r["symbol"] == "AAPL")
    assert float(aapl["quantity"]) == 100.0
    assert aapl["currency"] == "USD"  # server-side default applied


def test_iv_upsert_is_idempotent_per_ticker_date(integration_db, tag, cleanup):
    today = dt.date.today().isoformat()

    integration_db.upsert_iv([{"ticker": tag, "date": today, "atm_iv": 0.30}])
    integration_db.upsert_iv([{"ticker": tag, "date": today, "atm_iv": 0.42}])

    rows = integration_db.iv_for_ticker(tag)
    assert len(rows) == 1, "the (ticker, date) unique constraint should collapse these"
    assert float(rows[0]["atm_iv"]) == 0.42


def test_iv_history_records_how_the_reading_was_derived(integration_db, tag, cleanup):
    """atm_iv is computed, not quoted — so its inputs must persist with it.

    call_iv and put_iv are the load-bearing pair: solved from independent
    contracts, their agreement continuously validates the model, rate and time
    convention. Storing only the average would discard that check, and it is
    not reconstructable once the free data window rolls past.
    """
    from src.engine import iv as engine_iv

    written = integration_db.upsert_iv(
        [
            {
                "ticker": tag,
                "date": "2026-07-21",
                "atm_iv": 0.2976,
                "call_iv": 0.2939,
                "put_iv": 0.3013,
                "expiration": "2026-08-21",
                "strike": 327.5,
                "spot": 327.74,
                "rate": 0.0357,
                "method": engine_iv.METHOD,
            }
        ]
    )

    row = written[0]
    assert float(row["call_iv"]) == 0.2939
    assert float(row["put_iv"]) == 0.3013
    assert row["expiration"] == "2026-08-21"
    assert float(row["spot"]) == 327.74
    assert row["method"] == engine_iv.METHOD

    # the (ticker, date) constraint must still collapse a re-run
    integration_db.upsert_iv(
        [{"ticker": tag, "date": "2026-07-21", "atm_iv": 0.31, "method": engine_iv.METHOD}]
    )
    rows = integration_db.iv_for_ticker(tag)
    assert len(rows) == 1
    assert float(rows[0]["atm_iv"]) == 0.31


def test_suggestion_and_outcome_lifecycle(integration_db, tag, cleanup):
    written = integration_db.insert_suggestions(
        [
            {
                "run_id": tag,
                "ticker": "MSFT",
                "strategy": "covered_call",
                "expiry": "2026-08-21",
                "strike": 520,
                "delta": 0.27,
                "premium": 4.15,
                "annualized_yield": 0.18,
                "decision_snapshot": {"iv_rank": 61, "dte": 31},
                "rule_version": "v1",
            }
        ]
    )
    suggestion_id = written[0]["id"]
    assert written[0]["taken"] is False  # default
    assert written[0]["decision_snapshot"]["iv_rank"] == 61  # jsonb round-trips

    assert integration_db.suggestions_for_run(tag)[0]["id"] == suggestion_id

    integration_db.upsert_outcome(
        {"suggestion_id": suggestion_id, "realized_pnl": 415, "max_adverse_excursion": -120}
    )
    rescored = integration_db.upsert_outcome(
        {"suggestion_id": suggestion_id, "realized_pnl": 380, "max_adverse_excursion": -260}
    )

    outcomes = integration_db.select("outcomes", filters={"suggestion_id": suggestion_id})
    assert len(outcomes) == 1, "one outcome per suggestion; re-scoring updates in place"
    assert float(rescored[0]["realized_pnl"]) == 380.0


def test_sync_run_records_broker_refresh_metadata(integration_db, tag, cleanup):
    """The jsonb detail must survive the round trip — it is the whole payload.

    This is the evidence that sets MAX_HOLDINGS_AGE, and it cannot be
    reconstructed after the fact, so a silent schema mismatch would be costly.
    """
    synced_at = dt.datetime.now(dt.UTC).isoformat()
    written = integration_db.insert_sync_run(
        {
            "synced_at": synced_at,
            "status": "partial",
            "rows_written": 26,
            "error": tag,
            "accounts": {
                "acct-1": {
                    "name": "Robinhood Individual",
                    "included": True,
                    "last_successful_sync": "2026-07-21T17:17:40.750897+00:00",
                    "problems": [],
                },
                "acct-2": {
                    "name": "Robinhood Crypto",
                    "included": False,
                    "last_successful_sync": None,
                    "problems": ["brokerage connection disabled"],
                },
            },
        }
    )

    assert written and written[0]["id"]
    row = written[0]
    assert row["status"] == "partial"
    assert row["rows_written"] == 26
    # jsonb round-trips with nested structure intact
    assert row["accounts"]["acct-1"]["last_successful_sync"].startswith("2026-07-21")
    assert row["accounts"]["acct-2"]["included"] is False
    assert row["accounts"]["acct-2"]["problems"] == ["brokerage connection disabled"]


def test_heartbeat_insert_and_delete(integration_db, tag, cleanup):
    """The Sprint 0 pipe: proves the `heartbeat` migration is actually applied."""
    written = integration_db.insert_heartbeat({"source": "local", "note": tag})
    assert written and written[0]["ran_at"]

    deleted = integration_db.delete("heartbeat", {"note": tag})
    assert len(deleted) == 1
    assert integration_db.select("heartbeat", filters={"note": tag}) == []
