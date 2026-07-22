"""Unit tests for the historical IV backfill. No Terminal, no network, no DB.

The load-bearing test is `test_backfilled_row_is_identical_to_the_daily_job`.
Everything else is plumbing; that one is why the module is allowed to exist. A
backfill that derives even slightly different numbers puts a seam in
`iv_history`, and every IV rank spanning that seam is quietly wrong.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src import backfill, iv
from src.engine import black_scholes as bs
from src.engine import iv as engine_iv

RATE_PERCENT = 3.57
TRUE_VOL = 0.30

# A fortnight of sessions — weekends omitted, as the feed omits them.
SESSIONS = [dt.date(2024, 3, d) for d in (4, 5, 6, 7, 8, 11, 12, 13, 14, 15)]
# Three expiries, so the 30-DTE pick rolls mid-window as it does in real life.
EXPIRIES = [dt.date(2024, 4, 5), dt.date(2024, 4, 12), dt.date(2024, 4, 19)]
STRIKES = [95.0, 100.0, 105.0]


def _window(params: dict) -> tuple[dt.date, dt.date]:
    return (
        dt.datetime.strptime(params["start_date"], "%Y%m%d").date(),
        dt.datetime.strptime(params["end_date"], "%Y%m%d").date(),
    )


class RangeTheta:
    """A Terminal that answers date *ranges*, as the live one does.

    Serves single-day requests too (start == end), so the daily job and the
    backfill can run against the same feed and have their output compared.
    """

    def __init__(self, *, spots=None, expirations=None, strikes=None, vol=TRUE_VOL):
        self.spots = dict.fromkeys(SESSIONS, 100.0) if spots is None else spots
        self.expirations = EXPIRIES if expirations is None else expirations
        self.strikes = STRIKES if strikes is None else strikes
        self.vol = vol
        self.request_log: list[tuple[str, dict]] = []

    def quote(self, day, expiration, strike, right):
        """A closing quote consistent with `self.vol`, so IV round-trips."""
        fair = bs.price(
            right,
            self.spots[day],
            strike,
            bs.year_fraction((expiration - day).days),
            RATE_PERCENT / 100,
            self.vol,
        )
        return {
            "bid": round(fair - 0.02, 4),
            "ask": round(fair + 0.02, 4),
            "close": round(fair, 4),
            "created": f"{day.isoformat()}T20:15:00.000",
        }

    def get(self, path, **params):
        self.request_log.append((path, params))

        if path.endswith("/interest_rate/history/eod"):
            lo, hi = _window(params)
            return [
                {"rate": RATE_PERCENT, "created": f"{d.isoformat()}T18:00:00"}
                for d in sorted(self.spots)
                if lo <= d <= hi
            ]

        if path.endswith("/stock/history/eod"):
            lo, hi = _window(params)
            return [
                {"close": spot, "created": f"{d.isoformat()}T20:00:00"}
                for d, spot in sorted(self.spots.items())
                if lo <= d <= hi
            ]

        if path.endswith("/option/list/expirations"):
            return [{"expiration": e.isoformat()} for e in self.expirations]

        if path.endswith("/option/list/strikes"):
            return [{"strike": s} for s in self.strikes]

        if path.endswith("/option/history/eod"):
            lo, hi = _window(params)
            expiration = dt.date.fromisoformat(params["expiration"])
            strike = float(params["strike"])
            data = [
                self.quote(d, expiration, strike, params["right"])
                for d in sorted(self.spots)
                if lo <= d <= hi and d < expiration
            ]
            return [{"contract": {"strike": strike}, "data": data}] if data else []

        raise AssertionError(f"unexpected path {path}")


class FakeDB:
    def __init__(self, existing=None):
        self.existing = existing or []
        self.written: list[dict] = []

    def iv_for_ticker(self, ticker):
        return self.existing

    def upsert_iv(self, rows):
        self.written.extend(rows)
        return rows


def run_backfill(theta, db, ticker="AAPL", start=SESSIONS[0], end=SESSIONS[-1]):
    rates = backfill.fetch_rate_history(theta, start, end)
    return backfill.backfill_ticker(theta, db, ticker, start, end, rates)


# -- range chunking ---------------------------------------------------------


def test_chunks_never_exceed_the_servers_365_day_limit():
    """A longer window is HTTP 400, not a truncated answer."""
    chunks = list(backfill.date_chunks(dt.date(2023, 6, 1), dt.date(2026, 7, 22)))
    assert all((hi - lo).days < backfill.MAX_RANGE_DAYS for lo, hi in chunks)


def test_chunks_cover_the_range_exactly_once():
    """A gap silently loses months of history; an overlap wastes the request budget."""
    start, end = dt.date(2023, 6, 1), dt.date(2026, 7, 22)
    chunks = list(backfill.date_chunks(start, end))
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for (_, prev_hi), (next_lo, _) in zip(chunks, chunks[1:]):
        assert next_lo == prev_hi + dt.timedelta(days=1)


def test_single_day_range_is_one_chunk():
    day = dt.date(2024, 3, 4)
    assert list(backfill.date_chunks(day, day)) == [(day, day)]


# -- session dates ----------------------------------------------------------


def test_session_date_reads_the_date_half_of_created():
    assert backfill.session_date({"created": "2024-03-04T20:00:00.123"}) == dt.date(
        2024, 3, 4
    )


@pytest.mark.parametrize("value", [None, "", "not-a-date", "2024-13-45T00:00:00"])
def test_session_date_rejects_junk_rather_than_guessing(value):
    """An unparseable stamp must drop the row, never land it on the wrong day."""
    assert backfill.session_date({"created": value}) is None


# -- rate lookup ------------------------------------------------------------


def test_rate_uses_the_most_recent_publication_at_or_before_the_session():
    rates = [(dt.date(2024, 3, 1), 5.31), (dt.date(2024, 3, 4), 5.33)]
    assert backfill.rate_as_of(rates, dt.date(2024, 3, 5)) == 5.33
    # A Sunday takes Friday's rate — the same answer the daily job gives.
    assert backfill.rate_as_of(rates, dt.date(2024, 3, 3)) == 5.31


def test_rate_beyond_the_lookback_window_is_absent_not_stale():
    """Mirrors `src.iv.fetch_rate`: past the window, no rate rather than an old one."""
    assert backfill.rate_as_of([(dt.date(2024, 1, 1), 5.31)], dt.date(2024, 3, 5)) is None


# -- grouping ---------------------------------------------------------------


def test_grouping_collapses_sessions_onto_shared_contracts():
    """The entire cost saving: many sessions, one request per contract."""
    early = (dt.date(2024, 4, 5), 100.0)
    late = (dt.date(2024, 4, 12), 100.0)
    groups = backfill.group_by_contract(
        {
            dt.date(2024, 3, 4): early,
            dt.date(2024, 3, 5): early,
            dt.date(2024, 3, 6): late,
        }
    )
    assert groups[early] == [dt.date(2024, 3, 4), dt.date(2024, 3, 5)]
    assert groups[late] == [dt.date(2024, 3, 6)]


# -- the seam test ----------------------------------------------------------


def test_backfilled_row_is_identical_to_the_daily_job():
    """A backfilled reading must equal what `src.iv` would have written that day.

    Both paths run against the same feed: the daily job a day at a time, the
    backfill over ranges. Any divergence — expiry, strike, rate, day count — is
    a seam, and IV rank is a percentile over the series containing it.
    """
    day = SESSIONS[3]
    theta = RangeTheta()
    db = FakeDB()
    run_backfill(theta, db)

    backfilled = next(row for row in db.written if row["date"] == day.isoformat())
    assert backfilled == iv.reading_for(theta, "AAPL", day, RATE_PERCENT)


def test_every_session_recovers_the_true_volatility():
    """Sanity across the whole window, not just the one day compared above."""
    db = FakeDB()
    run_backfill(RangeTheta(), db)

    assert len(db.written) == len(SESSIONS)
    for row in db.written:
        assert row["atm_iv"] == pytest.approx(TRUE_VOL, abs=0.01)


def test_readings_carry_the_same_method_stamp_as_the_daily_job():
    """The stamp is what makes old and new rows comparable — it must not drift."""
    db = FakeDB()
    run_backfill(RangeTheta(), db)
    assert {row["method"] for row in db.written} == {engine_iv.METHOD}


# -- resumability -----------------------------------------------------------


def test_already_stored_sessions_are_skipped():
    """This runs for hours and will be interrupted; a re-run must not redo work."""
    stored = [{"date": d.isoformat(), "method": engine_iv.METHOD} for d in SESSIONS[:6]]
    db = FakeDB(existing=stored)

    assert run_backfill(RangeTheta(), db) == len(SESSIONS) - 6
    assert {row["date"] for row in db.written} == {d.isoformat() for d in SESSIONS[6:]}


def test_a_fully_backfilled_ticker_costs_no_option_requests():
    """Resumption must be cheap, not merely correct — requests are the real budget."""
    stored = [{"date": d.isoformat(), "method": engine_iv.METHOD} for d in SESSIONS]
    theta = RangeTheta()

    assert run_backfill(theta, FakeDB(existing=stored)) == 0
    assert not [p for p, _ in theta.request_log if p.endswith("/option/history/eod")]


def test_rows_from_a_different_method_do_not_count_as_done():
    """A method bump must rebuild the series, not leave it half one and half another."""
    stored = [{"date": d.isoformat(), "method": "bs-mid-45dte-v0"} for d in SESSIONS]
    db = FakeDB(existing=stored)
    assert run_backfill(RangeTheta(), db) == len(SESSIONS)


# -- cost -------------------------------------------------------------------


def test_ranged_fetching_beats_one_request_per_day():
    """The reason this is a separate program: per-day replay is ~69 hours."""
    theta = RangeTheta()
    run_backfill(theta, FakeDB())
    assert len(theta.request_log) < len(SESSIONS) * 5  # what `src.iv` would spend


# -- failure policy ---------------------------------------------------------


def test_a_session_with_no_solvable_leg_is_omitted_not_zero_filled():
    """A wrong reading poisons every percentile taken against it; absence does not."""
    theta = RangeTheta()
    blackout = SESSIONS[2]
    priced = theta.quote

    def blacked_out(day, expiration, strike, right):
        row = priced(day, expiration, strike, right)
        return row | {"bid": None, "ask": None, "close": None} if day == blackout else row

    theta.quote = blacked_out
    db = FakeDB()
    run_backfill(theta, db)

    assert blackout.isoformat() not in {row["date"] for row in db.written}
    assert len(db.written) == len(SESSIONS) - 1


def test_one_failing_ticker_does_not_sink_the_others():
    """History is expensive to recover — a broken symbol must never cost the rest."""

    class HalfBroken(RangeTheta):
        def get(self, path, **params):
            if params.get("symbol") == "BROKEN":
                raise RuntimeError("feed unavailable")
            return super().get(path, **params)

    results = backfill.backfill(
        HalfBroken(), FakeDB(), ["BROKEN", "AAPL"], SESSIONS[0], SESSIONS[-1]
    )
    assert "BROKEN" not in results
    assert results["AAPL"] == len(SESSIONS)


def test_no_tickers_is_an_error_not_a_silent_no_op():
    with pytest.raises(iv.IVError):
        backfill.backfill(RangeTheta(), FakeDB(), [], SESSIONS[0], SESSIONS[-1])


# -- CLI --------------------------------------------------------------------


def test_target_dte_is_not_exposed_as_a_flag():
    """It is part of the method stamp; a flag would invite a mixed series."""
    with pytest.raises(SystemExit):
        backfill.parse_args(["--target-dte", "45"])


def test_start_after_end_fails_loudly():
    assert backfill.main(["--start", "2026-01-01", "--end", "2025-01-01"]) == 1
