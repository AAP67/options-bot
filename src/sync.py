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


class PartialSyncError(SyncError):
    """Some accounts synced, others were excluded as untrustworthy.

    The healthy rows are already written by the time this is raised: position
    history cannot be backfilled, so a problem in one account must never cost
    the snapshot of another. It still subclasses SyncError, so a caller that
    does not care about the distinction fails loudly by default.
    """

    def __init__(
        self,
        written: list[dict[str, Any]],
        excluded: dict[str, list[str]],
    ) -> None:
        self.written = written
        self.excluded = excluded
        detail = "; ".join(
            f"{name} ({', '.join(problems)})" for name, problems in excluded.items()
        )
        super().__init__(
            f"wrote {len(written)} rows but excluded {len(excluded)} account(s): {detail}"
        )


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


def _authorization_id(account: dict[str, Any]) -> str | None:
    """The brokerage authorization backing an account, id or embedded object."""
    auth = account.get("brokerage_authorization")
    if isinstance(auth, dict) or hasattr(auth, "keys"):
        return str(_as_dict(auth).get("id") or "") or None
    return str(auth) if auth else None


def check_connections(
    accounts: list[dict[str, Any]],
    authorizations: list[dict[str, Any]],
) -> list[str]:
    """Return a complaint per account whose broker connection is broken.

    This is the authoritative staleness signal, and it is checked before
    `check_freshness`. SnapTrade's own docs are explicit that a disabled
    connection "can no longer access the latest data from the brokerage, but
    will continue to return the last available cached state" — so holdings keep
    arriving, looking perfectly normal, indefinitely. `disabled` says so
    directly; timestamp age only infers it.
    """
    by_id = {str(auth.get("id")): auth for auth in authorizations}
    return [
        problem
        for account in accounts
        for problem in _connection_problems(account, by_id)
    ]


