-- =====================================================================
-- Q1.2  DAILY CAMPAIGN FUNNEL REPORT  (date x campaign, same dates as the Click Funnel table)
-- Source: click_funnel (Q1.1) + media_cost_daily
-- Dialect: DuckDB / PostgreSQL-style SQL
-- =====================================================================
CREATE OR REPLACE TABLE daily_campaign_report AS
WITH
-- ---------------------------------------------------------------------
-- 0) Parameters: FX rate given in the question, report dates taken from
--    the Click Funnel table (1.1) instead of typed in.
-- ---------------------------------------------------------------------
params AS (
    SELECT 35.0            AS usd_to_thb,
           MIN(click_date) AS start_date,
           MAX(click_date) AS end_date
    FROM click_funnel
),
-- ---------------------------------------------------------------------
-- 1) Media cost: one row per (report_date, campaign_id)
--    * 56 date x campaign pairs were pulled twice (Social Ads, 18 Aug - 1 Sep)
--      -> keep the LATEST pull_at only (data dict: latest pull is final)
--    * cost in USD (Video Ads)   -> x 35 to THB
--    * impressions blank (CashBackr Affiliate does not report) -> CTR left NULL, not 0
-- ---------------------------------------------------------------------
cost_latest AS (
    SELECT t.*
    FROM (
        SELECT m.*,
               ROW_NUMBER() OVER (PARTITION BY report_date, campaign_id
                                  ORDER BY pulled_at DESC) AS rn
        FROM media_cost_daily m
    ) t
    CROSS JOIN params p
    WHERE rn = 1
      AND CAST(report_date AS DATE) BETWEEN p.start_date AND p.end_date
),
cost AS (
    SELECT CAST(report_date AS DATE) AS report_date,
           campaign_id,
           media_source,
           impressions,
           clicks_reported,
           CASE WHEN currency = 'USD' THEN cost * p.usd_to_thb ELSE cost END AS cost_thb
    FROM cost_latest
    CROSS JOIN params p
),
-- ---------------------------------------------------------------------
-- 2) Funnel counts from the Click Funnel table (by click date)
-- ---------------------------------------------------------------------
funnel AS (
    SELECT click_date                                                AS report_date,
           campaign_id,
           MAX(media_source)                                         AS media_source,
           MAX(campaign_type)                                        AS campaign_type,
           COUNT(*)                                                  AS valid_clicks,
           SUM(CASE WHEN user_segment = 'NEW'      THEN 1 ELSE 0 END) AS valid_clicks_new,
           SUM(CASE WHEN user_segment = 'EXISTING' THEN 1 ELSE 0 END) AS valid_clicks_existing,
           SUM(installed)                                            AS installs_new,
           SUM(CASE WHEN user_segment = 'EXISTING' THEN app_open ELSE 0 END) AS app_opens_existing,
           SUM(registered_7d)                                        AS registered_7d,
           SUM(transacted_7d)                                        AS transacted_7d,
           SUM(gmv_7d)                                               AS gmv_7d
    FROM click_funnel
    GROUP BY click_date, campaign_id
),
joined AS (
    SELECT COALESCE(c.report_date, f.report_date)   AS report_date,
           COALESCE(c.campaign_id, f.campaign_id)   AS campaign_id,
           COALESCE(c.media_source, f.media_source) AS media_source,
           COALESCE(cm.campaign_type, f.campaign_type, 'UA') AS campaign_type,
           c.impressions,
           COALESCE(c.clicks_reported, 0)           AS clicks_reported,
           COALESCE(f.valid_clicks, 0)              AS valid_clicks,
           COALESCE(f.valid_clicks_new, 0)          AS valid_clicks_new,
           COALESCE(f.valid_clicks_existing, 0)     AS valid_clicks_existing,
           COALESCE(f.installs_new, 0)              AS installs_new,
           COALESCE(f.app_opens_existing, 0)        AS app_opens_existing,
           COALESCE(f.registered_7d, 0)             AS registered_7d,
           COALESCE(f.transacted_7d, 0)             AS transacted_7d,
           COALESCE(f.gmv_7d, 0)                    AS gmv_7d,
           COALESCE(c.cost_thb, 0)                  AS cost_thb
    FROM cost c
    FULL OUTER JOIN funnel f
      ON f.report_date = c.report_date AND f.campaign_id = c.campaign_id
    LEFT JOIN campaign_master cm
      ON cm.campaign_id = COALESCE(c.campaign_id, f.campaign_id)
)
SELECT report_date                                         AS "Date",
       campaign_id                                         AS "Campaign ID",
       media_source                                        AS "Media Source",
       campaign_type                                       AS "Campaign Type",
       impressions                                         AS "Impressions",
       clicks_reported                                     AS "Clicks",
       valid_clicks                                        AS "Valid Clicks",
       installs_new                                        AS "Installs (NEW)",
       app_opens_existing                                  AS "App Opens (EXISTING)",
       registered_7d                                       AS "Registered 7D",
       transacted_7d                                       AS "Transacted 7D",
       ROUND(gmv_7d, 2)                                    AS "GMV 7D (THB)",
       ROUND(cost_thb, 2)                                  AS "Cost (THB)",
       -- NULLIF -> no divide-by-zero, blank impressions / zero cost give NULL, not 0
       clicks_reported * 1.0 / NULLIF(impressions, 0)      AS "CTR",
       ROUND(cost_thb / NULLIF(installs_new, 0), 2)        AS "CPI",
       ROUND(cost_thb / NULLIF(registered_7d, 0), 2)       AS "Cost per Member",
       ROUND(gmv_7d / NULLIF(cost_thb, 0), 2)              AS "ROAS"
FROM joined
ORDER BY report_date, campaign_id;

-- =====================================================================
-- Q1.2 RESULT in the "Example result" layout (all values from daily_campaign_report)
-- Example columns, plus Clicks, App Opens (EXISTING) and Transacted 7D asked for in point 1-2.
-- =====================================================================
CREATE OR REPLACE VIEW daily_campaign_report_result AS
SELECT "Date",
       "Campaign ID",
       "Media Source",
       "Impressions",
       "Clicks",
       "Valid Clicks",
       CAST("Installs (NEW)" AS INTEGER)                  AS "Installs",
       CAST("App Opens (EXISTING)" AS INTEGER)            AS "App Opens (EXISTING)",
       CAST("Registered 7D" AS INTEGER)                   AS "Registered 7D",
       CAST("Transacted 7D" AS INTEGER)                   AS "Transacted 7D",
       "Cost (THB)",
       CASE WHEN "CTR" IS NULL THEN NULL
            ELSE printf('%.2f%%', "CTR" * 100) END         AS "CTR",
       "CPI",
       "Cost per Member",
       "ROAS"
FROM daily_campaign_report;

SELECT * FROM daily_campaign_report_result ORDER BY "Date", "Campaign ID";
