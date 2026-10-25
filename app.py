"""Amaze growth dashboard (Q3 2026).

Loads the raw CSV files in ./data, builds the click funnel and the daily campaign report
with the SQL in ./sql (Question 1), and turns them into a business dashboard with filters.
"""
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

BASE = Path(__file__).parent
DATA, SQL = BASE / "data", BASE / "sql"

ACCENT, INK, MUTED, BAD, GOOD, LIGHT = "#E8590C", "#1F2328", "#9AA1AB", "#C2410C", "#15803D", "#D0D4DA"

st.set_page_config(page_title="Amaze growth dashboard", layout="wide")

# ----------------------------------------------------------------------------
# Build the data once
# ----------------------------------------------------------------------------
SCHEMA = {
    "ad_click": {"click_id": "VARCHAR", "click_time": "TIMESTAMP", "device_id": "VARCHAR", "platform": "VARCHAR",
                 "media_source": "VARCHAR", "campaign_id": "VARCHAR", "is_blocked": "INTEGER"},
    "app_event": {"event_id": "BIGINT", "event_ts_ms": "BIGINT", "device_id": "VARCHAR", "platform": "VARCHAR",
                  "event_name": "VARCHAR"},
    "campaign_master": {"campaign_id": "VARCHAR", "campaign_name": "VARCHAR", "media_source": "VARCHAR",
                        "campaign_type": "VARCHAR", "platform": "VARCHAR", "launch_date": "DATE"},
    "device_user_map": {"device_id": "VARCHAR", "user_id": "BIGINT", "first_linked_at": "TIMESTAMP"},
    "media_cost_daily": {"report_date": "DATE", "media_source": "VARCHAR", "campaign_id": "VARCHAR",
                         "impressions": "BIGINT", "clicks_reported": "BIGINT", "cost": "DOUBLE",
                         "currency": "VARCHAR", "pulled_at": "TIMESTAMP"},
    "txn_event": {"txn_id": "BIGINT", "user_id": "BIGINT", "txn_time": "TIMESTAMP", "activity": "VARCHAR",
                  "bu_lv1": "VARCHAR", "gmv_thb": "DOUBLE"},
    "user_profile": {"user_id": "BIGINT", "member_type": "VARCHAR", "amaze_register_date": "TIMESTAMP",
                     "gender": "VARCHAR", "birth_year": "INTEGER", "region": "VARCHAR"},
}

