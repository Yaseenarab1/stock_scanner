import sys
from pathlib import Path
from datetime import datetime

import streamlit as st

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.auth import require_login, logout_button
from app.db import pg_conn
from app.auto_trade_crypto import encrypt_str, decrypt_str, get_fernet_key_from_streamlit_secrets


st.set_page_config(page_title="Auto-trade (Webull)", layout="wide")
require_login()

st.title("🤖 Auto-trade (Webull)")
st.caption(
    "Trades fire only when the **ML model** scores an episode-start signal above its "
    "threshold. Requires worker env `AUTO_TRADE_FERNET_KEY` matching this app's secret."
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

# ── Ensure tables exist and load user row ────────────────────────────────────
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
          sl_enabled boolean not null default true,
          tp_enabled boolean not null default true,
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
          sl_enabled boolean not null default true,
          tp_enabled boolean not null default true,
          buy_client_order_id text not null,
          sell_client_order_id text,
          status text not null default 'open' check (status in ('open','closed')),
          opened_at timestamptz not null default now(),
          closed_at timestamptz
        )
        """
    )
    # Safe upgrade for existing deployments: add columns if missing
    for ddl in [
        "alter table public.user_auto_trade_settings  add column if not exists sl_enabled boolean not null default true",
        "alter table public.user_auto_trade_settings  add column if not exists tp_enabled boolean not null default true",
        "alter table public.user_auto_trade_positions add column if not exists sl_enabled boolean not null default true",
        "alter table public.user_auto_trade_positions add column if not exists tp_enabled boolean not null default true",
    ]:
        try:
            con.execute(ddl)
        except Exception:
            pass
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
               trades_done_today, last_trade_date,
               app_key_cipher, app_secret_cipher,
               coalesce(sl_enabled, true) as sl_enabled,
               coalesce(tp_enabled, true) as tp_enabled
        from public.user_auto_trade_settings
        where user_id = %s
        """,
        (user_id,),
    ).fetchone()

(
    enabled, budget, sl, tp, maxd, eod,
    stored_acct, has_key, has_secret,
    tdone, ltd,
    app_key_cipher_db, app_secret_cipher_db,
    sl_enabled_db, tp_enabled_db,
) = row


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — STATUS DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("📊 Status")

col1, col2, col3, col4 = st.columns(4)

with col1:
    status_color = "🟢" if enabled else "🔴"
    st.metric("Auto-trade", f"{status_color} {'ON' if enabled else 'OFF'}")

with col2:
    st.metric("Trades today", f"{tdone} / {maxd}")

with col3:
    last_date_str = str(ltd) if ltd else "—"
    st.metric("Last trade date", last_date_str)

with col4:
    creds_ok = has_key and has_secret
    st.metric("Credentials", "✅ On file" if creds_ok else "⚠️ Missing")

# Open position banner
with pg_conn() as con:
    open_pos = con.execute(
        """
        select id, ticker, qty, entry_price, stop_loss_pct, take_profit_pct,
               coalesce(sl_enabled, true), coalesce(tp_enabled, true),
               eod_closeout, opened_at, trade_date
        from public.user_auto_trade_positions
        where user_id = %s and status = 'open'
        limit 1
        """,
        (user_id,),
    ).fetchone()

