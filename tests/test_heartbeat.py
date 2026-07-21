"""Unit tests for the heartbeat entrypoint. No network — the DB layer's
insert path is exercised elsewhere; here we test row-building and main()'s
exit contract with a fake DB."""

from __future__ import annotations

import src.heartbeat as hb


def test_build_row_marks_ci_when_in_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.setenv("GITHUB_WORKFLOW", "ci")

    row = hb.build_row()

    assert row["source"] == "ci"
    assert row["git_sha"] == "abc123"
    assert row["note"] == "ci"


def test_build_row_marks_local_outside_ci(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("GITHUB_WORKFLOW", raising=False)

    row = hb.build_row()

    assert row["source"] == "local"
    assert "git_sha" not in row


def test_main_returns_zero_on_successful_write(monkeypatch):
    class FakeDB:
        def insert_heartbeat(self, row):
            return [{"id": 1, "source": row["source"]}]

    monkeypatch.setattr(hb.DB, "from_env", classmethod(lambda cls: FakeDB()))
    assert hb.main() == 0


def test_main_returns_one_when_no_row_written(monkeypatch):
    class FakeDB:
        def insert_heartbeat(self, row):
            return []

    monkeypatch.setattr(hb.DB, "from_env", classmethod(lambda cls: FakeDB()))
    assert hb.main() == 1