ENRICH = """
CREATE OR REPLACE TABLE click_enriched AS
WITH credit AS (   -- each win-back purchase credited once, to the latest click before it
    SELECT click_id, COUNT(*) AS credited_txns,
           SUM(CASE WHEN activity = 'purchase' THEN gmv_thb ELSE 0 END) AS credited_gmv
    FROM (
        SELECT f.click_id, t.txn_id, t.activity, t.gmv_thb,
               ROW_NUMBER() OVER (PARTITION BY t.txn_id ORDER BY f.click_time DESC, f.click_id DESC) AS rn
        FROM click_funnel f
        JOIN txn_event t ON t.user_id = f.user_id
         AND t.txn_time BETWEEN f.app_open_time AND f.app_open_time + INTERVAL 7 DAY
        WHERE f.user_segment = 'EXISTING' AND f.app_open = 1
    ) x WHERE rn = 1 GROUP BY click_id
)
SELECT f.*,
       COALESCE(u.gender, 'Unknown') AS gender,
       COALESCE(u.region, 'Unknown') AS region,
       CASE WHEN u.birth_year IS NULL THEN 'Unknown'
            WHEN YEAR(f.click_time) - u.birth_year < 25 THEN '18-24'
            WHEN YEAR(f.click_time) - u.birth_year < 35 THEN '25-34'
            WHEN YEAR(f.click_time) - u.birth_year < 45 THEN '35-44'
            WHEN YEAR(f.click_time) - u.birth_year < 55 THEN '45-54' ELSE '55+' END AS age_band,
       CASE WHEN f.user_segment <> 'NEW' OR f.installed = 0 THEN NULL
            WHEN f.registered_7d = 1 THEN 'Registered as a member'
            WHEN u.member_type = 'Guest' THEN 'Checked out as a guest'
            WHEN f.user_id IS NULL THEN 'No account'
            ELSE 'Linked to another account' END AS account_status,
       CASE WHEN f.user_segment = 'EXISTING' AND f.app_open = 1 THEN
            (SELECT COUNT(*) FROM txn_event t WHERE t.user_id = f.user_id AND t.txn_time < f.click_time
               AND t.txn_time >= f.click_time - INTERVAL 90 DAY) END AS purchases_prior_90d,
       epoch(f.app_open_time) - epoch(f.click_time) AS seconds_to_open,
       CASE WHEN f.user_segment = 'EXISTING' THEN CASE WHEN c.credited_txns > 0 THEN 1 ELSE 0 END END AS winback_buyer,
       CASE WHEN f.user_segment = 'EXISTING' THEN COALESCE(c.credited_gmv, 0) ELSE f.gmv_7d END AS gmv_credited
FROM click_funnel f
LEFT JOIN user_profile u ON u.user_id = f.user_id
LEFT JOIN credit c ON c.click_id = f.click_id;

CREATE OR REPLACE TABLE clicks_all AS        -- every de-duplicated click, incl. blocked, for traffic quality
SELECT CAST(click_time AS DATE) AS date, media_source, campaign_id, is_blocked, device_id IS NULL AS no_device
FROM (SELECT DISTINCT * FROM ad_click) a
WHERE CAST(click_time AS DATE) BETWEEN (SELECT MIN(click_date) FROM click_funnel) AND (SELECT MAX(click_date) FROM click_funnel);

CREATE OR REPLACE TABLE spend_raw AS          -- cost as loaded, before removing re-pulled rows
SELECT report_date AS date, media_source, campaign_id,
       SUM(CASE WHEN currency = 'USD' THEN cost * 35 ELSE cost END) AS spend_raw
FROM media_cost_daily
WHERE report_date BETWEEN (SELECT MIN(click_date) FROM click_funnel) AND (SELECT MAX(click_date) FROM click_funnel)
GROUP BY ALL;

CREATE OR REPLACE TABLE installs_all AS       -- every first install, credited to an ad or not
WITH i AS (SELECT device_id, MIN(make_timestamp(event_ts_ms * 1000) + INTERVAL 7 HOUR) AS t
           FROM app_event WHERE event_name = 'first_open' GROUP BY 1)
SELECT CAST(i.t AS DATE) AS date, CASE WHEN f.click_id IS NULL THEN 0 ELSE 1 END AS attributed
FROM i LEFT JOIN click_funnel f ON f.device_id = i.device_id AND f.installed = 1 AND f.app_open_time = i.t
WHERE CAST(i.t AS DATE) BETWEEN (SELECT MIN(click_date) FROM click_funnel) AND (SELECT MAX(click_date) FROM click_funnel);
"""


@st.cache_resource(show_spinner="Building the click funnel from raw data…")
def load():
    con = duckdb.connect()
    for table, cols in SCHEMA.items():
        col_sql = ", ".join(f"'{c}': '{t}'" for c, t in cols.items())
        con.execute(f"CREATE TABLE {table} AS SELECT * FROM read_csv('{(DATA / f'{table}.csv').as_posix()}', "
                    f"header = true, columns = {{{col_sql}}})")
    con.execute((SQL / "q1_1_click_funnel.sql").read_text())
    con.execute((SQL / "q1_2_daily_report.sql").read_text())
    con.execute(ENRICH)
    out = {
        "cf": con.execute("SELECT * FROM click_enriched").df(),
        "dr": con.execute("SELECT * FROM daily_campaign_report").df(),
        "clicks": con.execute("SELECT * FROM clicks_all").df(),
        "raw": con.execute("SELECT * FROM spend_raw").df(),
        "inst": con.execute("SELECT * FROM installs_all").df(),
    }
    out["cf"]["date"] = pd.to_datetime(out["cf"]["click_date"])
    out["dr"]["date"] = pd.to_datetime(out["dr"]["Date"])
    for k in ["clicks", "raw", "inst"]:
        out[k]["date"] = pd.to_datetime(out[k]["date"])
    return out


D = load()


def safe_div(a, b):
    return a / b if b else float("nan")


def fmt_int(x):
    return "–" if pd.isna(x) else f"{x:,.0f}"


def fmt_pct(x, d=0):
    return "–" if pd.isna(x) else f"{x * 100:.{d}f}%"


def fmt_k(x):
    return "–" if pd.isna(x) else (f"{x / 1e6:,.2f}M" if abs(x) >= 1e6 else f"{x / 1e3:,.1f}K")


def style(fig, h=340):
    fig.update_layout(height=h, margin=dict(l=8, r=8, t=8, b=8), plot_bgcolor="white", paper_bgcolor="white",
                      legend=dict(orientation="h", y=-0.18), font=dict(family="Poppins, sans-serif", color=INK))
    fig.update_yaxes(gridcolor="#EEF0F2", zeroline=False)
    return fig


