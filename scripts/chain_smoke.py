"""Prove chain fetch returns clean data in this environment.

Sprint 3's second exit criterion. `scripts/theta_smoke.py` proves the Terminal
runs on a runner; this proves the thing Sprint 5 will actually call — a whole
option chain, with IV and delta computed rather than quoted, since the FREE
tier sells neither.

    uv run python scripts/chain_smoke.py

Exits 0 only if the chain came back AND the numbers in it are defensible. The
checks are deliberately about *shape and consistency*, not exact values: a
market moves, but a call delta is always positive, a chain always straddles
spot, and put-call parity always holds. Those catch a broken solver, a
mislabelled `right`, or a silently-empty response — the failures that would
otherwise reach Sprint 5 looking like real candidates.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

# Run directly rather than as a module, so the repo root needs to be importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src import chain  # noqa: E402
from src.config import load_dotenv  # noqa: E402
from src.engine import black_scholes as bs  # noqa: E402
from src.theta import ThetaTerminal  # noqa: E402

# Liquid, and listed throughout the free-data window, so the check never fails
# for the boring reason that the ticker had no options.
SYMBOL = "AAPL"

# Fixed dates, not "yesterday": weekends and holidays would make this flaky,
# and it tests the plumbing rather than the freshness.
AS_OF = dt.date(2026, 7, 21)
EXPIRATION = dt.date(2026, 8, 21)

# A real chain for a liquid name runs to hundreds of contracts. A handful means
# a truncated response, which must fail rather than look like a thin market.
MIN_CONTRACTS = 20

# Deep wings legitimately have no quotes, so some rows are unsolvable by
# design. If most of them are, the solver or the quote fields are broken.
MIN_PRICED_FRACTION = 0.5


def main() -> int:
    load_dotenv()
    print(f"chain smoke — {SYMBOL} {EXPIRATION} as of {AS_OF}")

    with ThetaTerminal() as theta:
        rows = chain.fetch_chain(theta, SYMBOL, EXPIRATION, AS_OF)

    calls = sorted(
        (r for r in rows if r["right"] == bs.CALL), key=lambda r: r["strike"]
    )
    puts = [r for r in rows if r["right"] == bs.PUT]
    priced = [r for r in rows if r["iv"] is not None]
    spot = rows[0]["spot"]

    print(f"  {len(rows)} contracts ({len(calls)} calls, {len(puts)} puts)")
    print(f"  {len(priced)} priced, strikes "
          f"{min(r['strike'] for r in rows)}..{max(r['strike'] for r in rows)}, "
          f"spot {spot}")

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok   ' if ok else 'FAIL '} {label}{f': {detail}' if detail else ''}")
        if not ok:
            failures.append(label)

    check("chain is not truncated", len(rows) >= MIN_CONTRACTS, f"{len(rows)} contracts")
    check("both rights present", bool(calls and puts))
    check(
        "chain straddles spot",
        min(r["strike"] for r in rows) < spot < max(r["strike"] for r in rows),
    )
    check(
        "most contracts are priced",
        len(priced) >= len(rows) * MIN_PRICED_FRACTION,
        f"{len(priced)}/{len(rows)}",
    )
    check("rows are sorted", rows == sorted(rows, key=lambda r: (r["right"], r["strike"])))

    # Free-tier facts, asserted so a plan change or an API change is loud.
    check("open interest is absent (needs VALUE)", all(r["open_interest"] is None for r in rows))
    check("volume is carried through", any(r["volume"] is not None for r in rows))

    # Solver sanity. These are properties of Black-Scholes, not of the market,
    # so they hold on any day and catch a sign or labelling error.
    priced_calls = [r for r in calls if r["delta"] is not None]
    priced_puts = [r for r in puts if r["delta"] is not None]
    check(
        "call deltas in (0, 1)",
        all(0.0 < r["delta"] <= 1.0 for r in priced_calls),
    )
    check(
        "put deltas in (-1, 0)",
        all(-1.0 <= r["delta"] < 0.0 for r in priced_puts),
    )
    check(
        "call delta falls as strike rises",
        all(a["delta"] >= b["delta"] for a, b in zip(priced_calls, priced_calls[1:])),
    )
    check(
        "IVs are plausible",
        all(0.01 < r["iv"] < 5.0 for r in priced),
        f"{min(r['iv'] for r in priced):.3f}..{max(r['iv'] for r in priced):.3f}",
    )

    # Put-call parity on delta: call - put == 1 for a non-dividend European
    # option. The strongest single check here — it can only hold if the spot,
    # rate, day count and both solved vols are mutually consistent.
    by_strike = {r["strike"]: {} for r in rows}
    for r in rows:
        by_strike[r["strike"]][r["right"]] = r
    atm = min(by_strike, key=lambda k: abs(k - spot))
    pair = by_strike[atm]
    if pair.get(bs.CALL, {}).get("delta") and pair.get(bs.PUT, {}).get("delta"):
        parity = pair[bs.CALL]["delta"] - pair[bs.PUT]["delta"]
        check("put-call delta parity at ATM", abs(parity - 1.0) < 0.1, f"{parity:.4f}")
    else:
        check("put-call delta parity at ATM", False, f"no solvable pair at {atm}")

    print()
    if failures:
        print(f"chain smoke: FAILED — {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"chain smoke: OK ({len(rows)} contracts, {len(priced)} priced)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
