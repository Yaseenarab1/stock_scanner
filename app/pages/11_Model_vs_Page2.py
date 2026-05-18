"""
11_Model_vs_Page2.py
====================

Compares what Page 2 actually fired buys on, vs what the ML model would have
recommended buying, with the outcome of each (did it hit +3% intraday).

Reads from:
  rsi_observations    (raw observations + outcome labels)
  buy_signals         (what Page 2 actually emitted)
  model_predictions   (what the model scored, per version)
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from auth import require_login, logout_button
from db import pg_conn


# ─────────────────────────────────────────────────────────────────────────────
# Page chrome
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Model vs Page 2", layout="wide")
require_login()
st.title("🤖 Model vs Page 2 — comparison")
st.caption("Showing **episode-start rows only** — the first RSI<30 bar per ticker per oversold episode. "
           "These have ~82% historical precision vs 58% for subsequent bars in the same episode.")
logout_button()


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: which window of days, which model version
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.subheader("Window")
    n_days = st.slider("Last N trading days", 1, 60, 14)

    st.subheader("Model version")
    with pg_conn() as con:
        try:
            ver_rows = con.execute("""
              select model_version,
                     count(*) as n_preds,
                     min(trade_date) as first_day,
                     max(trade_date) as last_day
              from model_predictions
              group by model_version
              order by max(trade_date) desc, model_version desc
            """).fetchall()
        except Exception as e:
            ver_rows = []
            st.error(f"model_predictions table not ready: {e!r}")
    if not ver_rows:
        st.warning(
            "No predictions yet. Make sure predict_service.py is running and a "
            "model has been trained (train_model.py)."
        )
        st.stop()
    ver_options = [
        f"{v} ({n:,} preds, {d0} → {d1})" for v, n, d0, d1 in ver_rows
    ]
    chosen = st.selectbox("Model version", options=ver_options, index=0)
    chosen_version = ver_rows[ver_options.index(chosen)][0]


# ─────────────────────────────────────────────────────────────────────────────
# Pull data
# ─────────────────────────────────────────────────────────────────────────────
# We anchor on rsi_observations rows because both pages of comparison
# (page-2 buys, model buys) are subsets of those observations.
# For each row we join:
#   - the prediction (for the chosen model version)
#   - whether Page 2 actually fired a buy_signals row near that ts
#
# 'page2_bought' = there exists a buy_signals row for the same (trade_date, ticker)
#                  within ~10 minutes after this observation. Page 2's buy can
#                  fire on a *later* candle than the qualify candle, so we
#                  allow a small window.
with pg_conn() as con:
    df = pd.read_sql(
        """
        with recent as (
          select distinct trade_date
          from rsi_observations
          where trade_date >= (current_date - %s::int)
          order by trade_date desc
        ),
        obs as (
          select o.*
          from rsi_observations o
          where o.trade_date in (select trade_date from recent)
            and o.market_cap is not null
            and o.outcome_first is not null
            and o.close >= 0.10
            and coalesce(o.is_episode_start, false) = true
        )
        select
          o.id          as obs_id,
          o.trade_date,
          o.ts_et,
          o.ticker,
          o.close       as obs_close,
          o.rsi14, o.mfi14, o.willr14,
          o.market_cap,
          o.was_buy_signal,
          o.outcome_first,
          o.outcome_at,
          o.max_up_pct_eod,
          o.max_down_pct_eod,
          p.score_up,
          p.threshold,
          p.predicted_buy,
          exists (
            select 1 from buy_signals b
            where b.trade_date = o.trade_date
              and b.ticker    = o.ticker
              and b.ts_et::timestamp >= o.ts_et - interval '1 minute'
              and b.ts_et::timestamp <  o.ts_et + interval '15 minutes'
          ) as page2_bought_after
        from obs o
        left join model_predictions p
          on p.obs_id = o.id
         and p.model_version = %s
        order by o.trade_date desc, o.ts_et desc
        """,
        con,
        params=[int(n_days), chosen_version],
    )


if df.empty:
    st.info(f"No observations in the last {n_days} day(s).")
    st.stop()

st.caption(f"Loaded **{len(df):,}** observations from "
           f"`{df['trade_date'].min()}` → `{df['trade_date'].max()}` "
           f"using model version **{chosen_version}**")


# Outcomes are mutually exclusive: hit_up, hit_down, neither
def _outcome_emoji(s: str | None) -> str:
    if s == "up":
        return "✅ +3% first"
    if s == "down":
        return "❌ -3% first"
    if s == "neither":
        return "➖ neither (timed out)"
    return "—"


df["outcome_label"] = df["outcome_first"].map(_outcome_emoji)
df["model_pick"] = df["predicted_buy"].fillna(False)
df["page2_pick"] = df["page2_bought_after"].fillna(False)


# ─────────────────────────────────────────────────────────────────────────────
# Top: headline metrics (hit rate of +3% per strategy)
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("📊 Hit rate — did +3% actually happen?")

c1, c2, c3 = st.columns(3)

# Page 2 picks
p2 = df[df["page2_pick"]]
if len(p2) > 0:
    p2_hit = (p2["outcome_first"] == "up").sum()
    p2_rate = p2_hit / len(p2)
    c1.metric("Page 2 picks", f"{len(p2)}",
              f"{p2_hit} hit (+{p2_rate:.0%})")
else:
    c1.metric("Page 2 picks", "0", "no fires this window")

# Model picks
m = df[df["model_pick"]]
if len(m) > 0:
    m_hit = (m["outcome_first"] == "up").sum()
    m_rate = m_hit / len(m)
    c2.metric("Model picks", f"{len(m)}",
              f"{m_hit} hit (+{m_rate:.0%})")
else:
    c2.metric("Model picks", "0", "no fires this window")

# Both agreed
both = df[df["page2_pick"] & df["model_pick"]]
if len(both) > 0:
    b_hit = (both["outcome_first"] == "up").sum()
    b_rate = b_hit / len(both)
    c3.metric("Both agreed", f"{len(both)}",
              f"{b_hit} hit (+{b_rate:.0%})")
else:
    c3.metric("Both agreed", "0", "—")

# Baseline (what's the +3% rate of all qualified rows)
base = df[df["outcome_first"].isin(["up", "down"])]
if len(base):
    base_rate = (base["outcome_first"] == "up").sum() / len(base)
    st.caption(
        f"Baseline: across **{len(base):,}** labeled rows in this window, "
        f"the +3%-first rate is **{base_rate:.1%}**. "
        f"Any strategy above this is adding signal; any below it is hurting."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-day breakdown
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("📅 Per-day breakdown")

agg = df.groupby("trade_date").agg(
    obs=("obs_id", "count"),
    page2_buys=("page2_pick", "sum"),
    page2_wins=("outcome_first", lambda s: ((s == "up") & df.loc[s.index, "page2_pick"]).sum()),
    model_buys=("model_pick", "sum"),
    model_wins=("outcome_first", lambda s: ((s == "up") & df.loc[s.index, "model_pick"]).sum()),
).reset_index().sort_values("trade_date", ascending=False)

def _rate(wins, n):
    if n == 0:
        return "—"
    return f"{wins}/{n} = {wins/n:.0%}"

agg["Page 2 hit rate"] = [_rate(w, n) for w, n in zip(agg["page2_wins"], agg["page2_buys"])]
agg["Model hit rate"] = [_rate(w, n) for w, n in zip(agg["model_wins"], agg["model_buys"])]
show = agg[["trade_date", "obs", "page2_buys", "Page 2 hit rate",
            "model_buys", "Model hit rate"]].rename(columns={
    "trade_date": "Date",
    "obs": "Total obs",
    "page2_buys": "Page 2 buys",
    "model_buys": "Model buys",
})
st.dataframe(show, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Disagreement: rows where the strategies parted ways
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("⚔️ Where they disagree")

cdis1, cdis2 = st.columns(2)
model_only = df[df["model_pick"] & ~df["page2_pick"]]
page2_only = df[df["page2_pick"] & ~df["model_pick"]]

with cdis1:
    st.markdown(f"**Model would buy, Page 2 didn't** — {len(model_only)} rows")
    if not model_only.empty:
        hit = (model_only["outcome_first"] == "up").sum()
        st.caption(f"+3% hit on **{hit}** of these ({hit/len(model_only):.0%}). "
                   f"These are the missed opportunities — or false alarms.")
        st.dataframe(
            model_only[["trade_date", "ts_et", "ticker", "obs_close",
                        "rsi14", "score_up", "outcome_label"]]
            .head(50), use_container_width=True, hide_index=True
        )

with cdis2:
    st.markdown(f"**Page 2 bought, Model didn't** — {len(page2_only)} rows")
    if not page2_only.empty:
        hit = (page2_only["outcome_first"] == "up").sum()
        st.caption(f"+3% hit on **{hit}** of these ({hit/len(page2_only):.0%}). "
                   f"If hit rate is low, the model is correctly flagging bad Page 2 buys.")
        st.dataframe(
            page2_only[["trade_date", "ts_et", "ticker", "obs_close",
                        "rsi14", "score_up", "outcome_label"]]
            .head(50), use_container_width=True, hide_index=True
        )


# ─────────────────────────────────────────────────────────────────────────────
# Full table (filterable)
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("📋 All observations in window")
filter_choice = st.radio(
    "Filter", ["All", "Page 2 picks", "Model picks", "Both picks", "Disagreements"],
    horizontal=True,
)
view = df.copy()
if filter_choice == "Page 2 picks":
    view = view[view["page2_pick"]]
elif filter_choice == "Model picks":
    view = view[view["model_pick"]]
elif filter_choice == "Both picks":
    view = view[view["page2_pick"] & view["model_pick"]]
elif filter_choice == "Disagreements":
    view = view[view["page2_pick"] ^ view["model_pick"]]

cols = ["trade_date", "ts_et", "ticker", "obs_close",
        "rsi14", "mfi14", "willr14", "market_cap",
        "page2_pick", "model_pick", "score_up", "outcome_label",
        "max_up_pct_eod", "max_down_pct_eod"]
st.dataframe(view[cols].head(500), use_container_width=True, hide_index=True)
st.caption(f"Showing first 500 of {len(view):,}")