# ----------------------------------------------------------------------------
# Filters
# ----------------------------------------------------------------------------
st.sidebar.header("Filters")
dmin, dmax = D["cf"]["date"].min().date(), D["cf"]["date"].max().date()
rng = st.sidebar.date_input("Click date", (dmin, dmax), min_value=dmin, max_value=dmax)
start, end = rng if isinstance(rng, (list, tuple)) and len(rng) == 2 else (dmin, dmax)
sources_all = sorted(D["dr"]["Media Source"].unique())
sources = st.sidebar.multiselect("Media source", sources_all, default=sources_all)
ctypes = st.sidebar.multiselect("Campaign type", ["UA", "RT"], default=["UA", "RT"],
                                help="UA = new-user acquisition, RT = retargeting existing members")
grain = st.sidebar.radio("Trend by", ["Week", "Month"], horizontal=True)
st.sidebar.caption("Every number recomputes from the raw data with these filters. Spend in THB (1 USD = 35 THB).")

s_, e_ = pd.Timestamp(start), pd.Timestamp(end)
cf = D["cf"][D["cf"].date.between(s_, e_) & D["cf"].media_source.isin(sources) & D["cf"].campaign_type.isin(ctypes)]
dr = D["dr"][D["dr"].date.between(s_, e_) & D["dr"]["Media Source"].isin(sources) & D["dr"]["Campaign Type"].isin(ctypes)]
clk = D["clicks"][D["clicks"].date.between(s_, e_) & D["clicks"].media_source.isin(sources)]
new, ex = cf[cf.user_segment == "NEW"], cf[cf.user_segment == "EXISTING"]


def period(df):
    return df["date"].dt.to_period("W-SUN" if grain == "Week" else "M").dt.start_time


def metrics(cf_, dr_):
    n_, e_ = cf_[cf_.user_segment == "NEW"], cf_[cf_.user_segment == "EXISTING"]
    spend = dr_["Cost (THB)"].sum()
    ua_spend = dr_.loc[dr_["Campaign Type"] == "UA", "Cost (THB)"].sum()
    gmv = cf_.gmv_credited.sum()
    return {"spend": spend, "installs": n_.installed.sum(), "members": n_.registered_7d.sum(),
            "cpm": safe_div(ua_spend, n_[n_.campaign_type == "UA"].registered_7d.sum()),
            "winback": e_.winback_buyer.sum(), "gmv": gmv, "roas": safe_div(gmv, spend),
            "signup": safe_div(n_.registered_7d.sum(), n_.installed.sum())}


# ----------------------------------------------------------------------------
# Header + KPIs
# ----------------------------------------------------------------------------
st.title("Amaze growth dashboard")
st.caption(f"{start:%d %b %Y} – {end:%d %b %Y} · click, app, member, transaction and media cost data")

m = metrics(cf, dr)
months = sorted(cf.date.dt.to_period("M").unique())
cur = prev = None
if len(months) >= 2:
    cur = metrics(cf[cf.date.dt.to_period("M") == months[-1]], dr[dr.date.dt.to_period("M") == months[-1]])
    prev = metrics(cf[cf.date.dt.to_period("M") == months[-2]], dr[dr.date.dt.to_period("M") == months[-2]])


def delta(key):
    if prev is None or pd.isna(prev[key]) or not prev[key] or pd.isna(cur[key]):
        return None
    return f"{(cur[key] / prev[key] - 1) * 100:+.0f}% {months[-1].strftime('%b')} vs {months[-2].strftime('%b')}"


k = st.columns(6)
k[0].metric("Media spend (THB)", fmt_k(m["spend"]), delta("spend"), delta_color="off")
k[1].metric("New installs", fmt_int(m["installs"]), delta("installs"))
k[2].metric("New members", fmt_int(m["members"]), delta("members"))
k[3].metric("Cost per new member (THB)", fmt_int(m["cpm"]), delta("cpm"), delta_color="inverse",
            help="UA spend ÷ new users who registered within 7 days of installing")
k[4].metric("Members won back", fmt_int(m["winback"]), delta("winback"),
            help="Existing members who bought within 7 days of a retargeting click (each purchase counted once)")
k[5].metric("ROAS, 7-day GMV", "–" if pd.isna(m["roas"]) else f"{m['roas']:.2f}", delta("roas"))

tabs = st.tabs(["Overview", "Acquisition", "Win-back", "Customers", "Traffic quality", "Campaign report", "Plan"])

