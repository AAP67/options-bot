# CLAUDE.md — Options Income Bot

## What this is
A suggestion-only options income engine for a personal Robinhood portfolio.
It NEVER places trades. Output = a weekly ranked brief of covered-call / cash-secured-put
candidates, delivered via Telegram, with full decision logging for later scoring.

## Stack
- Python 3.12, managed with `uv`
- Supabase (Postgres) — all state; schema lives in `supabase/migrations/`
- SnapTrade personal API — read-only Robinhood portfolio sync
- ThetaData — option chains, Greeks, IV (via local Theta Terminal Java process on localhost)
- yfinance — earnings + ex-dividend calendars only
- Claude API — prose rationale only (see Iron Rules)
- GitHub Actions — all scheduled runtime (daily IV snapshot; Sunday engine run)
- Delivery: Telegram bot

## Iron rules
1. **The LLM never computes trade math.** All numbers (strikes, deltas, sizing, yields,
   IV rank) come from deterministic Python. Claude API receives computed numbers and
   writes prose. If a prompt asks the LLM to pick a strike, that's a bug.
2. **No magic numbers in engine code.** Every threshold lives in `rules/rules_vN.yaml`.
   Rule changes = new version file, never edit an old one. Every suggestion row is
   stamped with `rule_version`.
3. **Secrets never in the repo.** Local: `.env` (gitignored). CI: GitHub Actions secrets.
   If you need a new secret, add it to `.env.example` with a placeholder.
4. **Fail loudly.** A sync that can't reach SnapTrade or ThetaData must alert (Telegram/
   log error), never proceed with stale data silently.
5. **Suggestion-only.** No order placement code, ever, in this repo.

## Layout
```
src/
  db.py            # thin Supabase I/O layer — no business logic
  sync.py          # SnapTrade -> account_snapshots + positions
  theta.py         # Theta Terminal lifecycle + chain/IV queries
  engine/          # pure functions: eligibility, iv_rank, exclusions, select, size
  brief.py         # Claude API rationale + Telegram delivery
  score.py         # expiry scoring -> outcomes (Sprint 6)
rules/             # rules_v1.yaml, rules_v2.yaml, ...
supabase/          # CLI project + migrations/
tests/             # pytest; unit tests mock all I/O
.github/workflows/ # daily.yml, weekly.yml
```

## Conventions
- Engine functions are pure: data in, decisions out, no I/O inside `src/engine/`.
- Unit tests mock Supabase/HTTP; integration tests are gated behind `RUN_INTEGRATION=1`.
- `ruff` for lint/format; type hints everywhere; `pytest` must pass before commit.
- DB schema changes only via new migration files — never edit applied migrations.
- Timestamps in UTC in the DB; display conversion happens at delivery.

## Domain notes (context for suggestions)
- Covered calls need ≥100 shares per contract (US standard lots).
- CSP collateral = strike × 100 in cash.
- Short calls carry early-assignment risk ahead of ex-dividend dates.
- IV rank = today's ATM IV percentile vs trailing history in `iv_history`.
- Option-selling P&L is fat-left-tailed: scoring weights P&L, never win rate.
