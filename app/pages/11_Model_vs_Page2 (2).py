"""
11_Model_vs_Page2.py
====================

Compares when buy_signals fired (model scan) vs what the ML model scored,
with actual trade outcomes, with the outcome of each (did it hit +3% intraday).

Reads from:
  rsi_observations    (raw observations + outcome labels)
  buy_signals         (written by section_model_scan when model fires a buy)
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
st.set_page_config(page_title="Model vs Signal", layout="wide")
require_login()
st.title("🤖 Model vs Signal — comparison")
st.caption("Showing **episode-start rows only** — the first RSI<30 bar per ticker per oversold episode. "
           "**Note:** `score_up` shown here is from predict_service (background scorer). "
           "The actual buy decision uses the inline scorer in temp.py which may give slightly different scores "
           "due to different bar timing. Both use the same model file.")

# ── Head-to-head: all available model versions summarised ─────────────────────
st.subheader("⚔️ Model head-to-head")

with pg_conn() as con:
    try:
        all_versions = pd.read_sql(
            """
            select
                p.model_version,
                count(*)                                         as total_picks,
                sum(case when o.outcome_first='up' then 1 else 0 end) as hits,
                round(100.0 * sum(case when o.outcome_first='up' then 1 else 0 end)
                      / nullif(count(*), 0), 1)                  as precision_pct,
                round(avg(p.score_up)::numeric, 4)               as avg_score,
                round(avg(p.threshold)::numeric, 4)              as threshold,
                min(p.trade_date)                                as first_day,
                max(p.trade_date)                                as last_day,
                count(distinct p.trade_date)                     as n_days
            from model_predictions p
            join rsi_observations o on o.id = p.obs_id
            where p.predicted_buy = true
              and o.outcome_first is not null
              and o.close >= 0.10
              and coalesce(o.is_episode_start, false) = true
            group by p.model_version
            order by max(p.trade_date) desc, p.model_version
            """,
            con,
        )
    except Exception as e:
        all_versions = pd.DataFrame()
        st.error(f"Could not load version summary: {e!r}")

if not all_versions.empty:
    # Baseline precision across the same window
    try:
        with pg_conn() as con:
            base_row = con.execute(
                """
                select round(100.0 * sum(case when outcome_first='up' then 1 else 0 end)
                             / nullif(count(*), 0), 1)
                from rsi_observations
                where outcome_first is not null
                  and close >= 0.10
                  and coalesce(is_episode_start, false) = true
                """
            ).fetchone()
            baseline_pct = float(base_row[0]) if base_row and base_row[0] else None
    except Exception:
        baseline_pct = None

    hh = all_versions.rename(columns={
        "model_version": "Version",
        "total_picks": "Picks (predicted BUY)",
        "hits": "Hits (+3% first)",
        "precision_pct": "Precision %",
        "avg_score": "Avg score",
        "threshold": "Threshold",
        "first_day": "First day",
        "last_day": "Last day",
        "n_days": "Days",
    })
    st.dataframe(hh, use_container_width=True, hide_index=True)
    if baseline_pct is not None:
        st.caption(
            f"Baseline (buy all episode-starts, no model): **{baseline_pct}%** precision. "
            f"Any model row above this is adding value."
        )

    # Quick winner call
    if len(all_versions) >= 2:
        best = all_versions.loc[all_versions["precision_pct"].idxmax()]
        st.info(
            f"📊 Best precision so far: **{best['model_version']}** "
            f"at **{best['precision_pct']}%** on {int(best['total_picks'])} picks."
        )
else:
    st.info("No model predictions with outcomes yet — check back after market close.")

st.divider()
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
              and (b.ts_et at time zone 'America/New_York')::timestamp >= o.ts_et - interval '1 minute'
              and (b.ts_et at time zone 'America/New_York')::timestamp <  o.ts_et + interval '15 minutes'
          ) as signal_fired_after
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
df["signal_fired"] = df["signal_fired_after"].fillna(False)
# NOTE: signal_fired = buy_signals table was written (by model scan)
# model_pick = model scored this row as predicted_buy=true (from predict_service)
# These can differ: inline scorer (temp.py) and predict_service use different bar timing.
# The inline scorer is the ACTUAL buy decision maker.
# score_up in this table = predict_service score, NOT the inline score that triggered the buy.
df["page2_pick"] = df["signal_fired"]  # keep alias for backward compat


# ─────────────────────────────────────────────────────────────────────────────
# Top: headline metrics (hit rate of +3% per strategy)
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("📊 Hit rate — did +3% actually happen?")

c1, c2, c3 = st.columns(3)

# Page 2 picks
p2 = df[df["signal_fired"]]
if len(p2) > 0:
    p2_hit = (p2["outcome_first"] == "up").sum()
    p2_rate = p2_hit / len(p2)
    c1.metric("Signal fired (model buy)", f"{len(p2)}",
              f"{p2_hit} hit (+{p2_rate:.0%})")
else:
    c1.metric("Signal fired (model buy)", "0", "no fires this window")

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
both = df[df["signal_fired"] & df["model_pick"]]
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
    page2_buys=("signal_fired", "sum"),
    page2_wins=("outcome_first", lambda s: ((s == "up") & df.loc[s.index, "signal_fired"]).sum()),
    model_buys=("model_pick", "sum"),
    model_wins=("outcome_first", lambda s: ((s == "up") & df.loc[s.index, "model_pick"]).sum()),
).reset_index().sort_values("trade_date", ascending=False)

def _rate(wins, n):
    if n == 0:
        return "—"
    return f"{wins}/{n} = {wins/n:.0%}"

agg["Signal hit rate"] = [_rate(w, n) for w, n in zip(agg["page2_wins"], agg["page2_buys"])]
agg["Model hit rate"] = [_rate(w, n) for w, n in zip(agg["model_wins"], agg["model_buys"])]
show = agg[["trade_date", "obs", "page2_buys", "Signal hit rate",
            "model_buys", "Model hit rate"]].rename(columns={
    "trade_date": "Date",
    "obs": "Total obs",
    "page2_buys": "Signal fired",
    "model_buys": "Model buys",
})
st.dataframe(show, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Disagreement: rows where the strategies parted ways
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("⚔️ Where they disagree")

cdis1, cdis2 = st.columns(2)
model_only = df[df["model_pick"] & ~df["signal_fired"]]
page2_only = df[df["signal_fired"] & ~df["model_pick"]]

with cdis1:
    st.markdown(f"**Model scored YES, no buy_signals row** — {len(model_only)} rows")
    if not model_only.empty:
        hit = (model_only["outcome_first"] == "up").sum()
        st.caption(f"+3% hit on **{hit}** of these ({hit/len(model_only):.0%}). "
                   f"Model said YES but no buy fired — possible execution failure or timing gap.")
        st.dataframe(
            model_only[["trade_date", "ts_et", "ticker", "obs_close",
                        "rsi14", "score_up", "outcome_label"]]
            .head(50), use_container_width=True, hide_index=True
        )

with cdis2:
    st.markdown(f"**buy_signals fired, model scored NO** — {len(page2_only)} rows")
    if not page2_only.empty:
        hit = (page2_only["outcome_first"] == "up").sum()
        st.caption(f"+3% hit on **{hit}** of these ({hit/len(page2_only):.0%}). "
                   f"buy_signals fired but model score was below threshold — check why model disagreed.")
        st.dataframe(
            page2_only[["trade_date", "ts_et", "ticker", "obs_close",
                        "rsi14", "score_up", "outcome_label"]]
            .head(50), use_container_width=True, hide_index=True
        )


# ─────────────────────────────────────────────────────────────────────────────
# Actual trade results
# ─────────────────────────────────────────────────────────────────────────────
st.divider()
st.subheader("💰 Actual trade results (real fills, not theoretical labels)")
st.caption(
    "Measures what the auto-trader actually made/lost using real fill prices and "
    "SL/TP exits — NOT the theoretical label which measures from the RSI<30 "
    "observation bar close. This is the honest number."
)

with pg_conn() as con:
    try:
        trades_df = pd.read_sql(
            """
            select
                p.trade_date,
                p.ticker,
                p.entry_price,
                p.stop_loss_pct,
                p.take_profit_pct,
                p.trade_outcome,
                p.qty,
                p.opened_at,
                p.closed_at,
                o.outcome_first as label_outcome,
                o.ts_et         as obs_ts
            from public.user_auto_trade_positions p
            left join rsi_observations o
                on o.trade_date = p.trade_date
               and o.ticker = p.ticker
               and o.was_buy_signal = true
            where p.trade_date >= (current_date - %s::int)
            order by p.trade_date desc, p.opened_at desc
            """,
            con,
            params=[int(n_days)],
        )
    except Exception as e:
        trades_df = pd.DataFrame()
        st.error(f"Could not load trade results: {e!r}")

if not trades_df.empty:
    closed_t = trades_df[trades_df["trade_outcome"].notna()]
    if not closed_t.empty:
        wins = (closed_t["trade_outcome"] == "win").sum()
        losses = (closed_t["trade_outcome"] == "loss").sum()
        eods = (closed_t["trade_outcome"] == "eod").sum()
        total = len(closed_t)
        win_rate = wins / total if total > 0 else 0

        rc1, rc2, rc3, rc4 = st.columns(4)
        rc1.metric("Closed trades", total)
        rc2.metric("Wins (TP hit)", f"{wins} ({win_rate:.0%})")
        rc3.metric("Losses (SL hit)", losses)
        rc4.metric("EOD closes", eods)

        label_wins = (closed_t["label_outcome"] == "up").sum()
        label_total = closed_t["label_outcome"].notna().sum()
        if label_total > 0:
            label_rate = label_wins / label_total
            gap = win_rate - label_rate
            symbol = ("⚠ label was optimistic" if gap < -0.05
                      else ("✓ aligned" if abs(gap) <= 0.05 else "🎉 actual beats label"))
            st.caption(
                f"Label win rate (from obs close): **{label_rate:.0%}** | "
                f"Actual win rate (from fill): **{win_rate:.0%}** | "
                f"Gap: **{gap:+.0%}** — {symbol}"
            )

        st.dataframe(
            closed_t[["trade_date", "ticker", "entry_price", "qty",
                      "stop_loss_pct", "take_profit_pct",
                      "trade_outcome", "label_outcome"]].rename(columns={
                "trade_date": "Date", "entry_price": "Entry $",
                "stop_loss_pct": "SL %", "take_profit_pct": "TP %",
                "trade_outcome": "Actual result", "label_outcome": "Label said",
            }),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No closed trades yet in this window.")
else:
    st.info("No trades found in this window.")

# ─────────────────────────────────────────────────────────────────────────────
# Full table (filterable)
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("📋 All observations in window")
filter_choice = st.radio(
    "Filter", ["All", "Signal fired (model buy)", "Model picks", "Both picks", "Disagreements"],
    horizontal=True,
)
view = df.copy()
if filter_choice == "Signal fired (model buy)":
    view = view[view["signal_fired"]]
elif filter_choice == "Model picks":
    view = view[view["model_pick"]]
elif filter_choice == "Both picks":
    view = view[view["signal_fired"] & view["model_pick"]]
elif filter_choice == "Disagreements":
    view = view[view["signal_fired"] ^ view["model_pick"]]

cols = ["trade_date", "ts_et", "ticker", "obs_close",
        "rsi14", "mfi14", "willr14", "market_cap",
        "signal_fired", "model_pick", "score_up", "outcome_label",
        "max_up_pct_eod", "max_down_pct_eod"]
st.dataframe(view[cols].head(500), use_container_width=True, hide_index=True)
st.caption(f"Showing first 500 of {len(view):,}")