# ----------------------------------------------------------------------------
# Overview
# ----------------------------------------------------------------------------
with tabs[0]:
    c1, c2 = st.columns([3, 2])
    t = pd.DataFrame({
        "spend_ua": dr[dr["Campaign Type"] == "UA"].groupby(period(dr[dr["Campaign Type"] == "UA"]))["Cost (THB)"].sum(),
        "spend_rt": dr[dr["Campaign Type"] == "RT"].groupby(period(dr[dr["Campaign Type"] == "RT"]))["Cost (THB)"].sum(),
        "installs": new.groupby(period(new)).installed.sum(),
        "members": new.groupby(period(new)).registered_7d.sum(),
    }).fillna(0)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_bar(x=t.index, y=t.spend_ua, name="UA spend", marker_color=MUTED)
    fig.add_bar(x=t.index, y=t.spend_rt, name="Retargeting spend", marker_color=LIGHT)
    fig.add_scatter(x=t.index, y=t.installs, name="New installs", mode="lines+markers",
                    line=dict(color=INK, width=2), secondary_y=True)
    fig.add_scatter(x=t.index, y=t.members, name="New members", mode="lines+markers",
                    line=dict(color=ACCENT, width=3), secondary_y=True)
    fig.update_layout(barmode="stack")
    fig.update_yaxes(title_text="Spend, THB", secondary_y=False)
    fig.update_yaxes(title_text="People", secondary_y=True, showgrid=False, rangemode="tozero", tickformat=",.0f")
    fig.update_yaxes(rangemode="tozero", tickformat=",.0f", secondary_y=False)
    c1.markdown(f"**Spend, installs and new members by {grain.lower()}**")
    c1.plotly_chart(style(fig, 380), width="stretch")
    if grain == "Week":
        c1.caption("Weeks start on Monday. The first and last week of the date range can be partial.")

    by_src = (new[new.campaign_type == "UA"].groupby("media_source")
              .agg(installs=("installed", "sum"), members=("registered_7d", "sum")))
    by_src["spend"] = dr[dr["Campaign Type"] == "UA"].groupby("Media Source")["Cost (THB)"].sum()
    by_src["cpm"] = by_src.spend / by_src.members.where(by_src.members > 0)
    by_src = by_src[by_src.members >= 20].dropna(subset=["cpm"]).sort_values("cpm")
    no_acct = new[new.account_status == "No account"]
    blocked = clk.groupby("media_source").is_blocked.mean().sort_values(ascending=False)
    rt_by = ex.groupby("media_source").winback_buyer.sum()
    notes = []
    if len(new):
        notes.append(f"**{fmt_pct(safe_div(new.installed.sum(), len(new)))}** of new-user clicks lead to an install, and "
                     f"**{fmt_pct(m['signup'])}** of installers register within 7 days.")
    if len(no_acct):
        notes.append(f"**{fmt_int(len(no_acct))}** installers never created an account; "
                     f"{fmt_pct(no_acct.transacted_7d.mean())} of them bought.")
    if len(by_src) >= 2:
        notes.append(f"Cheapest new members: **{by_src.index[0]}** at {fmt_int(by_src.cpm.iloc[0])} THB each. "
                     f"Most expensive: **{by_src.index[-1]}** at {fmt_int(by_src.cpm.iloc[-1])} THB.")
    if len(blocked) and blocked.iloc[0] > 0.05:
        nxt = blocked.iloc[1] if len(blocked) > 1 else float("nan")
        notes.append(f"**{blocked.index[0]}**: {fmt_pct(blocked.iloc[0])} of its clicks are blocked as fraud "
                     f"(next highest source: {fmt_pct(nxt, 1)}).")
    if rt_by.sum():
        notes.append(f"**{rt_by.idxmax()}** brings back {fmt_pct(rt_by.max() / rt_by.sum())} of members won back.")
    c2.markdown("**What the data says**")
    c2.markdown("\n".join(f"- {n}" for n in notes) if notes else "No data for these filters.")

    st.markdown("**Where people drop**")
    f1, f2 = st.columns(2)
    for col, title, steps in [
        (f1, "New users", [("Clicks", len(new)), ("Installed", new.installed.sum()),
                           ("Registered", new.registered_7d.sum()),
                           ("Registered and bought", int(((new.registered_7d == 1) & (new.transacted_7d == 1)).sum()))]),
        (f2, "Existing members", [("Clicks", len(ex)), ("Opened the app", ex.app_open.sum()),
                                  ("Bought", ex.transacted_7d.sum())]),
    ]:
        if steps[0][1] == 0:
            col.info(f"No {title.lower()} clicks for these filters.")
            continue
        losses = [steps[i - 1][1] - steps[i][1] for i in range(1, len(steps))]
        worst = 1 + losses.index(max(losses))
        fig = go.Figure(go.Funnel(
            y=[s[0] for s in steps], x=[s[1] for s in steps],
            texttemplate=["%{value:,.0f}"] + ["%{value:,.0f}<br>%{percentPrevious:.0%} of previous step"] * (len(steps) - 1),
            marker=dict(color=[MUTED] + [BAD if i == worst else INK for i in range(1, len(steps))]),
            connector=dict(line=dict(color=LIGHT))))
        col.markdown(f"*{title}*: biggest drop is **{steps[worst - 1][0].lower()} → {steps[worst][0].lower()}** "
                     f"({fmt_int(losses[worst - 1])} people)")
        col.plotly_chart(style(fig, 280), width="stretch")

