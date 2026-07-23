"""Tests for the Claude call path (Sprint 4, step 3).

The prompt is a dummy, so the tests pin the *plumbing*: the request is shaped
right and carries the computed rows, the prose comes back out, and every failure
mode raises BriefError loudly rather than returning nothing. The Anthropic client
is a fake — no network, no key, the suite stays hermetic.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import anthropic
import pytest

from src import brief

ROWS = [
    {
        "ticker": "AAPL",
        "strategy": "covered_call",
        "expiry": "2026-08-21",
        "strike": 230.0,
        "delta": 0.28,
    },
    {
        "ticker": "MSFT",
        "strategy": "cash_secured_put",
        "expiry": "2026-08-21",
        "strike": 400.0,
        "delta": -0.22,
    },
]


class FakeBlock:
    def __init__(self, type: str, text: str = "") -> None:
        self.type = type
        self.text = text


class FakeResponse:
    def __init__(self, blocks: list[FakeBlock], stop_reason: str = "end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class FakeClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.messages = FakeMessages(response, error)


# -- prompt rendering -------------------------------------------------------


def test_prompt_lists_each_row_with_its_computed_numbers():
    prompt = brief.render_prompt(ROWS)
    assert "AAPL covered_call" in prompt
    assert "strike 230.0 delta 0.28" in prompt
    assert "MSFT cash_secured_put" in prompt


def test_empty_rows_render_an_honest_nothing_to_suggest_prompt():
    prompt = brief.render_prompt([])
    assert "No candidate contracts" in prompt


# -- the call path ----------------------------------------------------------


def test_summarize_returns_the_text_block():
    client = FakeClient(FakeResponse([FakeBlock("text", "Two candidates this week.")]))
    assert brief.summarize(ROWS, client) == "Two candidates this week."


def test_summarize_sends_the_right_model_boundary_and_rows():
    client = FakeClient(FakeResponse([FakeBlock("text", "ok")]))
    brief.summarize(ROWS, client)

    [call] = client.messages.calls
    assert call["model"] == "claude-opus-4-8"
    assert call["system"] == brief.SYSTEM
    # the computed rows reached the model as text (iron rule #1: prose in/out)
    assert "AAPL" in call["messages"][0]["content"]


def test_summarize_skips_thinking_blocks_and_uses_the_text():
    client = FakeClient(
        FakeResponse([FakeBlock("thinking", ""), FakeBlock("text", "the prose")])
    )
    assert brief.summarize(ROWS, client) == "the prose"


def test_summarize_wraps_sdk_errors_in_brief_error():
    client = FakeClient(error=anthropic.AnthropicError("boom"))
    with pytest.raises(brief.BriefError, match="Claude request failed"):
        brief.summarize(ROWS, client)


def test_summarize_raises_when_no_text_block_comes_back():
    client = FakeClient(FakeResponse([], stop_reason="max_tokens"))
    with pytest.raises(brief.BriefError, match="no text"):
        brief.summarize(ROWS, client)


# -- client construction ----------------------------------------------------


def test_build_client_fails_loudly_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(brief.BriefError, match="ANTHROPIC_API_KEY"):
        brief.build_client()


def test_build_client_rejects_the_placeholder(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    with pytest.raises(brief.BriefError, match="ANTHROPIC_API_KEY"):
        brief.build_client()


def test_build_client_builds_with_a_real_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    client = brief.build_client()
    assert isinstance(client, anthropic.Anthropic)


# -- Telegram delivery ------------------------------------------------------


class FakeHTTPResponse:
    """Context-manager stand-in for urlopen's return value."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def fake_urlopen(
    captured: dict[str, Any],
    *,
    body: bytes = b'{"ok": true, "result": {"message_id": 42}}',
    error: Exception | None = None,
):
    """A urlopen replacement that records the request and returns `body`."""

    def _urlopen(request: Any, timeout: float | None = None) -> FakeHTTPResponse:
        captured["request"] = request
        captured["timeout"] = timeout
        if error is not None:
            raise error
        return FakeHTTPResponse(body)

    return _urlopen


CREDS = {"token": "12345:secret", "chat_id": "999"}


def test_deliver_posts_the_text_and_returns_the_message_id(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(brief.urllib.request, "urlopen", fake_urlopen(captured))

    message_id = brief.deliver("this week's brief", **CREDS)

    assert message_id == 42
    request = captured["request"]
    assert request.method == "POST"
    assert request.full_url == "https://api.telegram.org/bot12345:secret/sendMessage"
    sent = json.loads(request.data)
    assert sent == {"chat_id": "999", "text": "this week's brief"}


def test_deliver_reads_credentials_from_the_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "env-chat")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(brief.urllib.request, "urlopen", fake_urlopen(captured))

    brief.deliver("hi")
    assert "botenv-token/" in captured["request"].full_url


@pytest.mark.parametrize("missing", ["token", "chat_id"])
def test_deliver_fails_loudly_when_a_credential_is_missing(monkeypatch, missing):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    creds = dict(CREDS)
    creds[missing] = "placeholder"
    with pytest.raises(brief.BriefError, match="TELEGRAM_"):
        brief.deliver("brief", **creds)


def test_deliver_refuses_an_empty_brief():
    with pytest.raises(brief.BriefError, match="empty"):
        brief.deliver("   ", **CREDS)


def test_deliver_refuses_a_brief_over_the_telegram_limit():
    too_long = "x" * (brief.TELEGRAM_MAX_CHARS + 1)
    with pytest.raises(brief.BriefError, match="caps a message"):
        brief.deliver(too_long, **CREDS)


def test_deliver_raises_when_telegram_reports_not_ok(monkeypatch):
    body = b'{"ok": false, "description": "Bad Request: chat not found"}'
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        brief.urllib.request, "urlopen", fake_urlopen(captured, body=body)
    )
    with pytest.raises(brief.BriefError, match="chat not found"):
        brief.deliver("brief", **CREDS)


def test_deliver_wraps_an_http_error(monkeypatch):
    err = urllib.error.HTTPError(
        url="https://api.telegram.org/botX/sendMessage",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"ok": false, "description": "Unauthorized"}'),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        brief.urllib.request, "urlopen", fake_urlopen(captured, error=err)
    )
    with pytest.raises(brief.BriefError, match="HTTP 401"):
        brief.deliver("brief", **CREDS)


def test_deliver_wraps_a_network_error(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        brief.urllib.request,
        "urlopen",
        fake_urlopen(captured, error=OSError("connection refused")),
    )
    with pytest.raises(brief.BriefError, match="could not reach Telegram"):
        brief.deliver("brief", **CREDS)
