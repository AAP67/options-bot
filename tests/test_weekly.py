"""Tests for the weekly run (Sprint 4, steps 2 and 5).

The engine's reshape is tested in test_engine_stub.py; here we pin the
orchestration around it: a stable run id, that the stub rows reach the DB
exactly once, that chains are fetched resiliently, and that the whole run wires
broker -> DB -> data -> LLM -> delivery in order.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from src import weekly
from src.engine import black_scholes as bs
from src.engine import stub

NOW = dt.datetime(2026, 7, 26, 14, 0, 0, tzinfo=dt.UTC)


class FakeDB:
    """Records what would be written, mirroring test_sync.py's double."""

    def __init__(self, positions: list[dict[str, Any]] | None = None) -> None:
        self.positions = positions or []
        self.written: list[dict[str, Any]] = []
        self.insert_calls = 0

    def latest_positions(self) -> list[dict[str, Any]]:
        return self.positions

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


# -- fetch_all_chains -------------------------------------------------------

AS_OF = dt.date(2026, 7, 20)  # Monday
EXPIRY = dt.date(2026, 8, 21)  # Friday, ~30 DTE
SPOT = 100.0
RATE_PERCENT = 3.57
STRIKES = [95.0, 100.0, 105.0]


def _quote(strike: float, right: str) -> dict[str, Any]:
    fair = bs.price(
        right,
        SPOT,
        strike,
        bs.year_fraction((EXPIRY - AS_OF).days),
        RATE_PERCENT / 100,
        0.30,
    )
    return {
        "bid": round(fair - 0.02, 4),
        "ask": round(fair + 0.02, 4),
        "close": round(fair, 4),
        "volume": 10,
    }


class ChainTheta:
    """Answers every path fetch_all_chains reaches, for any symbol."""

    def __init__(self, *, expiries_for: dict[str, list[dt.date]] | None = None) -> None:
        # Per-symbol expiry override; default is one good Friday expiry.
        self.expiries_for = expiries_for or {}

    def get(self, path: str, **params: Any) -> list[dict[str, Any]]:
        if path.endswith("/interest_rate/history/eod"):
            return [{"rate": RATE_PERCENT, "created": "2026-07-20"}]
        if path.endswith("/stock/history/eod"):
            return [{"close": SPOT}]
        if path.endswith("/option/list/expirations"):
            expiries = self.expiries_for.get(params["symbol"], [EXPIRY])
            return [{"expiration": e.isoformat()} for e in expiries]
        if path.endswith("/option/history/eod"):
            envelopes = []
            for strike in STRIKES:
                for api_right, right in (("CALL", bs.CALL), ("PUT", bs.PUT)):
                    envelopes.append(
                        {
                            "contract": {
                                "symbol": params["symbol"],
                                "expiration": EXPIRY.isoformat(),
                                "strike": strike,
                                "right": api_right,
                            },
                            "data": [_quote(strike, right)],
                        }
                    )
            return envelopes
        raise AssertionError(f"unexpected path {path}")


def test_fetch_all_chains_returns_rows_for_every_ticker():
    rows = weekly.fetch_all_chains(ChainTheta(), ["AAPL", "MSFT"], AS_OF)
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"AAPL", "MSFT"}
    # whole chain per ticker: 3 strikes x 2 rights x 2 tickers
    assert len(rows) == 12


def test_one_ticker_with_no_expiry_does_not_cost_the_others():
    theta = ChainTheta(expiries_for={"BADCO": []})
    rows = weekly.fetch_all_chains(theta, ["AAPL", "BADCO"], AS_OF)
    assert {r["ticker"] for r in rows} == {"AAPL"}


def test_all_tickers_failing_is_fatal():
    theta = ChainTheta(expiries_for={"A": [], "B": []})
    with pytest.raises(weekly.WeeklyError, match="no chains fetched"):
        weekly.fetch_all_chains(theta, ["A", "B"], AS_OF)


def test_empty_ticker_list_fails_loudly():
    with pytest.raises(weekly.WeeklyError, match="no tickers"):
        weekly.fetch_all_chains(ChainTheta(), [], AS_OF)


def test_the_shared_rate_is_fetched_once_not_per_ticker():
    calls: list[str] = []

    class Counting(ChainTheta):
        def get(self, path: str, **params: Any) -> list[dict[str, Any]]:
            calls.append(path)
            return super().get(path, **params)

    weekly.fetch_all_chains(Counting(), ["AAPL", "MSFT"], AS_OF)
    assert sum(p.endswith("/interest_rate/history/eod") for p in calls) == 1


# -- run_weekly (the whole pipe) --------------------------------------------


class TextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeAnthropic:
    """Returns canned prose; records the prompt it was asked to summarise."""

    def __init__(self, prose: str) -> None:
        self.calls: list[dict[str, Any]] = []
        outer = self

        class _Messages:
            def create(self, **kwargs: Any) -> Any:
                outer.calls.append(kwargs)
                return type(
                    "R", (), {"content": [TextBlock(prose)], "stop_reason": "end_turn"}
                )()

        self.messages = _Messages()


EQUITY_POSITIONS = [
    {"symbol": "AAPL", "asset_type": "equity"},
    {"symbol": "MSFT", "asset_type": "equity"},
    {"symbol": "USD", "asset_type": "cash"},
]


def _wire(monkeypatch, *, chains, prose="the brief"):
    """Stub the external boundaries; leave reshape/persist/prompt real."""
    synced: dict[str, Any] = {}
    delivered: dict[str, Any] = {}

    monkeypatch.setattr(
        weekly.sync,
        "sync",
        lambda client, db, now=None: synced.update(client=client, db=db, now=now),
    )
    monkeypatch.setattr(weekly.iv, "latest_session", lambda theta, today: AS_OF)
    monkeypatch.setattr(
        weekly, "fetch_all_chains", lambda theta, tickers, as_of: chains
    )
    monkeypatch.setattr(
        weekly.brief,
        "deliver",
        lambda text: delivered.update(text=text) or 4242,
    )
    return synced, delivered


def test_run_weekly_wires_the_whole_pipe(monkeypatch):
    db = FakeDB(positions=EQUITY_POSITIONS)
    chains = [chain_row(ticker="AAPL"), chain_row(ticker="MSFT", right=bs.PUT)]
    synced, delivered = _wire(monkeypatch, chains=chains, prose="two candidates")
    client = FakeAnthropic("two candidates")

    result = weekly.run_weekly(object(), db, object(), client, now=NOW)

    # broker synced, suggestions persisted, prose delivered
    assert synced["db"] is db
    assert db.insert_calls == 1 and len(db.written) == 2
    assert delivered["text"] == "two candidates"
    # the persisted rows are what the LLM was asked to summarise
    assert "AAPL" in client.calls[0]["messages"][0]["content"]
    assert result == {
        "run_id": "weekly-20260726T140000Z",
        "tickers": 2,
        "suggestions": 2,
        "message_id": 4242,
    }


def test_run_weekly_fails_loudly_with_no_tracked_tickers(monkeypatch):
    db = FakeDB(positions=[{"symbol": "USD", "asset_type": "cash"}])
    _wire(monkeypatch, chains=[])
    with pytest.raises(weekly.WeeklyError, match="no tracked tickers"):
        weekly.run_weekly(object(), db, object(), FakeAnthropic("x"), now=NOW)