# ----------------------------------------------------------------------------
# Acquisition
# ----------------------------------------------------------------------------
with tabs[1]:
    ua_new = new[new.campaign_type == "UA"]
    ua_dr = dr[dr["Campaign Type"] == "UA"]
    level = st.radio("Show by", ["Media source", "Campaign"], horizontal=True, key="acq_level")
    key_cf, key_dr = ("media_source", "Media Source") if level == "Media source" else ("campaign_id", "Campaign ID")
    a = ua_new.groupby(key_cf).agg(clicks=("click_id", "count"), installs=("installed", "sum"),
                                   members=("registered_7d", "sum"), buyers=("transacted_7d", "sum"),
                                   gmv=("gmv_7d", "sum"))
    a["spend"] = ua_dr.groupby(key_dr)["Cost (THB)"].sum()
    a = a.fillna(0)
    a["install_rate"] = a.installs / a.clicks
    a["cpi"] = a.spend / a.installs.where(a.installs > 0)
    a["signup_rate"] = a.members / a.installs.where(a.installs > 0)
    a["cost_per_member"] = a.spend / a.members.where(a.members > 0)
    a["cost_per_buyer"] = a.spend / a.buyers.where(a.buyers > 0)
    a["roas"] = a.gmv / a.spend.where(a.spend > 0)
    a = a.sort_values("cost_per_member")
    a[["spend", "gmv"]] = a[["spend", "gmv"]].round(0)
    st.markdown(f"**New-user acquisition by {level.lower()}**, sorted by cost per new member")
    st.dataframe(a[["spend", "installs", "install_rate", "cpi", "members", "signup_rate", "cost_per_member",
                    "buyers", "cost_per_buyer", "roas"]].reset_index(), hide_index=True, width="stretch",
                 column_config={key_cf: level,
                                "spend": st.column_config.NumberColumn("Spend", format="localized"),
                                "installs": "Installs",
                                "install_rate": st.column_config.NumberColumn("Install rate", format="percent"),
                                "cpi": st.column_config.NumberColumn("CPI", format="%.0f"),
                                "members": "New members",
                                "signup_rate": st.column_config.NumberColumn("Sign-up rate", format="percent"),
                                "cost_per_member": st.column_config.NumberColumn("Cost per member", format="%.0f"),
                                "buyers": "Buyers",
                                "cost_per_buyer": st.column_config.NumberColumn("Cost per buyer", format="%.0f"),
                                "roas": st.column_config.NumberColumn("ROAS 7D", format="%.2f")})
    p = a.dropna(subset=["cpi", "cost_per_member"]).reset_index()
    if len(p):
        fig = go.Figure(go.Scatter(x=p.cpi, y=p.cost_per_member, mode="markers+text", text=p[key_cf],
                                   textposition="top center",
                                   marker=dict(size=p.spend / p.spend.max() * 50 + 10, color=ACCENT, opacity=0.75)))
        fig.update_xaxes(title_text="Cost per install, THB")
        fig.update_yaxes(title_text="Cost per new member, THB")
        st.markdown("**A cheap install is not a cheap member** (bubble size = spend)")
        st.plotly_chart(style(fig, 420), width="stretch")

