# pages/2_Buy_Watcher.py
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st
from auth import require_login, get_user_email

require_login()
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

st.set_page_config(layout="wide", page_title="Page 2 — Buy Watcher (Per-user)")
st.title("🎟️ Page 2 — Buy Watcher (Per-user watchlist)")

conn = st.connection("supabase", type="sql")

now = datetime.now(ET)
trade_date = now.date().isoformat()
email = get_user_email()

# Ensure profile exists (safe)
if email:
    conn.query(
        "insert into public.profiles(email) values (:e) on conflict (email) do nothing",
        params={"e": email},
        ttl="0s"
    )

with st.sidebar:
    refresh_s = st.slider("Refresh (seconds)", 3, 60, 10)

    st.subheader("Add ticker to *my* watchlist")
    new_ticker = st.text_input("Ticker", value="", placeholder="e.g. NVDA").strip().upper()
    if st.button("➕ Add"):
        if new_ticker and email:
            conn.query(
                """
                insert into public.user_watchlist(trade_date, email, ticker, source)
                values (:d, :e, :t, 'manual')
                on conflict (trade_date, email, ticker) do nothing
                """,
                params={"d": trade_date, "e": email, "t": new_ticker},
                ttl="0s"
            )
            st.success(f"Added {new_ticker}")
            st.rerun()

st.subheader("📌 My Watchlist (today)")
watch = conn.query(
    """
    select ticker, source, added_at
    from public.user_watchlist
    where trade_date = :d and email = :e
    order by added_at desc
    """,
    params={"d": trade_date, "e": email},
    ttl="5s"
)

if watch.empty:
    st.warning("Your watchlist is empty. Add tickers from the sidebar.")
else:
    st.dataframe(watch, use_container_width=True, hide_index=True)
    with st.expander("🧹 Remove tickers (today)"):
        options = watch["ticker"].tolist()
        rm = st.multiselect("Select tickers to remove", options=options)
        if st.button("Remove selected"):
            for t in rm:
                conn.query(
                    """
                    delete from public.user_watchlist
                    where trade_date = :d and email = :e and ticker = :t
                    """,
                    params={"d": trade_date, "e": email, "t": t},
                    ttl="0s"
                )
            st.rerun()

st.divider()
st.subheader("📈 Latest computed indicators (for my tickers)")
if watch.empty:
    st.info("Add tickers to see indicator state.")
else:
    tickers = watch["ticker"].tolist()
    placeholders = ", ".join([f"'{t}'" for t in tickers])
    state = conn.query(
        f"""
        select trade_date, ticker, closed_minute_et, rsi14, boll_lower, pattern, boll_touch, price, updated_at
        from public.p2_ticker_state
        where trade_date = :d
          and ticker in ({placeholders})
        order by updated_at desc
        """,
        params={"d": trade_date},
        ttl="5s"
    )

    if state.empty:
        st.info("No computed state yet (worker may not have processed these tickers).")
    else:
        st.dataframe(state, use_container_width=True, hide_index=True)

st.divider()
st.subheader("🧾 My stored BUY signals (today)")
buys = conn.query(
    """
    select ts_et, ticker, price, rsi, boll_lower, pattern, details
    from public.p2_buy_signals
    where trade_date = :d and email = :e
    order by ts_et desc
    limit 300
    """,
    params={"d": trade_date, "e": email},
    ttl="5s"
)
if buys.empty:
    st.info("No BUY signals stored yet today for you.")
else:
    st.dataframe(buys, use_container_width=True, hide_index=True)

st.caption("Auto-refreshing…")
st.experimental_rerun()
