# Options Income Bot — Sprint Plan (Infra First, Logic Last)

**Principle:** Build the entire pipeline as a "walking skeleton" with a stub engine, prove every
pipe end-to-end, then swap real logic in last. No strategy code until data flows reliably.

---

## PHASE A — INFRASTRUCTURE (Sprints 0–4)

### Sprint 0 — Scaffold (half day)
- Repo init, `CLAUDE.md` (stack, conventions, "LLM never computes trade math")
- `.env` + `.gitignore` hygiene; GitHub Actions secrets configured
- Python project skeleton (`uv` or `poetry`), pytest wired, ruff/lint
- Hello-world GH Actions cron: runs nightly, writes one heartbeat row to Supabase
- **Exit criteria:** scheduled job runs in CI and lands a row in the cloud DB

### Sprint 1 — Data Stores
- Supabase schema migration, 4 tables:
  - `positions` (snapshot per sync)
  - `iv_history` (ticker, date, atm_iv)
  - `suggestions` (+ `decision_snapshot` JSONB, `rule_version`, `taken`)
  - `outcomes` (+ `realized_pnl`, `max_adverse_excursion`, `counterfactual_pnl`)
- `rules_v1.yaml` scaffold (empty thresholds, versioned in git from day one)
- Thin DB access layer + tests against a local/branch database
- **Exit criteria:** migrations reproducible from scratch; CRUD tested

### Sprint 2 — Portfolio Pipe (SnapTrade)
- SnapTrade personal API key, one-time OAuth link to Robinhood (read-only)
- Sync script: SnapTrade → normalized rows → `positions`
- Failure handling: stale-token detection → loud Telegram/log alert (never silent stale data)
- Wire into a cron (can piggyback the heartbeat job)
- **Exit criteria:** positions + cash land in Supabase automatically; raw sync only, zero interpretation

**Carried forward — open items from Sprint 2 (recorded 2026-07-21):**

- [ ] **TODO: record per-run sync metadata.** Nothing currently persists *when the
  broker last refreshed*: `sync_status` sits on the SnapTrade account object,
  is read by `check_freshness`, and is then discarded — `positions.raw` holds
  only position/balance payloads. Every question below about thresholds is
  blocked on this, and the data is not recoverable retroactively.
  Preferred shape: a `sync_runs` table (new migration) with one row per sync —
  run timestamp, accounts included, accounts excluded + why, and each account's
  `sync_status`. Sprint 4 needs run metadata for the brief regardless, so this
  is that work pulled earlier rather than extra work.

- `MAX_HOLDINGS_AGE` in `src/sync.py` is **72h, provisional**. The open question is
  whether SnapTrade refreshes holdings on non-trading days. If it pauses over
  weekends, a Friday market holiday (Good Friday every year, sometimes July 4 /
  Christmas) puts the Monday 01:00 UTC run at ~77h and it fails on healthy data.
  If it refreshes daily regardless, 72h is already generous and could tighten.
  **Blocked on evidence we are not yet collecting:** `sync_status` lives on the
  SnapTrade *account* object, and `positions.raw` only stores position and
  balance payloads. Nothing records when the broker last refreshed. Capture it
  first (account-level metadata per run — a `sync_runs` table is the clean home,
  and Sprint 4 needs run metadata anyway), then read a weekend off it.
- Right fix long-term: measure against the last *trading session* rather than
  wall-clock hours. Needs a market calendar — see Sprint 5, where one is required
  anyway. Until then the hour threshold is only a backstop: `check_connections`
  catches a genuinely broken connection directly via SnapTrade's `disabled` flag.
- Cash rows are written per account, including Robinhood Crypto. Sprint 5's CSP
  capacity must filter by account or it will overstate usable collateral.

### Sprint 3 — Market Data Pipe (ThetaData)
- Theta Terminal wrapper: launch Java process, auth, health-check, query, teardown
  (works locally AND inside GH Actions runner)