# ----------------------------------------------------------------------------
# Win-back
# ----------------------------------------------------------------------------
with tabs[2]:
    rt_ex = ex[ex.campaign_type == "RT"]
    rt_dr = dr[dr["Campaign Type"] == "RT"]
    w = rt_ex.groupby("campaign_id").agg(source=("media_source", "first"), clicks=("click_id", "count"),
                                         opens=("app_open", "sum"), buyers=("winback_buyer", "sum"),
                                         gmv=("gmv_credited", "sum"))
    w["spend"] = rt_dr.groupby("Campaign ID")["Cost (THB)"].sum()
    w = w.fillna(0)
    w["open_rate"] = w.opens / w.clicks
    w["buy_rate"] = w.buyers / w.opens.where(w.opens > 0)
    w["cost_per_buyer"] = w.spend / w.buyers.where(w.buyers > 0)
    w["roas"] = w.gmv / w.spend.where(w.spend > 0)
    w = w.sort_values("buyers", ascending=False)
    w[["spend", "gmv"]] = w[["spend", "gmv"]].round(0)
    c1, c2 = st.columns([3, 2])
    c1.markdown("**Retargeting campaigns** (each purchase credited once, to the latest click before it)")
    c1.dataframe(w.reset_index(), hide_index=True, width="stretch",
                 column_config={"campaign_id": "Campaign", "source": "Source", "clicks": "Clicks",
                                "opens": "App opens", "buyers": "Members won back",
                                "gmv": st.column_config.NumberColumn("GMV 7D", format="localized"),
                                "spend": st.column_config.NumberColumn("Spend", format="localized"),
                                "open_rate": st.column_config.NumberColumn("Open rate", format="percent"),
                                "buy_rate": st.column_config.NumberColumn("Buy rate", format="percent"),
                                "cost_per_buyer": st.column_config.NumberColumn("Cost per buyer", format="%.0f"),
                                "roas": st.column_config.NumberColumn("ROAS 7D", format="%.2f")})
    if w.buyers.sum():
        fig = go.Figure(go.Pie(labels=w.index, values=w.buyers, hole=0.55, sort=False,
                               marker=dict(colors=[ACCENT, INK, MUTED, LIGHT, "#5F6670"])))
        c2.markdown("**Share of members won back**")
        c2.plotly_chart(style(fig, 300), width="stretch")
    if len(rt_ex):
        tw = rt_ex.groupby([period(rt_ex), "campaign_id"]).winback_buyer.sum().unstack(fill_value=0)
        fig = go.Figure([go.Scatter(x=tw.index, y=tw[c], name=c, mode="lines+markers") for c in tw.columns])
        st.markdown(f"**Members won back by {grain.lower()}**")
        st.plotly_chart(style(fig, 300), width="stretch")

# ----------------------------------------------------------------------------
# Customers
# ----------------------------------------------------------------------------
with tabs[3]:
    c1, c2 = st.columns(2)
    acc = new[new.installed == 1].groupby("account_status").transacted_7d.agg(["mean", "count"]).sort_values("mean")
    if len(acc):
        fig = go.Figure(go.Bar(x=acc["mean"], y=acc.index, orientation="h",
                               marker_color=[BAD if v == 0 else INK for v in acc["mean"]],
                               text=[f"{fmt_pct(v)} of {fmt_int(n)}" for v, n in zip(acc["mean"], acc["count"])],
                               textposition="outside"))
        fig.update_xaxes(tickformat=".0%", range=[0, max(acc["mean"].max() * 1.35, 0.1)])
        c1.markdown("**New installers: share who bought in 7 days, by account**")
        c1.plotly_chart(style(fig, 300), width="stretch")
    eo = ex[ex.app_open == 1].copy()
    if len(eo):
        eo["bucket"] = pd.cut(eo.purchases_prior_90d, [-1, 0, 2, 5, 10_000],
                              labels=["None", "1 to 2", "3 to 5", "6 or more"])
        fr = eo.groupby("bucket", observed=True).transacted_7d.agg(["mean", "count"])
        fig = go.Figure(go.Bar(x=fr.index.astype(str), y=fr["mean"], marker_color=ACCENT,
                               text=[fmt_pct(v) for v in fr["mean"]], textposition="outside"))
        fig.update_yaxes(tickformat=".0%")
        c2.markdown("**Members: share who bought, by purchases in the 90 days before**")
        c2.plotly_chart(style(fig, 300), width="stretch")
    st.markdown("**Buy rate by profile**")
    p1, p2 = st.columns([1, 3])
    seg = p1.radio("Customers", ["New users", "Existing members"])
    dim = p1.selectbox("Split by", ["age_band", "gender", "region", "platform"],
                       format_func=lambda x: {"age_band": "Age", "gender": "Gender", "region": "Region",
                                              "platform": "Platform"}[x])
    base = new[new.installed == 1] if seg == "New users" else ex[ex.app_open == 1]
    pr = base.groupby(dim).transacted_7d.agg(["mean", "count"])
    if len(pr):
        fig = go.Figure(go.Bar(x=pr.index, y=pr["mean"],
                               marker_color=[MUTED if g == "Unknown" else INK for g in pr.index],
                               text=[f"{fmt_pct(v)}<br>n={fmt_int(n)}" for v, n in zip(pr["mean"], pr["count"])],
                               textposition="outside"))
        fig.update_yaxes(tickformat=".0%", range=[0, max(pr["mean"].max() * 1.3, 0.1)])
        p2.plotly_chart(style(fig, 320), width="stretch")
    st.caption("Buy rate = share who made any transaction within 7 days of opening the app. “Unknown” for new users is "
               "mostly installers with no account. Purchase history starts 24 Jun 2026.")

