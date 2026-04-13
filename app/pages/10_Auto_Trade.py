import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.auth import require_login, logout_button
from app.db import pg_conn
from common.auto_trade_crypto import encrypt_str, get_fernet_key_from_streamlit_secrets


st.set_page_config(page_title="Auto-trade (Webull)", layout="wide")
require_login()

st.title("🤖 Auto-trade (Webull)")
st.caption(
    "Uses the **same Page 2 buy signals** as email alerts (market cap ≤ 150M). "
    "Requires worker env `AUTO_TRADE_FERNET_KEY` matching this app’s secret."
)
logout_button()

user = st.session_state.auth_user
user_id = user["id"]
email = user.get("email") or ""

fernet_key = get_fernet_key_from_streamlit_secrets()
if not fernet_key:
    st.error(
        "Missing **AUTO_TRADE_FERNET_KEY** in Streamlit secrets. "
        "Generate with: `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`"
    )
    st.stop()

st.write(f"Signed in as: **{email}**")

with pg_conn() as con:
    con.execute(
        """
        create table if not exists public.user_auto_trade_settings (
          user_id uuid primary key references auth.users (id) on delete cascade,
          enabled boolean not null default false,
          budget_usd numeric(18,2) not null default 100,
          stop_loss_pct numeric(10,4) not null default 5,
          take_profit_pct numeric(10,4) not null default 10,
          max_trades_per_day int not null default 1,
          eod_closeout boolean not null default true,
          webull_account_id text,
          app_key_cipher text,
          app_secret_cipher text,
          trades_done_today int not null default 0,
          last_trade_date date,
          updated_at timestamptz not null default now()
        )
        """
    )
    con.execute(
        """
        create table if not exists public.user_auto_trade_positions (
          id bigserial primary key,
          user_id uuid not null references auth.users (id) on delete cascade,
          trade_date date not null,
          ticker text not null,
          instrument_id text not null,
          qty int not null,
          entry_price numeric(18,6) not null,
          stop_loss_pct numeric(10,4) not null,
          take_profit_pct numeric(10,4) not null,
          eod_closeout boolean not null,
          buy_client_order_id text not null,
          sell_client_order_id text,
          status text not null default 'open' check (status in ('open','closed')),
          opened_at timestamptz not null default now(),
          closed_at timestamptz
        )
        """
    )
    con.execute(
        """
        create unique index if not exists user_auto_trade_one_open_per_user
          on public.user_auto_trade_positions (user_id)
          where status = 'open'
        """
    )
    con.execute(
        """
        create table if not exists public.user_auto_trade_orders (
          id bigserial primary key,
          user_id uuid not null references auth.users (id) on delete cascade,
          trade_date date not null,
          ticker text not null,
          side text not null check (side in ('BUY','SELL')),
          qty int not null,
          client_order_id text not null,
          instrument_id text,
          signal_ts_et timestamptz,
          exit_reason text,
          http_status int,
          response_body text,
          created_at timestamptz not null default now()
        )
        """
    )
    con.execute(
        "insert into public.user_auto_trade_settings (user_id) values (%s) on conflict do nothing",
        (user_id,),
    )
    row = con.execute(
        """
        select enabled, budget_usd, stop_loss_pct, take_profit_pct, max_trades_per_day, eod_closeout,
               webull_account_id,
               (app_key_cipher is not null and length(trim(app_key_cipher)) > 0) as has_key,
               (app_secret_cipher is not null and length(trim(app_secret_cipher)) > 0) as has_secret,
               trades_done_today, last_trade_date
        from public.user_auto_trade_settings
        where user_id = %s
        """,
        (user_id,),
    ).fetchone()

enabled, budget, sl, tp, maxd, eod, stored_acct, has_key, has_secret, tdone, ltd = row

st.subheader("Trading limits")
en = st.toggle("Enable auto-trade", value=bool(enabled))
budget_usd = st.number_input("Max dollars per buy (approx. position size)", min_value=10.0, max_value=1_000_000.0, value=float(budget), step=50.0)
sl_pct = st.number_input("Stop loss % (below entry)", min_value=0.1, max_value=90.0, value=float(sl), step=0.5)
tp_pct = st.number_input("Take profit % (above entry)", min_value=0.1, max_value=500.0, value=float(tp), step=0.5)
max_trades = st.number_input("Max completed buy+sell cycles per day", min_value=1, max_value=50, value=int(maxd))
eod_closeout = st.toggle("Sell at end of day (≈ 3:55 PM ET) if still open", value=bool(eod))

st.subheader("Webull API credentials")
st.caption("Stored **encrypted** in Postgres. Leave blank to keep existing values.")
acct_in = st.text_input(
    "Webull account ID (optional — leave empty to auto-detect)",
    value=stored_acct or "",
    placeholder="from API subscriptions",
)
app_key_in = st.text_input("App key", type="password", placeholder="••••" if has_key else "")
app_secret_in = st.text_input("App secret", type="password", placeholder="••••" if has_secret else "")

if has_key:
    st.info("App key on file (encrypted). Enter a new value only to replace it.")
if has_secret:
    st.info("App secret on file (encrypted). Enter a new value only to replace it.")

if st.button("Save settings"):
    key_cipher = None
    sec_cipher = None
    if app_key_in.strip():
        key_cipher = encrypt_str(app_key_in.strip(), fernet_key)
    if app_secret_in.strip():
        sec_cipher = encrypt_str(app_secret_in.strip(), fernet_key)

    with pg_conn() as con:
        con.execute(
            """
            insert into public.user_auto_trade_settings(
              user_id, enabled, budget_usd, stop_loss_pct, take_profit_pct,
              max_trades_per_day, eod_closeout, webull_account_id, updated_at
            ) values (%s,%s,%s,%s,%s,%s,%s,nullif(trim(%s),''), now())
            on conflict (user_id) do update set
              enabled = excluded.enabled,
              budget_usd = excluded.budget_usd,
              stop_loss_pct = excluded.stop_loss_pct,
              take_profit_pct = excluded.take_profit_pct,
              max_trades_per_day = excluded.max_trades_per_day,
              eod_closeout = excluded.eod_closeout,
              webull_account_id = excluded.webull_account_id,
              updated_at = now()
            """,
            (user_id, en, budget_usd, sl_pct, tp_pct, max_trades, eod, acct_in or ""),
        )
        if key_cipher:
            con.execute(
                "update public.user_auto_trade_settings set app_key_cipher=%s, updated_at=now() where user_id=%s",
                (key_cipher, user_id),
            )
        if sec_cipher:
            con.execute(
                "update public.user_auto_trade_settings set app_secret_cipher=%s, updated_at=now() where user_id=%s",
                (sec_cipher, user_id),
            )
    st.success("Saved.")
    st.rerun()

st.divider()
st.subheader("Today (worker)")
st.write(f"Trades used today (worker-maintained): **{tdone}** / {maxd}  ·  last trade date: **{ltd}**")

st.warning(
    "Risk: live market orders. Test in Webull **paper / API sandbox** first. "
    "Install worker deps: `webull-python-sdk-core`, `webull-python-sdk-trade`, `webull-python-sdk-mdata`, `cryptography`."
)
