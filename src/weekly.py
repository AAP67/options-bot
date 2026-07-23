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
from typing import Any

from src.db import DB
from src.engine import stub

logger = logging.getLogger(__name__)


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