# ----------------------------------------------------------------------------
# Traffic quality
# ----------------------------------------------------------------------------
with tabs[4]:
    qd = clk.groupby("media_source").agg(clicks=("is_blocked", "size"), blocked=("is_blocked", "mean"),
                                         no_device=("no_device", "mean"))
    rep = dr.groupby("Media Source").agg(reported=("Clicks", "sum"), valid=("Valid Clicks", "sum"))
    qd["valid_vs_reported"] = rep.valid / rep.reported
    ins = cf[cf.installed == 1]
    g = ins.groupby("media_source")
    qd["installs_under_10s"] = g.seconds_to_open.apply(lambda s: (s < 10).mean())
    qd["installs_over_1_day"] = g.seconds_to_open.apply(lambda s: (s > 86400).mean())
    qd["signup_rate"] = g.registered_7d.mean()
    qd = qd.sort_values("blocked", ascending=False)
    st.markdown("**Fraud and quality signals by media source**")
    st.dataframe(qd.reset_index(), hide_index=True, width="stretch",
                 column_config={"media_source": "Source",
                                "clicks": st.column_config.NumberColumn("Clicks", format="localized"),
                                "blocked": st.column_config.NumberColumn("Blocked as fraud", format="percent"),
                                "no_device": st.column_config.NumberColumn("No device id", format="percent"),
                                "valid_vs_reported": st.column_config.NumberColumn("Valid ÷ network clicks", format="percent"),
                                "installs_under_10s": st.column_config.NumberColumn("Installs < 10 sec", format="percent"),
                                "installs_over_1_day": st.column_config.NumberColumn("Installs > 1 day", format="percent"),
                                "signup_rate": st.column_config.NumberColumn("Sign-up rate", format="percent")})
    st.caption("An install within seconds of a click is not humanly possible (click injection); a long tail of installs "
               "days later is typical of click spam. Both take credit for installs that would have happened anyway.")
    c1, c2 = st.columns(2)
    inst = D["inst"][D["inst"].date.between(s_, e_)]
    ti = (inst.groupby([period(inst), "attributed"]).size().unstack(fill_value=0)
          .rename(columns={0: "Not credited to an ad", 1: "Credited to an ad"}))
    fig = go.Figure([go.Bar(x=ti.index, y=ti[c], name=c, marker_color=(MUTED if c.startswith("Not") else INK))
                     for c in ti.columns])
    fig.update_layout(barmode="stack")
    c1.markdown(f"**All installs by {grain.lower()}** (all sources)")
    c1.plotly_chart(style(fig, 300), width="stretch")
    raw = D["raw"][D["raw"].date.between(s_, e_) & D["raw"].media_source.isin(sources)]
    ts = pd.DataFrame({"As loaded": raw.groupby(period(raw)).spend_raw.sum(),
                       "Duplicates removed": dr.groupby(period(dr))["Cost (THB)"].sum()}).fillna(0)
    fig = go.Figure([go.Bar(x=ts.index, y=ts[c], name=c, marker_color=(LIGHT if c == "As loaded" else INK))
                     for c in ts.columns])
    c2.markdown("**Spend as loaded vs after removing re-pulled cost rows**")
    c2.plotly_chart(style(fig, 300), width="stretch")
    st.caption("Social Ads cost rows for 18–31 Aug 2026 were loaded twice; the report keeps the latest pull only.")

