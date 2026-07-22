-- Sprint 3 — record how each iv_history reading was derived.
--
-- `atm_iv` is not a vendor quote. ThetaData gates implied volatility behind its
-- STANDARD plan, so the bot solves it from free EOD quotes with Black-Scholes
-- (src/engine/black_scholes.py). That makes the inputs part of the datum: a
-- number with no provenance cannot be audited, and IV rank is a percentile over
-- these readings, so one bad series silently corrupts every later signal.
--
-- The load-bearing columns are call_iv and put_iv. They are solved independently
-- from different contracts, so their agreement continuously validates the model,
-- the risk-free rate and the time convention (observed within ~1 point on
-- 2026-07-21: RIOT 104.9/103.9, AAPL 29.4/30.1). A widening gap is the earliest
-- signal that an assumption drifted — but only if both legs are stored, and this
-- is not reconstructable after the free data window rolls past.
--
-- All columns are nullable: existing rows predate them, and one unusable leg
-- (a crossed or stale quote) still yields a valid reading from the other.
-- Migrations are immutable: never edit once applied; add 0006_*.sql etc.

alter table iv_history add column if not exists call_iv     numeric;
alter table iv_history add column if not exists put_iv      numeric;
alter table iv_history add column if not exists expiration  date;
alter table iv_history add column if not exists strike      numeric;
alter table iv_history add column if not exists spot        numeric;
alter table iv_history add column if not exists rate        numeric;
alter table iv_history add column if not exists method      text;

-- Finding a series computed under a superseded method is the query that matters
-- when a convention changes and the history has to be rebuilt.
create index if not exists iv_history_method_idx on iv_history (method);

comment on column iv_history.atm_iv is
    'At-the-money implied volatility as a decimal (0.2976 = 29.76%). Derived, '
    'not quoted: the mean of call_iv and put_iv where both are usable.';
comment on column iv_history.call_iv is
    'IV solved from the ATM call''s closing quote. Compare against put_iv — a '
    'widening gap means a model assumption (dividends, rate, exercise style) '
    'has drifted.';
comment on column iv_history.put_iv is
    'IV solved from the ATM put''s closing quote. See call_iv.';
comment on column iv_history.expiration is
    'Expiry the reading was taken from — nearest DEFAULT_TARGET_DTE (~30d). '
    'Query this to confirm selection is not oscillating between cycles, which '
    'would make readings incomparable and IV rank meaningless.';
comment on column iv_history.strike is
    'ATM strike used (nearest listed strike to spot).';
comment on column iv_history.spot is
    'Underlying close used as the Black-Scholes spot input.';
comment on column iv_history.rate is
    'Risk-free rate used, as a decimal (SOFR/100).';
comment on column iv_history.method is
    'Identifier for the derivation convention, e.g. "bs-mid-30dte-v1". Bump it '
    'whenever the method changes, then REBUILD the affected series — never '
    'patch forward, because readings from different methods are not comparable.';
