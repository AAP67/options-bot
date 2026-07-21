"""SnapTrade -> `positions`. Raw sync only, zero interpretation.

Reads every account linked to the SnapTrade Personal key, normalizes holdings
and cash into `positions` rows, and writes them under a single `synced_at`
stamp so one sync is one recoverable snapshot.

Authentication note: Personal API keys authenticate with `clientId` +
`consumerKey` alone — the key already belongs to the account owner, so there
is no `userId`/`userSecret` to register. The SDK still requires those query
parameters, so they are passed as empty strings.

Staleness is the real hazard here. When a brokerage connection breaks,
SnapTrade keeps serving the last cached holdings rather than erroring, so a
sync can look successful while writing week-old data. `check_freshness` turns
that into a loud failure (iron rule #4).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from typing import Any

from src.config import load_dotenv
from src.db import DB

logger = logging.getLogger(__name__)

# Holdings older than this mean the connection has stopped refreshing.
#
# 72h, not 36h, because of the Sunday engine run: if the broker's last
# successful refresh is Friday's close, Sunday morning is already ~48h later on
# entirely healthy data. A tighter limit would fail the weekly brief every week.
# This is a provisional number — once the daily cron has a few weeks of
# `last_successful_sync` values across weekends, set it from evidence.
#
# Operational plumbing, not strategy math: deliberately not in rules_vN.yaml
# (iron rule #2 governs engine thresholds, and nothing here feeds a trade
# decision), and those files are immutable per version — retuning a timeout
# should not require cutting rules_v2.
MAX_HOLDINGS_AGE = dt.timedelta(hours=72)

# SnapTrade instrument kinds -> the `asset_type` vocabulary in `positions`.
# Unmapped kinds pass through verbatim rather than being coerced or dropped:
# this is a raw sync, and the engine filters later.
ASSET_TYPE_BY_KIND = {
    "stock": "equity",
    "etf": "equity",
    "mutual_fund": "equity",
    "option": "option",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
}

# Options quote per share but cost and trade per contract.
DEFAULT_OPTION_MULTIPLIER = 100


class SyncError(RuntimeError):
    """Raised when a sync cannot be trusted — never swallowed, never silent."""


# ---------------------------------------------------------------------------
# pure normalization — no I/O, fully unit-testable
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """SDK responses are frozendict-like; normalize to a plain dict."""
    return dict(value) if value is not None else {}


def _num(value: Any) -> float | None:
    """Coerce SnapTrade's stringified numerics to float, tolerating nulls."""
    if value is None or value == "":
        return None
    return float(value)


def asset_type_for(kind: str | None) -> str:
    """Map an instrument kind to an `asset_type`, passing unknowns through."""
    if not kind:
        return "unknown"
    return ASSET_TYPE_BY_KIND.get(kind, kind)


def symbol_for(instrument: dict[str, Any]) -> str:
    """Best available ticker: raw_symbol for equities, OCC symbol for options."""
    return str(
        instrument.get("raw_symbol")
        or instrument.get("symbol")
        or instrument.get("id")
        or "UNKNOWN"
    )


def normalize_position(
    account_id: str,
    row: dict[str, Any],
    synced_at: str,
) -> dict[str, Any]:
    """Turn one unified-positions row into a `positions` row.

    Unit conventions from the SnapTrade unified endpoint, verified against the
    live API: `cost_basis` is per unit for equities but per *contract* for
    options (a contract sold at $1.48/share reports 148), while `price` is
    always per share. Market value therefore needs the option multiplier and
    `avg_cost` does not.
    """
    instrument = _as_dict(row.get("instrument"))
    kind = instrument.get("kind")
    units = _num(row.get("units")) or 0.0
    price = _num(row.get("price"))

    multiplier = 1.0
    if asset_type_for(kind) == "option":
        multiplier = float(
            instrument.get("multiplier") or DEFAULT_OPTION_MULTIPLIER
        )

    market_value = None if price is None else price * units * multiplier

    return {
        "synced_at": synced_at,
        "account_id": account_id,
        "symbol": symbol_for(instrument),
        "asset_type": asset_type_for(kind),
        "quantity": units,
        "avg_cost": _num(row.get("cost_basis")),
        "market_value": market_value,
        "currency": str(row.get("currency") or instrument.get("currency") or "USD"),
        "raw": row,
    }


def normalize_cash(
    account_id: str,
    balance: dict[str, Any],
    synced_at: str,
) -> dict[str, Any] | None:
    """Turn one account balance into a cash `positions` row.

    Cash lives in the same table as holdings (symbol = currency code) so a
    single sync's full account state is one query away.
    """
    amount = _num(balance.get("cash"))
    if amount is None:
        return None
    currency = str(_as_dict(balance.get("currency")).get("code") or "USD")
    return {
        "synced_at": synced_at,
        "account_id": account_id,
        "symbol": currency,
        "asset_type": "cash",
        "quantity": amount,
        "avg_cost": None,
        "market_value": amount,
        "currency": currency,
        "raw": balance,
    }


