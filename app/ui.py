"""
app/ui.py — shared UI toolkit for the Stock Scanner.

A single place that gives every page the same modern "trading terminal" look:
glassy cards, gradient headers, KPI tiles, status pills and nicer tables.

All helpers are pure presentation — they never touch the database or trading
logic, so they are safe to drop into any page.

Import it defensively so it works whether a page runs with the repo root or the
``app/`` folder on ``sys.path``::

    try:
        from ui import apply_theme, page_header
    except ModuleNotFoundError:
        from app.ui import apply_theme, page_header
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Iterable, Sequence

import streamlit as st

ET = ZoneInfo("America/New_York")

# ──────────────────────────────────────────────────────────────────────────────
# Palette
# ──────────────────────────────────────────────────────────────────────────────
BG = "#0b0f17"
PANEL = "#121826"
PANEL_2 = "#161e2e"
BORDER = "#243044"
TEXT = "#e6edf3"
MUTED = "#8b97a7"
ACCENT = "#22d3a5"        # mint / "up"
ACCENT_2 = "#3b82f6"      # electric blue
DANGER = "#ef4466"        # "down"
WARN = "#f5a524"
GOLD = "#f7b955"

_THEME_FLAG = "_ui_theme_injected"


# ──────────────────────────────────────────────────────────────────────────────
# Global CSS
# ──────────────────────────────────────────────────────────────────────────────
_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap');

:root {{
  --bg: {BG};
  --panel: {PANEL};
  --panel2: {PANEL_2};
  --border: {BORDER};
  --text: {TEXT};
  --muted: {MUTED};
  --accent: {ACCENT};
  --accent2: {ACCENT_2};
  --danger: {DANGER};
}}

html, body, [class*="css"], .stApp, .stMarkdown, p, span, label {{
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}

.stApp {{
  background:
    radial-gradient(1100px 540px at 12% -8%, rgba(59,130,246,0.10), transparent 60%),
    radial-gradient(1000px 520px at 100% 0%, rgba(34,211,165,0.10), transparent 55%),
    var(--bg);
  color: var(--text);
}}

/* numbers / tickers feel like a terminal */
code, .stMetric [data-testid="stMetricValue"], .mono {{
  font-family: 'JetBrains Mono', monospace !important;
}}

/* tighten default top padding */
.block-container {{ padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1500px; }}

/* hide Streamlit's default chrome we don't need */
#MainMenu, footer {{ visibility: hidden; }}

/* ── Sidebar ───────────────────────────────────────────── */
section[data-testid="stSidebar"] {{
  background: linear-gradient(180deg, #0d121d 0%, #0a0e16 100%);
  border-right: 1px solid var(--border);
}}
section[data-testid="stSidebar"] .block-container {{ padding-top: 1.4rem; }}

/* ── Hero header ──────────────────────────────────────── */
.app-hero {{
  position: relative;
  border: 1px solid var(--border);
  border-radius: 18px;
  padding: 22px 26px;
  margin-bottom: 22px;
  background:
    linear-gradient(135deg, rgba(34,211,165,0.10), rgba(59,130,246,0.06) 55%, rgba(18,24,38,0.4)),
    var(--panel);
  box-shadow: 0 18px 40px -24px rgba(0,0,0,0.8), inset 0 1px 0 rgba(255,255,255,0.03);
  overflow: hidden;
}}
.app-hero::after {{
  content: ""; position: absolute; inset: 0;
  background: radial-gradient(700px 200px at 88% -40%, rgba(34,211,165,0.18), transparent 60%);
  pointer-events: none;
}}
.app-hero .eyebrow {{
  font-size: 12px; letter-spacing: .16em; text-transform: uppercase;
  color: var(--accent); font-weight: 700; margin-bottom: 6px;
}}
.app-hero h1 {{
  font-size: 30px; font-weight: 800; margin: 0; line-height: 1.15;
  background: linear-gradient(92deg, #ffffff, #b9f5e4);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}}
.app-hero .sub {{ color: var(--muted); margin-top: 8px; font-size: 14px; max-width: 70ch; }}

/* ── KPI tiles ────────────────────────────────────────── */
.kpi-grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); margin: 4px 0 18px; }}
.kpi {{
  border: 1px solid var(--border); border-radius: 14px; padding: 16px 18px;
  background: linear-gradient(180deg, var(--panel2), var(--panel));
  box-shadow: 0 12px 26px -22px rgba(0,0,0,0.9);
  transition: transform .15s ease, border-color .15s ease;
}}
.kpi:hover {{ transform: translateY(-2px); border-color: rgba(34,211,165,0.45); }}
.kpi .label {{ color: var(--muted); font-size: 12px; letter-spacing: .06em; text-transform: uppercase; font-weight: 600; }}
.kpi .value {{ font-family: 'JetBrains Mono', monospace; font-size: 26px; font-weight: 700; margin-top: 6px; }}
.kpi .delta {{ font-size: 12.5px; margin-top: 6px; font-weight: 600; }}
.kpi .delta.up {{ color: var(--accent); }}
.kpi .delta.down {{ color: var(--danger); }}
.kpi .delta.flat {{ color: var(--muted); }}

/* ── Section header ───────────────────────────────────── */
.section-h {{
  display: flex; align-items: center; gap: 10px;
  margin: 26px 0 12px; padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}}
.section-h .bar {{ width: 4px; height: 20px; border-radius: 4px; background: linear-gradient(180deg, var(--accent), var(--accent2)); }}
.section-h .t {{ font-size: 17px; font-weight: 700; letter-spacing: .01em; }}
.section-h .hint {{ color: var(--muted); font-size: 12.5px; font-weight: 500; margin-left: auto; }}

/* ── Pills ────────────────────────────────────────────── */
.pill {{
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 11px; border-radius: 999px; font-size: 12px; font-weight: 600;
  border: 1px solid transparent;
}}
.pill .dot {{ width: 7px; height: 7px; border-radius: 50%; }}
.pill.live  {{ background: rgba(34,211,165,0.12); color: var(--accent); border-color: rgba(34,211,165,0.35); }}
.pill.live .dot {{ background: var(--accent); box-shadow: 0 0 0 0 rgba(34,211,165,0.7); animation: pulse 1.6s infinite; }}
.pill.warn  {{ background: rgba(245,165,36,0.12); color: {WARN}; border-color: rgba(245,165,36,0.35); }}
.pill.down  {{ background: rgba(239,68,102,0.12); color: var(--danger); border-color: rgba(239,68,102,0.35); }}
.pill.muted {{ background: rgba(139,151,167,0.10); color: var(--muted); border-color: rgba(139,151,167,0.30); }}
@keyframes pulse {{ 0% {{ box-shadow: 0 0 0 0 rgba(34,211,165,0.55);}} 70% {{ box-shadow: 0 0 0 7px rgba(34,211,165,0);}} 100% {{ box-shadow: 0 0 0 0 rgba(34,211,165,0);}} }}

/* ── Buttons ──────────────────────────────────────────── */
.stButton > button {{
  border-radius: 10px; border: 1px solid var(--border);
  background: linear-gradient(180deg, var(--panel2), var(--panel));
  color: var(--text); font-weight: 600; transition: all .15s ease;
}}
.stButton > button:hover {{ border-color: var(--accent); color: var(--accent); transform: translateY(-1px); }}
.stButton > button[kind="primary"] {{
  background: linear-gradient(135deg, var(--accent), #18b78c);
  color: #04130d; border: none;
}}
.stButton > button[kind="primary"]:hover {{ filter: brightness(1.07); color: #04130d; }}

/* ── Inputs ───────────────────────────────────────────── */
.stTextInput input, .stNumberInput input, .stTextArea textarea, .stSelectbox div[data-baseweb="select"] > div {{
  background: var(--panel) !important; border-radius: 10px !important;
  border: 1px solid var(--border) !important; color: var(--text) !important;
}}
.stTextInput input:focus, .stNumberInput input:focus {{ border-color: var(--accent) !important; }}

/* ── Tables / dataframes ──────────────────────────────── */
[data-testid="stDataFrame"] {{
  border: 1px solid var(--border); border-radius: 14px; overflow: hidden;
  box-shadow: 0 14px 30px -26px rgba(0,0,0,0.9);
}}
[data-testid="stDataFrame"] thead tr th {{
  background: var(--panel2) !important; color: var(--muted) !important;
  text-transform: uppercase; font-size: 11px; letter-spacing: .05em;
}}

/* ── Metric (native) polish ───────────────────────────── */
[data-testid="stMetric"] {{
  background: linear-gradient(180deg, var(--panel2), var(--panel));
  border: 1px solid var(--border); border-radius: 14px; padding: 14px 16px;
}}
[data-testid="stMetricLabel"] {{ color: var(--muted) !important; }}

/* ── Alerts / info boxes ──────────────────────────────── */
[data-testid="stNotification"], .stAlert {{ border-radius: 12px; border: 1px solid var(--border); }}

/* ── Tabs ─────────────────────────────────────────────── */
.stTabs [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid var(--border); }}
.stTabs [data-baseweb="tab"] {{ border-radius: 10px 10px 0 0; padding: 8px 16px; }}
.stTabs [aria-selected="true"] {{ background: var(--panel2); color: var(--accent) !important; }}

/* expander */
.streamlit-expanderHeader, [data-testid="stExpander"] summary {{ border-radius: 10px; }}
[data-testid="stExpander"] {{ border: 1px solid var(--border); border-radius: 12px; }}

/* ── Login card ───────────────────────────────────────── */
.login-wrap {{ max-width: 420px; margin: 4vh auto 0; }}
.login-card {{
  border: 1px solid var(--border); border-radius: 20px; padding: 30px 30px 8px;
  background: linear-gradient(180deg, var(--panel2), var(--panel));
  box-shadow: 0 30px 70px -40px rgba(0,0,0,0.9);
}}
.brand {{ display:flex; align-items:center; gap:12px; justify-content:center; margin-bottom: 4px; }}
.brand .logo {{
  width: 42px; height: 42px; border-radius: 12px; display:grid; place-items:center;
  background: linear-gradient(135deg, var(--accent), var(--accent2)); color:#04130d;
  font-weight:800; font-size: 20px; box-shadow: 0 8px 20px -8px rgba(34,211,165,0.6);
}}
.brand .name {{ font-size: 20px; font-weight: 800; letter-spacing: .01em; }}
.brand .name span {{ color: var(--accent); }}
</style>
"""