- Backfill script: 1 year EOD ATM IV per tracked ticker → `iv_history`
- Daily cron (weeknights ~6pm PT): append EOD IV snapshot
- Chain fetch function: ticker → strikes/bid/ask/delta/IV/OI as clean dataframe (fetch only, no filtering)
- **Exit criteria:** `iv_history` backfilled + growing daily on its own; chain fetch returns clean data in CI
- ⚠️ **Start the daily cron the moment it works** — every day it runs is history you own

**Plan revision — what the FREE ThetaData tier actually allows (measured 2026-07-22):**
| Capability | Status |
| --- | --- |
| Option EOD incl. **bid/ask**, OHLC, volume | ✅ free |
| Stock EOD (underlying price) | ✅ free |
| Expirations / strikes lists | ✅ free |
| Interest rate (SOFR) | ✅ free |
| History depth | ✅ 2023-06-01 → today (~3 yrs, not the 1 yr advertised) |
| **Open interest** | ❌ needs VALUE ($40/mo) |
| **Implied volatility + Greeks** | ❌ needs STANDARD ($80/mo) |

Two consequences the original plan did not anticipate:
1. **IV and delta are computed, not fetched** (`src/engine/black_scholes.py`). Free
   gives quotes + underlying, which is everything Black-Scholes needs. Saves $80/mo.
2. **`min_open_interest` cannot be evaluated on the free tier.** Volume *is* in the
   EOD response and is a reasonable liquidity proxy for a weekly strategy — but
   swapping OI for volume is a real rules change for Sprint 5, not a free
   substitution. Decide there: pay $40, or rewrite the liquidity floor.

**Open decision — track tickers beyond the current portfolio?**

`src/iv.py::tracked_tickers()` takes the equity symbols from the latest sync (22
names as of 2026-07-21) and nothing else. That is self-maintaining: buy a stock,
and IV history starts accruing for it the same evening.

The gap is cash-secured puts. A CSP is how you *acquire* a position, so the
interesting candidates are names you do **not** hold yet — and today none of
them are tracked. As it stands the engine can only propose CSPs on names already
in the portfolio, which is a real strategy but a narrow one.

Options:
- Leave as-is. CSPs restricted to existing holdings.
- Add a static watchlist (a `tickers` table, or a list in `rules_vN.yaml`) so
  history starts accruing now for names you might want later.
- Rely on the backfill. ThetaData sells ~3 years, so a name added in November
  can have its history purchased retroactively.

**Not urgent, and deliberately so** — unlike `positions` or `sync_runs`, this
history *is* buyable after the fact, so deferring costs nothing but the backfill
run. Decide alongside Sprint 5's CSP sizing, when it is clear which names the
strategy actually wants. Whatever is chosen, the `method` stamp and the DTE
convention must match the existing series or the readings are not comparable.

**Re-check before trusting IV rank in Sprint 5 — all three, in this order:**

- [ ] **Is the 30-DTE target still right?** Lives in `src/engine/iv.py` as
  `DEFAULT_TARGET_DTE`, deliberately NOT in `rules_v1.yaml` (no loader exists yet
  and an applied rules file must not be edited). It is arguably a strategy
  threshold, so iron rule #2 says it belongs in rules — resolve when the loader
  lands. **How to re-check:** the number only has to be *stable*, not optimal —
  IV rank is a percentile against readings picked the same way. Query
  `iv_history` and confirm the chosen expiry does not oscillate between two
  cycles week to week (`test_selection_is_stable_across_days_as_expiries_roll`
  covers the synthetic case; real expiry calendars are messier). If it flips,
  the history is not comparable and the rank is noise — fix the rule, then
  **rebuild the whole series**, never patch it forward.
