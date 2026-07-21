-- Sprint 2 — sync_runs: one row per portfolio sync attempt.
--
-- `positions` records what you own; this records what happened when we asked.
-- Without it, the broker's own refresh timestamp (sync_status.holdings.
-- last_successful_sync) is read by the freshness check and then discarded, so
-- questions like "does SnapTrade refresh holdings on weekends?" — which set
-- MAX_HOLDINGS_AGE in src/sync.py — are unanswerable, and unanswerable
-- retroactively: the history only starts once we begin writing it.
--
-- Also the durable record of a PARTIAL sync. A GitHub Actions run scrolls out
-- of retention; this does not.
--
-- Migrations are immutable: never edit once applied; add 0005_*.sql etc.

create table if not exists sync_runs (
    id            bigint generated always as identity primary key,
    synced_at     timestamptz not null,          -- joins to positions.synced_at
    status        text        not null,          -- 'ok' | 'partial' | 'failed'
    rows_written  integer     not null default 0,
    -- Per-account detail, shaped: {"<account_id>": {"name": ..., "included":
    -- true/false, "last_successful_sync": ..., "problems": [...]}}.
    -- jsonb rather than a child table: this is diagnostic breadcrumbs, not
    -- domain data, and the shape follows whatever SnapTrade reports.
    accounts      jsonb       not null default '{}'::jsonb,
    error         text,                            -- one-line reason when not 'ok'
    created_at    timestamptz not null default now()
);

create index if not exists sync_runs_synced_at_idx on sync_runs (synced_at);
create index if not exists sync_runs_status_idx    on sync_runs (status);

comment on table sync_runs is
    'One row per sync attempt: which accounts were included, what the broker '
    'reported as its last refresh, and why anything was excluded. Feeds the '
    'staleness-threshold decision and, from Sprint 4, brief run metadata.';

comment on column sync_runs.synced_at is
    'The snapshot stamp shared with positions.synced_at for this run. Rows in '
    'positions with this value are exactly what this run wrote.';
