"""Claude API rationale + delivery.

Sprint 4 proves the call path with a **dummy** prompt: hand the computed
suggestion rows to Claude and get prose back. The prompt is deliberately dumb
("summarize this data") — the point is to prove auth, request, response, and
error handling end to end, not to write a good brief. The real strategy-aware
rationale prompt lands in Sprint 5.

Iron rule #1 holds from the very first call: **the LLM writes prose only.** Every
number (strike, delta, and later premium/yield/IV-rank) is computed by
deterministic Python and handed to Claude as text. Claude describes what it is
given; it never picks a strike, ranks a candidate, or computes a figure. The
system prompt states that boundary so it is enforced even for the stub.

Telegram delivery is the next step; for now this module only produces the prose.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

# Iron rule #1: default to the latest, most capable model for the rationale.
MODEL = "claude-opus-4-8"

# A brief is short prose; 1024 keeps the non-streaming call well under the SDK's
# ~10-minute timeout guard. Sprint 5's real brief can revisit this.
MAX_TOKENS = 1024

# The boundary, stated to the model. Even with a dummy prompt, Claude must never
# be the thing that computes or ranks — that is always deterministic Python.
SYSTEM = (
    "You are the writer for an options-income brief. You write prose only. "
    "Every number you are given (strikes, deltas, premiums, yields) was computed "
    "by deterministic code upstream — treat it as fact. Never compute, re-rank, "
    "invent, or second-guess a figure; only describe what you are given. This is "
    "a suggestion-only tool: never tell the reader to place a trade."
)


class BriefError(RuntimeError):
    """Raised when the rationale cannot be produced (iron rule #4: fail loudly)."""


def build_client() -> anthropic.Anthropic:
    """Build an Anthropic client, failing loudly if the key is missing."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or key == "placeholder":
        raise BriefError("ANTHROPIC_API_KEY must be set (see .env.example).")
    return anthropic.Anthropic(api_key=key)


def render_prompt(rows: list[dict[str, Any]]) -> str:
    """Render the suggestion rows into the dummy prompt's text.

    One line per row, identifying fields only — the numbers are already computed
    (iron rule #1), so this is formatting, not decision-making. An empty set is
    stated plainly so the model has something honest to summarise.
    """
    if not rows:
        return (
            "No candidate contracts were produced this week. Write one short "
            "sentence saying there is nothing to suggest."
        )

    lines = [
        f"- {r.get('ticker')} {r.get('strategy')} exp {r.get('expiry')} "
        f"strike {r.get('strike')} delta {r.get('delta')}"
        for r in rows
    ]
    return (
        "Here is this week's candidate options data, already computed. "
        "Summarise it in a few plain sentences for the reader:\n\n" + "\n".join(lines)
    )


def _first_text(response: Any) -> str | None:
    """The first text block of a Messages response, or None if there is none."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            return block.text
    return None


def summarize(
    rows: list[dict[str, Any]],
    client: anthropic.Anthropic,
    *,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> str:
    """Ask Claude to summarise the rows and return the prose.

    A dummy call to prove the path (Sprint 4). Any SDK-level failure is wrapped
    in BriefError so the caller alerts rather than delivering nothing silently
    (iron rule #4).
    """
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM,
            messages=[{"role": "user", "content": render_prompt(rows)}],
        )
    except anthropic.AnthropicError as exc:
        raise BriefError(f"Claude request failed: {type(exc).__name__}: {exc}") from exc

    text = _first_text(response)
    if not text:
        raise BriefError(
            f"Claude returned no text (stop_reason={getattr(response, 'stop_reason', None)})."
        )
    return text
