"""Unit tests for the IV snapshot. No Terminal, no network, no database."""

from __future__ import annotations

import datetime as dt

import pytest

from src import iv
from src.engine import black_scholes as bs
from src.engine import iv as engine_iv

AS_OF = dt.date(2026, 7, 21)
EXPIRY = dt.date(2026, 8, 21)
SPOT = 100.0
STRIKE = 100.0
RATE_PERCENT = 3.57


def quote_at(vol: float, right: str = bs.CALL) -> dict:
    """A closing quote consistent with a known volatility."""
    days = (EXPIRY - AS_OF).days
    fair = bs.price(right, SPOT, STRIKE, bs.year_fraction(days), RATE_PERCENT / 100, vol)
    return {"bid": fair - 0.02, "ask": fair + 0.02, "close": fair}


class FakeTheta:
    """Answers the v3 paths `src.iv` calls, from canned data."""

    def __init__(self, *, spot=SPOT, expirations=None, strikes=None, legs=None, rate=RATE_PERCENT):
        self.spot = spot
        self.expirations = expirations if expirations is not None else [EXPIRY]
        self.strikes = strikes if strikes is not None else [95.0, 100.0, 105.0]
        self.legs = legs if legs is not None else {
            bs.CALL: quote_at(0.30, bs.CALL),
            bs.PUT: quote_at(0.30, bs.PUT),
        }
        self.rate = rate
        self.calls: list[str] = []

    def get(self, path, **params):
        self.calls.append(path)
        if path.endswith("/interest_rate/history/eod"):
            return [] if self.rate is None else [{"rate": self.rate, "created": "2026-07-21"}]
        if path.endswith("/stock/history/eod"):
            return [] if self.spot is None else [{"close": self.spot}]
        if path.endswith("/option/list/expirations"):
            return [{"expiration": e.isoformat()} for e in self.expirations]
        if path.endswith("/option/list/strikes"):
            return [{"strike": s} for s in self.strikes]
        if path.endswith("/option/history/eod"):
            quote = self.legs.get(params["right"])
            return [{"data": [quote]}] if quote else []
        raise AssertionError(f"unexpected path {path}")


class FakeDB:
    def __init__(self, positions=None):
        self.positions = positions or []
        self.written: list[dict] = []

    def latest_positions(self):
        return self.positions

    def upsert_iv(self, rows):
        self.written = rows
        return rows


# -- assembly ---------------------------------------------------------------


def test_reading_carries_full_provenance():
    """atm_iv is derived, so its inputs must travel with it."""
    row = iv.build_reading(
        "AAPL", AS_OF, SPOT, EXPIRY, STRIKE, RATE_PERCENT,
        quote_at(0.30, bs.CALL), quote_at(0.30, bs.PUT),
    )
    assert row["ticker"] == "AAPL"
    assert row["date"] == "2026-07-21"
    assert row["atm_iv"] == pytest.approx(0.30, abs=1e-3)
    assert row["call_iv"] == pytest.approx(0.30, abs=1e-3)
    assert row["put_iv"] == pytest.approx(0.30, abs=1e-3)
    assert row["expiration"] == "2026-08-21"
    assert row["strike"] == STRIKE
    assert row["spot"] == SPOT
    assert row["rate"] == pytest.approx(0.0357)
    assert row["method"] == engine_iv.METHOD


def test_one_unusable_leg_still_yields_a_reading():
    row = iv.build_reading(
        "AAPL", AS_OF, SPOT, EXPIRY, STRIKE, RATE_PERCENT, quote_at(0.30, bs.CALL), None
    )
    assert row["atm_iv"] == pytest.approx(0.30, abs=1e-3)
    assert row["put_iv"] is None


def test_no_solvable_leg_yields_no_row_rather_than_a_zero():
    """A bad value would distort every percentile computed against it."""
    assert iv.build_reading(
        "AAPL", AS_OF, SPOT, EXPIRY, STRIKE, RATE_PERCENT, None, None
    ) is None


def test_a_crossed_quote_does_not_become_a_reading():
    junk = {"bid": 0.0, "ask": 0.0, "close": 0.0}
    assert iv.build_reading(
        "AAPL", AS_OF, SPOT, EXPIRY, STRIKE, RATE_PERCENT, junk, junk
    ) is None


def test_missing_rate_is_treated_as_zero_not_a_failure():
    row = iv.build_reading(
        "AAPL", AS_OF, SPOT, EXPIRY, STRIKE, None,
        quote_at(0.30, bs.CALL), quote_at(0.30, bs.PUT),
    )
    assert row["rate"] == 0.0


# -- per-ticker derivation --------------------------------------------------


def test_reading_for_derives_from_the_terminal():
    row = iv.reading_for(FakeTheta(), "AAPL", AS_OF, RATE_PERCENT)
    assert row["atm_iv"] == pytest.approx(0.30, abs=1e-3)
    assert row["strike"] == 100.0


def test_missing_underlying_close_is_named():
    with pytest.raises(iv.IVError, match="no underlying close"):
        iv.reading_for(FakeTheta(spot=None), "AAPL", AS_OF, RATE_PERCENT)


def test_no_expiry_near_the_target_is_named():
    near = [AS_OF + dt.timedelta(days=2)]
    with pytest.raises(iv.IVError, match="no expiry near"):
        iv.reading_for(FakeTheta(expirations=near), "AAPL", AS_OF, RATE_PERCENT)