if open_pos:
    (pos_id, tkr, qty, entry, sl_pos, tp_pos,
     sl_on_pos, tp_on_pos, eod_pos, opened_at, tdate) = open_pos
    stop_px = float(entry) * (1 - float(sl_pos) / 100)
    tp_px = float(entry) * (1 + float(tp_pos) / 100)
    sl_desc = f"stop ${stop_px:.4f} ({float(sl_pos):.2f}%)" if sl_on_pos else "stop **OFF**"
    tp_desc = f"target ${tp_px:.4f} ({float(tp_pos):.2f}%)" if tp_on_pos else "target **OFF**"
    st.info(
        f"📈 **Open position:** {tkr} · {qty} shares · entry ${float(entry):.4f} · "
        f"{sl_desc} · {tp_desc} · "
        f"opened {opened_at.strftime('%Y-%m-%d %H:%M ET') if hasattr(opened_at, 'strftime') else opened_at}"
    )

    # ───── Live controls for the currently open position ─────
    st.subheader("🎛 Live position controls")
    st.caption(
        "Changes here apply to the **currently open position only**. "
        "To change defaults for *future* trades, edit Trading Limits below."
    )

    lcol1, lcol2, lcol3 = st.columns(3)
    with lcol1:
        live_sl_on = st.toggle(
            "Stop loss",
            value=bool(sl_on_pos),
            key=f"live_sl_on_{pos_id}",
            help="Uncheck to disable the stop loss for this position only.",
        )
        live_sl_pct = st.number_input(
            "Stop loss %",
            min_value=0.1, max_value=90.0,
            value=float(sl_pos), step=0.25,
            key=f"live_sl_pct_{pos_id}",
            disabled=not live_sl_on,
        )
    with lcol2:
        live_tp_on = st.toggle(
            "Take profit",
            value=bool(tp_on_pos),
            key=f"live_tp_on_{pos_id}",
            help="Uncheck to disable the take profit for this position only.",
        )
        live_tp_pct = st.number_input(
            "Take profit %",
            min_value=0.1, max_value=500.0,
            value=float(tp_pos), step=0.25,
            key=f"live_tp_pct_{pos_id}",
            disabled=not live_tp_on,
        )
    with lcol3:
        live_eod = st.toggle(
            "EOD close-out (≈3:55 PM ET)",
            value=bool(eod_pos),
            key=f"live_eod_{pos_id}",
        )
        st.caption(" ")  # vertical align with sibling column
        apply_live = st.button("💾 Apply to this position", type="primary",
                               key=f"apply_live_{pos_id}")

    # Preview of live exit prices
    live_stop_px = float(entry) * (1 - float(live_sl_pct) / 100)
    live_tp_px = float(entry) * (1 + float(live_tp_pct) / 100)
    _sl_preview = f"stop ${live_stop_px:.4f}" if live_sl_on else "stop OFF"
    _tp_preview = f"target ${live_tp_px:.4f}" if live_tp_on else "target OFF"
    st.caption(f"Preview after apply: {_sl_preview} · {_tp_preview} · "
               f"EOD {'ON' if live_eod else 'OFF'}")

    if apply_live:
        with pg_conn() as con:
            con.execute(
                """
                update public.user_auto_trade_positions
                set stop_loss_pct   = %s,
                    take_profit_pct = %s,
                    sl_enabled      = %s,
                    tp_enabled      = %s,
                    eod_closeout    = %s
                where id = %s and user_id = %s and status = 'open'
                """,
                (float(live_sl_pct), float(live_tp_pct),
                 bool(live_sl_on), bool(live_tp_on),
                 bool(live_eod), pos_id, user_id),
            )
        st.success("✅ Applied to open position. The worker will pick this up on its next tick.")
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SETTINGS
# ══════════════════════════════════════════════════════════════════════════════
st.divider()
st.subheader("⚙️ Trading limits")

en = st.toggle("Enable auto-trade", value=bool(enabled))

col_a, col_b = st.columns(2)
with col_a:
    budget_usd = st.number_input(
        "Max dollars per buy (position size)",
        min_value=10.0, max_value=1_000_000.0,
        value=float(budget), step=50.0,
    )
    sl_pct = st.number_input(
        "Stop loss % below entry",
        min_value=0.1, max_value=90.0,
        value=float(sl), step=0.5,
        disabled=not bool(sl_enabled_db),
    )
    sl_on = st.toggle(
        "Use stop loss by default",
        value=bool(sl_enabled_db),
        help="When off, new trades are opened with NO stop loss. "
             "You can still set one later via Live position controls.",
    )

