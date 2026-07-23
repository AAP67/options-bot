"""Tests for the Claude call path (Sprint 4, step 3).

The prompt is a dummy, so the tests pin the *plumbing*: the request is shaped
right and carries the computed rows, the prose comes back out, and every failure
mode raises BriefError loudly rather than returning nothing. The Anthropic client
is a fake — no network, no key, the suite stays hermetic.
"""

from __future__ import annotations

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
