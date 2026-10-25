# Amaze growth dashboard, Q3 2026

Streamlit app for the Commercial Business Analyst case. It loads the seven raw CSV files,
runs the Question 1 SQL (click funnel and daily campaign report) in DuckDB, and turns the
results into a business dashboard with filters. No number is typed in by hand; the only fixed
inputs are the FX rate given in the brief (1 USD = 35 THB) and the recommended budget moves on
the Plan tab.

## What is inside

| Path | What it is |
|---|---|
| `app.py` | The dashboard |
| `sql/q1_1_click_funnel.sql` | Q1.1 Click Funnel table (+ `click_funnel_result` view in the example layout) |
| `sql/q1_2_daily_report.sql` | Q1.2 Daily Campaign Funnel Report (+ `daily_campaign_report_result` view) |
| `sql/q2_analysis.sql` | The queries behind every Q2 number (run after Q1) |
| `data/*.csv` | The seven sheets of the case workbook |

## Run it on your computer

```bash
pip install -r requirements.txt
streamlit run app.py
```

It opens at http://localhost:8501.

## Deploy it (free, Streamlit Community Cloud)

1. Create a new GitHub repository (Private is fine) and upload every file and folder in this
   project, keeping the same structure (`app.py`, `requirements.txt`, `sql/`, `data/`, `.streamlit/`).
2. Go to https://share.streamlit.io and sign in with GitHub.
3. Click **Create app** → **Deploy a public app from GitHub** (or from a private repo).
4. Pick the repository, branch `main`, main file `app.py`, then **Deploy**.
5. After a minute you get a link like `https://<your-app>.streamlit.app` to send with your submission.
   For a private repository, open the app's **Share** settings and allow viewers by email or make it public.

## Tabs

Filters in the sidebar (click date, media source, campaign type, week or month) apply to every tab except Plan.

- **Overview**: headline KPIs with the change vs the previous month, spend vs installs vs new members over time, what the data says, and where people drop
- **Acquisition**: new-user sources or campaigns ranked by cost per new member, with CPI, sign-up rate, cost per buyer and ROAS
- **Win-back**: retargeting campaigns, members won back (each purchase credited once, to the latest click), cost per buyer
- **Customers**: who buys, by account status, purchase frequency and profile
- **Traffic quality**: fraud and quality signals by source, installs credited vs not credited to ads, spend before and after removing re-pulled cost rows
- **Campaign report**: the Q1.2 daily report (or totals for the period) with CSV download, plus the click-level funnel
- **Plan**: recommended budget moves for next month, estimated extra members, and data to ask for
