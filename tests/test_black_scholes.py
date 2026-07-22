"""Tests for the Black-Scholes engine.

These check the maths against properties that must hold regardless of
implementation — put-call parity, monotonicity, known reference values, and
round-tripping price -> implied vol -> price — rather than against numbers this
same code produced.
"""

from __future__ import annotations

import math

import pytest

from src.engine import black_scholes as bs

# A liquid, unremarkable contract: spot 100, strike 100, 30 days, 5% rates.
SPOT = 100.0
STRIKE = 100.0
YEARS = 30 / 365
RATE = 0.05
VOL = 0.30


# -- pricing ----------------------------------------------------------------


def test_atm_call_matches_a_reference_value():
    """Cross-checked against a standard Black-Scholes calculator."""
    value = bs.price(bs.CALL, SPOT, STRIKE, YEARS, RATE, VOL)
    assert value == pytest.approx(3.6, abs=0.05)


def test_put_call_parity_holds():
    """C - P = S*e^(-qT) - K*e^(-rT). Independent of the model's correctness."""
    call = bs.price(bs.CALL, SPOT, STRIKE, YEARS, RATE, VOL)
    put = bs.price(bs.PUT, SPOT, STRIKE, YEARS, RATE, VOL)
    expected = SPOT - STRIKE * math.exp(-RATE * YEARS)
    assert call - put == pytest.approx(expected, abs=1e-9)


def test_parity_holds_with_a_dividend_yield():
    call = bs.price(bs.CALL, SPOT, STRIKE, YEARS, RATE, VOL, dividend=0.03)
    put = bs.price(bs.PUT, SPOT, STRIKE, YEARS, RATE, VOL, dividend=0.03)
    expected = SPOT * math.exp(-0.03 * YEARS) - STRIKE * math.exp(-RATE * YEARS)
    assert call - put == pytest.approx(expected, abs=1e-9)


def test_price_rises_with_volatility():
    cheap = bs.price(bs.CALL, SPOT, STRIKE, YEARS, RATE, 0.20)
    dear = bs.price(bs.CALL, SPOT, STRIKE, YEARS, RATE, 0.60)
    assert dear > cheap


def test_price_never_falls_below_intrinsic():
    deep_itm = bs.price(bs.CALL, 150.0, 100.0, YEARS, RATE, VOL)
    assert deep_itm >= bs.intrinsic(bs.CALL, 150.0, 100.0)


def test_expired_option_is_worth_intrinsic():
    assert bs.price(bs.CALL, 120.0, 100.0, 0.0, RATE, VOL) == pytest.approx(20.0)
    assert bs.price(bs.PUT, 120.0, 100.0, 0.0, RATE, VOL) == pytest.approx(0.0)


# -- delta ------------------------------------------------------------------


def test_atm_call_delta_is_near_half():
    value = bs.delta(bs.CALL, SPOT, STRIKE, YEARS, RATE, VOL)
    assert 0.5 < value < 0.6  # slightly above 0.5 from the drift term


def test_put_delta_is_negative_and_parity_holds():
    """Call delta - put delta = e^(-qT), which is 1 with no dividend."""
    call = bs.delta(bs.CALL, SPOT, STRIKE, YEARS, RATE, VOL)
    put = bs.delta(bs.PUT, SPOT, STRIKE, YEARS, RATE, VOL)
    assert put < 0
    assert call - put == pytest.approx(1.0, abs=1e-9)


def test_delta_stays_within_bounds_across_moneyness():
    for spot in (50.0, 90.0, 100.0, 110.0, 200.0):
        call = bs.delta(bs.CALL, spot, STRIKE, YEARS, RATE, VOL)
        assert 0.0 <= call <= 1.0


def test_covered_call_target_delta_lands_out_of_the_money():
    """Sanity check against how rules_vN.yaml will use this.

    A ~0.25-0.30 delta call should sit above spot — that is the whole premise
    of covered calls: sell upside you do not expect to be assigned on.
    """
    strikes = [100.0, 105.0, 110.0, 115.0]
    deltas = {k: bs.delta(bs.CALL, SPOT, k, YEARS, RATE, VOL) for k in strikes}
    chosen = min(strikes, key=lambda k: abs(deltas[k] - 0.275))
    assert chosen > SPOT


