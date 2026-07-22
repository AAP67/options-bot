"""Unit tests for the Theta Terminal wrapper. No Java, no network.

The process and HTTP layers are faked; what is under test is the lifecycle
logic — attach vs launch, readiness polling, teardown — and the response
handling that separates "no data" from "your plan does not include this".
"""

from __future__ import annotations

import io
import pathlib
import subprocess

import pytest

from src import theta


@pytest.fixture()
def jar(tmp_path) -> pathlib.Path:
    """A stand-in jar file; nothing ever executes it."""
    path = tmp_path / "ThetaTerminalv3.jar"
    path.write_bytes(b"not really a jar")
    return path


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("THETADATA_API_KEY", "test-key")


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Keep the startup poll and throttle from actually waiting."""
    monkeypatch.setattr(theta.time, "sleep", lambda _s: None)


class FakeProcess:
    """A launched process that stays alive until terminated."""

    def __init__(self, *_a, **_kw):
        self.terminated = False
        self.killed = False
        self._exit_code = None

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True
        self._exit_code = 0

    def kill(self):
        self.killed = True
        self._exit_code = -9

    def wait(self, timeout=None):
        return self._exit_code


def make_terminal(jar, monkeypatch, *, healthy_after=1, process_cls=FakeProcess):
    """A ThetaTerminal whose health probe succeeds after N failed checks.

    The default is 1, not 0: with an immediately-healthy probe `start()`
    correctly attaches to the "existing" Terminal and never launches anything,
    which is not what the launch/teardown tests mean to exercise.
    """
    term = theta.ThetaTerminal(jar_path=jar, startup_timeout=30, request_interval=0)
    calls = {"health": 0, "spawned": []}

    def fake_request(path, params, timeout=60.0):
        if path == theta.HEALTH_PATH:
            calls["health"] += 1
            if calls["health"] <= healthy_after:
                raise theta.ThetaError("not up yet")
            return '{"response": []}'
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(term, "_request", fake_request)

    def fake_popen(argv, **kwargs):
        calls["spawned"].append(argv)
        return process_cls()

    monkeypatch.setattr(theta.subprocess, "Popen", fake_popen)
    return term, calls


# -- lifecycle --------------------------------------------------------------


def test_attaches_to_an_already_running_terminal_instead_of_launching():
    """A second instance would fail to bind and look like a crash."""
    term = theta.ThetaTerminal(jar_path=pathlib.Path("/nonexistent.jar"))
    term.is_up = lambda: True  # type: ignore[method-assign]
    term.start()
    assert term._process is None
    assert term._owns_process is False


def test_launches_and_returns_once_healthy(jar, monkeypatch):
    term, calls = make_terminal(jar, monkeypatch, healthy_after=2)
    term.start()
    assert len(calls["spawned"]) == 1
    assert calls["spawned"][0][:2] == ["java", "-jar"]
    assert calls["health"] == 3  # two failures, then ready
    term.stop()


def test_missing_jar_explains_how_to_get_it(tmp_path):
    term = theta.ThetaTerminal(jar_path=tmp_path / "absent.jar")
    term.is_up = lambda: False  # type: ignore[method-assign]
    with pytest.raises(theta.ThetaError, match="downloads.thetadata.us"):
        term.start()


def test_missing_api_key_fails_before_launching(jar, monkeypatch):
    monkeypatch.delenv("THETADATA_API_KEY")
    term = theta.ThetaTerminal(jar_path=jar)
    term.is_up = lambda: False  # type: ignore[method-assign]
    with pytest.raises(theta.ThetaError, match="THETADATA_API_KEY"):
        term.start()


def test_a_process_that_dies_during_startup_is_reported(jar, monkeypatch):
    class DeadProcess(FakeProcess):
        def poll(self):
            return 1

    term, _ = make_terminal(jar, monkeypatch, healthy_after=99, process_cls=DeadProcess)
    with pytest.raises(theta.ThetaError, match="exited during startup"):
        term.start()


def test_startup_timeout_stops_the_process_rather_than_leaking_it(jar, monkeypatch):
    """An orphaned Terminal holds a CI job open until the runner times out."""
    term, _ = make_terminal(jar, monkeypatch, healthy_after=999)
    spawned = []
    monkeypatch.setattr(
        theta.subprocess, "Popen", lambda *a, **k: spawned.append(FakeProcess()) or spawned[-1]
    )
    monkeypatch.setattr(theta.time, "monotonic", _clock([0, 1, 2, 999]))

    with pytest.raises(theta.ThetaError, match="not ready after"):
        term.start()
    assert spawned[0].terminated is True


def _clock(values):
    """A monotonic() that walks a fixed sequence, repeating the last value."""
    remaining = list(values)

    def clock():
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return clock


def test_stop_kills_a_process_that_ignores_terminate(jar, monkeypatch):
    class StubbornProcess(FakeProcess):
        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired("java", timeout or 0)
            return -9

    term, _ = make_terminal(jar, monkeypatch, process_cls=StubbornProcess)
    term.start()
    process = term._process
    term.stop()
    assert process.killed is True


def test_stop_is_idempotent(jar, monkeypatch):
    term, _ = make_terminal(jar, monkeypatch)
    term.start()
    term.stop()
    term.stop()  # must not raise


def test_context_manager_stops_even_when_the_body_raises(jar, monkeypatch):
    term, _ = make_terminal(jar, monkeypatch)
    with pytest.raises(ValueError):
        with term:
            process = term._process
            raise ValueError("boom")
    assert process.terminated is True


def test_attached_terminal_is_never_terminated_by_stop():
    """We did not start it, so it is not ours to kill."""
    term = theta.ThetaTerminal()
    term.is_up = lambda: True  # type: ignore[method-assign]
    term.start()
    term.stop()  # must not raise, nothing to terminate
    assert term._process is None


# -- responses --------------------------------------------------------------


def response_terminal(monkeypatch, body):
    term = theta.ThetaTerminal(request_interval=0)
    monkeypatch.setattr(term, "_request", lambda *a, **k: body)
    return term


def test_get_returns_the_response_list(monkeypatch):
    body = '{"response": [{"symbol": "RIOT", "expiration": "2026-08-21"}]}'
    term = response_terminal(monkeypatch, body)
    assert term.get("/v3/option/list/expirations", symbol="RIOT") == [
        {"symbol": "RIOT", "expiration": "2026-08-21"}
    ]


def test_no_data_is_an_empty_list_not_an_error(monkeypatch):
    """A contract with no trades that day is a real, empty answer."""
    term = response_terminal(monkeypatch, "No data found for your request")
    assert term.get("/v3/option/history/eod", symbol="RIOT") == []


def test_a_blocked_endpoint_raises_subscription_error_not_a_generic_one(monkeypatch):
    """Verified live: a plan-gated endpoint answers 403, not 200-with-text.

    Getting this wrong means a paywall surfaces as an unexplained failure — or
    worse, as empty data.
    """
    body = (
        "Requesting an option endpoint requiring a value subscription, but you "
        "only have a FREE subscription. Please consider upgrading!"
    )
    term = theta.ThetaTerminal(request_interval=0)

    def forbidden(*_a, **_k):
        raise theta.urllib.error.HTTPError(
            url="http://127.0.0.1:25503/v3/option/history/open_interest",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(body.encode()),
        )

    monkeypatch.setattr(theta.urllib.request, "urlopen", forbidden)
    with pytest.raises(theta.ThetaSubscriptionError, match="FREE subscription"):
        term.get("/v3/option/history/open_interest", symbol="RIOT")


def test_a_genuine_http_error_is_not_mistaken_for_a_paywall(monkeypatch):
    term = theta.ThetaTerminal(request_interval=0)

    def server_error(*_a, **_k):
        raise theta.urllib.error.HTTPError(
            url="http://127.0.0.1:25503/v3/option/history/eod",
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b"something broke"),
        )

    monkeypatch.setattr(theta.urllib.request, "urlopen", server_error)
    with pytest.raises(theta.ThetaError, match="HTTP 500") as caught:
        term.get("/v3/option/history/eod", symbol="RIOT")
    assert not isinstance(caught.value, theta.ThetaSubscriptionError)


def test_non_json_body_is_reported_with_its_content(monkeypatch):
    term = response_terminal(monkeypatch, "<html>error</html>")
    with pytest.raises(theta.ThetaError, match="expected JSON"):
        term.get("/v3/option/history/eod", symbol="RIOT")


def test_json_format_is_requested_by_default(monkeypatch):
    """The Terminal defaults to csv; every caller here expects json."""
    seen = {}

    term = theta.ThetaTerminal(request_interval=0)
    def capture(path, params, timeout=60.0):
        seen.update(params)
        return '{"response": []}'

    monkeypatch.setattr(term, "_request", capture)
    term.get("/v3/option/list/strikes", symbol="RIOT")
    assert seen["format"] == "json"


def test_subscription_error_still_counts_as_healthy(monkeypatch):
    """Reachable and authenticated — the plan simply lacks that endpoint."""
    term = theta.ThetaTerminal(request_interval=0)

    def blocked(*_a, **_k):
        raise theta.ThetaSubscriptionError("needs VALUE")

    monkeypatch.setattr(term, "_request", blocked)
    assert term.is_up() is True