# ----------------------------------------------------------------------------
# Campaign report
# ----------------------------------------------------------------------------
with tabs[5]:
    rep = dr.drop(columns=["date"]).sort_values(["Date", "Campaign ID"])
    roll = st.radio("Rows", ["Daily", "Total for the period"], horizontal=True)
    if roll.startswith("Total"):
        g = rep.groupby(["Campaign ID", "Media Source", "Campaign Type"], as_index=False)[
            ["Impressions", "Clicks", "Valid Clicks", "Installs (NEW)", "App Opens (EXISTING)", "Registered 7D",
             "Transacted 7D", "GMV 7D (THB)", "Cost (THB)"]].sum(min_count=1)
        g["CTR"] = g.Clicks / g.Impressions
        g["CPI"] = g["Cost (THB)"] / g["Installs (NEW)"].where(g["Installs (NEW)"] > 0)
        g["Cost per Member"] = g["Cost (THB)"] / g["Registered 7D"].where(g["Registered 7D"] > 0)
        g["ROAS"] = g["GMV 7D (THB)"] / g["Cost (THB)"].where(g["Cost (THB)"] > 0)
        rep = g
    st.caption(f"{len(rep):,} rows")
    st.dataframe(rep, hide_index=True, width="stretch", height=520,
                 column_config={"CTR": st.column_config.NumberColumn(format="percent"),
                                "Cost (THB)": st.column_config.NumberColumn(format="localized"),
                                "GMV 7D (THB)": st.column_config.NumberColumn(format="localized"),
                                "CPI": st.column_config.NumberColumn(format="%.2f"),
                                "Cost per Member": st.column_config.NumberColumn(format="%.2f"),
                                "ROAS": st.column_config.NumberColumn(format="%.2f")})
    d1, d2 = st.columns(2)
    d1.download_button("Download this report (CSV)", rep.to_csv(index=False).encode("utf-8"),
                       file_name="campaign_report.csv", mime="text/csv")
    d2.download_button("Download click-level funnel (CSV)", cf.drop(columns=["date"]).to_csv(index=False).encode("utf-8"),
                       file_name="click_funnel.csv", mime="text/csv")

# ----------------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------------
with tabs[6]:
    st.markdown("**Recommended changes for next month** (total budget unchanged; not affected by the filters)")
    alld, allcf = D["dr"], D["cf"]
    last30 = alld[alld.date > alld.date.max() - pd.Timedelta(days=30)].groupby("Campaign ID")["Cost (THB)"].sum()
    ua_members = allcf[(allcf.user_segment == "NEW") & (allcf.campaign_type == "UA")].groupby("campaign_id").registered_7d.sum()
    cpm = alld[alld["Campaign Type"] == "UA"].groupby("Campaign ID")["Cost (THB)"].sum() / ua_members
    stops = {"C501": "ClickNova: fraud signals", "C301": "Video UA: highest cost per member",
             "C202": "Social iOS Lookalike: weakest Social campaign"}
    adds = {"C401": (7000, "App Store Ads: high install and sign-up rate"),
            "C101": (7000, "Search Android Brand: cheapest Search member"),
            "C211": (5000, "Social DPA Android: cheapest paid win-back"),
            "C601": (3000, "CashBackr: cheapest member, keep capped")}
    plan = pd.DataFrame(
        [{"Change": "Stop", "Campaign": c, "Why": why, "THB per month": -last30.get(c, 0)} for c, why in stops.items()]
        + [{"Change": "Add", "Campaign": c, "Why": why, "THB per month": v} for c, (v, why) in adds.items()]
        + [{"Change": "Test", "Campaign": "C701, CRM hold-out", "Why": "Prove what is incremental", "THB per month": 5500}])
    est = sum(v / cpm[c] for c, (v, _) in adds.items() if c in cpm.index and cpm[c] > 0)
    c1, c2 = st.columns([3, 1])
    c1.dataframe(plan, hide_index=True, width="stretch",
                 column_config={"THB per month": st.column_config.NumberColumn(format="localized")})
    c2.metric("Extra new members a month", f"~{est:,.0f}",
              help="Added UA spend ÷ each campaign's cost per member so far. An estimate: cost per member usually rises with spend.")
    st.markdown("""
**Also, with no media cost**
- Show the sign-up offer on first open with a one-step sign-up, and offer membership to guest buyers at checkout.
- Check the CRM “Lapsed30D” audience rule and keep a hold-out group.
- Add a click-to-install-time rule to the fraud tool and ask ClickNova for a refund.

**Data to ask for**
- Raw attribution logs and the Play install referrer
- Hold-out results for CRM and Social DPA
- 30 and 90-day retention, repeat purchase and margin by source
- Sign-up funnel events and points redemption
- CashBackr payout terms and iOS SKAdNetwork data
""")
    st.caption("Stop amounts are the last 30 days of spend. Add amounts are a recommendation.")