def apply_theme(page_title: str = "Stock Scanner", icon: str = "📈", layout: str = "wide") -> None:
    """Set page config (once) and inject the global stylesheet.

    Safe to call at the top of every page. ``set_page_config`` is wrapped so a
    second call (Streamlit only allows one) never crashes the page.
    """
    try:
        st.set_page_config(page_title=page_title, page_icon=icon, layout=layout)
    except Exception:
        pass
    # Always (re)inject CSS: Streamlit clears markup between reruns.
    st.markdown(_CSS, unsafe_allow_html=True)
    st.session_state[_THEME_FLAG] = True


def page_header(title: str, subtitle: str = "", eyebrow: str = "STOCK SCANNER",
                status: str | None = "live") -> None:
    """Render the glossy hero header used at the top of each page."""
    pill = ""
    if status:
        pill_map = {
            "live": ('<span class="pill live"><span class="dot"></span>LIVE</span>'),
            "warn": ('<span class="pill warn"><span class="dot"></span>DEGRADED</span>'),
            "down": ('<span class="pill down"><span class="dot"></span>OFFLINE</span>'),
        }
        pill = pill_map.get(status, "")
    sub = f'<div class="sub">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f"""
        <div class="app-hero">
          <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;">
            <div>
              <div class="eyebrow">{eyebrow}</div>
              <h1>{title}</h1>
              {sub}
            </div>
            <div>{pill}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section(title: str, hint: str = "") -> None:
    """A clean section divider with an accent bar."""
    hint_html = f'<div class="hint">{hint}</div>' if hint else ""
    st.markdown(
        f'<div class="section-h"><div class="bar"></div><div class="t">{title}</div>{hint_html}</div>',
        unsafe_allow_html=True,
    )


