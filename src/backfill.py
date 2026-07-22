"""Backfill historical ATM IV readings into `iv_history`.

`src/iv.py` appends one reading per ticker per day going forward. This fills in
the past, so IV rank has a percentile to be a percentile *of* — a rank against
three weeks of history is not a signal.

**The readings must be indistinguishable from the daily job's.** Every decision
about which expiry, which strike, and how the legs combine is delegated to the
same `src/engine/iv.py` conventions, and each row is assembled by the same
`src.iv.build_reading`. Nothing here decides anything about the derivation; it
only fetches the same inputs for older dates. A backfill computed even slightly
differently would put a seam in the middle of the series, and every percentile
spanning that seam would be wrong in a way nobody can see.

Why this is a separate program from the daily job rather than a flag on it:

The daily job spends 5 requests per ticker to derive one day. Replayed over
three years that is ~82,000 requests, and at the FREE tier's 20/min it would
take about 69 hours. Instead this fetches *ranges*: one request covers a whole
year of underlying closes, and one covers every session of a given contract.
Verified live on 2026-07-22 — a quote pulled from a ranged request is byte-
identical to the same quote pulled a day at a time, so this is a cheaper route
to the same numbers, not a different measurement.

That lands at roughly 780 requests per ticker (~14 hours for a 22-name
portfolio), which is why this is a **local, resumable, run-it-overnight
script** and not a CI job — a GitHub Actions job is capped at 6 hours. Progress
is written continuously and completed days are skipped on the next run, so
killing it and restarting costs only the day in flight.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections import defaultdict
from typing import Any, Iterator

from src.config import load_dotenv
from src.db import DB
from src.engine import black_scholes as bs
from src.engine import iv as engine_iv
from src.iv import (
    IVError,
    RATE_LOOKBACK_DAYS,
    RATE_SYMBOL,
    _date_param,
    build_reading,
    fetch_expirations,
    fetch_strikes,
    tracked_tickers,
)
from src.theta import ThetaTerminal

logger = logging.getLogger(__name__)

# The FREE tier's option history begins here (measured 2026-07-22 — the docs
# advertise 1 year, the feed actually serves ~3). Asking for earlier dates
# returns nothing rather than erroring, so this is a courtesy floor that keeps
# the request count honest.
BACKFILL_START = dt.date(2023, 6, 1)

# Hard server-side limit: "Too many days between start and end date; max 365
# days allowed" (HTTP 400). Every range request must be chunked under it.
MAX_RANGE_DAYS = 365

# Write partial progress this often. Small enough that an interrupt loses
# minutes rather than the hour a whole ticker takes; large enough that the
# write is not the bottleneck.
FLUSH_EVERY = 100


def date_chunks(
    start: dt.date, end: dt.date, span: int = MAX_RANGE_DAYS
) -> Iterator[tuple[dt.date, dt.date]]:
    """Split [start, end] into windows the API will accept."""
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + dt.timedelta(days=span - 1))
        yield cursor, stop
        cursor = stop + dt.timedelta(days=1)


def session_date(row: dict[str, Any]) -> dt.date | None:
    """The session an EOD row describes.

    Theta stamps EOD rows with `created`, an ISO timestamp written after the
    close of the session it summarises. Verified against single-day fetches:
    the date half names the session, so it is the join key between a ranged
    response and the day being derived.
    """
    created = str(row.get("created") or "")[:10]
    if len(created) != 10:
        return None
    try:
        return dt.date.fromisoformat(created)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# ranged fetches — the whole reason this module exists
# ---------------------------------------------------------------------------


def fetch_spot_history(
    theta: ThetaTerminal, ticker: str, start: dt.date, end: dt.date
) -> dict[dt.date, float]:
    """Underlying closes by session. Doubles as the trading calendar.

    A date absent here had no session (weekend, holiday) or no data, and gets
    no reading — which is correct either way, and needs no market calendar.
    """
    closes: dict[dt.date, float] = {}
    for lo, hi in date_chunks(start, end):
        for row in theta.get(
            "/v3/stock/history/eod",
            symbol=ticker,
            start_date=_date_param(lo),
            end_date=_date_param(hi),
        ):
            day = session_date(row)
            close = row.get("close")
            if day is not None and close:
                closes[day] = float(close)
    return closes


def fetch_rate_history(
    theta: ThetaTerminal, start: dt.date, end: dt.date
) -> list[tuple[dt.date, float]]:
    """Published SOFR by date, ascending. Fetched once and shared by every ticker."""
    rates: dict[dt.date, float] = {}
    # Reach back a lookback window so the earliest sessions can still resolve a
    # rate published before the range opened.
    for lo, hi in date_chunks(start - dt.timedelta(days=RATE_LOOKBACK_DAYS), end):
        for row in theta.get(
            "/v3/interest_rate/history/eod",
            symbol=RATE_SYMBOL,
            start_date=_date_param(lo),
            end_date=_date_param(hi),
        ):
            day = session_date(row)
            if day is not None and row.get("rate") is not None:
                rates[day] = float(row["rate"])
    return sorted(rates.items())


def rate_as_of(
    rates: list[tuple[dt.date, float]],
    day: dt.date,
    lookback: int = RATE_LOOKBACK_DAYS,
) -> float | None:
    """The most recent rate at or before `day`, mirroring `src.iv.fetch_rate`.

    Same lookback window as the daily job, so a session that would have run
    without a rate then also runs without one now.
    """
    floor = day - dt.timedelta(days=lookback)
    candidates = [rate for stamp, rate in rates if floor <= stamp <= day]
    return candidates[-1] if candidates else None


def fetch_leg_history(
    theta: ThetaTerminal,
    ticker: str,
    expiration: dt.date,
    strike: float,
    right: str,
    start: dt.date,
    end: dt.date,
) -> dict[dt.date, dict[str, Any]]:
    """Every session's closing quote for one contract, keyed by session.

    One request replaces one-per-day. Sessions the contract did not trade are
    simply absent, exactly as a single-day fetch would have returned nothing.
    """
    quotes: dict[dt.date, dict[str, Any]] = {}
    for lo, hi in date_chunks(start, end):
        for envelope in theta.get(
            "/v3/option/history/eod",
            symbol=ticker,
            expiration=expiration.isoformat(),
            strike=f"{strike:.3f}",
            right=right,
            start_date=_date_param(lo),
            end_date=_date_param(hi),
        ):
            for row in envelope.get("data") or []:
                day = session_date(row)
                if day is not None:
                    quotes[day] = row
    return quotes


# ---------------------------------------------------------------------------
# planning (pure — no I/O, fully testable)
# ---------------------------------------------------------------------------


def group_by_contract(
    plan: dict[dt.date, tuple[dt.date, float]],
) -> dict[tuple[dt.date, float], list[dt.date]]:
    """Invert date -> contract into contract -> dates.

    This is where the saving comes from: the 30-DTE expiry only rolls weekly
    and spot rarely crosses a strike boundary, so a handful of contracts cover
    every session and each costs one request instead of one per day.
    """
    groups: dict[tuple[dt.date, float], list[dt.date]] = defaultdict(list)
    for day, contract in plan.items():
        groups[contract].append(day)
    return {contract: sorted(days) for contract, days in groups.items()}


def existing_dates(db: DB, ticker: str) -> set[dt.date]:
    """Sessions already stored for this ticker *under the current method*.

    Method-aware on purpose. If the derivation is ever bumped, old rows stop
    counting as done and get rebuilt rather than leaving a series that is half
    one method and half another — the failure `engine_iv.METHOD` exists to
    prevent.
    """
    done: set[dt.date] = set()
    for row in db.iv_for_ticker(ticker):
        if row.get("method") != engine_iv.METHOD:
            continue
        try:
            done.add(dt.date.fromisoformat(str(row["date"])[:10]))
        except (ValueError, KeyError):
            continue
    return done


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def backfill_ticker(
    theta: ThetaTerminal,
    db: DB,
    ticker: str,
    start: dt.date,
    end: dt.date,
    rates: list[tuple[dt.date, float]],
    target_dte: int = engine_iv.DEFAULT_TARGET_DTE,
) -> int:
    """Derive and write every missing reading for one ticker. Returns the count."""
    done = existing_dates(db, ticker)
    spots = fetch_spot_history(theta, ticker, start, end)
    sessions = sorted(day for day in spots if day not in done)
    if not sessions:
        logger.info("%s: nothing missing in %s..%s", ticker, start, end)
        return 0

    # The expiry calendar is fetched once and reused for every session. Note
    # this is the list as it stands *today*, which includes expiries back to
    # 2012 — so historical expiries are available. The approximation is that a
    # near-dated expiry was already listed on the older session being derived;
    # at ~30 DTE that holds for any optionable name, since weeklies are listed
    # well over a month out.
    expirations = fetch_expirations(theta, ticker)
    if not expirations:
        raise IVError(f"{ticker}: no expirations listed")

    plan: dict[dt.date, tuple[dt.date, float]] = {}
    strikes_by_expiry: dict[dt.date, list[float]] = {}
    for day in sessions:
        expiry = engine_iv.pick_expiration(expirations, day, target_dte)
        if expiry is None:
            continue
        if expiry not in strikes_by_expiry:
            strikes_by_expiry[expiry] = fetch_strikes(theta, ticker, expiry)
        strike = engine_iv.pick_atm_strike(strikes_by_expiry[expiry], spots[day])
        if strike is not None:
            plan[day] = (expiry, strike)

    written = 0
    pending: list[dict[str, Any]] = []
    for (expiry, strike), days in group_by_contract(plan).items():
        calls = fetch_leg_history(
            theta, ticker, expiry, strike, bs.CALL, days[0], days[-1]
        )
        puts = fetch_leg_history(
            theta, ticker, expiry, strike, bs.PUT, days[0], days[-1]
        )
        for day in days:
            row = build_reading(
                ticker,
                day,
                spots[day],
                expiry,
                strike,
                rate_as_of(rates, day),
                calls.get(day),
                puts.get(day),
            )
            # None means neither leg was solvable. Skipped, never zero-filled:
            # one bad value distorts every percentile taken against it.
            if row is not None:
                pending.append(row)

        # Flush as we go so an interrupt keeps the work already paid for.
        if len(pending) >= FLUSH_EVERY:
            written += len(db.upsert_iv(pending))
            pending = []

    if pending:
        written += len(db.upsert_iv(pending))
    logger.info("%s: wrote %d reading(s) of %d session(s)", ticker, written, len(sessions))
    return written


def backfill(
    theta: ThetaTerminal,
    db: DB,
    tickers: list[str],
    start: dt.date,
    end: dt.date,
) -> dict[str, int]:
    """Backfill every ticker. One bad symbol never stops the rest."""
    if not tickers:
        raise IVError("no tickers to backfill")

    rates = fetch_rate_history(theta, start, end)
    if not rates:
        logger.warning("no %s history for %s..%s; using 0.0", RATE_SYMBOL, start, end)

    results: dict[str, int] = {}
    excluded: dict[str, str] = {}
    for index, ticker in enumerate(tickers, start=1):
        logger.info("[%d/%d] %s", index, len(tickers), ticker)
        try:
            results[ticker] = backfill_ticker(theta, db, ticker, start, end, rates)
        except Exception as exc:  # noqa: BLE001 — one symbol must not sink the run
            excluded[ticker] = f"{type(exc).__name__}: {exc}"[:200]
            logger.error("%s: FAILED — %s", ticker, excluded[ticker])

    if excluded:
        logger.error(
            "%d ticker(s) incomplete: %s",
            len(excluded),
            "; ".join(f"{t} ({why})" for t, why in excluded.items()),
        )
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill historical ATM IV into iv_history. "
        "Resumable: already-stored sessions are skipped, so re-running after "
        "an interrupt picks up where it stopped."
    )
    parser.add_argument(
        "--start",
        type=dt.date.fromisoformat,
        default=BACKFILL_START,
        help=f"first session to derive (default {BACKFILL_START}, the earliest "
        "the FREE tier serves)",
    )
    parser.add_argument(
        "--end",
        type=dt.date.fromisoformat,
        default=None,
        help="last session to derive (default: yesterday — FREE data lags a day)",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="comma-separated symbols (default: the tracked portfolio). Useful "
        "for backfilling one new name without rescanning the rest.",
    )
    # Deliberately NO --target-dte flag. It is part of the method stamped on
    # every row; exposing it would invite a series half-derived at one DTE and
    # half at another, which is exactly what makes a percentile meaningless.
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    args = parse_args(argv)

    end = args.end or dt.date.today() - dt.timedelta(days=1)
    if args.start > end:
        print(f"backfill: FAILED — start {args.start} is after end {end}", file=sys.stderr)
        return 1

    try:
        db = DB.from_env()
        tickers = (
            [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            if args.tickers
            else tracked_tickers(db)
        )
        with ThetaTerminal() as theta:
            logger.info(
                "backfilling %d ticker(s) over %s..%s", len(tickers), args.start, end
            )
            results = backfill(theta, db, tickers, args.start, end)
    except IVError as exc:
        print(f"backfill: FAILED — {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # Expected: this runs for hours. Progress is already written, so say so
        # rather than printing a traceback that looks like data loss.
        print("\nbackfill: INTERRUPTED — progress saved, re-run to resume", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — deliberate top-level catch
        logger.debug("unexpected error during backfill", exc_info=True)
        print(f"backfill: FAILED — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        return 1

    total = sum(results.values())
    print(f"backfill: wrote {total} reading(s) across {len(results)} ticker(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
