-- ====== Core meta ======
create table if not exists meta (
  k text primary key,
  v text
);

-- ====== Scanner (Page 1) ======
create table if not exists scanner_tickers (
  trade_date date not null,
  ticker text not null,
  baseline_8am double precision,
  start_price double precision,
  current_price double precision,
  cum_vol double precision,
  max_gain_window double precision,
  hit_gain boolean default false,
  qualified_locked boolean default false,
  bursts_5m integer default 0,
  bursts_10m integer default 0,
  bursts_total integer default 0,
  last_5m_counted_t bigint,
  last_5m_vol double precision default 0,
  last_10m_vol double precision default 0,
  updated_at timestamptz default now(),
  primary key (trade_date, ticker)
);

create table if not exists scanner_series (
  trade_date date not null,
  ticker text not null,
  ts_et timestamptz not null,
  gain_pct double precision,
  cum_vol double precision,
  last_5m_vol double precision,
  last_10m_vol double precision,
  bursts_total integer,
  primary key (trade_date, ticker, ts_et)
);

create table if not exists scanner_events (
  id bigserial primary key,
  trade_date date not null,
  ts_et timestamptz not null,
  ticker text not null,
  event_type text not null,
  details text
);

-- ====== User watchlists (per-user) ======
create table if not exists user_watchlist (
  user_id uuid not null,
  trade_date date not null,
  ticker text not null,
  source text default 'manual',
  added_at timestamptz default now(),
  primary key (user_id, trade_date, ticker)
);

-- ====== Buy signals (computed once; user filters via watchlist) ======
create table if not exists buy_signals (
  trade_date date not null,
  ts_et timestamptz not null,
  ticker text not null,
  price double precision,
  rsi double precision,
  boll_lower double precision,
  pattern text,
  details text,
  primary key (trade_date, ts_et, ticker)
);

-- ====== RTH alerts (Page 3) ======
create table if not exists rth_alerts (
  trade_date date not null,
  ts_et timestamptz not null,
  ticker text not null,
  reason text,
  delta_shares bigint,
  window_min integer,
  price double precision,
  primary key (trade_date, ts_et, ticker)
);

-- Helpful indexes
create index if not exists idx_events_trade_ts on scanner_events (trade_date, ts_et desc);
create index if not exists idx_series_trade_ticker_ts on scanner_series (trade_date, ticker, ts_et);
create index if not exists idx_buy_trade_ticker_ts on buy_signals (trade_date, ticker, ts_et desc);
create index if not exists idx_alerts_trade_ts on rth_alerts (trade_date, ts_et desc);
create index if not exists idx_watch_trade_user on user_watchlist (trade_date, user_id);