def test_no_strikes_is_named():
    with pytest.raises(iv.IVError, match="no strikes listed"):
        iv.reading_for(FakeTheta(strikes=[]), "AAPL", AS_OF, RATE_PERCENT)


def test_unsolvable_legs_are_named():
    with pytest.raises(iv.IVError, match="neither leg solvable"):
        iv.reading_for(FakeTheta(legs={}), "AAPL", AS_OF, RATE_PERCENT)


# -- snapshot ---------------------------------------------------------------


def test_snapshot_writes_one_row_per_ticker():
    db = FakeDB()
    written = iv.snapshot(FakeTheta(), db, ["AAPL", "MSFT"], AS_OF)
    assert len(written) == 2
    assert {r["ticker"] for r in written} == {"AAPL", "MSFT"}
    assert db.written == written


def test_one_bad_ticker_does_not_cost_the_others_their_reading():
    """History cannot be backfilled; one symbol must not sink the day."""

    class OneBadTicker(FakeTheta):
        def get(self, path, **params):
            if path.endswith("/stock/history/eod") and params["symbol"] == "BADCO":
                return []
            return super().get(path, **params)

    db = FakeDB()
    with pytest.raises(iv.PartialIVError) as caught:
        iv.snapshot(OneBadTicker(), db, ["AAPL", "BADCO"], AS_OF)

    assert [r["ticker"] for r in db.written] == ["AAPL"]
    assert "BADCO" in caught.value.excluded
    assert caught.value.written == db.written


def test_partial_is_still_an_iv_error():
    assert issubclass(iv.PartialIVError, iv.IVError)


def test_all_tickers_failing_writes_nothing():
    db = FakeDB()
    with pytest.raises(iv.IVError, match="no readings could be derived"):
        iv.snapshot(FakeTheta(spot=None), db, ["AAPL"], AS_OF)
    assert db.written == []


def test_empty_ticker_list_fails_loudly():
    with pytest.raises(iv.IVError, match="no tickers"):
        iv.snapshot(FakeTheta(), FakeDB(), [], AS_OF)


def test_a_missing_rate_does_not_stop_the_snapshot():
    db = FakeDB()
    written = iv.snapshot(FakeTheta(rate=None), db, ["AAPL"], AS_OF)
    assert written[0]["rate"] == 0.0


def test_rate_lookback_picks_the_most_recent_published_value():
    """Monday must reach back to Friday — rates pause on weekends."""

    class MultiDayRate(FakeTheta):
        def get(self, path, **params):
            if path.endswith("/interest_rate/history/eod"):
                return [
                    {"rate": 3.50, "created": "2026-07-17"},
                    {"rate": 3.57, "created": "2026-07-20"},
                    {"rate": 3.55, "created": "2026-07-18"},
                ]
            return super().get(path, **params)

    assert iv.fetch_rate(MultiDayRate(), AS_OF) == 3.57


# -- session selection ------------------------------------------------------


class SessionTheta(FakeTheta):
    """Has stock data only on the dates listed in `available`."""

    def __init__(self, available: set[dt.date]):
        super().__init__()
        self.available = available
        self.probed: list[str] = []

    def get(self, path, **params):
        if path.endswith("/stock/history/eod"):
            self.probed.append(params["start_date"])
            day = dt.datetime.strptime(params["start_date"], "%Y%m%d").date()
            return [{"close": 500.0}] if day in self.available else []
        return super().get(path, **params)


def test_latest_session_skips_a_day_with_no_published_data():
    """FREE data lags a day, so today usually has nothing."""
    theta = SessionTheta({dt.date(2026, 7, 21)})
    assert iv.latest_session(theta, dt.date(2026, 7, 22)) == dt.date(2026, 7, 21)


def test_latest_session_walks_back_over_a_weekend():
    """Monday's job must land on Friday, not on Sunday."""
    friday = dt.date(2026, 7, 17)
    theta = SessionTheta({friday})
    assert iv.latest_session(theta, dt.date(2026, 7, 20)) == friday


def test_latest_session_uses_the_date_itself_when_data_exists():
    day = dt.date(2026, 7, 21)
    assert iv.latest_session(SessionTheta({day}), day) == day


def test_latest_session_fails_loudly_rather_than_reaching_for_stale_data():
    with pytest.raises(iv.IVError, match="feed looks stale"):
        iv.latest_session(SessionTheta(set()), dt.date(2026, 7, 22))


def test_latest_session_stops_at_the_lookback_limit():
    theta = SessionTheta({dt.date(2026, 6, 1)})
    with pytest.raises(iv.IVError):
        iv.latest_session(theta, dt.date(2026, 7, 22), max_lookback=3)
    assert len(theta.probed) == 4  # the day itself plus three back


# -- ticker selection -------------------------------------------------------


def test_tracked_tickers_takes_equities_from_the_latest_sync():
    db = FakeDB(
        positions=[
            {"symbol": "AAPL", "asset_type": "equity"},
            {"symbol": "MSFT", "asset_type": "equity"},
            {"symbol": "USD", "asset_type": "cash"},
            {"symbol": "BTC", "asset_type": "crypto"},
            {"symbol": "RIOT  260821C00025000", "asset_type": "option"},
        ]
    )
    assert iv.tracked_tickers(db) == ["AAPL", "MSFT"]


def test_tracked_tickers_deduplicates_across_accounts():
    db = FakeDB(
        positions=[
            {"symbol": "AAPL", "asset_type": "equity"},
            {"symbol": "AAPL", "asset_type": "equity"},
        ]
    )
    assert iv.tracked_tickers(db) == ["AAPL"]
