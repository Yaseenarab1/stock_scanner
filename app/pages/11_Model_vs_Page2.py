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

try:
    from ui import apply_theme, page_header, section, kpi_row, auto_column_config
except ModuleNotFoundError:
    from app.ui import apply_theme, page_header, section, kpi_row, auto_column_config


# ─────────────────────────────────────────────────────────────────────────────
# Page chrome
# ─────────────────────────────────────────────────────────────────────────────
apply_theme(page_title="Model vs Signal", icon="⚔️")
require_login()
page_header(
    "Model vs Signal",
    subtitle="Episode-start rows only — the first RSI&lt;30 bar per ticker per oversold "
             "episode. <code>score_up</code> here comes from the background scorer; the "
             "live buy decision uses the inline scorer (same model, slightly different timing).",
    eyebrow="PAGE 11 · ANALYTICS",
    status=None,
)
with st.sidebar:
    logout_button()

# ── Head-to-head: all available model versions summarised ─────────────────────
section("Model Head-to-Head", hint="all versions")

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
    st.dataframe(hh, use_container_width=True, hide_index=True,
             column_config=auto_column_config(hh))
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
section("Hit Rate", hint="did +3% actually happen?")

# Page 2 picks
p2 = df[df["signal_fired"]]
m = df[df["model_pick"]]
both = df[df["signal_fired"] & df["model_pick"]]


def _hit_kpi(label, sub):
    if len(sub) > 0:
        hit = int((sub["outcome_first"] == "up").sum())
        rate = hit / len(sub)
        return {"label": label, "value": f"{len(sub)}",
                "delta": f"{hit} hit (+{rate:.0%})", "trend": "up" if rate >= 0.5 else "down"}
    return {"label": label, "value": "0", "delta": "no fires this window", "trend": "flat"}


kpi_row([
    _hit_kpi("Signal fired (model buy)", p2),
    _hit_kpi("Model picks", m),
    _hit_kpi("Both agreed", both),
])

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
section("Per-Day Breakdown")

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
st.dataframe(show, use_container_width=True, hide_index=True,
             column_config=auto_column_config(show))


# ─────────────────────────────────────────────────────────────────────────────
# Disagreement: rows where the strategies parted ways
# ─────────────────────────────────────────────────────────────────────────────
section("Where They Disagree")

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
            .head(50), use_container_width=True, hide_index=True,
            column_config=auto_column_config(model_only)
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
            .head(50), use_container_width=True, hide_index=True,
            column_config=auto_column_config(page2_only)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Full table (filterable)
# ─────────────────────────────────────────────────────────────────────────────
section("All Observations", hint="filterable")
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
st.dataframe(view[cols].head(500), use_container_width=True, hide_index=True,
             column_config=auto_column_config(view[cols]))
st.caption(f"Showing first 500 of {len(view):,}")