- [ ] **Are the Black-Scholes numbers sound?** Verified so far: put-call parity
  (with and without dividends), price→IV→price round-trip across 0.10–1.50 vol
  and four moneyness levels, reference ATM value, and — the strongest signal —
  **live call and put IV agreeing within ~1 point** (RIOT 104.9 vs 103.9, AAPL
  29.4 vs 30.1 on 2026-07-21). Call and put are solved from independent quotes,
  so agreement means the model, rate and time convention are consistent.
  **How to re-check:** log both sides, not just the average. A widening
  call/put gap is the canary — it means an assumption drifted. Candidates below.
- [ ] **Known approximations, none yet validated against a reference:**
  - **Dividends assumed 0.** Wrong for dividend payers; inflates put IV and
    deflates call IV. Averaging the two hides most of it. yfinance already
    supplies ex-div dates in Sprint 5 — feed a yield in and see if the call/put
    gap narrows.
  - **American exercise ignored** (European Black-Scholes). Small at 30-45 DTE
    near the money, and it cancels out of a percentile.
  - **SOFR used as a continuously-compounded rate** though it is quoted simple.
    Worth ~0.01 vol points at 30 DTE; correct with `ln(1+r)` if ever material.
  - **Calendar days, not trading days** (`year_fraction`, /365). Consistent, and
    needs no market calendar — revisit alongside the Sprint 5 calendar work.
  - ⚠️ **Never change any of these silently.** A method change makes new readings
    incomparable to old ones. Change it, then rebuild the series from scratch.

**Deferred improvement — expiry selection is Friday-only, not as-of-accurate
(measured 2026-07-22, method bumped to `bs-mid-30dte-v2`).**

The backfill picks an expiry from the list of expiries as it stands *today*, but
some are listed only shortly before they expire. SPY-style short-dated daily
expiries (Mon–Thu) were listed ~2 weeks out, so a historical session targeting
~30 DTE would choose one that had no quotes on that date and be silently
dropped — 13 of 34 SPY June sessions in the pilot. The drop is *biased*, not
random: whether a session survives depends on how near its 30-DTE target lands
to a Friday, so the surviving history skews toward particular weekday/expiry
combinations. Worse, the daily job forward has no such bug (it reads the live
list for the same day it fetches), so backfilled history and forward history
would measure different contracts under the same stamp — the exact seam METHOD
exists to prevent.

The ThetaData API cannot fix this at the root: `/v3/option/list/expirations`
returns only what is listed *today* (five as-of parameter names tried, all
ignored; rows carry no first-listed date), and it is not a paywall — no tier
sells it. The v2 fix restricts selection to standard **Friday** expiries
(weeklies + monthlies), which have existed for years and so resolve at any point
in the backfill window, and are what the strategy actually writes against.

Two things left for later, neither blocking:
- **Empty-leg fallback.** Keep exact 30-DTE targeting and, when the chosen
  expiry returns no quotes, step to the next expiry that has data. More faithful
  to what a contemporaneous run saw, but more requests and it does *not* fix the
  forward/backward seam on its own — the forward job must adopt the same rule.
- **Holiday Fridays.** A standard monthly whose Friday is a market holiday
  expires the preceding **Thursday** (e.g. Good Friday). The `weekday() == FRIDAY`
  filter drops those, losing one monthly a few times a year. Harmless for a
  percentile as long as it stays consistent, but revisit alongside the Sprint 5
  market calendar, which knows the holiday dates. Any change here is a method
  bump + full rebuild, like everything else in this list.

### Sprint 4 — Orchestration + Delivery (Walking Skeleton Complete)
- Sunday cron: sync → fetch chains → **STUB engine** (pass-through: dumps raw eligible data, no decisions)
- Claude API wiring with a dummy prompt ("summarize this data") to prove the call path
- Telegram bot (or email) delivery of the stub brief
- Stub output written to `suggestions` with `rule_version = "stub"`
- **Exit criteria:** every Sunday, an automated (dumb) brief arrives on your phone.
  All pipes live: broker → DB → data → LLM → delivery. Zero strategy logic exists yet.

