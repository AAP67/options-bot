-- Sprint 1 — Data Stores: initial schema.
-- Four tables: positions, iv_history, suggestions, outcomes.
-- Migrations are immutable: never edit this file once applied; add 0002_*.sql etc.
-- All timestamps are UTC (timestamptz); display conversion happens at delivery.

-- ---------------------------------------------------------------------------
-- positions: one snapshot row per holding per sync (from SnapTrade, Sprint 2).
-- Raw sync only, zero interpretation. Cash is modelled as a row with
-- asset_type = 'cash' (symbol = currency code, quantity = amount) so that a
-- single sync's full account state lives in one table.
-- ---------------------------------------------------------------------------
create table if not exists positions (
    id            bigint generated always as identity primary key,
    synced_at     timestamptz not null,          -- when this sync ran (groups a snapshot)
    account_id    text        not null,          -- SnapTrade account id
    symbol        text        not null,          -- ticker, option symbol, or currency code
    asset_type    text        not null,          -- 'equity' | 'option' | 'cash'
    quantity      numeric     not null,          -- shares, contracts, or cash amount
    avg_cost      numeric,                        -- per-unit cost basis (null for cash)
    market_value  numeric,                        -- current value in `currency`
    currency      text        not null default 'USD',
    raw           jsonb       not null default '{}'::jsonb,  -- raw SnapTrade payload for this row
    created_at    timestamptz not null default now()
);

create index if not exists positions_synced_at_idx on positions (synced_at);
create index if not exists positions_symbol_idx    on positions (symbol);

-- ---------------------------------------------------------------------------
-- iv_history: daily EOD at-the-money implied vol per tracked ticker.
-- Feeds IV-rank (percentile vs trailing history). One row per (ticker, date).
-- ---------------------------------------------------------------------------
create table if not exists iv_history (
    id          bigint generated always as identity primary key,
    ticker      text        not null,
    date        date        not null,
    atm_iv      numeric     not null,            -- at-the-money implied volatility
    created_at  timestamptz not null default now(),
    unique (ticker, date)
);

create index if not exists iv_history_ticker_date_idx on iv_history (ticker, date);

-- ---------------------------------------------------------------------------
-- suggestions: the bot's ranked trade ideas. Numbers come from deterministic
-- Python; `decision_snapshot` captures the full computed context; every row is
-- stamped with the rule_version that produced it. Suggestion-only — never an order.
-- ---------------------------------------------------------------------------
create table if not exists suggestions (
    id                 bigint generated always as identity primary key,
    created_at         timestamptz not null default now(),
    run_id             text        not null,     -- groups one weekly engine run
    ticker             text        not null,
    strategy           text        not null,     -- 'covered_call' | 'cash_secured_put'
    expiry             date,
    strike             numeric,
    delta              numeric,
    premium            numeric,                   -- credit per contract
    annualized_yield   numeric,                   -- ranking metric
    decision_snapshot  jsonb       not null default '{}'::jsonb,
    rule_version       text        not null,      -- e.g. 'v1' or 'stub'
    taken              boolean     not null default false
);

create index if not exists suggestions_run_id_idx on suggestions (run_id);
create index if not exists suggestions_ticker_idx  on suggestions (ticker);

-- ---------------------------------------------------------------------------
-- outcomes: post-expiry scoring for a suggestion (Sprint 6). P&L-weighted, never
-- win-rate — option-selling returns are fat-left-tailed.
-- ---------------------------------------------------------------------------
create table if not exists outcomes (
    id                     bigint generated always as identity primary key,
    suggestion_id          bigint      not null references suggestions (id) on delete cascade,
    scored_at              timestamptz not null default now(),
    realized_pnl           numeric,               -- realized P&L at/after expiry
    max_adverse_excursion  numeric,               -- worst mark-to-market drawdown while open
    counterfactual_pnl     jsonb       not null default '{}'::jsonb,  -- {vs_hold, vs_cash, ...}
    unique (suggestion_id)
);

create index if not exists outcomes_suggestion_id_idx on outcomes (suggestion_id);
