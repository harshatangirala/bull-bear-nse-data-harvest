#!/usr/bin/env python3
"""Bull Bear NSE Data Harvest -- live dashboard.

    streamlit run dashboard.py

Read-only: this process never writes to the database, so it is always safe
to run alongside the scheduler (`python main.py run`) -- SQLite's WAL mode,
already enabled by Dao, lets a reader see committed rows without blocking
the poller's writes. If the DB does not exist yet, every section degrades to
an empty-state message rather than crashing, so `streamlit run dashboard.py`
works even before the first `python main.py once --all`.

Two version-specific traps already found and fixed here, worth knowing if a
future streamlit/pandas upgrade reintroduces them:
  - `df.style.applymap(...)` was removed from pandas' Styler API; use
    `.style.map(...)`. The alerts table stopped using Styler altogether
    instead (see the severity-emoji comment below) -- it was also the
    difference between a ~25s and a ~2s render on 487 rows.
  - `st.dataframe(..., use_container_width=True)` is deprecated toward
    `width="stretch"` / `width="content"` as of streamlit 1.59.

Every tab's content is built on every script rerun regardless of which tab
is visible -- `st.tabs()` just toggles CSS, it does not defer execution. On
a cold process with 25 categories of data this means the first render pays
for all ~4 charts and ~4 tables at once (several seconds); reruns within the
15s cache TTL are much faster since the SQL queries are cached even though
the charts still re-render.

Database target: BBNSE_DB_URL, same variable the CLI and scheduler use.
Locally that comes from .env via python-dotenv (see core/config.py).
Deployed on Streamlit Community Cloud there is no .env -- committing one
would leak the DB credential -- so the same key is pasted into the app's
Secrets panel instead and bridged into the environment below, before
load_config() ever runs. Point every process (local scheduler, local CLI,
and the deployed dashboard) at the SAME hosted database and there is
nothing to keep in sync: it's one source of truth, not a copy.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import text

try:
    if "BBNSE_DB_URL" in st.secrets:
        os.environ["BBNSE_DB_URL"] = st.secrets["BBNSE_DB_URL"]
except Exception:
    pass  # no secrets.toml (normal for a local run) -- .env covers it instead

from bbnse.core.config import load_config
from bbnse.reports.base import to_local
from bbnse.storage.dao import Dao

SEVERITY_COLOR = {"critical": "#e05252", "notable": "#e0a626", "info": "#5b8fd6"}
SEVERITY_ORDER = ["critical", "notable", "info"]

st.set_page_config(page_title="Bull Bear NSE", page_icon="🐂", layout="wide")


# ---------------------------------------------------------------------------
# Data access -- cached briefly so widget interactions don't re-hit SQLite on
# every rerun, but short enough that "Refresh now" feels meaningful during
# market hours. Every loader tolerates missing tables (fresh DB) and empty
# results, returning an empty DataFrame with the right columns rather than
# raising, so downstream rendering code never special-cases "no data yet".
# ---------------------------------------------------------------------------
@st.cache_resource
def get_dao() -> Dao:
    cfg = load_config()
    return Dao(cfg.db_url)


def _read_sql(dao: Dao, sql: str, params: dict) -> pd.DataFrame:
    # sqlalchemy.text() is required, not optional: a raw SQL string handed
    # straight to pandas skips SQLAlchemy's bind-param translation, so the
    # ":start"/":end" placeholders below only happened to work against
    # SQLite (whose DBAPI accepts ":name" as its own native paramstyle) --
    # psycopg2's native style is "%(name)s", and every one of these queries
    # raised a bare psycopg2.errors.SyntaxError on Postgres until this was
    # wrapped in text(), which is why this needs to stay.
    try:
        return pd.read_sql(text(sql), dao.engine, params=params)
    except Exception as exc:
        # Broad on purpose (a fresh DB with no tables yet must not crash the
        # page) but surfaced, not silent -- a bad query silently returning
        # "no data" instead of erroring is exactly the bug that shipped here
        # once already.
        st.warning(f"Query failed, showing empty: {exc}", icon="⚠️")
        return pd.DataFrame()


@st.cache_data(ttl=15)
def load_alerts(_dao_key: str, start: date, end: date) -> pd.DataFrame:
    dao = get_dao()
    df = _read_sql(dao, """
        SELECT id, event_key, category, rule_id, entity, severity, kind,
               title, body, value, created_at, trade_date, payload
        FROM alert
        WHERE trade_date BETWEEN :start AND :end
        ORDER BY created_at DESC
    """, {"start": start.isoformat(), "end": end.isoformat()})
    if df.empty:
        return df
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df["local_time"] = df["created_at"].apply(
        lambda dt: to_local(dt.to_pydatetime()))
    return df


@st.cache_data(ttl=15)
def load_health(_dao_key: str) -> pd.DataFrame:
    dao = get_dao()
    df = _read_sql(dao, """
        SELECT category, last_success_at, last_failure_at,
               consecutive_failures, total_success, total_failure,
               last_error, last_row_count, last_elapsed_sec
        FROM fetch_health ORDER BY category
    """, {})
    if df.empty:
        return df
    for col in ("last_success_at", "last_failure_at"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    return df


@st.cache_data(ttl=15)
def load_deals(_dao_key: str, start: date, end: date) -> pd.DataFrame:
    dao = get_dao()
    return _read_sql(dao, """
        SELECT trade_date, deal_type, symbol, company, client_name,
               buy_sell, quantity, price, value_cr, remarks
        FROM deal_observation
        WHERE trade_date BETWEEN :start AND :end
        ORDER BY value_cr DESC
    """, {"start": start.isoformat(), "end": end.isoformat()})


@st.cache_data(ttl=15)
def load_week52(_dao_key: str, start: date, end: date) -> pd.DataFrame:
    dao = get_dao()
    return _read_sql(dao, """
        SELECT trade_date, category, COUNT(*) AS n
        FROM observation
        WHERE category IN ('week52_high', 'week52_low')
          AND trade_date BETWEEN :start AND :end
        GROUP BY trade_date, category
        ORDER BY trade_date
    """, {"start": start.isoformat(), "end": end.isoformat()})


@st.cache_data(ttl=15)
def load_breadth(_dao_key: str, start: date, end: date) -> pd.DataFrame:
    dao = get_dao()
    df = _read_sql(dao, """
        SELECT trade_date, observed_at, extra
        FROM observation
        WHERE category = 'advance_decline'
          AND trade_date BETWEEN :start AND :end
        ORDER BY observed_at
    """, {"start": start.isoformat(), "end": end.isoformat()})
    if df.empty:
        return df
    parsed = df["extra"].apply(lambda x: json.loads(x) if isinstance(x, str) else (x or {}))
    df["advances"] = parsed.apply(lambda e: e.get("advances"))
    df["declines"] = parsed.apply(lambda e: e.get("declines"))
    df["ad_ratio"] = parsed.apply(lambda e: e.get("ad_ratio"))
    return df.drop(columns=["extra"])


@st.cache_data(ttl=15)
def load_correlation_stats(_dao_key: str, start: date, end: date) -> pd.DataFrame:
    dao = get_dao()
    return _read_sql(dao, """
        SELECT dedup_group, first_category, top_severity, corroborations
        FROM event_correlation
        WHERE session_date BETWEEN :start AND :end
    """, {"start": start.isoformat(), "end": end.isoformat()})


@st.cache_data(ttl=15)
def db_bounds(_dao_key: str) -> tuple[date | None, date | None]:
    dao = get_dao()
    df = _read_sql(dao, "SELECT MIN(trade_date) AS lo, MAX(trade_date) AS hi "
                        "FROM alert", {})
    if df.empty or pd.isna(df.loc[0, "lo"]):
        return None, None
    return (pd.to_datetime(df.loc[0, "lo"]).date(),
            pd.to_datetime(df.loc[0, "hi"]).date())


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
dao = get_dao()
dao_key = str(dao.engine.url)   # cheap cache-bust handle for @st.cache_data

st.title("🐂 Bull Bear — NSE Data Harvest")

lo, hi = db_bounds(dao_key)
if lo is None:
    st.info(
        "No data yet. Run `python main.py once --all` (or start "
        "`python main.py run`) to populate the database, then reload this page."
    )
    st.stop()

with st.sidebar:
    st.header("Filters")
    default_start = max(lo, hi - timedelta(days=6))
    date_range = st.date_input(
        "Date range", value=(default_start, hi), min_value=lo, max_value=hi
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date = end_date = date_range if isinstance(date_range, date) else hi

    st.divider()
    if st.button("🔄 Refresh now", width='stretch'):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"Auto-cached 15s · DB: `{dao.engine.url.database}`")

alerts = load_alerts(dao_key, start_date, end_date)
health = load_health(dao_key)
deals = load_deals(dao_key, start_date, end_date)
week52 = load_week52(dao_key, start_date, end_date)
breadth = load_breadth(dao_key, start_date, end_date)
corr = load_correlation_stats(dao_key, start_date, end_date)

with st.sidebar:
    if not alerts.empty:
        cats = sorted(alerts["category"].unique())
        sel_cats = st.multiselect("Category", cats, default=cats)
        sevs = st.multiselect("Severity", SEVERITY_ORDER, default=SEVERITY_ORDER)
        symbol_q = st.text_input("Symbol / entity contains").strip().upper()
    else:
        sel_cats, sevs, symbol_q = [], [], ""

filtered = alerts
if not filtered.empty:
    filtered = filtered[filtered["category"].isin(sel_cats)
                        & filtered["severity"].isin(sevs)]
    if symbol_q:
        filtered = filtered[filtered["entity"].str.upper().str.contains(symbol_q)]

# -- KPI row -----------------------------------------------------------------
k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Alerts in range", len(filtered))
k2.metric("Critical", int((filtered["severity"] == "critical").sum())
          if not filtered.empty else 0)
k3.metric("Notable", int((filtered["severity"] == "notable").sum())
          if not filtered.empty else 0)
unhealthy = int((health["consecutive_failures"] > 0).sum()) if not health.empty else 0
k4.metric("Unhealthy fetchers", unhealthy, delta=None,
          delta_color="inverse" if unhealthy else "off")
k5.metric("Large deal value (cr)", f"{deals['value_cr'].sum():,.0f}"
          if not deals.empty else "0")

st.divider()

tab_overview, tab_alerts, tab_health, tab_deals, tab_breadth = st.tabs(
    ["Overview", "Alerts", "Fetcher health", "Large deals", "Market breadth"]
)

# -- Overview ------------------------------------------------------------
with tab_overview:
    c1, c2 = st.columns([3, 2])

    with c1:
        st.subheader("Alerts over time, by severity")
        if filtered.empty:
            st.caption("No alerts in the selected range/filters.")
        else:
            daily = (filtered.assign(day=filtered["local_time"].dt.date)
                    .groupby(["day", "severity"]).size()
                    .reset_index(name="count"))
            fig = px.bar(daily, x="day", y="count", color="severity",
                        color_discrete_map=SEVERITY_COLOR,
                        category_orders={"severity": SEVERITY_ORDER})
            fig.update_layout(height=320, margin=dict(t=10, b=10, l=10, r=10),
                              legend_title_text="")
            st.plotly_chart(fig, width='stretch')

    with c2:
        st.subheader("By category")
        if filtered.empty:
            st.caption("No alerts in the selected range/filters.")
        else:
            by_cat = (filtered.groupby("category").size()
                     .sort_values(ascending=True).reset_index(name="count"))
            fig = px.bar(by_cat, x="count", y="category", orientation="h")
            fig.update_layout(height=320, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, width='stretch')

    st.subheader("52-week extremes")
    if week52.empty:
        st.caption("No 52-week high/low observations in range.")
    else:
        pivot = week52.pivot(index="trade_date", columns="category",
                             values="n").fillna(0)
        for col in ("week52_high", "week52_low"):
            if col not in pivot:
                pivot[col] = 0
        fig = go.Figure()
        fig.add_bar(x=pivot.index, y=pivot["week52_high"], name="52w high",
                   marker_color="#3fae5e")
        fig.add_bar(x=pivot.index, y=-pivot["week52_low"], name="52w low",
                   marker_color="#e05252")
        fig.update_layout(barmode="relative", height=280,
                          margin=dict(t=10, b=10, l=10, r=10))
        st.plotly_chart(fig, width='stretch')

    if not corr.empty:
        merged = corr["corroborations"].apply(
            lambda c: len(json.loads(c)) if isinstance(c, str) and c else 0
        ).sum()
        st.caption(f"Cross-feed dedup: {len(corr):,} distinct events tracked, "
                   f"{merged:,} corroborating duplicates collapsed in range.")

# -- Alerts ----------------------------------------------------------------
with tab_alerts:
    st.subheader(f"{len(filtered):,} alerts")
    if filtered.empty:
        st.caption("Nothing matches the current filters.")
    else:
        show = filtered.copy()
        show["time"] = show["local_time"].dt.strftime("%d-%b %H:%M:%S")
        # A vectorized dict lookup, not per-cell Styler.map: on 487+ rows the
        # Styler's HTML-per-cell rendering added a ~10s gap between the
        # script finishing and the table actually mounting in the browser.
        SEV_EMOJI = {"critical": "🔴 critical", "notable": "🟠 notable",
                    "info": "🔵 info"}
        show["severity"] = show["severity"].map(SEV_EMOJI).fillna(show["severity"])
        show = show[["time", "severity", "category", "entity", "kind",
                    "title", "body", "value"]]

        st.dataframe(show, width='stretch', height=560, hide_index=True)

# -- Fetcher health ----------------------------------------------------------
with tab_health:
    st.subheader("Fetcher health")
    if health.empty:
        st.caption("No fetcher has run yet.")
    else:
        show = health.copy()
        show["status"] = show["consecutive_failures"].apply(
            lambda n: "🔴 FAILING" if n > 0 else "🟢 OK")
        show["last_success_local"] = show["last_success_at"].apply(
            lambda dt: to_local(dt.to_pydatetime()).strftime("%d-%b %H:%M:%S")
            if pd.notna(dt) else "never")
        show = show[["status", "category", "last_success_local", "last_row_count",
                    "consecutive_failures", "total_success", "total_failure",
                    "last_elapsed_sec", "last_error"]]
        st.dataframe(show, width='stretch', hide_index=True, height=560)

# -- Large deals ---------------------------------------------------------
with tab_deals:
    st.subheader(f"{len(deals):,} bulk/block/short deals in range")
    if deals.empty:
        st.caption("No deals recorded in this range.")
    else:
        c1, c2 = st.columns([1, 2])
        with c1:
            by_type = deals.groupby("deal_type")["value_cr"].sum().reset_index()
            fig = px.pie(by_type, names="deal_type", values="value_cr", hole=0.5)
            fig.update_layout(height=280, margin=dict(t=10, b=10, l=10, r=10))
            st.plotly_chart(fig, width='stretch')
        with c2:
            top = (deals.groupby("symbol")["value_cr"].sum()
                  .sort_values(ascending=False).head(10)
                  .reset_index())
            fig = px.bar(top, x="value_cr", y="symbol", orientation="h",
                        title="Top symbols by deal value (Rs cr)")
            fig.update_layout(height=280, margin=dict(t=10, b=10, l=10, r=10),
                              yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig, width='stretch')

        st.dataframe(
            deals[["trade_date", "deal_type", "symbol", "client_name",
                  "buy_sell", "quantity", "price", "value_cr", "remarks"]],
            width='stretch', hide_index=True, height=420,
        )

# -- Market breadth ----------------------------------------------------------
with tab_breadth:
    st.subheader("Advance / decline")
    if breadth.empty:
        st.caption("No market-breadth observations in range.")
    else:
        breadth["local_time"] = pd.to_datetime(
            breadth["observed_at"], utc=True
        ).apply(lambda dt: to_local(dt.to_pydatetime()))
        fig = go.Figure()
        fig.add_scatter(x=breadth["local_time"], y=breadth["ad_ratio"],
                        mode="lines+markers", name="A/D ratio")
        fig.add_hline(y=1.0, line_dash="dot", line_color="gray")
        fig.update_layout(height=340, margin=dict(t=10, b=10, l=10, r=10),
                          yaxis_title="advances : declines")
        st.plotly_chart(fig, width='stretch')
        st.dataframe(breadth[["local_time", "advances", "declines", "ad_ratio"]],
                    width='stretch', hide_index=True, height=300)
