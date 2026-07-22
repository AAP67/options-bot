# Backfill runbook — historical ATM IV

How to run `src/backfill.py` to seed `iv_history` with historical readings.

**Why this is a local, hand-run job and not CI:** it takes ~14 hours (roughly
780 requests per ticker at the free tier's 20 req/min), and GitHub Actions caps
a job at 6 hours. It is resumable and idempotent, so the right home is a machine
you can leave running overnight — see [SPRINTS.md](../SPRINTS.md), Sprint 3.

## Before you start

Two hard constraints, both because the free ThetaData key allows **one request
at a time**:

- **Only one thing may hit the key at once.** While the backfill runs, do not
  run the daily job, the smoke tests, or a second backfill on any machine.
- **Do not overlap the scheduled CI daily run at 01:00 UTC.** It uses the same
  key from a GitHub runner. Start the backfill after ~01:10 UTC.

And, as always: `.env` holds live secrets — copy it to a new machine securely,
never commit it (iron rule #3).

## One-time setup (skip whatever is already in place)

```bash
# 1. Prerequisites — Java 21+ (for Theta Terminal v3) and uv
java -version        # need 21+; install Temurin 21+ if missing
uv --version         # install: https://docs.astral.sh/uv/getting-started/

# 2. Repo
git clone https://github.com/AAP67/options-bot
cd options-bot
uv sync

# 3. Secrets — the backfill needs only these three in .env
#    (copy from an existing .env, or the Supabase / ThetaData dashboards)
cat > .env <<'EOF'
SUPABASE_URL=https://YOUR-PROJECT.supabase.co
SUPABASE_SERVICE_KEY=your-real-key
THETADATA_API_KEY=your-real-key
EOF

# 4. Theta Terminal jar (gitignored, ~41MB; it then bootstraps ~98MB of libs)
mkdir -p vendor
curl -sSfL -o vendor/ThetaTerminalv3.jar https://downloads.thetadata.us/ThetaTerminalv3.jar
```

## Run it

No flags needed. Defaults: start `2023-06-01` (earliest the free tier serves),
end yesterday, tickers pulled from the latest portfolio sync. Expect ~14 hours.

```bash
# macOS — keeps the machine awake, tees output to a log
caffeinate -i uv run python -m src.backfill 2>&1 | tee backfill.log

# Linux — equivalent
systemd-inhibit --what=idle:sleep:handle-lid-switch uv run python -m src.backfill 2>&1 | tee backfill.log
```

You can keep working on the machine meanwhile — the job is ~95% idle-waiting on
the rate limit (a 5-minute pilot used 15 seconds of CPU). Just don't fire off
other ThetaData commands (see constraints above).

### Useful flags

| Flag | Default | Use |
| --- | --- | --- |
| `--start YYYY-MM-DD` | `2023-06-01` | shorten the window |
| `--end YYYY-MM-DD` | yesterday | shorten the window |
| `--tickers SYM,SYM` | tracked portfolio | backfill one new name without rescanning the rest |

There is deliberately **no** `--target-dte` flag: the DTE is part of the method
stamped on every row, and mixing DTEs would make the percentile meaningless.

## While it runs

- Progress logs per ticker: `[3/22] COIN` … `COIN: wrote 780 reading(s) of 780
  session(s)`. Watch with `tail -f backfill.log` in another tab.
- Any ticker already covered under the current method is skipped, so a
  previously-run name (e.g. a pilot) flies by.

## If it dies (sleep, crash, Ctrl-C)

Re-run the **exact same command**. It is resumable: completed `(ticker, date)`
rows under the current method are skipped, so you lose only the day in flight.
No cleanup, no flags.

Keep the machine awake once you step away — the `caffeinate` / `systemd-inhibit`
wrappers above handle idle-sleep, but a closed lid on some laptops still
suspends. If it does sleep, just resume.

## When it finishes

Final line: `backfill: wrote N reading(s) across 22 ticker(s)`. Verify the
newest stored session under the current method:

```bash
uv run python -c "from src.config import load_dotenv; load_dotenv(); from src.db import DB; print(DB.from_env().latest_iv_date('bs-mid-30dte-v2'))"
```

From then on the daily CI job keeps the series current and self-heals any missed
session (see `src/iv.py`), so the backfill is a one-time seed — re-run only to
add a new ticker's history (`--tickers NEWSYM`).

## Method note

Every row is stamped `bs-mid-30dte-v2`. If the derivation ever changes (target
DTE, expiry rule, mid vs close, dividends, day-count), bump `METHOD` in
`src/engine/iv.py` and **rebuild the whole series** — readings from different
methods are not comparable, and a percentile over a mixed series is meaningless.
`existing_dates` is method-aware, so bumping `METHOD` makes a re-run rebuild
rather than skip.
