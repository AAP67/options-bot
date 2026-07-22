"""Derive one ATM implied-volatility reading per ticker per day -> `iv_history`.

The conventions live in `src/engine/iv.py` (which expiry, which strike, how the
legs combine); this module is the plumbing around them: fetch the inputs from
the Theta Terminal, assemble a row, write it.

Every row carries its own provenance — call and put legs, expiry, strike, spot,
rate, and a `method` stamp — because `atm_iv` is computed rather than quoted.
IV rank is a percentile over these readings, so a reading nobody can audit is a
signal nobody should trust.

Failure policy mirrors `src/sync.py`: a ticker that cannot be derived is
excluded and named, the rest are written, and the run still fails. History that
cannot be backfilled must not be lost to a problem with one symbol.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from typing import Any

from src.config import load_dotenv
from src.db import DB
from src.engine import black_scholes as bs
from src.engine import iv as engine_iv
from src.theta import ThetaTerminal

logger = logging.getLogger(__name__)

# SOFR: an overnight rate published daily. Used as the risk-free input.
RATE_SYMBOL = "SOFR"

# How far back to look for a published rate. Rates are not published on
# weekends or holidays, so a Monday snapshot must reach back to Friday.
RATE_LOOKBACK_DAYS = 7

# A liquid ETF used only to ask "which day is the newest one with data?".
# SPY trades every session and has existed throughout the free data window.
SESSION_PROBE_SYMBOL = "SPY"

# How far back to walk looking for that session. Covers a three-day weekend
# plus the FREE tier's one-day publication delay, with room to spare. Beyond
# this, something is wrong and should fail rather than silently reach for
# week-old data.
MAX_SESSION_LOOKBACK = 7


class IVError(RuntimeError):
    """A snapshot that cannot be trusted — never swallowed, never silent."""


class PartialIVError(IVError):
    """Some tickers were derived, others were not. The good rows are written."""

    def __init__(self, written: list[dict[str, Any]], excluded: dict[str, str]) -> None:
        self.written = written
        self.excluded = excluded
        detail = "; ".join(f"{ticker} ({why})" for ticker, why in excluded.items())
        super().__init__(
            f"wrote {len(written)} reading(s) but excluded {len(excluded)}: {detail}"
        )


def _date_param(value: dt.date) -> str:
    """Theta accepts YYYYMMDD for date ranges."""
    return value.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


def fetch_rate(theta: ThetaTerminal, as_of: dt.date) -> float | None:
    """The most recent published SOFR at or before `as_of`, as a percent.

    Looks back rather than requiring an exact match: rates are not published on
    weekends or market holidays, and the previous session's rate is the correct
    input on those days anyway.
    """
    rows = theta.get(
        "/v3/interest_rate/history/eod",
        symbol=RATE_SYMBOL,
        start_date=_date_param(as_of - dt.timedelta(days=RATE_LOOKBACK_DAYS)),
        end_date=_date_param(as_of),
    )
    if not rows:
        return None
    return max(rows, key=lambda row: str(row.get("created", "")))["rate"]


def latest_session(
    theta: ThetaTerminal,
    on_or_before: dt.date,
    probe: str = SESSION_PROBE_SYMBOL,
    max_lookback: int = MAX_SESSION_LOOKBACK,
) -> dt.date:
    """The most recent date that actually has EOD data, at or before `on_or_before`.

    Three separate reasons today's date is usually wrong: the market is shut on
    weekends and holidays, and the FREE tier publishes with a one-day delay. So
    the daily job asks the data which session is newest rather than assuming.

    Walking back beats hardcoding "yesterday": it handles long weekends without
    a market calendar, which does not exist until Sprint 5.
    """
    for offset in range(max_lookback + 1):
        day = on_or_before - dt.timedelta(days=offset)
        if fetch_spot(theta, probe, day) is not None:
            return day
    raise IVError(
        f"no {probe} data in the {max_lookback} days to {on_or_before} — "
        "the feed looks stale or the symbol is wrong"
    )


def fetch_spot(theta: ThetaTerminal, ticker: str, as_of: dt.date) -> float | None:
    """Underlying close on `as_of`, the Black-Scholes spot input."""
    rows = theta.get(
        "/v3/stock/history/eod",
        symbol=ticker,
        start_date=_date_param(as_of),
        end_date=_date_param(as_of),
    )
    if not rows:
        return None
    close = rows[0].get("close")
    return float(close) if close else None


def fetch_expirations(theta: ThetaTerminal, ticker: str) -> list[dt.date]:
    return [
        dt.date.fromisoformat(row["expiration"])
        for row in theta.get("/v3/option/list/expirations", symbol=ticker)
        if row.get("expiration")
    ]


def fetch_strikes(
    theta: ThetaTerminal, ticker: str, expiration: dt.date
) -> list[float]:
    return sorted(
        {
            float(row["strike"])
            for row in theta.get(
                "/v3/option/list/strikes",
                symbol=ticker,
                expiration=expiration.isoformat(),
            )
            if row.get("strike") is not None
        }
    )


def fetch_leg(
    theta: ThetaTerminal,
    ticker: str,
    expiration: dt.date,
    strike: float,
    right: str,
    as_of: dt.date,
) -> dict[str, Any] | None:
    """The closing quote for one contract on `as_of`, or None if it did not trade."""
    rows = theta.get(
        "/v3/option/history/eod",
        symbol=ticker,
        expiration=expiration.isoformat(),
        strike=f"{strike:.3f}",
        right=right,
        start_date=_date_param(as_of),
        end_date=_date_param(as_of),
    )
    if not rows:
        return None
    data = rows[0].get("data") or []
    return data[0] if data else None


# ---------------------------------------------------------------------------
# assembly (pure — no I/O, fully testable)
# ---------------------------------------------------------------------------


def build_reading(
    ticker: str,
    as_of: dt.date,
    spot: float,
    expiration: dt.date,
    strike: float,
    rate_percent: float | None,
    call_quote: dict[str, Any] | None,
    put_quote: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Assemble one `iv_history` row, or None if neither leg is solvable.

    None rather than a zero or a partial row: a reading that could not be
    derived must be absent from the history, not present and wrong. A single
    bad value distorts every percentile computed against it afterwards.
    """
    rate = engine_iv.annual_rate_from_percent(rate_percent)
    days = (expiration - as_of).days

    legs: dict[str, float | None] = {}
    for right, quote in ((bs.CALL, call_quote), (bs.PUT, put_quote)):
        legs[right] = (
            engine_iv.quote_implied_vol(
                right,
                quote.get("bid"),
                quote.get("ask"),
                quote.get("close"),
                spot,
                strike,
                days,
                rate,
            )
            if quote
            else None
        )

    atm_iv = engine_iv.combine(legs[bs.CALL], legs[bs.PUT])
    if atm_iv is None:
        return None

    return {
        "ticker": ticker,
        "date": as_of.isoformat(),
        "atm_iv": atm_iv,
        "call_iv": legs[bs.CALL],
        "put_iv": legs[bs.PUT],
        "expiration": expiration.isoformat(),
        "strike": strike,
        "spot": spot,
        "rate": rate,
        "method": engine_iv.METHOD,
    }


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def reading_for(
    theta: ThetaTerminal,
    ticker: str,
    as_of: dt.date,
    rate_percent: float | None,
    target_dte: int = engine_iv.DEFAULT_TARGET_DTE,
) -> dict[str, Any]:
    """Derive one ticker's reading. Raises IVError naming the missing input."""
    spot = fetch_spot(theta, ticker, as_of)
    if spot is None:
        raise IVError("no underlying close")

    expiration = engine_iv.pick_expiration(
        fetch_expirations(theta, ticker), as_of, target_dte
    )
    if expiration is None:
        raise IVError("no expiry near the target DTE")

    strike = engine_iv.pick_atm_strike(fetch_strikes(theta, ticker, expiration), spot)
    if strike is None:
        raise IVError(f"no strikes listed for {expiration}")

    reading = build_reading(
        ticker,
        as_of,
        spot,
        expiration,
        strike,
        rate_percent,
        fetch_leg(theta, ticker, expiration, strike, bs.CALL, as_of),
        fetch_leg(theta, ticker, expiration, strike, bs.PUT, as_of),
    )
    if reading is None:
        raise IVError(f"neither leg solvable at {strike} exp {expiration}")
    return reading


