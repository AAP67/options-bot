"""Unit tests for chain fetch. No Terminal, no network, no database.

Two properties matter most here, and both are about *not deciding things*:
the chain comes back whole (filtering is Sprint 5's job, driven by rules), and
a number that cannot be derived is None rather than invented.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src import chain
from src.engine import black_scholes as bs
from src.engine import iv as engine_iv

AS_OF = dt.date(2026, 7, 21)
EXPIRY = dt.date(2026, 8, 21)
SPOT = 100.0
RATE_PERCENT = 3.57
TRUE_VOL = 0.30
STRIKES = [90.0, 95.0, 100.0, 105.0, 110.0]


def quote_at(strike: float, right: str, vol: float = TRUE_VOL) -> dict:
    """A closing quote consistent with a known volatility."""
    fair = bs.price(
        right, SPOT, strike, bs.year_fraction((EXPIRY - AS_OF).days), RATE_PERCENT / 100, vol
    )
    return {
        "bid": round(fair - 0.02, 4),
        "ask": round(fair + 0.02, 4),
        "close": round(fair, 4),
        "volume": 1234,
    }


class FakeTheta:
    """Answers the paths `src.chain` calls, chain-style."""

    def __init__(self, *, spot=SPOT, strikes=None, rate=RATE_PERCENT, quotes=None):
        self.spot = spot
        self.strikes = STRIKES if strikes is None else strikes
        self.rate = rate
        self.quotes = quotes
        self.request_log: list[str] = []

    def get(self, path, **params):
        self.request_log.append(path)

        if path.endswith("/interest_rate/history/eod"):
            return [] if self.rate is None else [{"rate": self.rate, "created": "2026-07-21"}]

        if path.endswith("/stock/history/eod"):
            return [] if self.spot is None else [{"close": self.spot}]

        if path.endswith("/option/history/eod"):
            assert params["right"] == chain.BOTH_RIGHTS, "chain must be one request"
            assert "strike" not in params, "chain must not fetch per strike"
            envelopes = []
            for strike in self.strikes:
                # The live API reports `right` upper-case and echoes the contract.
                for api_right, right in (("CALL", bs.CALL), ("PUT", bs.PUT)):
                    quote = (
                        self.quotes(strike, right)
                        if self.quotes
                        else quote_at(strike, right)
                    )
                    if quote is not None:
                        envelopes.append(
                            {
                                "contract": {
                                    "symbol": "AAPL",
                                    "expiration": EXPIRY.isoformat(),
                                    "strike": strike,
                                    "right": api_right,
                                },
                                "data": [quote],
                            }
                        )
            return envelopes

        raise AssertionError(f"unexpected path {path}")


def fetch(theta=None):
    return chain.fetch_chain(theta or FakeTheta(), "AAPL", EXPIRY, AS_OF)


# -- right normalisation ----------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"), [("CALL", bs.CALL), ("put", bs.PUT), ("Call", bs.CALL)]
)
def test_api_rights_map_onto_the_engines_constants(given, expected):
    """The API says CALL; Black-Scholes says call. A mismatch misprices silently."""
    assert chain.normalise_right(given) == expected


@pytest.mark.parametrize("given", [None, "", "straddle", 7])
def test_unrecognised_rights_are_rejected(given):
    assert chain.normalise_right(given) is None


# -- the chain comes back whole ---------------------------------------------


def test_every_listed_contract_is_returned():
    """Fetch only, no filtering — Sprint 5 decides what is eligible, not this."""
    rows = fetch()
    assert len(rows) == len(STRIKES) * 2
    assert {row["strike"] for row in rows} == set(STRIKES)
    assert {row["right"] for row in rows} == {bs.CALL, bs.PUT}


def test_rows_are_sorted_by_right_then_strike():
    rows = fetch()
    assert rows == sorted(rows, key=lambda r: (r["right"], r["strike"]))


def test_deep_out_of_the_money_contracts_are_not_dropped():
    """A far strike is still a fact. Liquidity floors are a Sprint 5 rule."""
    theta = FakeTheta(strikes=[10.0, 100.0, 500.0])
    assert {row["strike"] for row in fetch(theta)} == {10.0, 100.0, 500.0}


def test_the_whole_chain_costs_one_request():
    """At 20 requests/minute, per-contract fetching would be unusable."""
    theta = FakeTheta()
    fetch(theta)
    assert theta.request_log.count("/v3/option/history/eod") == 1


# -- derived numbers --------------------------------------------------------


def test_iv_round_trips_back_to_the_volatility_that_priced_the_quote():
    for row in fetch():
        assert row["iv"] == pytest.approx(TRUE_VOL, abs=0.01)


def test_call_and_put_deltas_have_the_right_signs_and_magnitudes():
    """This is what strike selection reads; a sign error would invert the book."""
    rows = {(r["right"], r["strike"]): r for r in fetch()}
    atm_call = rows[(bs.CALL, 100.0)]
    atm_put = rows[(bs.PUT, 100.0)]

    assert 0.0 < atm_call["delta"] < 1.0
    assert -1.0 < atm_put["delta"] < 0.0
    # An OTM call is further from the money, so nearer zero delta.
    assert rows[(bs.CALL, 110.0)]["delta"] < atm_call["delta"]


def test_delta_falls_monotonically_as_call_strikes_rise():
    calls = sorted(
        (r for r in fetch() if r["right"] == bs.CALL), key=lambda r: r["strike"]
    )
    deltas = [r["delta"] for r in calls]
    assert deltas == sorted(deltas, reverse=True)


def test_rows_carry_the_inputs_behind_the_derived_numbers():
    """IV and delta are computed, so their inputs must travel with them."""
    row = fetch()[0]
    assert row["spot"] == SPOT
    assert row["rate"] == pytest.approx(RATE_PERCENT / 100)
    assert row["dte"] == (EXPIRY - AS_OF).days
    assert row["method"] == engine_iv.METHOD


# -- the free-tier gaps -----------------------------------------------------


def test_volume_is_carried_through():
    """The only liquidity signal the FREE tier gives."""
    assert all(row["volume"] == 1234 for row in fetch())


def test_open_interest_is_none_rather_than_absent_or_faked():
    """It needs the VALUE plan. Present-and-None says 'unavailable' out loud;
    a missing key would read as an oversight and a zero would read as illiquid."""
    assert all(row["open_interest"] is None for row in fetch())


# -- unsolvable contracts ---------------------------------------------------


def test_a_contract_with_no_quote_keeps_its_row_but_has_no_greeks():
    """Absence of a Greek is a fact Sprint 5 may want; a fabricated one is a bug."""
    def quotes(strike, right):
        if strike == 110.0:
            return {"bid": None, "ask": None, "close": None, "volume": 0}
        return quote_at(strike, right)

    rows = {(r["right"], r["strike"]): r for r in fetch(FakeTheta(quotes=quotes))}
    dead = rows[(bs.CALL, 110.0)]

    assert dead["mid"] is None
    assert dead["iv"] is None
    assert dead["delta"] is None
    # Still present — dropping it would be a filtering decision.
    assert len(rows) == len(STRIKES) * 2


def test_a_contract_without_a_strike_is_dropped():
    """Unidentifiable, so unpriceable and unactionable."""
    assert chain.build_contract(
        "AAPL", AS_OF, EXPIRY, SPOT, RATE_PERCENT,
        {"right": "CALL"}, quote_at(100.0, bs.CALL),
    ) is None


def test_a_contract_with_an_unreadable_right_is_dropped():
    assert chain.build_contract(
        "AAPL", AS_OF, EXPIRY, SPOT, RATE_PERCENT,
        {"right": "SPREAD", "strike": 100.0}, quote_at(100.0, bs.CALL),
    ) is None


# -- failure policy ---------------------------------------------------------


def test_a_missing_underlying_close_fails_loudly():
    """Every IV and delta in the chain depends on spot — never guess it."""
    with pytest.raises(chain.IVError):
        fetch(FakeTheta(spot=None))


def test_an_empty_chain_fails_rather_than_returning_nothing():
    """Silence would look like 'no candidates' to Sprint 5 (iron rule #4)."""
    with pytest.raises(chain.IVError):
        fetch(FakeTheta(strikes=[]))


def test_a_missing_rate_falls_back_to_zero_like_the_daily_job():
    """At 30-45 DTE the discount term is worth a fraction of a vol point."""
    rows = fetch(FakeTheta(rate=None))
    assert all(row["rate"] == 0.0 for row in rows)


def test_a_supplied_rate_is_not_refetched():
    """A caller sweeping many chains should pay for the rate once."""
    theta = FakeTheta()
    chain.fetch_chain(theta, "AAPL", EXPIRY, AS_OF, rate_percent=RATE_PERCENT)
    assert not [p for p in theta.request_log if p.endswith("/interest_rate/history/eod")]
