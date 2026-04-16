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
    "Uses the **same Page 2 buy signals** as email alerts (market cap ≤ 150M). "
    "Requires worker env `AUTO_TRADE_FERNET_KEY` matching this app's secret."
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
               trades_done_today, last_trade_date,
               app_key_cipher, app_secret_cipher
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
        select ticker, qty, entry_price, stop_loss_pct, take_profit_pct, opened_at, trade_date
        from public.user_auto_trade_positions
        where user_id = %s and status = 'open'
        limit 1
        """,
        (user_id,),
    ).fetchone()

if open_pos:
    tkr, qty, entry, sl_pos, tp_pos, opened_at, tdate = open_pos
    stop_px = float(entry) * (1 - float(sl_pos) / 100)
    tp_px = float(entry) * (1 + float(tp_pos) / 100)
    st.info(
        f"📈 **Open position:** {tkr} · {qty} shares · entry ${float(entry):.4f} · "
        f"stop ${stop_px:.4f} · target ${tp_px:.4f} · opened {opened_at.strftime('%Y-%m-%d %H:%M ET') if hasattr(opened_at, 'strftime') else opened_at}"
    )


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
    )

with col_b:
    tp_pct = st.number_input(
        "Take profit % above entry",
        min_value=0.1, max_value=500.0,
        value=float(tp), step=0.5,
    )
    max_trades = st.number_input(
        "Max buy+sell cycles per day",
        min_value=1, max_value=50,
        value=int(maxd),
    )

eod_closeout = st.toggle(
    "Sell at end of day (≈ 3:55 PM ET) if still open",
    value=bool(eod),
)

# Live preview of thresholds
if budget_usd and sl_pct and tp_pct:
    st.caption(
        f"On a ${budget_usd:,.0f} position — stop triggers at **-${budget_usd * sl_pct / 100:,.2f}** "
        f"({sl_pct}%), profit target at **+${budget_usd * tp_pct / 100:,.2f}** ({tp_pct}%)"
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
            (user_id, en, budget_usd, sl_pct, tp_pct, max_trades, eod_closeout, acct_in or ""),
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
    "Decrypts your stored keys and makes a live call to Webull to verify they work. "
    "Save credentials first before testing."
)

col_test, col_spacer = st.columns([1, 3])
with col_test:
    test_btn = st.button("▶ Test credentials now", disabled=not (has_key and has_secret))

if not (has_key and has_secret):
    st.caption("⚠️ No credentials on file yet — save your app key and secret first.")

if test_btn:
    with st.spinner("Connecting to Webull API…"):
        try:
            # Step 1: check SDK installed
            try:
                from worker import webull_trade as wbt  # noqa
            except ImportError:
                try:
                    import webull_trade as wbt  # noqa
                except ImportError:
                    wbt = None

            try:
                app_key_plain = decrypt_str(app_key_cipher_db, fernet_key)
                app_secret_plain = decrypt_str(app_secret_cipher_db, fernet_key)
            except Exception as dec_err:
                st.error(f"❌ Failed to decrypt stored credentials: {repr(dec_err)}")
                app_key_plain = None

            if app_key_plain:
                # Step 3: build client
                client = wbt.make_client(app_key_plain, app_secret_plain)

                # Step 4: call get_app_subscriptions — cheapest authenticated endpoint
                try:
                    resolved_acct = wbt.resolve_account_id(client, stored_acct or "")
                    st.success(f"✅ Connection successful! Account ID: **{resolved_acct}**")

                    # Auto-save resolved account ID if it was blank
                    if not (stored_acct or "").strip() and resolved_acct:
                        with pg_conn() as con:
                            con.execute(
                                "update public.user_auto_trade_settings set webull_account_id=%s, updated_at=now() where user_id=%s",
                                (resolved_acct, user_id),
                            )
                        st.info(f"Account ID **{resolved_acct}** auto-saved to your settings.")

                except RuntimeError as api_err:
                    err_str = str(api_err)
                    st.error(f"❌ Webull API rejected the credentials: {err_str}")

                    # Give specific guidance based on common error patterns
                    if "401" in err_str or "403" in err_str:
                        st.warning(
                            "**HTTP 401/403** — Your app key or secret is invalid or expired. "
                            "Re-generate them at developer.webull.com and re-enter above."
                        )
                    elif "subscriptions empty" in err_str.lower():
                        st.warning(
                            "**No subscriptions found** — Your API app exists but has no brokerage account linked. "
                            "Go to developer.webull.com → your app → link your brokerage account."
                        )
                    elif "404" in err_str:
                        st.warning(
                            "**HTTP 404** — The API endpoint was not found. "
                            "Ensure you are using US region keys and the SDK version matches."
                        )
                    else:
                        st.warning(
                            "Check that your API app is approved, the brokerage account is linked, "
                            "and the keys belong to the US region."
                        )
                except Exception as e:
                    st.error(f"❌ Unexpected error during connection test: {repr(e)}")

        except Exception as outer_err:
            st.error(f"❌ Test failed: {repr(outer_err)}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — OPEN POSITION DETAIL
# ══════════════════════════════════════════════════════════════════════════════
if open_pos:
    st.divider()
    st.subheader("📂 Current open position")
    tkr, qty, entry, sl_pos, tp_pos, opened_at, tdate = open_pos
    entry_f = float(entry)
    stop_px = entry_f * (1 - float(sl_pos) / 100)
    tp_px = entry_f * (1 + float(tp_pos) / 100)

    pc1, pc2, pc3, pc4 = st.columns(4)
    pc1.metric("Ticker", tkr)
    pc2.metric("Qty", qty)
    pc3.metric("Entry price", f"${entry_f:.4f}")
    pc4.metric("Trade date", str(tdate))

    pc5, pc6 = st.columns(2)
    pc5.metric("Stop loss trigger", f"${stop_px:.4f}", f"-{sl_pos}%", delta_color="inverse")
    pc6.metric("Take profit trigger", f"${tp_px:.4f}", f"+{tp_pos}%")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — ORDER HISTORY (THE MISSING PIECE)
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
# SECTION 7 — CLOSED POSITIONS HISTORY
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
# SECTION 8 — RISK & SETUP NOTES
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
