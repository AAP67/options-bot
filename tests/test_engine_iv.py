"""Tests for the ATM IV selection rules.

What matters here is not that a particular expiry is "right" but that the
choice is *deterministic and stable*: iv_history is only meaningful if today's
reading was picked by the same rule as every previous one.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.engine import black_scholes as bs
from src.engine import iv

AS_OF = dt.date(2026, 7, 21)


def d(days: int) -> dt.date:
    return AS_OF + dt.timedelta(days=days)


# -- expiry selection -------------------------------------------------------


# The nearest/min/tie logic is exercised on synthetic dates with the weekday
# restriction lifted; the Friday restriction has its own tests below on real
# calendar dates. Both are properties of the same function.


def test_picks_the_expiry_nearest_thirty_days():
    assert iv.pick_expiration([d(3), d(10), d(31), d(60)], AS_OF, weekday=None) == d(31)


def test_skips_expiries_inside_the_minimum_window():
    """A 3-day option's IV measures pin risk, not the volatility level."""
    assert iv.pick_expiration([d(3), d(45)], AS_OF, weekday=None) == d(45)


def test_never_picks_a_past_or_same_day_expiry():
    assert iv.pick_expiration([d(-10), d(0), d(40)], AS_OF, weekday=None) == d(40)


def test_ties_break_toward_the_longer_expiry():
    """25 and 35 are equidistant from 30; the longer IV is the steadier one."""
    assert iv.pick_expiration([d(25), d(35)], AS_OF, weekday=None) == d(35)


def test_no_usable_expiry_returns_none():
    assert iv.pick_expiration([d(1), d(2)], AS_OF, weekday=None) is None
    assert iv.pick_expiration([], AS_OF, weekday=None) is None


def test_target_dte_is_configurable():
    assert (
        iv.pick_expiration([d(31), d(45), d(60)], AS_OF, target_dte=60, weekday=None)
        == d(60)
    )


def test_selection_is_stable_across_days_as_expiries_roll():
    """The same rule applied daily must not oscillate between two expiries."""
    expiries = [dt.date(2026, 8, 21), dt.date(2026, 9, 18), dt.date(2026, 10, 16)]
    picks = [
        iv.pick_expiration(expiries, dt.date(2026, 7, 21) + dt.timedelta(days=n))
        for n in range(0, 5)
    ]
    assert len(set(picks)) == 1, f"selection flipped within a week: {picks}"


# -- Friday restriction (the v2 fix) ----------------------------------------


def test_daily_expiries_are_skipped_for_the_nearest_friday():
    """The bug that motivated v2: a Wed daily nearer 30 DTE is passed over.

    Real 2026 dates. From a Mon 2026-06-01 session, the Wed 2026-07-01 daily is
    exactly 30 days out but was only listed weeks before expiry, so historically
    it had no quotes. The Fri 2026-07-10 weekly is further out but always
    resolves — that is the one the rule must pick.
    """
    session = dt.date(2026, 6, 1)
    wed_daily = dt.date(2026, 7, 1)   # weekday() == 2, exactly 30 DTE
    fri_weekly = dt.date(2026, 7, 10)  # weekday() == 4, 39 DTE
    assert iv.pick_expiration([wed_daily, fri_weekly], session) == fri_weekly


def test_only_fridays_are_ever_returned():
    """Sweep a run of sessions; every pick must land on a Friday."""
    expiries = [dt.date(2026, 8, 1) + dt.timedelta(days=n) for n in range(0, 60)]
    for n in range(0, 20):
        pick = iv.pick_expiration(expiries, dt.date(2026, 7, 1) + dt.timedelta(days=n))
        assert pick is not None and pick.weekday() == iv.FRIDAY, pick


def test_no_friday_in_range_returns_none_rather_than_a_daily():
    """Better a gap than a reading against a contract with no history."""
    only_dailies = [dt.date(2026, 7, 1), dt.date(2026, 7, 2)]  # Wed, Thu
    assert iv.pick_expiration(only_dailies, dt.date(2026, 6, 1)) is None


# -- strike selection -------------------------------------------------------


def test_picks_the_strike_nearest_spot():
    assert iv.pick_atm_strike([20.0, 22.5, 25.0, 27.5], 23.96) == 25.0


def test_exact_match_wins():
    assert iv.pick_atm_strike([20.0, 25.0, 30.0], 25.0) == 25.0


def test_strike_ties_break_upward():
    assert iv.pick_atm_strike([20.0, 30.0], 25.0) == 30.0


def test_no_strikes_returns_none():
    assert iv.pick_atm_strike([], 25.0) is None
    assert iv.pick_atm_strike([20.0], 0.0) is None


# -- implied vol from quotes ------------------------------------------------


def test_quote_implied_vol_recovers_the_input_volatility():
    """Price a contract at a known vol, quote it, and solve back."""
    spot, strike, days, rate, vol = 100.0, 100.0, 30.0, 0.0357, 0.42
    fair = bs.price(bs.CALL, spot, strike, bs.year_fraction(days), rate, vol)
    solved = iv.quote_implied_vol(
        bs.CALL, bid=fair - 0.01, ask=fair + 0.01, close=None,
        spot=spot, strike=strike, days_to_expiry=days, rate=rate,
    )
    assert solved == pytest.approx(vol, abs=1e-3)


def test_unusable_quote_yields_none():
    assert iv.quote_implied_vol(
        bs.CALL, bid=None, ask=None, close=None,
        spot=100.0, strike=100.0, days_to_expiry=30.0, rate=0.05,
    ) is None


def test_quote_below_intrinsic_yields_none_not_zero():
    """A stale deep-ITM quote must not enter the history as 0% vol."""
    assert iv.quote_implied_vol(
        bs.CALL, bid=1.0, ask=1.1, close=None,
        spot=150.0, strike=100.0, days_to_expiry=30.0, rate=0.05,
    ) is None


# -- combining --------------------------------------------------------------


def test_combine_averages_both_sides():
    assert iv.combine(0.40, 0.50) == pytest.approx(0.45)


def test_combine_tolerates_one_missing_side():
    assert iv.combine(0.40, None) == 0.40
    assert iv.combine(None, 0.50) == 0.50


def test_combine_returns_none_when_neither_side_is_usable():
    assert iv.combine(None, None) is None
    assert iv.combine(0.0, None) is None


# -- rate -------------------------------------------------------------------


def test_percent_becomes_a_decimal():
    assert iv.annual_rate_from_percent(3.57) == pytest.approx(0.0357)


def test_missing_rate_falls_back_to_zero_rather_than_guessing():
    assert iv.annual_rate_from_percent(None) == 0.0