def tracked_tickers(db: DB) -> list[str]:
    """Equity symbols from the most recent portfolio sync.

    Sprint 2 feeds Sprint 3 directly. Options and cash rows are skipped, and
    crypto with them — there are no listed options on a Robinhood crypto
    holding, so an IV reading would be meaningless.
    """
    tickers = {
        str(row["symbol"])
        for row in db.latest_positions()
        if row.get("asset_type") == "equity"
    }
    return sorted(tickers)


def snapshot(
    theta: ThetaTerminal,
    db: DB,
    tickers: list[str],
    as_of: dt.date,
) -> list[dict[str, Any]]:
    """Derive and write one reading per ticker. Raises rather than write junk."""
    if not tickers:
        raise IVError("no tickers to snapshot")

    rate_percent = fetch_rate(theta, as_of)
    if rate_percent is None:
        # Not fatal: at 30 DTE the discounting term is worth a fraction of a
        # volatility point. Loud, because a silently-zero rate would quietly
        # shift every reading taken that day.
        logger.warning("no %s rate available for %s; using 0.0", RATE_SYMBOL, as_of)

    rows: list[dict[str, Any]] = []
    excluded: dict[str, str] = {}
    for ticker in tickers:
        try:
            rows.append(reading_for(theta, ticker, as_of, rate_percent))
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not stop the rest
            excluded[ticker] = f"{type(exc).__name__}: {exc}"[:200]

    if not rows:
        raise IVError(
            "no readings could be derived: "
            + "; ".join(f"{t} ({why})" for t, why in excluded.items())
        )

    # Upsert, not insert: (ticker, date) is unique, so re-running a day
    # corrects it in place rather than failing or duplicating.
    written = db.upsert_iv(rows)
    if excluded:
        raise PartialIVError(written, excluded)
    return written


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()

    as_of: dt.date | None = None
    try:
        db = DB.from_env()
        tickers = tracked_tickers(db)
        with ThetaTerminal() as theta:
            # Not today: the market may be shut and FREE data lags a day.
            as_of = latest_session(theta, dt.date.today())
            logger.info("snapshotting %d ticker(s) as of %s", len(tickers), as_of)
            written = snapshot(theta, db, tickers, as_of)
    except PartialIVError as exc:
        print(f"iv: PARTIAL — {exc}", file=sys.stderr)
        return 1
    except IVError as exc:
        print(f"iv: FAILED — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — deliberate top-level catch
        logger.debug("unexpected error during IV snapshot", exc_info=True)
        print(f"iv: FAILED — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        return 1

    print(f"iv: wrote {len(written)} reading(s) for {as_of}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
