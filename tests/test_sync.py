"""Unit tests for the SnapTrade sync. All I/O is faked.

Fixture payloads mirror the shapes the live SnapTrade Personal API actually
returns (unified positions endpoint, account balances, account sync_status),
including its unit quirks: numerics arrive as strings, and option `cost_basis`
is per contract while `price` is per share.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src import sync

NOW = dt.datetime(2026, 7, 21, 18, 0, tzinfo=dt.UTC)
SYNCED_AT = NOW.isoformat()


AUTH_ID = "auth-1"
HEALTHY_AUTH = {"id": AUTH_ID, "disabled": False, "disabled_date": None}
DISABLED_AUTH = {"id": AUTH_ID, "disabled": True, "disabled_date": "2026-07-19"}


def account(name: str = "Robinhood Individual", **overrides):
    """A linked account with a fresh holdings sync unless told otherwise."""
    base = {
        "id": "acct-1",
        "name": name,
        "status": "open",
        "brokerage_authorization": AUTH_ID,
        "sync_status": {
            "holdings": {
                "last_successful_sync": "2026-07-21T17:17:40.750897+00:00",
                "initial_sync_completed": True,
            }
        },
    }
    base.update(overrides)
    return base


EQUITY_ROW = {
    "units": "14.53947",
    "price": "251.34",
    "cost_basis": "202.11",
    "currency": "USD",
    "instrument": {
        "kind": "stock",
        "symbol": "AAPL",
        "raw_symbol": "AAPL",
        "currency": "USD",
        "description": "Apple Inc.",
    },
}

# A short call: one contract sold for $1.48/share -> cost_basis 148 per contract.
OPTION_ROW = {
    "units": "-1",
    "price": "2.3",
    "cost_basis": "148",
    "instrument": {
        "kind": "option",
        "symbol": "AAPL  260821C00260000",
        "multiplier": 100,
        "option_type": "CALL",
        "strike_price": 260.0,
        "expiration_date": "2026-08-21",
        "underlying": "AAPL",
    },
}

BALANCE = {"cash": 15234.56, "buying_power": 15234.56, "currency": {"code": "USD"}}


# -- normalization ----------------------------------------------------------


def test_equity_position_uses_per_unit_cost_and_plain_market_value():
    row = sync.normalize_position("acct-1", EQUITY_ROW, SYNCED_AT)
    assert row["symbol"] == "AAPL"
    assert row["asset_type"] == "equity"
    assert row["quantity"] == pytest.approx(14.53947)
    assert row["avg_cost"] == pytest.approx(202.11)
    assert row["market_value"] == pytest.approx(14.53947 * 251.34)
    assert row["currency"] == "USD"
    assert row["raw"] is EQUITY_ROW
    assert row["synced_at"] == SYNCED_AT


def test_short_option_applies_multiplier_and_keeps_cost_basis_per_contract():
    row = sync.normalize_position("acct-1", OPTION_ROW, SYNCED_AT)
    assert row["asset_type"] == "option"
    assert row["quantity"] == -1
    # cost_basis is already per contract — must NOT be multiplied again.
    assert row["avg_cost"] == pytest.approx(148.0)
    # price is per share, so market value needs the 100x multiplier.
    assert row["market_value"] == pytest.approx(-230.0)


def test_option_without_multiplier_falls_back_to_100():
    payload = {**OPTION_ROW, "instrument": {"kind": "option", "symbol": "X"}}
    assert sync.normalize_position("a", payload, SYNCED_AT)["market_value"] == pytest.approx(-230.0)


def test_unknown_instrument_kind_passes_through_rather_than_being_dropped():
    payload = {
        "units": "1",
        "price": "5",
        "cost_basis": "4",
        "instrument": {"kind": "future", "symbol": "ESZ6"},
    }
    assert sync.normalize_position("a", payload, SYNCED_AT)["asset_type"] == "future"


def test_missing_price_yields_null_market_value_not_zero():
    payload = {**EQUITY_ROW, "price": None}
    assert sync.normalize_position("a", payload, SYNCED_AT)["market_value"] is None


def test_cash_becomes_a_position_row_keyed_by_currency():
    row = sync.normalize_cash("acct-1", BALANCE, SYNCED_AT)
    assert row["symbol"] == "USD"
    assert row["asset_type"] == "cash"
    assert row["quantity"] == pytest.approx(15234.56)
    assert row["market_value"] == pytest.approx(15234.56)
    assert row["avg_cost"] is None


def test_balance_without_cash_is_skipped():
    assert sync.normalize_cash("acct-1", {"currency": {"code": "USD"}}, SYNCED_AT) is None


def test_build_rows_covers_every_account():
    accounts = [account(), account("Robinhood Crypto", id="acct-2")]
    rows = sync.build_rows(
        accounts,
        {"acct-1": [EQUITY_ROW, OPTION_ROW], "acct-2": []},
        {"acct-1": [BALANCE], "acct-2": [BALANCE]},
        SYNCED_AT,
    )
    assert len(rows) == 4
    assert {r["account_id"] for r in rows} == {"acct-1", "acct-2"}
    assert sum(r["asset_type"] == "cash" for r in rows) == 2


# -- connection health ------------------------------------------------------


def test_healthy_connection_reports_no_problems():
    assert sync.check_connections([account()], [HEALTHY_AUTH]) == []


def test_disabled_connection_is_reported_even_when_holdings_look_fresh():
    """The whole point: SnapTrade keeps serving cached holdings when disabled."""
    fresh_account = account()
    assert sync.check_freshness([fresh_account], NOW) == []  # age check sees nothing
    problems = sync.check_connections([fresh_account], [DISABLED_AUTH])
    assert len(problems) == 1
    assert "disabled since 2026-07-19" in problems[0]


def test_account_referencing_an_unlisted_authorization_is_reported():
    assert "not found" in sync.check_connections([account()], [])[0]


def test_account_without_an_authorization_is_reported():
    orphan = account(brokerage_authorization=None)
    assert "no brokerage authorization" in sync.check_connections([orphan], [])[0]


def test_authorization_may_be_an_embedded_object_not_just_an_id():
    embedded = account(brokerage_authorization={"id": AUTH_ID, "name": "Connection-1"})
    assert sync.check_connections([embedded], [HEALTHY_AUTH]) == []


def test_only_the_affected_account_is_reported():
    accounts = [account(), account("Other", id="acct-2", brokerage_authorization="auth-2")]
    problems = sync.check_connections(
        accounts, [HEALTHY_AUTH, {"id": "auth-2", "disabled": True}]
    )
    assert len(problems) == 1
    assert problems[0].startswith("Other:")


# -- freshness --------------------------------------------------------------


def test_fresh_account_reports_no_problems():
    assert sync.check_freshness([account()], NOW) == []


def test_stale_holdings_are_reported():
    stale = account(
        sync_status={
            "holdings": {
                "last_successful_sync": "2026-07-18T00:00:00+00:00",
                "initial_sync_completed": True,
            }
        }
    )
    problems = sync.check_freshness([stale], NOW)
    assert len(problems) == 1
    assert "last synced" in problems[0]


def test_incomplete_initial_sync_is_reported():
    never = account(
        sync_status={
            "holdings": {
                "last_successful_sync": None,
                "initial_sync_completed": False,
            }
        }
    )
    assert "no completed holdings sync" in sync.check_freshness([never], NOW)[0]


def test_closed_account_is_reported():
    assert "status" in sync.check_freshness([account(status="closed")], NOW)[0]


def test_naive_timestamp_is_treated_as_utc():
    naive = account(
        sync_status={
            "holdings": {
                "last_successful_sync": "2026-07-21T17:17:40",
                "initial_sync_completed": True,
            }
        }
    )
    assert sync.check_freshness([naive], NOW) == []


def test_unparseable_timestamp_is_reported_not_ignored():
    bad = account(
        sync_status={
            "holdings": {
                "last_successful_sync": "not-a-date",
                "initial_sync_completed": True,
            }
        }
    )
    assert sync.check_freshness([bad], NOW) != []


# -- orchestration ----------------------------------------------------------


class FakeClient:
    """Stands in for the SnapTrade SDK, mimicking its `.body` responses."""

    def __init__(self, accounts, positions, balances, authorizations=None):
        self._accounts = accounts
        self._positions = positions
        self._balances = balances
        self._authorizations = [HEALTHY_AUTH] if authorizations is None else authorizations
        self.account_information = self
        self.connections = self

    class _Response:
        def __init__(self, body):
            self.body = body

    def list_user_accounts(self, **_):
        return self._Response(self._accounts)

    def list_brokerage_authorizations(self, **_):
        return self._Response(self._authorizations)

    def get_all_account_positions(self, account_id, **_):
        return self._Response({"results": self._positions.get(account_id, [])})

    def get_user_account_balance(self, account_id, **_):
        return self._Response(self._balances.get(account_id, []))


class FakeDB:
    def __init__(self):
        self.written: list[dict] = []
        self.runs: list[dict] = []

    def insert_positions(self, rows):
        self.written = rows
        return rows

    def insert_sync_run(self, row):
        self.runs.append(row)
        return [row]


# -- sync_runs bookkeeping --------------------------------------------------


def test_run_row_captures_the_brokers_refresh_timestamp():
    """The whole reason the table exists: this value is otherwise discarded."""
    row = sync.build_run_row(SYNCED_AT, [account()], {}, {"acct-1"}, 3, "ok")
    detail = row["accounts"]["acct-1"]
    assert detail["last_successful_sync"] == "2026-07-21T17:17:40.750897+00:00"
    assert detail["included"] is True
    assert detail["name"] == "Robinhood Individual"
    assert row["status"] == "ok"
    assert row["rows_written"] == 3


def test_run_row_records_why_an_account_was_excluded():
    row = sync.build_run_row(
        SYNCED_AT,
        [account()],
        {"acct-1": ["Robinhood Individual: brokerage connection disabled"]},
        set(),
        0,
        "failed",
        "everything broke",
    )
    detail = row["accounts"]["acct-1"]
    assert detail["included"] is False
    assert "disabled" in detail["problems"][0]
    assert row["error"] == "everything broke"


class ExplodingRunDB(FakeDB):
    """A DB whose sync_runs write fails but whose positions write works."""

    def insert_sync_run(self, row):
        raise RuntimeError("sync_runs table missing")


def test_bookkeeping_failure_never_breaks_a_good_sync():
    """Diagnostics must not become the failure they are meant to diagnose."""
    client = FakeClient([account()], {"acct-1": [EQUITY_ROW]}, {"acct-1": [BALANCE]})
    db = ExplodingRunDB()
    written = sync.sync(client, db, now=NOW)
    assert len(written) == 2


def test_a_run_is_recorded_on_success():
    client = FakeClient([account()], {"acct-1": [EQUITY_ROW]}, {"acct-1": [BALANCE]})
    db = FakeDB()
    sync.sync(client, db, now=NOW)
    assert len(db.runs) == 1
    assert db.runs[0]["status"] == "ok"
    assert db.runs[0]["synced_at"] == SYNCED_AT  # joins to positions.synced_at


def test_a_run_is_recorded_when_everything_fails():
    """A failed sync is exactly the evidence worth keeping."""
    bad = account(brokerage_authorization="auth-gone")
    db = FakeDB()
    with pytest.raises(sync.SyncError):
        sync.sync(FakeClient([bad], {}, {}), db, now=NOW)
    assert len(db.runs) == 1
    assert db.runs[0]["status"] == "failed"
    assert db.runs[0]["rows_written"] == 0
    # the broker timestamp is captured even though nothing was written
    assert db.runs[0]["accounts"]["acct-1"]["last_successful_sync"]


def test_a_partial_run_is_recorded_as_partial():
    client = FakeClient(
        [account(), account("Crypto", id="acct-2", brokerage_authorization="auth-2")],
        {"acct-1": [EQUITY_ROW], "acct-2": [EQUITY_ROW]},
        {"acct-1": [BALANCE], "acct-2": [BALANCE]},
        authorizations=[HEALTHY_AUTH, {"id": "auth-2", "disabled": True}],
    )
    db = FakeDB()
    with pytest.raises(sync.PartialSyncError):
        sync.sync(client, db, now=NOW)
    run = db.runs[0]
    assert run["status"] == "partial"
    assert run["rows_written"] == 2
    assert run["accounts"]["acct-1"]["included"] is True
    assert run["accounts"]["acct-2"]["included"] is False


def test_sync_writes_one_snapshot_under_a_single_timestamp():
    client = FakeClient([account()], {"acct-1": [EQUITY_ROW, OPTION_ROW]}, {"acct-1": [BALANCE]})
    db = FakeDB()
    written = sync.sync(client, db, now=NOW)
    assert len(written) == 3
    assert {r["synced_at"] for r in written} == {SYNCED_AT}


def test_one_broken_account_does_not_cost_the_healthy_one_its_snapshot():
    """The whole point of the partition: equity history survives a bad crypto leg."""
    good = account()
    bad = account("Robinhood Crypto", id="acct-2", brokerage_authorization="auth-2")
    client = FakeClient(
        [good, bad],
        {"acct-1": [EQUITY_ROW, OPTION_ROW], "acct-2": [EQUITY_ROW]},
        {"acct-1": [BALANCE], "acct-2": [BALANCE]},
        authorizations=[HEALTHY_AUTH, {"id": "auth-2", "disabled": True}],
    )
    db = FakeDB()

    with pytest.raises(sync.PartialSyncError) as caught:
        sync.sync(client, db, now=NOW)

    # the healthy account's rows are written, the broken one's are not
    assert {r["account_id"] for r in db.written} == {"acct-1"}
    assert len(db.written) == 3
    assert caught.value.written == db.written
    assert "Robinhood Crypto" in caught.value.excluded
    assert "disabled" in str(caught.value)


def test_partial_sync_error_is_still_a_sync_error():
    """Callers that do not care about the distinction must still fail loudly."""
    assert issubclass(sync.PartialSyncError, sync.SyncError)


def test_all_accounts_broken_writes_nothing():
    bad = account(brokerage_authorization="auth-gone")
    db = FakeDB()
    with pytest.raises(sync.SyncError, match="no trustworthy accounts"):
        sync.sync(FakeClient([bad], {}, {}), db, now=NOW)
    assert db.written == []


def test_problems_are_attributed_to_the_right_account():
    good = account()
    bad = account("Crypto", id="acct-2", brokerage_authorization="auth-2")
    keyed = sync.problems_by_account(
        [good, bad], [HEALTHY_AUTH, {"id": "auth-2", "disabled": True}], NOW
    )
    assert keyed["acct-1"] == []
    assert len(keyed["acct-2"]) == 1


def test_sync_refuses_to_write_stale_data():
    stale = account(
        sync_status={
            "holdings": {
                "last_successful_sync": "2026-07-01T00:00:00+00:00",
                "initial_sync_completed": True,
            }
        }
    )
    db = FakeDB()
    with pytest.raises(sync.SyncError, match="no trustworthy accounts") as caught:
        sync.sync(FakeClient([stale], {}, {}), db, now=NOW)
    assert "last synced" in str(caught.value)
    assert db.written == []


def test_sync_refuses_to_write_when_the_connection_is_disabled():
    """Holdings are fresh and plentiful — only the disabled flag says otherwise."""
    client = FakeClient(
        [account()],
        {"acct-1": [EQUITY_ROW]},
        {"acct-1": [BALANCE]},
        authorizations=[DISABLED_AUTH],
    )
    db = FakeDB()
    with pytest.raises(sync.SyncError, match="disabled"):
        sync.sync(client, db, now=NOW)
    assert db.written == []


def test_main_reports_unexpected_errors_as_one_line_not_a_traceback(monkeypatch, capsys):
    """A SnapTrade/Supabase failure must read like a sentence, not a stack dump.

    From Sprint 4 this string is delivered to a phone.
    """

    def boom():
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(sync, "build_client", boom)
    monkeypatch.setattr(sync.DB, "from_env", classmethod(lambda cls: FakeDB()))

    assert sync.main() == 1
    # assert on the reported line itself; logging handlers may write elsewhere
    reported = [
        line for line in capsys.readouterr().err.splitlines() if line.startswith("sync:")
    ]
    assert reported == ["sync: FAILED — RuntimeError: connection reset by peer"]


def test_one_line_strips_the_http_header_dump():
    """A SnapTrade ApiException stringifies to a dozen lines of headers."""
    verbose = RuntimeError(
        "(401)\n"
        "Reason: Unauthorized\n"
        "HTTP response headers: HTTPHeaderDict({'Date': 'Tue', 'Server': 'gunicorn'})\n"
        "HTTP response body: {'detail': 'Invalid clientId provided', 'code': '1083'}"
    )
    result = sync.one_line(verbose)
    assert "\n" not in result
    assert "HTTPHeaderDict" not in result
    assert "Invalid clientId provided" in result  # the part that explains it
    assert result.startswith("RuntimeError: (401)")


def test_one_line_truncates_runaway_messages():
    result = sync.one_line(RuntimeError("x" * 5000))
    assert len(result) < 350
    assert result.endswith("…")


def test_main_returns_zero_on_a_clean_sync(monkeypatch, capsys):
    client = FakeClient([account()], {"acct-1": [EQUITY_ROW]}, {"acct-1": [BALANCE]})
    monkeypatch.setattr(sync, "build_client", lambda: client)
    monkeypatch.setattr(sync.DB, "from_env", classmethod(lambda cls: FakeDB()))

    assert sync.main() == 0
    assert "wrote 2 position rows" in capsys.readouterr().out


def test_main_returns_one_and_says_partial_when_an_account_is_excluded(monkeypatch, capsys):
    client = FakeClient(
        [account(), account("Robinhood Crypto", id="acct-2", brokerage_authorization="auth-2")],
        {"acct-1": [EQUITY_ROW], "acct-2": [EQUITY_ROW]},
        {"acct-1": [BALANCE], "acct-2": [BALANCE]},
        authorizations=[HEALTHY_AUTH, {"id": "auth-2", "disabled": True}],
    )
    monkeypatch.setattr(sync, "build_client", lambda: client)
    monkeypatch.setattr(sync.DB, "from_env", classmethod(lambda cls: FakeDB()))

    assert sync.main() == 1
    err = capsys.readouterr().err
    assert "sync: PARTIAL" in err
    assert "Robinhood Crypto" in err


def test_sync_fails_when_no_accounts_are_linked():
    with pytest.raises(sync.SyncError, match="no linked accounts"):
        sync.sync(FakeClient([], {}, {}), FakeDB(), now=NOW)


def test_sync_refuses_to_record_an_empty_snapshot():
    client = FakeClient([account()], {"acct-1": []}, {"acct-1": []})
    with pytest.raises(sync.SyncError, match="zero rows"):
        sync.sync(client, FakeDB(), now=NOW)