with col_b:
    tp_pct = st.number_input(
        "Take profit % above entry",
        min_value=0.1, max_value=500.0,
        value=float(tp), step=0.5,
        disabled=not bool(tp_enabled_db),
    )
    max_trades = st.number_input(
        "Max buy+sell cycles per day",
        min_value=1, max_value=50,
        value=int(maxd),
    )
    tp_on = st.toggle(
        "Use take profit by default",
        value=bool(tp_enabled_db),
        help="When off, new trades are opened with NO take profit.",
    )

eod_closeout = st.toggle(
    "Sell at end of day (≈ 3:55 PM ET) if still open",
    value=bool(eod),
)

# Live preview of thresholds
if budget_usd and sl_pct and tp_pct:
    sl_preview = (f"stop at **-${budget_usd * sl_pct / 100:,.2f}** ({sl_pct}%)"
                  if sl_on else "stop **OFF**")
    tp_preview = (f"target at **+${budget_usd * tp_pct / 100:,.2f}** ({tp_pct}%)"
                  if tp_on else "target **OFF**")
    st.caption(
        f"On a ${budget_usd:,.0f} position — {sl_preview}, {tp_preview}. "
        f"These defaults apply to *future* trades only."
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — WEBULL CREDENTIALS
# ══════════════════════════════════════════════════════════════════════════════
st.divider()
st.subheader("🔐 Webull API credentials")
st.caption("Stored **encrypted** (Fernet) in Postgres. Leave blank to keep existing values.")

acct_in = st.text_input(
    "Webull account ID (optional — leave empty to auto-detect)",
    value=stored_acct or "",
    placeholder="from API subscriptions page",
)

col_k, col_s = st.columns(2)
with col_k:
    app_key_in = st.text_input(
        "App key",
        type="password",
        placeholder="••••  (on file)" if has_key else "Paste app key here",
    )
    if has_key:
        st.caption("✅ App key on file. Enter a new value only to replace it.")

with col_s:
    app_secret_in = st.text_input(
        "App secret",
        type="password",
        placeholder="••••  (on file)" if has_secret else "Paste app secret here",
    )
    if has_secret:
        st.caption("✅ App secret on file. Enter a new value only to replace it.")

# ── Save button ──────────────────────────────────────────────────────────────
if st.button("💾 Save settings", type="primary"):
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
              max_trades_per_day, eod_closeout, sl_enabled, tp_enabled,
              webull_account_id, updated_at
            ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,nullif(trim(%s),''), now())
            on conflict (user_id) do update set
              enabled = excluded.enabled,
              budget_usd = excluded.budget_usd,
              stop_loss_pct = excluded.stop_loss_pct,
              take_profit_pct = excluded.take_profit_pct,
              max_trades_per_day = excluded.max_trades_per_day,
              eod_closeout = excluded.eod_closeout,
              sl_enabled = excluded.sl_enabled,
              tp_enabled = excluded.tp_enabled,
              webull_account_id = excluded.webull_account_id,
              updated_at = now()
            """,
            (user_id, en, budget_usd, sl_pct, tp_pct, max_trades, eod_closeout,
             bool(sl_on), bool(tp_on), acct_in or ""),
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
    st.success("Settings saved.")
    st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — TEST CREDENTIALS (THE MISSING PIECE)
# ══════════════════════════════════════════════════════════════════════════════
st.divider()
st.subheader("🧪 Test Webull connection")
st.caption(
    "Checks your credential health based on worker activity logs — "
    "no SDK required on the Streamlit server."
)

col_test, col_spacer = st.columns([1, 3])
with col_test:
    test_btn = st.button("▶ Check credential health", disabled=not (has_key and has_secret))

if not (has_key and has_secret):
    st.caption("⚠️ No credentials on file yet — save your app key and secret first.")

if test_btn:
    with pg_conn() as con:
        # Most recent order attempt
        last_order = con.execute(
            """
            select http_status, response_body, side, ticker, created_at
            from public.user_auto_trade_orders
            where user_id = %s
            order by created_at desc
            limit 1
            """,
            (user_id,),
        ).fetchone()

        # Last successful order
        last_ok = con.execute(
            """
            select side, ticker, created_at
            from public.user_auto_trade_orders
            where user_id = %s and http_status = 200
            order by created_at desc
            limit 1
            """,
            (user_id,),
        ).fetchone()

        # Count of failed orders in last 24h
        recent_failures = con.execute(
            """
            select count(*), max(response_body)
            from public.user_auto_trade_orders
            where user_id = %s
              and http_status != 200
              and created_at > now() - interval '24 hours'
            """,
            (user_id,),
        ).fetchone()

    if last_order is None:
        # No orders ever — check if auto-trade is even enabled and keys exist
        st.info(
            "ℹ️ No orders have been attempted yet — the worker hasn't fired a trade for you. "
            "This is normal if no Page 2 buy signals have triggered since you enabled auto-trade. "
            "It does **not** mean your credentials are wrong."
        )
        st.markdown("""
