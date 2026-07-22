"""Fetch a full option chain: every listed strike with quote, IV and delta.

Sprint 5 selects strikes by delta and ranks them by premium yield. This is the
pipe that feeds it — **fetch only, no filtering**. Nothing here decides whether
a contract is eligible, liquid, or worth writing; a chain comes back whole and
the engine takes a view. Judgement belongs in `src/engine/`, driven by
`rules_vN.yaml` (iron rules #1 and #2).

Two things the FREE tier forces, both measured on 2026-07-22:

**IV and delta are computed, not quoted.** Greeks need the STANDARD plan. Quotes
plus the underlying are all Black-Scholes needs, so every row is solved through
`src/engine/black_scholes.py` — the same solver behind `iv_history`, so a delta
here and an ATM IV there mean the same thing.

**Open interest cannot be fetched at all** — it needs the VALUE plan. `volume`
is in the EOD response and travels with every row so Sprint 5 has the option of
using it, but this module does not decide that. Swapping the `min_open_interest`
floor for a volume floor is a real rules change and belongs in Sprint 5, where
the choice is pay $40/mo or rewrite the liquidity floor.

Cost: one request returns the entire chain. Asking for `right=both` and omitting
`strike` returns every listed contract — 178 envelopes for AAPL's 2026-08-21
expiry — so a chain costs 3 requests (chain, spot, rate) rather than one per
contract. That matters at 20 requests/minute.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from src.engine import black_scholes as bs
from src.engine import iv as engine_iv
from src.iv import IVError, _date_param, fetch_rate, fetch_spot
from src.theta import ThetaTerminal

logger = logging.getLogger(__name__)

# Asking for both sides in one request. The v3 API reports `right` back in
# upper case ("CALL"), while Black-Scholes uses the lower-case constants.
BOTH_RIGHTS = "both"


def normalise_right(value: Any) -> str | None:
    """Map the API's `CALL`/`PUT` onto the engine's constants."""
    right = str(value or "").strip().lower()
    return right if right in (bs.CALL, bs.PUT) else None


def fetch_chain_quotes(
    theta: ThetaTerminal,
    ticker: str,
    expiration: dt.date,
    as_of: dt.date,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Every listed contract for one expiry, as (contract, quote) pairs.

    One request for the whole chain — see the module docstring. Contracts that
    did not trade are absent rather than zero-filled, exactly as a single
    contract fetch would return nothing.
    """
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for envelope in theta.get(
        "/v3/option/history/eod",
        symbol=ticker,
        expiration=expiration.isoformat(),
        right=BOTH_RIGHTS,
        start_date=_date_param(as_of),
        end_date=_date_param(as_of),
    ):
        contract = envelope.get("contract") or {}
        for quote in envelope.get("data") or []:
            pairs.append((contract, quote))
    return pairs


def build_contract(
    ticker: str,
    as_of: dt.date,
    expiration: dt.date,
    spot: float,
    rate_percent: float | None,
    contract: dict[str, Any],
    quote: dict[str, Any],
) -> dict[str, Any] | None:
    """Assemble one chain row, or None if the contract cannot be identified.

    A row whose strike or right is unreadable is dropped — it cannot be priced
    and could not be acted on. A row that is merely *unsolvable* (no quote, so
    no IV) is kept with `iv` and `delta` as None: absence of a Greek is a fact
    Sprint 5 may want, and filtering is not this module's job.
    """
    right = normalise_right(contract.get("right"))
    strike = contract.get("strike")
    if right is None or strike is None:
        return None
    strike = float(strike)

    rate = engine_iv.annual_rate_from_percent(rate_percent)
    days = (expiration - as_of).days
    bid, ask, close = quote.get("bid"), quote.get("ask"), quote.get("close")

    implied = engine_iv.quote_implied_vol(
        right, bid, ask, close, spot, strike, days, rate
    )
    # Delta needs a volatility. Without one there is no defensible number, and
    # a fabricated delta would be selected on in Sprint 5 as if it were real.
    greek = (
        bs.delta(right, spot, strike, bs.year_fraction(days), rate, implied)
        if implied is not None
        else None
    )

    return {
        "ticker": ticker,
        "date": as_of.isoformat(),
        "expiration": expiration.isoformat(),
        "dte": days,
        "right": right,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "close": close,
        "mid": bs.mid_price(bid, ask, close),
        # Liquidity proxy. NOT open interest — that needs the VALUE plan and
        # cannot be fetched here. See the module docstring.
        "volume": quote.get("volume"),
        "open_interest": None,
        "iv": implied,
        "delta": greek,
        "spot": spot,
        "rate": rate,
        "method": engine_iv.METHOD,
    }


def fetch_chain(
    theta: ThetaTerminal,
    ticker: str,
    expiration: dt.date,
    as_of: dt.date,
    rate_percent: float | None = None,
) -> list[dict[str, Any]]:
    """The full chain for one ticker and expiry, sorted by right then strike.

    `rate_percent` is accepted so a caller fetching several chains pays for the
    rate once; omitted, it is fetched here.
    """
    spot = fetch_spot(theta, ticker, as_of)
    if spot is None:
        raise IVError(f"{ticker}: no underlying close for {as_of}")

    if rate_percent is None:
        rate_percent = fetch_rate(theta, as_of)
        if rate_percent is None:
            logger.warning("no rate available for %s; using 0.0", as_of)

    rows = [
        row
        for contract, quote in fetch_chain_quotes(theta, ticker, expiration, as_of)
        if (row := build_contract(
            ticker, as_of, expiration, spot, rate_percent, contract, quote
        ))
        is not None
    ]
    if not rows:
        raise IVError(f"{ticker}: no contracts returned for {expiration} on {as_of}")

    rows.sort(key=lambda row: (row["right"], row["strike"]))
    return rows