def _connection_problems(
    account: dict[str, Any],
    authorizations_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Connection complaints for a single account."""
    name = str(account.get("name") or account.get("id"))
    auth_id = _authorization_id(account)
    if not auth_id:
        return [f"{name}: no brokerage authorization reported"]
    auth = authorizations_by_id.get(auth_id)
    if auth is None:
        # The account references a connection SnapTrade no longer lists —
        # treat as broken rather than assuming it is fine.
        return [f"{name}: brokerage authorization {auth_id} not found"]
    if auth.get("disabled"):
        since = auth.get("disabled_date") or "unknown date"
        return [
            f"{name}: brokerage connection disabled since {since} — "
            "holdings are cached, not live. See "
            "https://docs.snaptrade.com/docs/fix-broken-connections"
        ]
    return []


def check_freshness(
    accounts: list[dict[str, Any]],
    now: dt.datetime,
    max_age: dt.timedelta = MAX_HOLDINGS_AGE,
) -> list[str]:
    """Return a complaint per account whose holdings are too old to trust.

    The secondary staleness signal, behind `check_connections`: it catches a
    connection that has quietly stopped refreshing without being marked
    disabled. An empty list means every account is fresh. Callers must treat a
    non-empty list as fatal — stale positions produce confident nonsense.
    """
    return [
        problem
        for account in accounts
        for problem in _freshness_problems(account, now, max_age)
    ]


def _freshness_problems(
    account: dict[str, Any],
    now: dt.datetime,
    max_age: dt.timedelta,
) -> list[str]:
    """Freshness complaints for a single account."""
    name = str(account.get("name") or account.get("id"))
    if str(account.get("status") or "").lower() not in ("open", ""):
        return [f"{name}: account status is {account.get('status')!r}"]
    last = _last_holdings_sync(account)
    if last is None:
        return [f"{name}: no completed holdings sync reported"]
    age = now - last
    if age > max_age:
        hours = age.total_seconds() / 3600
        return [
            f"{name}: holdings last synced {hours:.1f}h ago "
            f"(limit {max_age.total_seconds() / 3600:.0f}h)"
        ]
    return []


def problems_by_account(
    accounts: list[dict[str, Any]],
    authorizations: list[dict[str, Any]],
    now: dt.datetime,
    max_age: dt.timedelta = MAX_HOLDINGS_AGE,
) -> dict[str, list[str]]:
    """Every account's health complaints, keyed by account id.

    The keyed view is what lets `sync()` exclude one bad account instead of
    abandoning the whole snapshot — a stale crypto connection must not cost the
    equity history the strategy actually runs on.
    """
    by_id = {str(auth.get("id")): auth for auth in authorizations}
    return {
        str(account.get("id")): (
            _connection_problems(account, by_id)
            + _freshness_problems(account, now, max_age)
        )
        for account in accounts
    }


def build_run_row(
    synced_at: str,
    accounts: list[dict[str, Any]],
    problems: dict[str, list[str]],
    included_ids: set[str],
    rows_written: int,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Assemble the `sync_runs` row describing one attempt.

    Captures each account's `last_successful_sync` verbatim — the broker's own
    refresh timestamp, which the freshness check consumes and would otherwise
    discard. It is the only record of whether SnapTrade refreshes on non-trading
    days, and it cannot be reconstructed after the fact.
    """
    detail: dict[str, Any] = {}
    for account in accounts:
        account_id = str(account.get("id"))
        holdings = _as_dict(_as_dict(account.get("sync_status")).get("holdings"))
        detail[account_id] = {
            "name": account.get("name"),
            "included": account_id in included_ids,
            "status": account.get("status"),
            "last_successful_sync": holdings.get("last_successful_sync"),
            "initial_sync_completed": holdings.get("initial_sync_completed"),
            "problems": problems.get(account_id, []),
        }
    return {
        "synced_at": synced_at,
        "status": status,
        "rows_written": rows_written,
        "accounts": detail,
        "error": error,
    }


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


def fetch_authorizations(client: Any) -> list[dict[str, Any]]:
    """List the broker connections, which carry the authoritative health flag."""
    return [
        _as_dict(a)
        for a in client.connections.list_brokerage_authorizations(**_NO_USER).body
    ]


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


def _record_run(db: DB, row: dict[str, Any]) -> None:
    """Write the sync_runs row, never masking whatever the caller is reporting.

    Diagnostics must not become the failure. If this write is the thing that is
    broken, the sync's own outcome is still what surfaces.
    """
    try:
        db.insert_sync_run(row)
    except Exception:  # noqa: BLE001 — bookkeeping must never mask the real error
        logger.warning("could not record sync_runs row", exc_info=True)


def sync(client: Any, db: DB, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Run one full sync and write it. Raises SyncError rather than write junk."""
    now = now or dt.datetime.now(dt.UTC)
    synced_at = now.isoformat()

    accounts = fetch_accounts(client)
    if not accounts:
        _record_run(
            db,
            build_run_row(synced_at, [], {}, set(), 0, "failed", "no linked accounts"),
        )
        raise SyncError("SnapTrade returned no linked accounts.")

    # Per account, not per sync: connection health first (the direct signal),
    # then holdings age (the backstop).
    problems = problems_by_account(accounts, fetch_authorizations(client), now)
    healthy = [a for a in accounts if not problems[str(a.get("id"))]]
    excluded = {
        str(a.get("name") or a.get("id")): problems[str(a.get("id"))]
        for a in accounts
        if problems[str(a.get("id"))]
    }

    if not healthy:
        reason = "; ".join(p for ps in excluded.values() for p in ps)
        _record_run(
            db,
            build_run_row(synced_at, accounts, problems, set(), 0, "failed", reason),
        )
        raise SyncError("no trustworthy accounts: " + reason)

    positions_by_account: dict[str, list[dict[str, Any]]] = {}
    balances_by_account: dict[str, list[dict[str, Any]]] = {}
    for account in healthy:
        account_id = str(account.get("id"))
        positions_by_account[account_id] = fetch_positions(client, account_id)
        balances_by_account[account_id] = fetch_balances(client, account_id)

    rows = build_rows(
        healthy, positions_by_account, balances_by_account, synced_at
    )
    included_ids = {str(a.get("id")) for a in healthy}
    if not rows:
        _record_run(
            db,
            build_run_row(
                synced_at, accounts, problems, included_ids, 0, "failed", "zero rows"
            ),
        )
        raise SyncError("sync produced zero rows — refusing to record an empty snapshot.")

    # Write BEFORE raising: the healthy accounts' history is not recoverable
    # later, so the alarm must never cost the data it is warning about.
    written = db.insert_positions(rows)

    partial = PartialSyncError(written, excluded) if excluded else None
    _record_run(
        db,
        build_run_row(
            synced_at,
            accounts,
            problems,
            included_ids,
            len(written),
            "partial" if partial else "ok",
            str(partial) if partial else None,
        ),
    )
    if partial:
        raise partial
    return written


def one_line(exc: BaseException, limit: int = 300) -> str:
    """Compress an exception into a single readable line.

    SnapTrade's ApiException stringifies to a dozen lines including every HTTP
    response header, which is unreadable as an alert. Keeps the status, reason
    and response body — where `detail` actually explains the failure — and drops
    the header dump.
    """
    kept = [
        line.strip()
        for line in str(exc).splitlines()
        if line.strip() and not line.strip().startswith("HTTP response headers")
    ]
    message = " | ".join(kept) or repr(exc)
    if len(message) > limit:
        message = message[: limit - 1] + "…"
    return f"{type(exc).__name__}: {message}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    try:
        written = sync(build_client(), DB.from_env())
    except PartialSyncError as exc:
        # Data landed AND the run fails: a red run no longer implies "no data".
        print(f"sync: PARTIAL — {exc}", file=sys.stderr)
        return 1
    except SyncError as exc:
        # Iron rule #4: a sync that cannot be trusted fails visibly.
        print(f"sync: FAILED — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — deliberate top-level catch
        # Anything the sync did not anticipate: a SnapTrade ApiException, a
        # Supabase outage, a malformed numeric. Reported as one readable line,
        # because from Sprint 4 this text is what gets delivered to a phone.
        # The traceback goes to the debug log — available with `-v`/DEBUG when
        # actually debugging, rather than dumped into every alert.
        logger.debug("unexpected error during sync", exc_info=True)
        print(f"sync: FAILED — {one_line(exc)}", file=sys.stderr)
        return 1
    print(f"sync: wrote {len(written)} position rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
