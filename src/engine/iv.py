"""Choosing what "the ATM IV" means, and deriving it from quotes.

`iv_history` stores one at-the-money implied volatility per ticker per day, and
IV rank is that value's percentile against its own past. So the *definition*
matters more than the precision: whatever expiry and strike are chosen, they
must be chosen the same way every single day, or the history stops being
comparable and the rank becomes noise.

Hence the conventions here, all fixed rather than adaptive:
  * the expiry nearest a target DTE (~30 days, VIX-like), never an expiry
    inside the target — a 2-day option's IV is a different animal
  * the strike nearest spot
  * the average of the call and put IV, which cancels most of the put-call
    skew and small errors in the dividend assumption

Pure functions, no I/O.
"""

from __future__ import annotations

import datetime as dt
import statistics

from src.engine import black_scholes as bs

# Target days-to-expiry for the IV measurement. 30 days mirrors the VIX
# convention and sits in the middle of the 30-45 DTE window this strategy
# writes, so the IV being ranked is the IV actually being sold.
#
# NOTE: this is arguably a strategy threshold and belongs in rules_vN.yaml
# (iron rule #2). It is a default argument for now because no rules loader
# exists yet and rules_v1.yaml must not be edited once cut. Move it when the
# loader lands in Sprint 5.
DEFAULT_TARGET_DTE = 30

# Stamped on every iv_history row. Bump this whenever the derivation changes —
# target DTE, mid vs close, averaging, dividend handling, day-count, or which
# expiry is chosen — and then REBUILD the affected series. Readings from
# different methods are not comparable, and a percentile over a mixed series is
# meaningless.
#
# v2 (2026-07-22): restrict expiry choice to standard Friday expiries. The
# backfill picks an expiry from the list as it stands *today*, but SPY-style
# short-dated daily expiries (Mon-Thu) were only listed ~2 weeks before they
# expired, so a historical session targeting one found no quotes and was
# silently dropped — 13 of 34 SPY June sessions. Friday weeklies and monthlies
# have existed for years, so they resolve at any point in the backfill window,
# and they are the expiries the strategy actually writes against. See SPRINTS.md
# for the fuller fix (an as-of expiry calendar) deferred to later.
METHOD = "bs-mid-30dte-v2"

# An expiry this close is dominated by pin risk and gamma rather than by the
# volatility level being measured.
MIN_DTE = 7

# dt.date.weekday(): Monday=0 .. Friday=4. Standard weekly and monthly options
# expire on a Friday; the daily expiries that broke the backfill do not.
FRIDAY = 4


def pick_expiration(
    expirations: list[dt.date],
    as_of: dt.date,
    target_dte: int = DEFAULT_TARGET_DTE,
    min_dte: int = MIN_DTE,
    weekday: int | None = FRIDAY,
) -> dt.date | None:
    """The listed expiry closest to `target_dte` days after `as_of`.

    Ties break toward the longer expiry: its IV is the more stable of the two,
    and stability is what a percentile history wants.

    `weekday` restricts the candidates to a single weekday (Friday by default,
    see METHOD). Passing None lifts the restriction — used by tests exercising
    the nearest/tie logic on synthetic dates, not by the daily or backfill jobs.
    """
    candidates = [e for e in expirations if (e - as_of).days >= min_dte]
    if weekday is not None:
        candidates = [e for e in candidates if e.weekday() == weekday]
    if not candidates:
        return None
    return min(candidates, key=lambda e: (abs((e - as_of).days - target_dte), -e.toordinal()))


def pick_atm_strike(strikes: list[float], spot: float) -> float | None:
    """The listed strike closest to spot; ties break to the higher strike."""
    if not strikes or spot <= 0:
        return None
    return min(strikes, key=lambda k: (abs(k - spot), -k))


def quote_implied_vol(
    right: str,
    bid: float | None,
    ask: float | None,
    close: float | None,
    spot: float,
    strike: float,
    days_to_expiry: float,
    rate: float,
    dividend: float = 0.0,
) -> float | None:
    """Implied vol for one contract from its closing quote, or None."""
    option_price = bs.mid_price(bid, ask, close)
    if option_price is None:
        return None
    return bs.implied_vol(
        right,
        option_price,
        spot,
        strike,
        bs.year_fraction(days_to_expiry),
        rate,
        dividend,
    )


def combine(call_iv: float | None, put_iv: float | None) -> float | None:
    """Average the call and put IV, tolerating one side being unusable.

    Averaging both sides cancels most of the put-call skew and any small error
    in the dividend assumption, which affects calls and puts in opposite
    directions. One side alone is still a usable reading; neither is not.
    """
    usable = [v for v in (call_iv, put_iv) if v is not None and v > 0]
    if not usable:
        return None
    return statistics.fmean(usable)


def annual_rate_from_percent(percent: float | None) -> float:
    """SOFR arrives as a percent (3.57 meaning 3.57%); Black-Scholes wants a
    continuously-compounded decimal.

    Falls back to 0.0 when unavailable rather than guessing a rate: at 30 DTE
    the discounting term is worth a fraction of a volatility point, so a
    missing rate must not fail the whole snapshot.
    """
    if percent is None:
        return 0.0
    return percent / 100.0