**One-time Telegram setup (needed before the first real send).** `src/brief.py`'s
`deliver()` needs two secrets — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (both
already placeholders in `.env.example`; put real values in local `.env` and in
GitHub Actions secrets — iron rule #3). To get them:

1. **Create the bot.** In Telegram, message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts. It returns a token like
   `123456789:AAE...` → that is `TELEGRAM_BOT_TOKEN`.
2. **Start a chat with your new bot** (open it and send it any message, e.g.
   `hi`). A bot cannot message you until you have messaged it first — skip this
   and delivery fails with `403: bot can't initiate conversation`.
3. **Find your chat id.** Visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser (token from step
   1). In the JSON, read `result[].message.chat.id` — that integer is
   `TELEGRAM_CHAT_ID`. (Alternatively message [@userinfobot](https://t.me/userinfobot),
   which replies with your id.)
4. **Smoke-test the pipe once** (proves delivery end to end before Sprint 5):
   `uv run python -c "from src.config import load_dotenv; load_dotenv(); from src
   import brief; brief.deliver('options-bot: hello')"` — a message should land on
   your phone. `deliver()` fails loudly (BriefError) if either secret is missing
   or Telegram rejects the send.

Keep the weekly cron **manual-trigger only** (`workflow_dispatch`, no `schedule`)
until Sprint 5's real engine lands — prove the loop once, then no weekly stub
spam. Turn the schedule on when the brief is worth receiving.

---

## PHASE B — LOGIC (Sprints 5–7)

### Sprint 5 — Strategy Engine (swap the stub)
All pure functions, unit-tested, driven entirely by `rules_v1.yaml`:
- Eligibility classifier (≥100 sh → CC; cash ÷ strike×100 → CSP capacity; odd lots skipped)
- IV-rank signal (percentile vs `iv_history`; below threshold → stand down)
- Exclusions (earnings ≤14d via yfinance, ex-div before expiry on short calls, OI/spread liquidity floors)
- Strike selection (CC ~0.25–0.30Δ, CSP ~0.20–0.30Δ, 30–45 DTE, rank by annualized premium yield)
- Position sizing (≤5% notional per name, ≤20% cash committed to CSPs)
- Market calendar (needed for DTE/expiry math anyway) — once it exists, replace
  `src/sync.py`'s wall-clock `MAX_HOLDINGS_AGE` with "has this refreshed since
  the last market close?" See the carried-forward note under Sprint 2.
- Real Claude rationale prompt (computed numbers in, prose out)
- **Exit criteria:** Sunday brief now contains real ranked suggestions with full decision snapshots

### Sprint 6 — Reflection Layer
- Extend daily cron: mark-to-market open suggestions (feeds max adverse excursion)
- Expiry scorer → `outcomes`: realized P&L, MAE, counterfactuals (vs hold, vs cash), `taken` flag
- Attribution SQL: performance by strategy / IV-rank bucket / delta bucket / ticker / DTE
- Dashboard tab on existing React app rendering attribution
- **Exit criteria:** every expired suggestion auto-scored; attribution queryable

### Sprint 7 — Retro Loop
- Monthly Claude retrospective over attribution tables
- Rule-change *proposals* only (hard-coded min-N guard; weight by P&L, never hit rate)
- `rules_vN.yaml` versioning + stamping; rollback = git revert
- **Exit criteria:** first monthly retro lands with proposals; you approve/reject manually

---

## Sequencing Notes
- Natural pause after Sprint 5: run live 6–8 weeks before Sprints 6–7 mean anything
- Sprint 2 and 3 are independent — parallelize if using Claude Code on both
- Dev environment: local VS Code + Claude Code preferred (Theta Terminal = local Java process);
  Codespaces works but add Java + terminal launch to devcontainer
- Production runtime is GitHub Actions throughout — dev choice never affects prod