def test_expired_option_delta_is_degenerate():
    assert bs.delta(bs.CALL, 120.0, 100.0, 0.0, RATE, VOL) == 1.0
    assert bs.delta(bs.CALL, 80.0, 100.0, 0.0, RATE, VOL) == 0.0
    assert bs.delta(bs.PUT, 80.0, 100.0, 0.0, RATE, VOL) == -1.0


# -- implied volatility -----------------------------------------------------


@pytest.mark.parametrize("vol", [0.10, 0.25, 0.45, 0.80, 1.50])
@pytest.mark.parametrize("right", [bs.CALL, bs.PUT])
def test_implied_vol_round_trips(right, vol):
    """price(vol) -> implied_vol -> the same vol back."""
    target = bs.price(right, SPOT, STRIKE, YEARS, RATE, vol)
    solved = bs.implied_vol(right, target, SPOT, STRIKE, YEARS, RATE)
    assert solved == pytest.approx(vol, abs=1e-4)


def test_implied_vol_round_trips_away_from_the_money():
    for strike in (80.0, 90.0, 110.0, 125.0):
        target = bs.price(bs.CALL, SPOT, strike, YEARS, RATE, 0.35)
        solved = bs.implied_vol(bs.CALL, target, SPOT, strike, YEARS, RATE)
        assert solved == pytest.approx(0.35, abs=1e-3)


def test_price_below_intrinsic_is_unanswerable_not_zero():
    """A crossed or stale quote must not enter iv_history as a real reading."""
    assert bs.implied_vol(bs.CALL, 5.0, 150.0, 100.0, YEARS, RATE) is None


def test_a_quote_sitting_on_intrinsic_is_unanswerable_not_a_floor_vol():
    """The deep-ITM case: priced, not crossed, but carrying no time value.

    The real numbers, seen on 2026-07-21 — AAPL calls at 110 against a spot of
    327.74, quoted within $0.26 of intrinsic on zero volume. Bisection would
    converge onto the bottom of the bracket and report a near-zero vol, which
    prices the contract at delta 1.0. Sprint 5 selects on delta, so that floor
    would read as a genuine signal rather than as an unquoted contract.
    """
    spot, strike, years = 327.74, 110.0, 31 / 365
    on_intrinsic = spot - strike + 0.26

    assert bs.implied_vol(bs.CALL, on_intrinsic, spot, strike, years, RATE) is None
    # A contract with real time value on the same strike still solves.
    assert bs.implied_vol(bs.CALL, on_intrinsic + 5.0, spot, strike, years, RATE)


def test_absurdly_rich_price_is_rejected():
    assert bs.implied_vol(bs.CALL, 95.0, 100.0, 100.0, YEARS, RATE) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"option_price": 0.0},
        {"option_price": -1.0},
        {"years": 0.0},
        {"spot": 0.0},
        {"strike": 0.0},
    ],
)
def test_degenerate_inputs_return_none(kwargs):
    args = {
        "right": bs.CALL,
        "option_price": 3.0,
        "spot": SPOT,
        "strike": STRIKE,
        "years": YEARS,
        "rate": RATE,
    }
    args.update(kwargs)
    assert bs.implied_vol(**args) is None


# -- helpers ----------------------------------------------------------------


def test_mid_price_prefers_the_quote_midpoint():
    assert bs.mid_price(2.83, 2.95, close=2.94) == pytest.approx(2.89)


def test_mid_price_falls_back_to_close_when_the_quote_is_unusable():
    assert bs.mid_price(None, None, close=2.94) == 2.94
    assert bs.mid_price(3.0, 2.0, close=2.94) == 2.94  # crossed quote
    assert bs.mid_price(0.0, 0.0, close=2.94) == 2.94


def test_mid_price_is_none_when_nothing_is_usable():
    assert bs.mid_price(None, None, close=None) is None
    assert bs.mid_price(None, None, close=0.0) is None


def test_year_fraction_uses_365_days():
    assert bs.year_fraction(365) == 1.0
    assert bs.year_fraction(30) == pytest.approx(30 / 365)
