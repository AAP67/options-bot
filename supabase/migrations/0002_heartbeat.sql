-- Sprint 0 — heartbeat: proof-of-life for the cron -> Supabase pipe.
-- One row written by each scheduled/CI run. Operational only, not domain data.
-- Migrations are immutable: never edit once applied; add 0003_*.sql etc.

create table if not exists heartbeat (
    id       bigint generated always as identity primary key,
    ran_at   timestamptz not null default now(),   -- when the job ran (UTC)
    source   text        not null default 'ci',     -- 'ci' | 'local'
    git_sha  text,                                   -- commit that produced the run
    note     text
);

create index if not exists heartbeat_ran_at_idx on heartbeat (ran_at);
