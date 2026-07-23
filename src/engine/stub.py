"""The stub engine — the walking skeleton's stand-in for real strategy logic.

Sprint 4 proves every pipe end to end (broker -> DB -> market data -> LLM ->
delivery) *before* any strategy code exists. This module is the placeholder in
the middle: it reshapes the fetched chains into `suggestions` rows so the pipe
carries real-shaped data, and it makes **no decisions** — no eligibility test,
no delta targeting, no ranking, no sizing. Every fetched contract becomes one
stub row, stamped `rule_version = "stub"` so it is never mistaken for a real
ranked suggestion (and can be filtered out of Sprint 6 scoring).

Iron rule #1 holds even here: the numbers it carries (strike, delta) come
straight from `src/chain.py`'s deterministic Black-Scholes solve, never
invented. The fields that are genuinely strategy *outputs* — `premium` as a
ranked credit and `annualized_yield` — are left null; Sprint 5's engine computes
them when it replaces this file wholesale (iron rule #2: driven by rules_vN.yaml).

Pure function, no I/O. Persistence and run orchestration live outside `engine/`.
"""

from __future__ import annotations

from typing import Any

from src.engine import black_scholes as bs

# Stamped on every row this engine emits. `suggestions.rule_version` is not-null
# and every row records what produced it (iron rule #2).
RULE_VERSION = "stub"

# A call written against shares held is a covered call; a put is cash-secured.
# This is a mechanical relabel of the contract's `right`, not a decision about
# which to write — the stub takes a view on nothing.
_STRATEGY_BY_RIGHT = {
    bs.CALL: "covered_call",
    bs.PUT: "cash_secured_put",
}


def build_suggestions(
    chains: list[dict[str, Any]], run_id: str
) -> list[dict[str, Any]]:
    """Reshape fetched chain rows into stub `suggestions` rows for one run.

    `chains` is the flat list of rows from `src/chain.py` across every tracked
    ticker; each row already carries its own `ticker`. Rows whose `right` is
    neither a call nor a put are skipped — they cannot be labelled a strategy,
    and a stub has no business guessing. Input order is preserved, so the output
    is deterministic for a given input.
    """
    rows: list[dict[str, Any]] = []
    for row in chains:
        strategy = _STRATEGY_BY_RIGHT.get(row.get("right"))
        if strategy is None:
            continue
        rows.append(
            {
                "run_id": run_id,
                "ticker": row.get("ticker"),
                "strategy": strategy,
                "expiry": row.get("expiration"),
                "strike": row.get("strike"),
                "delta": row.get("delta"),
                # Strategy outputs, not stub outputs: a ranked credit and its
                # annualized yield are Sprint 5's job. Null here so a stub row
                # never reads as ranked.
                "premium": None,
                "annualized_yield": None,
                # Nothing fetched is thrown away — the whole chain row travels
                # for the LLM prompt and any later inspection.
                "decision_snapshot": row,
                "rule_version": RULE_VERSION,
            }
        )
    return rows
