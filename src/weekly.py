"""The weekly engine run — orchestration for the Sunday brief.

This is the top of the pipe the walking skeleton is proving out: one Sunday run
that syncs the portfolio, fetches chains, turns them into `suggestions`, and
(later) writes a brief and delivers it. It stays deliberately thin — judgement
lives in `src/engine/`, I/O in `src/db.py` / `src/theta.py`; this module only
sequences them and stamps each run.

Sprint 4 builds it a step at a time. Right now it does the persistence half:
mint a run id, run the stub engine over already-fetched chains, and write the
rows to `suggestions`. Fetching the chains (off the sync) and the Claude +
Telegram delivery arrive in the following steps; the stub engine is replaced by
the real one in Sprint 5.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
from typing import Any

import anthropic

from src import brief, chain, iv, sync
from src.brief import BriefError
from src.config import load_dotenv
from src.db import DB
from src.engine import iv as engine_iv
from src.engine import stub
from src.iv import IVError
from src.sync import SyncError
from src.theta import ThetaTerminal

logger = logging.getLogger(__name__)


class WeeklyError(RuntimeError):
    """Raised when the weekly run cannot complete (iron rule #4: fail loudly)."""


def new_run_id(now: dt.datetime) -> str:
    """A sortable, unique id grouping one weekly run's suggestions.

    Derived from the run's UTC timestamp — greppable and orderable, the same
    idea as `positions.synced_at` grouping one sync. `now` is normalised to UTC
    so the trailing `Z` is honest; one run per week, so second resolution never
    collides.
    """
    return "weekly-" + now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def write_stub_suggestions(
    db: DB,
    chains: list[dict[str, Any]],
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Run the stub engine over `chains` and persist the rows for one run.

    Returns the rows written — each stamped with the run id — so the delivery
    steps can brief on exactly what landed. An empty chain set writes nothing
    (there is no run to record) and returns an empty list rather than issuing an
    empty insert.
    """
    now = now or dt.datetime.now(dt.UTC)
    run_id = new_run_id(now)

    rows = stub.build_suggestions(chains, run_id)
    if not rows:
        logger.warning("no chain rows to persist for %s; wrote nothing", run_id)
        return []

    db.insert_suggestions(rows)
    logger.info("wrote %d stub suggestion(s) for %s", len(rows), run_id)
    return rows


def fetch_all_chains(
    theta: ThetaTerminal,
    tickers: list[str],
    as_of: dt.date,
    *,
    rate_percent: float | None = None,
    target_dte: int = engine_iv.DEFAULT_TARGET_DTE,
) -> list[dict[str, Any]]:
    """The full option chain for each ticker at a ~target-DTE Friday expiry.

    Resilient like the daily IV job: a ticker with no listed expiry near the
    target, or no quotes, is skipped and logged — one bad name must not cost the
    rest their chains. If *every* ticker fails, that is fatal. The rate is
    fetched once and shared across all the chains.
    """
    if not tickers:
        raise WeeklyError("no tickers to fetch chains for")
    if rate_percent is None:
        rate_percent = iv.fetch_rate(theta, as_of)

    rows: list[dict[str, Any]] = []
    excluded: dict[str, str] = {}
    for ticker in tickers:
        try:
            expiration = engine_iv.pick_expiration(
                iv.fetch_expirations(theta, ticker), as_of, target_dte
            )
            if expiration is None:
                raise WeeklyError("no Friday expiry near the target DTE")
            rows.extend(
                chain.fetch_chain(theta, ticker, expiration, as_of, rate_percent)
            )
        except Exception as exc:  # noqa: BLE001 — one bad ticker must not stop the rest
            excluded[ticker] = f"{type(exc).__name__}: {exc}"[:200]

    if not rows:
        raise WeeklyError(
            "no chains fetched: "
            + "; ".join(f"{t} ({why})" for t, why in excluded.items())
        )
    if excluded:
        logger.warning(
            "skipped %d ticker(s) with no chain: %s", len(excluded), excluded
        )
    return rows


def run_weekly(
    snaptrade: Any,
    db: DB,
    theta: ThetaTerminal,
    anthropic_client: anthropic.Anthropic,
    *,
    now: dt.datetime | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """One Sunday run, end to end: broker -> DB -> data -> LLM -> delivery.

    The walking skeleton complete (Sprint 4): every pipe is live, but the engine
    is the stub, so the brief is a dumb summary. Sprint 5 swaps the engine and
    the prompt without touching this wiring. Any step failing raises loudly
    (WeeklyError / SyncError / IVError / BriefError) — a broken run alerts rather
    than delivering stale or partial nonsense.
    """
    now = now or dt.datetime.now(dt.UTC)
    today = today or dt.date.today()

    sync.sync(snaptrade, db, now=now)  # broker -> positions
    tickers = iv.tracked_tickers(db)
    if not tickers:
        raise WeeklyError("no tracked tickers from the latest sync")

    as_of = iv.latest_session(theta, today)  # newest session that has data
    chains = fetch_all_chains(theta, tickers, as_of)

    rows = write_stub_suggestions(db, chains, now=now)  # stub engine -> suggestions
    prose = brief.summarize(rows, anthropic_client)  # computed numbers -> prose
    message_id = brief.deliver(prose)  # -> Telegram

    run_id = new_run_id(now)
    logger.info(
        "weekly %s: %d ticker(s), %d suggestion(s), message %s",
        run_id,
        len(tickers),
        len(rows),
        message_id,
    )
    return {
        "run_id": run_id,
        "tickers": len(tickers),
        "suggestions": len(rows),
        "message_id": message_id,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()

    try:
        db = DB.from_env()
        snaptrade = sync.build_client()
        anthropic_client = brief.build_client()
        with ThetaTerminal() as theta:
            result = run_weekly(snaptrade, db, theta, anthropic_client)
    except (WeeklyError, SyncError, IVError, BriefError) as exc:
        print(f"weekly: FAILED — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — deliberate top-level catch
        logger.debug("unexpected error during weekly run", exc_info=True)
        print(f"weekly: FAILED — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        return 1

    print(
        f"weekly: delivered {result['suggestions']} suggestion(s) "
        f"for {result['run_id']} (message {result['message_id']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
