"""Prove the Theta Terminal works in this environment.

Sprint 3's risky assumption is that a Java process can be launched,
authenticated and queried on a GitHub Actions runner — not just on a
development machine that happens to have Java and a warm library cache. This
script is that proof, and it stays useful afterwards as the first thing to run
when the daily IV job starts failing for environmental reasons.

    uv run python scripts/theta_smoke.py

Exits 0 only if the Terminal started AND returned real data for every check.
Prints timings, because a cold start here measured 141s and a runner may be
slower — if that number creeps toward theta.STARTUP_TIMEOUT, raise the timeout
before it starts failing intermittently.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
import time

# Run directly (`uv run python scripts/theta_smoke.py`) rather than as a module,
# so the repo root needs to be importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.config import load_dotenv  # noqa: E402
from src.engine import black_scholes as bs  # noqa: E402
from src.engine import iv as engine_iv  # noqa: E402
from src.theta import ThetaSubscriptionError, ThetaTerminal  # noqa: E402

# A liquid name that has existed for the whole free-data window, so the smoke
# test never fails for the boring reason that the ticker had no options.
SYMBOL = "AAPL"

# Fixed, not "yesterday": weekends and market holidays would make a relative
# date flaky, and this checks the plumbing rather than the freshness.
AS_OF = dt.date(2026, 7, 21)


def check(label: str, fn):
    """Run one check, printing its result and timing."""
    started = time.time()
    try:
        result = fn()
    except Exception as exc:
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}"[:300])
        return None
    print(f"  ok    {label} ({time.time() - started:.1f}s): {result}")
    return result


def main() -> int:
    load_dotenv()
    date_str = AS_OF.strftime("%Y%m%d")
    failures: list[str] = []

    print(f"Theta smoke test — {SYMBOL} as of {AS_OF}")
    started = time.time()
    with ThetaTerminal() as theta:
        startup = time.time() - started
        print(f"  ok    terminal started ({startup:.1f}s)")

        spot_rows = check(
            "stock EOD",
            lambda: theta.get(
                "/v3/stock/history/eod",
                symbol=SYMBOL,
                start_date=date_str,
                end_date=date_str,
            )[0]["close"],
        )
        expirations = check(
            "option expirations",
            lambda: len(theta.get("/v3/option/list/expirations", symbol=SYMBOL)),
        )
        rate = check(
            "SOFR",
            lambda: theta.get(
                "/v3/interest_rate/history/eod",
                symbol="SOFR",
                start_date=date_str,
                end_date=date_str,
            )[0]["rate"],
        )

        # The end-to-end result that matters: a derived IV, not just a 200.
        # A number here means quotes, underlying, rate, and the solver all work
        # together on this machine.
        atm_iv = None
        if spot_rows and expirations:
            atm_iv = check("derived ATM IV", lambda: _atm_iv(theta, spot_rows, rate, date_str))

        # Confirm the paywall is still classified correctly. If ThetaData ever
        # changes this response, we want a loud failure here rather than a
        # subscription error silently reaching the IV pipeline as "no data".
        blocked = check(
            "open interest is correctly blocked",
            lambda: _expect_blocked(theta, date_str),
        )

    for label, value in [
        ("stock EOD", spot_rows),
        ("expirations", expirations),
        ("SOFR", rate),
        ("derived ATM IV", atm_iv),
        ("paywall classification", blocked),
    ]:
        if not value:
            failures.append(label)

    if atm_iv is not None and not (0.05 < atm_iv < 3.0):
        failures.append(f"ATM IV out of plausible range: {atm_iv}")

    print()
    if failures:
        print(f"theta smoke: FAILED — {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"theta smoke: OK (startup {startup:.1f}s, ATM IV {atm_iv:.4f})")
    return 0


def _atm_iv(theta: ThetaTerminal, spot: float, rate: float | None, date_str: str) -> float | None:
    """The full derivation path, exactly as the daily job will run it."""
    expirations = [
        dt.date.fromisoformat(row["expiration"])
        for row in theta.get("/v3/option/list/expirations", symbol=SYMBOL)
    ]
    expiration = engine_iv.pick_expiration(expirations, AS_OF)
    if expiration is None:
        return None

    strikes = sorted(
        {
            row["strike"]
            for row in theta.get(
                "/v3/option/list/strikes", symbol=SYMBOL, expiration=expiration.isoformat()
            )
        }
    )
    strike = engine_iv.pick_atm_strike(strikes, spot)
    if strike is None:
        return None

    days = (expiration - AS_OF).days
    legs: dict[str, float | None] = {}
    for right in (bs.CALL, bs.PUT):
        rows = theta.get(
            "/v3/option/history/eod",
            symbol=SYMBOL,
            expiration=expiration.isoformat(),
            strike=f"{strike:.3f}",
            right=right,
            start_date=date_str,
            end_date=date_str,
        )
        day = rows[0]["data"][0] if rows and rows[0].get("data") else None
        legs[right] = (
            engine_iv.quote_implied_vol(
                right,
                day.get("bid"),
                day.get("ask"),
                day.get("close"),
                spot,
                strike,
                days,
                engine_iv.annual_rate_from_percent(rate),
            )
            if day
            else None
        )
    return engine_iv.combine(legs[bs.CALL], legs[bs.PUT])


def _expect_blocked(theta: ThetaTerminal, date_str: str) -> str | None:
    """Open interest needs the VALUE plan; the block must raise, not return []."""
    try:
        theta.get(
            "/v3/option/history/open_interest",
            symbol=SYMBOL,
            expiration="2026-08-21",
            strike="330.000",
            right="call",
            start_date=date_str,
            end_date=date_str,
        )
    except ThetaSubscriptionError:
        return "raises ThetaSubscriptionError"
    return None  # allowed (plan upgraded?) or, worse, silently empty


if __name__ == "__main__":
    raise SystemExit(main())
