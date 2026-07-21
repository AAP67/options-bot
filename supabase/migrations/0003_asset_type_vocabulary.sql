-- Sprint 2 — document the real `asset_type` vocabulary.
-- Comment-only: no table, column, data or constraint is altered.
--
-- 0001_init.sql described asset_type as 'equity' | 'option' | 'cash'. The live
-- SnapTrade sync also returns crypto holdings (a linked Robinhood Crypto
-- account), and may return kinds we have never seen — futures, mutual funds.
-- src/sync.py maps known instrument kinds and passes unknown ones through
-- verbatim, because Sprint 2 is a raw sync with zero interpretation and the
-- engine filters later.
--
-- Deliberately NOT a CHECK constraint: a new SnapTrade instrument kind would
-- then fail the entire nightly sync rather than land as one unrecognised row.
-- Migrations are immutable: never edit once applied; add 0004_*.sql etc.

comment on column positions.asset_type is
    'Instrument class from the broker sync. Mapped values: equity (stock, etf, '
    'mutual_fund), option, crypto, cash. Unmapped SnapTrade instrument kinds are '
    'stored verbatim rather than dropped — the engine filters on this column, so '
    'read it as an allow-list, never assume the set is closed.';

comment on column positions.avg_cost is
    'Per-unit cost basis, EXCEPT for options where the broker reports it per '
    'contract (a contract sold at $1.48/share reports 148). Stored as reported.';

comment on column positions.market_value is
    'Quantity x price, with the option contract multiplier applied where relevant. '
    'Negative for short positions.';