**To verify your setup is ready:**
- ✅ Auto-trade is **enabled** above
- ✅ App key and secret are **on file**
- ✅ Worker is running (check your worker logs / deployment)
- ✅ Page 2 email alerts are **enabled** for your account (auto-trade only fires when you'd also get an email)
- ⏳ Wait for the next Page 2 buy signal — the first order attempt will appear in Order History below
""")
    else:
        http, body, side, ticker, created_at = last_order
        ts_str = created_at.strftime("%Y-%m-%d %H:%M ET") if hasattr(created_at, "strftime") else str(created_at)

        if http == 200:
            st.success(
                f"✅ Credentials are working. Last order: **{side} {ticker}** "
                f"at {ts_str} — HTTP 200."
            )
        else:
            st.error(f"❌ Last order attempt failed — HTTP {http} at {ts_str}")
            rb_low = (body or "").lower()
            if "401" in (body or "") or "unauthorized" in rb_low:
                st.warning("🔑 **Invalid credentials** — your app key or secret was rejected by Webull. Re-enter them above.")
            elif "403" in (body or "") or "forbidden" in rb_low:
                st.warning("🔒 **Forbidden** — your API app may not have trading permissions enabled at developer.webull.com.")
            elif "invalid" in rb_low and ("key" in rb_low or "secret" in rb_low):
                st.warning("🔑 **Invalid key/secret** — re-enter your credentials above and save.")
            elif "account" in rb_low and ("not found" in rb_low or "invalid" in rb_low):
                st.warning("🏦 **Bad account ID** — clear the account ID field above and let the worker auto-detect it.")
            elif "insufficient" in rb_low or "buying power" in rb_low:
                st.warning("💰 **Insufficient funds** — not enough buying power in your Webull account.")
            else:
                st.warning(f"Response snippet: `{(body or '')[:300]}`")

        fail_count, last_fail_body = recent_failures
        if last_ok:
            ok_side, ok_ticker, ok_ts = last_ok
            ok_ts_str = ok_ts.strftime("%Y-%m-%d %H:%M ET") if hasattr(ok_ts, "strftime") else str(ok_ts)
            st.info(f"Last **successful** order: {ok_side} {ok_ticker} at {ok_ts_str}")
        if fail_count and fail_count > 0:
            st.warning(f"{fail_count} failed order(s) in the last 24 hours.")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — OPEN POSITION DETAIL + MANUAL RECONCILIATION
# ══════════════════════════════════════════════════════════════════════════════
if open_pos:
    st.divider()
    st.subheader("📂 Current open position")
    # NOTE: open_pos was already unpacked higher up the page (live-controls
    # section). Re-use those locals instead of unpacking again.
    entry_f = float(entry)
    stop_px = entry_f * (1 - float(sl_pos) / 100)
    tp_px = entry_f * (1 + float(tp_pos) / 100)

    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric("Ticker", tkr)
    pc2.metric("Qty", qty)
    pc3.metric("Entry price", f"${entry_f:.4f}")
    pc4.metric("Trade date", str(tdate))

    pc5, pc6 = st.columns(2)
    if sl_on_pos:
        pc5.metric("Stop loss trigger", f"${stop_px:.4f}",
                   f"-{sl_pos}%", delta_color="inverse")
    else:
        pc5.metric("Stop loss", "OFF")
    if tp_on_pos:
        pc6.metric("Take profit trigger", f"${tp_px:.4f}", f"+{tp_pos}%")
    else:
        pc6.metric("Take profit", "OFF")

    # ────────────────────────────────────────────────────────────────────
    # Manual reconciliation: I closed this position on Webull (or it
    # otherwise filled outside the worker) and the row is stuck open.
    # ────────────────────────────────────────────────────────────────────
    with st.expander("🛠 Position out of sync? Fix it manually"):
        st.markdown(
            "Use this **only** if you closed the position outside this app "
            "(e.g. directly in the Webull app), and the row above is still "
            "showing as open. This marks the row closed in our database — "
            "it does **not** place any order on Webull."
        )
        confirm = st.checkbox(
            "I confirm this position is already closed on Webull",
            key=f"manual_close_confirm_{pos_id}",
        )
        if st.button(
            "Mark as closed (manual reconciliation)",
            disabled=not confirm,
            key=f"manual_close_btn_{pos_id}",
        ):
            with pg_conn() as con:
                con.execute(
                    """
                    update public.user_auto_trade_positions
                    set status = 'closed',
                        closed_at = now(),
                        sell_client_order_id =
                            coalesce(sell_client_order_id, 'manual_external')
                    where id = %s and user_id = %s and status = 'open'
                    """,
                    (pos_id, user_id),
                )
                # Audit trail in user_auto_trade_orders if that table exists
                try:
                    con.execute(
                        """
                        insert into public.user_auto_trade_orders(
                            user_id, trade_date, ticker, side, qty,
                            client_order_id, instrument_id, signal_ts_et,
                            exit_reason, http_status, response_body
                        ) values (%s,%s,%s,'SELL',%s,
                                  'manual_external', null, null,
                                  'manual_external_close (user marked closed in UI)',
                                  null, 'reconciled by user via UI')
                        """,
                        (user_id, tdate, tkr, qty),
                    )
                except Exception:
                    # orders table might not exist or schema may differ;
                    # the position update above is the source of truth
                    pass
            st.success(f"✅ {tkr} marked as closed. Refreshing…")
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — TODAY'S STOP-LOSS BLACKLIST
# ══════════════════════════════════════════════════════════════════════════════
st.divider()
st.subheader("🚫 Today's stop-loss blacklist")
st.caption(
    "Tickers that hit their stop-loss today. The worker will not re-buy these "
    "for the rest of the trading day. Clears automatically tomorrow."
)

bl_rows = []
with pg_conn() as con:
    # Ensure table exists (first load before worker has created it)
    try:
        con.execute("""
            create table if not exists public.user_auto_trade_blacklist (
                id           bigserial primary key,
                user_id      uuid      not null,
                ticker       text      not null,
                reason       text,
                blacklisted_at    timestamptz not null default now(),
                blacklisted_until date        not null,
                constraint uatb_uniq unique (user_id, ticker, blacklisted_until)
            )
        """)
    except Exception:
        pass

    try:
        bl_rows = con.execute(
            """
            select ticker, reason, blacklisted_at
            from public.user_auto_trade_blacklist
            where user_id = %s and blacklisted_until >= current_date
            order by blacklisted_at desc
            """,
            (user_id,),
        ).fetchall()
    except Exception:
        bl_rows = []

if not bl_rows:
    st.info("No tickers blacklisted today — all clear.")
else:
    bl_df = pd.DataFrame(bl_rows, columns=["Ticker", "Reason", "Blacklisted at"])
    st.dataframe(bl_df, use_container_width=True, hide_index=True)

    with st.expander("Remove a ticker from the blacklist (allow re-buy today)"):
        bl_tickers = [r[0] for r in bl_rows]
        remove_ticker = st.selectbox("Select ticker to unblock", options=bl_tickers,
                                     key="bl_remove_select")
        if st.button("✅ Unblock this ticker", key="bl_remove_btn"):
            with pg_conn() as con:
                con.execute(
                    """
                    delete from public.user_auto_trade_blacklist
                    where user_id = %s and ticker = %s and blacklisted_until >= current_date
                    """,
                    (user_id, remove_ticker),
                )
            st.success(f"✅ {remove_ticker} removed from blacklist. "
                       f"The worker will consider buying it again on the next signal.")
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — ORDER HISTORY (THE MISSING PIECE)
# ══════════════════════════════════════════════════════════════════════════════
st.divider()
st.subheader("📋 Order history")
st.caption("Last 30 orders sent to Webull by the worker. HTTP 200 = accepted by exchange.")

with pg_conn() as con:
    orders = con.execute(
        """
        select trade_date, created_at, ticker, side, qty,
               http_status, exit_reason, response_body, client_order_id
        from public.user_auto_trade_orders
        where user_id = %s
        order by created_at desc
        limit 30
        """,
        (user_id,),
    ).fetchall()

if not orders:
    st.info("No orders recorded yet. Orders appear here once the worker places a trade.")
else:
    for o in orders:
        trade_date, created_at, ticker, side, qty, http_status, exit_reason, response_body, coid = o

        ts_str = created_at.strftime("%Y-%m-%d %H:%M ET") if hasattr(created_at, "strftime") else str(created_at)
        status_ok = http_status == 200
        icon = "✅" if status_ok else "❌"
        side_icon = "🟢 BUY" if side == "BUY" else "🔴 SELL"

        label = f"{icon} {ts_str} · {ticker} · {side_icon} {qty} shares · HTTP {http_status}"
        if exit_reason:
            label += f" · {exit_reason}"

        with st.expander(label, expanded=(not status_ok)):
            col_l, col_r = st.columns(2)
            col_l.write(f"**Client order ID:** `{coid}`")
            col_l.write(f"**Trade date:** {trade_date}")
            col_r.write(f"**HTTP status:** `{http_status}`")
            if exit_reason:
                col_r.write(f"**Exit reason:** {exit_reason}")

            if response_body:
                if not status_ok:
                    st.error(f"**Webull response (error):**\n```\n{response_body[:1000]}\n```")

                    # Diagnose common errors inline
                    rb_low = response_body.lower()
                    if "invalid" in rb_low and ("key" in rb_low or "secret" in rb_low or "token" in rb_low):
                        st.warning("🔑 This looks like an **invalid API key/secret**. Go to Credentials above and re-enter your keys, then test the connection.")
                    elif "401" in response_body or "unauthorized" in rb_low:
                        st.warning("🔑 **Unauthorized** — API credentials were rejected. Re-enter and test your credentials above.")
                    elif "403" in response_body or "forbidden" in rb_low:
                        st.warning("🔒 **Forbidden** — Your API app may not have trading permissions enabled.")
                    elif "account" in rb_low and ("not found" in rb_low or "invalid" in rb_low):
                        st.warning("🏦 **Account ID issue** — The account ID stored may be wrong. Clear it in Credentials and let the worker auto-detect it.")
                    elif "insufficient" in rb_low or "buying power" in rb_low:
                        st.warning("💰 **Insufficient funds** — Your Webull account doesn't have enough buying power for this position size.")
                    elif "market" in rb_low and "closed" in rb_low:
                        st.warning("🕐 **Market closed** — The order was sent outside of market hours.")
                    else:
                        st.write(f"**Full response:** `{response_body[:500]}`")
                else:
                    st.write(f"**Webull response:** `{response_body[:300]}`")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — CLOSED POSITIONS HISTORY
# ══════════════════════════════════════════════════════════════════════════════
st.divider()
st.subheader("📈 Closed positions")
st.caption("Completed buy+sell cycles. P&L is estimated from entry price vs. yfinance exit price used by the worker.")

with pg_conn() as con:
    closed = con.execute(
        """
        select p.trade_date, p.ticker, p.qty, p.entry_price,
               p.stop_loss_pct, p.take_profit_pct, p.opened_at, p.closed_at,
               sell_ord.exit_reason, sell_ord.http_status as sell_http
        from public.user_auto_trade_positions p
        left join lateral (
          select exit_reason, http_status
          from public.user_auto_trade_orders o
          where o.user_id = p.user_id
            and o.ticker = p.ticker
            and o.side = 'SELL'
            and o.trade_date = p.trade_date
          order by o.created_at desc
          limit 1
        ) sell_ord on true
        where p.user_id = %s and p.status = 'closed'
        order by p.closed_at desc
        limit 20
        """,
        (user_id,),
    ).fetchall()

if not closed:
    st.info("No closed positions yet.")
else:
    for c in closed:
        tdate, ticker, qty, entry, sl_c, tp_c, opened_at, closed_at, exit_reason, sell_http = c
        entry_f = float(entry)
        closed_str = closed_at.strftime("%Y-%m-%d %H:%M ET") if hasattr(closed_at, "strftime") else str(closed_at)
        sell_ok = sell_http == 200 if sell_http else None
        sell_icon = "✅" if sell_ok else ("❌" if sell_ok is False else "?")
        reason_short = (exit_reason or "unknown")[:60]
        with st.expander(f"{sell_icon} {tdate} · {ticker} · {qty} shares · closed {closed_str} · {reason_short}"):
            col1, col2, col3 = st.columns(3)
            col1.write(f"**Entry:** ${entry_f:.4f}")
            col2.write(f"**Stop was:** {sl_c}%")
            col3.write(f"**Target was:** {tp_c}%")
            if exit_reason:
                st.write(f"**Exit reason:** {exit_reason}")
            if sell_http and sell_http != 200:
                st.error(f"SELL order returned HTTP {sell_http} — check Order History above for details.")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — RISK & SETUP NOTES
# ══════════════════════════════════════════════════════════════════════════════
st.divider()

with st.expander("⚠️ Risk warnings & setup checklist", expanded=False):
    st.warning(
        "**Live market orders** are placed automatically without per-trade confirmation. "
        "Test in Webull **paper trading / API sandbox** before enabling with real funds."
    )
    st.markdown("""
**Worker dependencies** (must be installed in the worker environment):
```
pip install webull-python-sdk-core webull-python-sdk-trade webull-python-sdk-mdata cryptography
```

**Setup checklist:**
- [ ] Create an app at [developer.webull.com](https://developer.webull.com)
- [ ] Link your brokerage account to the app (API subscriptions page)
- [ ] Copy the **App Key** and **App Secret** into Credentials above
- [ ] Click **Test credentials now** — confirm you get a green success with your account ID
- [ ] Set your position size, stop loss, and take profit above
- [ ] Enable auto-trade with the toggle
- [ ] Ensure the worker env var `AUTO_TRADE_FERNET_KEY` matches this app's secret

**Known limitations:**
- Exit monitoring uses **yfinance** delayed prices, not real-time quotes — stop/TP triggers may lag up to ~1–2 minutes
- EOD closeout targets 3:55 PM ET but fires on the next worker tick (every ~30s), so actual close time may vary slightly
- No partial fills — all orders are for the full calculated qty
- One open position per user at a time (by design)
- `AUTO_TRADE_FERNET_KEY` must be **identical** between the Streamlit app secrets and the worker env var; mismatches silently break decryption
""")
