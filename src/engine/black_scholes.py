"""Black-Scholes pricing, delta, and implied volatility.

ThetaData gates implied volatility and Greeks behind its STANDARD plan ($80/mo),
but a FREE key returns option EOD quotes and underlying prices — everything
Black-Scholes needs to derive both. So the bot computes them rather than buying
them.

Absolute accuracy matters less than it looks: IV rank is a *percentile against
this ticker's own history*, so a consistent method matters more than matching
any particular vendor's number to the decimal. What would break the signal is
changing the method midway — the history would no longer be comparable.

Pure functions, no I/O, stdlib only (iron rule #1: the engine computes, the
LLM only narrates).
"""

from __future__ import annotations

import math

# Bisection bounds for the volatility solve. 500% annualised is far beyond any
# equity option that could pass the liquidity filters; below 0.01% the price is
# indistinguishable from intrinsic value at double precision.
_MIN_VOL = 0.0001
_MAX_VOL = 5.0
_VOL_TOLERANCE = 1e-8
_MAX_ITERATIONS = 200

CALL = "call"
PUT = "put"


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(spot: float, strike: float, years: float, rate: float, dividend: float, vol: float) -> float:
    return (
        math.log(spot / strike) + (rate - dividend + 0.5 * vol * vol) * years
    ) / (vol * math.sqrt(years))


def price(
    right: str,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    vol: float,
    dividend: float = 0.0,
) -> float:
    """Black-Scholes price of a European option.

    `years` is time to expiry in years, `rate` the continuously-compounded
    risk-free rate, `dividend` the continuous dividend yield. American exercise
    is ignored: for the ~30-45 DTE, near-the-money contracts this strategy
    writes, the early-exercise premium is small, and it cancels out of a
    percentile ranking anyway.
    """
    if years <= 0 or vol <= 0:
        # At or past expiry the option is worth its intrinsic value.
        return intrinsic(right, spot, strike)

    d1 = _d1(spot, strike, years, rate, dividend, vol)
    d2 = d1 - vol * math.sqrt(years)
    discounted_spot = spot * math.exp(-dividend * years)
    discounted_strike = strike * math.exp(-rate * years)

    if right == CALL:
        return discounted_spot * _norm_cdf(d1) - discounted_strike * _norm_cdf(d2)
    return discounted_strike * _norm_cdf(-d2) - discounted_spot * _norm_cdf(-d1)


def intrinsic(right: str, spot: float, strike: float) -> float:
    """Value at expiry — the floor any option price must respect."""
    return max(spot - strike, 0.0) if right == CALL else max(strike - spot, 0.0)


def delta(
    right: str,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    vol: float,
    dividend: float = 0.0,
) -> float:
    """Option delta. Positive for calls, negative for puts.

    This is what strike selection reads: `rules_vN.yaml` targets ~0.25-0.30 for
    covered calls and ~0.20-0.30 for cash-secured puts.
    """
    if years <= 0 or vol <= 0:
        # Degenerate: already expired, so delta is 0 or ±1.
        in_the_money = intrinsic(right, spot, strike) > 0
        if not in_the_money:
            return 0.0
        return 1.0 if right == CALL else -1.0

    d1 = _d1(spot, strike, years, rate, dividend, vol)
    discount = math.exp(-dividend * years)
    if right == CALL:
        return discount * _norm_cdf(d1)
    return -discount * _norm_cdf(-d1)


def implied_vol(
    right: str,
    option_price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    dividend: float = 0.0,
) -> float | None:
    """Solve for the volatility that reproduces `option_price`.

    Bisection rather than Newton-Raphson: it cannot diverge, needs no vega, and
    converges in ~40 iterations over the bracket — irrelevant next to the
    network time of fetching the quote. Robustness matters more than speed in
    an unattended nightly job.

    Returns None when no volatility can produce the price — typically a quote
    below intrinsic value (stale or crossed market). None means "unanswerable",
    never zero: a zero would silently enter the IV history as a real reading.
    """
    if option_price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None

    # A price below intrinsic has no solution at any volatility.
    if option_price < intrinsic(right, spot, strike) - 1e-9:
        return None

    low, high = _MIN_VOL, _MAX_VOL
    price_at_high = price(right, spot, strike, years, rate, high, dividend)
    if option_price > price_at_high:
        return None  # richer than 500% vol explains; treat as bad data

    # ...and the same guard at the bottom of the bracket. A quote sitting on
    # intrinsic value carries no time value, so no volatility explains it and
    # bisection would simply converge onto _MIN_VOL. Returning that floor would
    # be the "zero entering the history as a real reading" this function
    # promises never to do — and it is not harmless: it prices a deep-in-the-
    # money contract at delta exactly 1.0, which strike selection would read as
    # a genuine signal. Seen live on 2026-07-21: AAPL calls 110-145 against a
    # spot of 327.74, all quoted within $0.26 of intrinsic on zero volume.
    if option_price <= price(right, spot, strike, years, rate, low, dividend):
        return None

    for _ in range(_MAX_ITERATIONS):
        mid = 0.5 * (low + high)
        modelled = price(right, spot, strike, years, rate, mid, dividend)
        if abs(modelled - option_price) < _VOL_TOLERANCE or high - low < _VOL_TOLERANCE:
            return mid
        if modelled > option_price:
            high = mid
        else:
            low = mid
    return 0.5 * (low + high)


def mid_price(bid: float | None, ask: float | None, close: float | None = None) -> float | None:
    """The price to solve against: bid/ask midpoint, falling back to close.

    The midpoint is used rather than the last trade because an illiquid
    contract's last trade can be hours stale, while the closing quote reflects
    where the market actually stood at the bell.
    """
    if bid is not None and ask is not None and ask >= bid > 0:
        return (bid + ask) / 2.0
    return close if close and close > 0 else None


def year_fraction(days: float) -> float:
    """Calendar days to years, 365-day convention.

    Calendar rather than trading days: consistency across the history matters
    more than the convention, and calendar days need no market calendar (which
    does not exist until Sprint 5).
    """
    return days / 365.0