def _last_holdings_sync(account: dict[str, Any]) -> dt.datetime | None:
    """Parse the account's last successful holdings sync, or None if absent."""
    holdings = _as_dict(_as_dict(account.get("sync_status")).get("holdings"))
    if not holdings.get("initial_sync_completed"):
        return None
    raw = holdings.get("last_successful_sync")
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    # Treat a naive timestamp as UTC rather than guessing a local zone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def check_freshness(
    accounts: list[dict[str, Any]],
    now: dt.datetime,
    max_age: dt.timedelta = MAX_HOLDINGS_AGE,
) -> list[str]:
    """Return a complaint per account whose holdings can't be trusted.

    An empty list means every account is fresh. Callers must treat a non-empty
    list as fatal — SnapTrade serves stale cached holdings silently when a
    connection breaks, and stale positions would produce confident nonsense.
    """
    problems: list[str] = []
    for account in accounts:
        name = str(account.get("name") or account.get("id"))
        if str(account.get("status") or "").lower() not in ("open", ""):
            problems.append(f"{name}: account status is {account.get('status')!r}")
            continue
        last = _last_holdings_sync(account)
        if last is None:
            problems.append(f"{name}: no completed holdings sync reported")
            continue
        age = now - last
        if age > max_age:
            hours = age.total_seconds() / 3600
            problems.append(
                f"{name}: holdings last synced {hours:.1f}h ago "
                f"(limit {max_age.total_seconds() / 3600:.0f}h)"
            )
    return problems


def build_rows(
    accounts: list[dict[str, Any]],
    positions_by_account: dict[str, list[dict[str, Any]]],
    balances_by_account: dict[str, list[dict[str, Any]]],
    synced_at: str,
) -> list[dict[str, Any]]:
    """Assemble every `positions` row for one sync across all accounts."""
    rows: list[dict[str, Any]] = []
    for account in accounts:
        account_id = str(account.get("id"))
        for position in positions_by_account.get(account_id, []):
            rows.append(normalize_position(account_id, position, synced_at))
        for balance in balances_by_account.get(account_id, []):
            cash = normalize_cash(account_id, balance, synced_at)
            if cash is not None:
                rows.append(cash)
    return rows


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def build_client() -> Any:
    """Build a SnapTrade client, failing loudly if credentials are missing."""
    client_id = os.environ.get("SNAPTRADE_CLIENT_ID", "").strip()
    consumer_key = os.environ.get("SNAPTRADE_CONSUMER_KEY", "").strip()
    if not client_id or not consumer_key or "placeholder" in (client_id, consumer_key):
        raise SyncError(
            "SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY must be set "
            "(see .env.example)."
        )
    from snaptrade_client import SnapTrade  # lazy: unit tests never import it

    return SnapTrade(client_id=client_id, consumer_key=consumer_key)


# Personal keys carry no user credentials, but the SDK requires the params.
_NO_USER = {"user_id": "", "user_secret": ""}


def fetch_accounts(client: Any) -> list[dict[str, Any]]:
    return [_as_dict(a) for a in client.account_information.list_user_accounts(**_NO_USER).body]


def fetch_positions(client: Any, account_id: str) -> list[dict[str, Any]]:
    """Unified positions: equities, options and crypto in one call."""
    body = _as_dict(
        client.account_information.get_all_account_positions(
            account_id=account_id, **_NO_USER
        ).body
    )
    return [_as_dict(row) for row in body.get("results", [])]


def fetch_balances(client: Any, account_id: str) -> list[dict[str, Any]]:
    return [
        _as_dict(b)
        for b in client.account_information.get_user_account_balance(
            account_id=account_id, **_NO_USER
        ).body
    ]


def sync(client: Any, db: DB, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Run one full sync and write it. Raises SyncError rather than write junk."""
    now = now or dt.datetime.now(dt.UTC)
    synced_at = now.isoformat()

    accounts = fetch_accounts(client)
    if not accounts:
        raise SyncError("SnapTrade returned no linked accounts.")

    problems = check_freshness(accounts, now)
    if problems:
        raise SyncError("stale brokerage data: " + "; ".join(problems))

    positions_by_account: dict[str, list[dict[str, Any]]] = {}
    balances_by_account: dict[str, list[dict[str, Any]]] = {}
    for account in accounts:
        account_id = str(account.get("id"))
        positions_by_account[account_id] = fetch_positions(client, account_id)
        balances_by_account[account_id] = fetch_balances(client, account_id)

    rows = build_rows(
        accounts, positions_by_account, balances_by_account, synced_at
    )
    if not rows:
        raise SyncError("sync produced zero rows — refusing to record an empty snapshot.")

    return db.insert_positions(rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    try:
        written = sync(build_client(), DB.from_env())
    except SyncError as exc:
        # Iron rule #4: a sync that cannot be trusted fails visibly.
        print(f"sync: FAILED — {exc}", file=sys.stderr)
        return 1
    print(f"sync: wrote {len(written)} position rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