def _kpi_html(label: str, value, delta: str | None = None, trend: str = "flat") -> str:
    d = f'<div class="delta {trend}">{delta}</div>' if delta else ""
    return f'<div class="kpi"><div class="label">{label}</div><div class="value">{value}</div>{d}</div>'


def kpi_row(items: Sequence[dict]) -> None:
    """Render a responsive grid of KPI tiles.

    Each item: ``{"label": str, "value": str|num, "delta": str?, "trend": "up"|"down"|"flat"?}``
    """
    cards = "".join(
        _kpi_html(i["label"], i["value"], i.get("delta"), i.get("trend", "flat"))
        for i in items
    )
    st.markdown(f'<div class="kpi-grid">{cards}</div>', unsafe_allow_html=True)


def pill(text: str, kind: str = "muted") -> str:
    """Return inline HTML for a status pill (use with st.markdown)."""
    dot = '<span class="dot"></span>' if kind in ("live", "warn", "down") else ""
    return f'<span class="pill {kind}">{dot}{text}</span>'


def auto_column_config(df) -> dict:
    """Build a Streamlit ``column_config`` dict from a DataFrame's column names.

    Purely heuristic and fully defensive: it only configures columns that exist
    and silently skips anything that errors, so a renamed/missing column can
    never crash a page. Use as::

        st.dataframe(df, column_config=auto_column_config(df), ...)
    """
    cfg: dict = {}
    try:
        cols = list(getattr(df, "columns", []))
    except Exception:
        return cfg

    for col in cols:
        try:
            name = str(col).strip().lower()
            c = st.column_config  # noqa: N806

            # RSI → progress bar (0..100)
            if name in ("rsi", "rsi14") or name.startswith("rsi"):
                cfg[col] = c.ProgressColumn(str(col), min_value=0, max_value=100, format="%.0f")
                continue
            # Percentages / gains
            if "pct" in name or "gain" in name or name.endswith("%") or "%" in name:
                cfg[col] = c.NumberColumn(str(col), format="%.2f%%")
                continue
            # Market cap (big dollars)
            if "market_cap" in name or name == "mcap":
                cfg[col] = c.NumberColumn(str(col), format="$%d")
                continue
            # Prices
            if (name in ("price", "entry", "current_price", "start_price",
                         "baseline_8am", "boll_lower")
                    or name.endswith("_price") or name.endswith("price")):
                cfg[col] = c.NumberColumn(str(col), format="$%.4f")
                continue
            # Volumes / share counts
            if "vol" in name or "shares" in name:
                cfg[col] = c.NumberColumn(str(col), format="%d")
                continue
        except Exception:
            # Never let table styling break a page.
            continue
    return cfg


def clock_caption(now: datetime | None = None) -> None:
    """A small market-clock caption (ET) with last-updated time."""
    now = now or datetime.now(ET)
    st.markdown(
        f'<div style="color:{MUTED};font-size:12.5px;margin-top:-6px;">'
        f'🕒 {now.strftime("%a %b %d, %Y · %H:%M:%S")} ET &nbsp;·&nbsp; '
        f'auto-refreshing</div>',
        unsafe_allow_html=True,
    )
