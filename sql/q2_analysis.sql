-- =====================================================================
-- Q2  BUSINESS INSIGHT QUERIES
-- Built on the Question #1 tables: click_funnel (1.1) and daily_campaign_report (1.2),
-- plus the raw tables where a check needs them.
-- Run after 01_run_all.sql on the same DuckDB connection (Option+X).
-- =====================================================================

-- ---------------------------------------------------------------------
-- SCENARIO  "Spend +40% in August, installs grew, new members flat"
-- Raw spend sums every pull of media_cost_daily, true spend keeps the latest pull.
-- ---------------------------------------------------------------------
WITH raw AS (
    SELECT strftime(report_date, '%Y-%m') AS month,
           SUM(CASE WHEN currency = 'USD' THEN cost * 35 ELSE cost END) AS spend_raw
    FROM media_cost_daily
    WHERE CAST(report_date AS DATE) BETWEEN (SELECT MIN(click_date) FROM click_funnel)
                                        AND (SELECT MAX(click_date) FROM click_funnel)
    GROUP BY 1
),
true_spend AS (
    SELECT strftime("Date", '%Y-%m') AS month, SUM("Cost (THB)") AS spend_true
    FROM daily_campaign_report GROUP BY 1
),
funnel AS (
    SELECT strftime(click_date, '%Y-%m') AS month,
           SUM(installed)                                                            AS installs,
           SUM(CASE WHEN media_source = 'ClickNova Network' THEN installed ELSE 0 END) AS installs_clicknova,
           SUM(registered_7d)                                                        AS members_attributed
    FROM click_funnel WHERE user_segment = 'NEW' GROUP BY 1
),
all_members AS (
    SELECT strftime(amaze_register_date, '%Y-%m') AS month, COUNT(*) AS members_all
    FROM user_profile WHERE member_type = 'Amaze Member' GROUP BY 1
)
SELECT r.month,
       ROUND(r.spend_raw)  AS spend_raw_thb,
       ROUND(t.spend_true) AS spend_true_thb,
       ROUND(r.spend_raw  / LAG(r.spend_raw)  OVER (ORDER BY r.month) - 1, 3) AS spend_raw_mom,
       ROUND(t.spend_true / LAG(t.spend_true) OVER (ORDER BY r.month) - 1, 3) AS spend_true_mom,
       f.installs, f.installs_clicknova, f.members_attributed, m.members_all
FROM raw r
JOIN true_spend t USING (month)
JOIN funnel f     USING (month)
JOIN all_members m USING (month)
ORDER BY r.month;

-- ---------------------------------------------------------------------
-- 2.1  Funnel for new users and existing members
-- ---------------------------------------------------------------------
SELECT user_segment,
       COUNT(*)                                        AS valid_clicks,
       SUM(app_open)                                   AS app_opens,      -- NEW = installs
       SUM(registered_7d)                              AS registered_7d,  -- NEW only
       SUM(transacted_7d)                              AS transacted_7d,
       ROUND(SUM(app_open) * 1.0 / COUNT(*), 3)        AS click_to_open,
       ROUND(SUM(registered_7d) * 1.0 / NULLIF(SUM(app_open), 0), 3) AS open_to_register,
       ROUND(SUM(transacted_7d) * 1.0 / NULLIF(SUM(app_open), 0), 3) AS open_to_transact
FROM click_funnel
GROUP BY user_segment
ORDER BY user_segment DESC;

-- 2.1b  What happens to NEW installers after the install (account status)
SELECT CASE WHEN f.registered_7d = 1          THEN '1 Registered 7D'
            WHEN u.member_type = 'Guest'      THEN '2 Guest checkout'
            WHEN f.user_id IS NULL            THEN '4 No account'
            ELSE                                   '3 Linked to another account' END AS account_status,
       COUNT(*)                                   AS installs,
       SUM(f.transacted_7d)                       AS transacted_7d,
       ROUND(AVG(f.transacted_7d), 3)             AS transact_rate
FROM click_funnel f
LEFT JOIN user_profile u ON u.user_id = f.user_id
WHERE f.user_segment = 'NEW' AND f.installed = 1
GROUP BY 1
ORDER BY 1;

-- ---------------------------------------------------------------------
-- 2.2  New users: STOP / CONTINUE by media source (look further than CPI)
-- Cost = UA campaign spend, outcomes = NEW-segment clicks of UA campaigns.
-- ---------------------------------------------------------------------
WITH cost AS (
    SELECT "Media Source" AS media_source, SUM("Cost (THB)") AS cost_thb
    FROM daily_campaign_report WHERE "Campaign Type" = 'UA' GROUP BY 1
),
outcome AS (
    SELECT media_source,
           COUNT(*)           AS valid_clicks,
           SUM(installed)     AS installs,
           SUM(registered_7d) AS members,
           SUM(transacted_7d) AS buyers,
           SUM(gmv_7d)        AS gmv_7d
    FROM click_funnel
    WHERE user_segment = 'NEW' AND campaign_type = 'UA'
    GROUP BY 1
)
SELECT media_source,
       ROUND(cost_thb)                                   AS cost_thb,
       installs, members, buyers,
       ROUND(installs * 1.0 / valid_clicks, 3)           AS install_rate,
       ROUND(cost_thb / NULLIF(installs, 0), 1)          AS cpi,
       ROUND(members * 1.0 / NULLIF(installs, 0), 3)     AS register_rate,
       ROUND(cost_thb / NULLIF(members, 0), 1)           AS cost_per_member,
       ROUND(cost_thb / NULLIF(buyers, 0), 1)            AS cost_per_buyer,
       ROUND(gmv_7d / NULLIF(cost_thb, 0), 2)            AS roas_7d
FROM cost JOIN outcome USING (media_source)
ORDER BY cost_per_member;

-- 2.2b  Same view by UA campaign
SELECT f.campaign_id,
       ROUND(c.cost_thb)                                 AS cost_thb,
       SUM(f.installed)                                  AS installs,
       SUM(f.registered_7d)                              AS members,
       ROUND(c.cost_thb / NULLIF(SUM(f.installed), 0), 1)     AS cpi,
       ROUND(c.cost_thb / NULLIF(SUM(f.registered_7d), 0), 1) AS cost_per_member,
       ROUND(SUM(f.gmv_7d) / NULLIF(c.cost_thb, 0), 2)        AS roas_7d
FROM click_funnel f
JOIN (SELECT "Campaign ID" AS campaign_id, SUM("Cost (THB)") AS cost_thb
      FROM daily_campaign_report GROUP BY 1) c USING (campaign_id)
WHERE f.user_segment = 'NEW' AND f.campaign_type = 'UA'
GROUP BY f.campaign_id, c.cost_thb
ORDER BY cost_per_member;

-- ---------------------------------------------------------------------
-- 2.3  The media source that does not look right: quality signals by source
-- ---------------------------------------------------------------------
WITH clk AS (
    SELECT media_source, AVG(is_blocked) AS blocked_share
    FROM (SELECT DISTINCT * FROM ad_click) a
    WHERE CAST(click_time AS DATE) BETWEEN (SELECT MIN(click_date) FROM click_funnel)
                                       AND (SELECT MAX(click_date) FROM click_funnel)
    GROUP BY 1
),
rep AS (
    SELECT "Media Source" AS media_source,
           SUM("Valid Clicks") * 1.0 / SUM("Clicks")                                AS valid_vs_reported,
           SUM(CASE WHEN "Impressions" > 0 THEN "Clicks" END) * 1.0 / SUM("Impressions") AS ctr
    FROM daily_campaign_report GROUP BY 1
),
ins AS (
    SELECT media_source,
           AVG(CASE WHEN epoch(app_open_time) - epoch(click_time) < 10    THEN 1 ELSE 0 END) AS install_under_10s,
           AVG(CASE WHEN epoch(app_open_time) - epoch(click_time) > 86400 THEN 1 ELSE 0 END) AS install_over_1d,
           AVG(registered_7d) AS register_rate,
           AVG(transacted_7d) AS transact_rate
    FROM click_funnel WHERE installed = 1 GROUP BY 1
),
ex AS (
    SELECT media_source, AVG(app_open) AS existing_open_rate
    FROM click_funnel WHERE user_segment = 'EXISTING' GROUP BY 1
)
SELECT media_source,
       ROUND(blocked_share, 3)       AS blocked_share,
       ROUND(valid_vs_reported, 3)   AS valid_vs_reported,
       ROUND(ctr, 4)                 AS ctr,
       ROUND(install_under_10s, 3)   AS install_under_10s,
       ROUND(install_over_1d, 3)     AS install_over_1d,
       ROUND(register_rate, 3)       AS register_rate,
       ROUND(transact_rate, 3)       AS transact_rate,
       ROUND(existing_open_rate, 3)  AS existing_open_rate
FROM clk
JOIN rep USING (media_source)
LEFT JOIN ins USING (media_source)
LEFT JOIN ex  USING (media_source)
ORDER BY blocked_share DESC;

-- 2.3b  Unattributed (organic) installs by install month: they fall when ClickNova starts
WITH installs AS (
    SELECT device_id, MIN(make_timestamp(event_ts_ms * 1000) + INTERVAL 7 HOUR) AS install_time
    FROM app_event WHERE event_name = 'first_open' GROUP BY 1
)
SELECT strftime(i.install_time, '%Y-%m')  AS install_month,
       COUNT(*)                           AS all_installs,
       COUNT(f.click_id)                  AS attributed_installs,
       COUNT(*) - COUNT(f.click_id)       AS unattributed_installs
FROM installs i
LEFT JOIN click_funnel f
  ON f.device_id = i.device_id AND f.installed = 1 AND f.app_open_time = i.install_time
WHERE CAST(i.install_time AS DATE) BETWEEN (SELECT MIN(click_date) FROM click_funnel)
                                       AND (SELECT MAX(click_date) FROM click_funnel)
GROUP BY 1 ORDER BY 1;

-- ---------------------------------------------------------------------
-- 2.4  Bringing existing members back to transact (RT campaigns)
-- One purchase can fall inside the 7D window of several clicks by the same member.
-- To compare channels, each transaction is credited ONCE, to the latest click
-- before it (same last-click idea as the app-open rule).
-- ---------------------------------------------------------------------
WITH txn_click AS (
    SELECT f.click_id, f.click_time, f.campaign_id, t.txn_id, t.activity, t.gmv_thb
    FROM click_funnel f
    JOIN txn_event t
      ON t.user_id  = f.user_id
     AND t.txn_time >= f.app_open_time
     AND t.txn_time <= f.app_open_time + INTERVAL 7 DAY
    WHERE f.user_segment = 'EXISTING' AND f.app_open = 1
),
credited AS (
    SELECT * FROM (
        SELECT tc.*, ROW_NUMBER() OVER (PARTITION BY txn_id ORDER BY click_time DESC, click_id DESC) AS rn
        FROM txn_click tc
    ) x WHERE rn = 1
),
per_campaign AS (
    SELECT campaign_id,
           COUNT(DISTINCT click_id)                                         AS buyers,
           SUM(CASE WHEN activity = 'purchase' THEN gmv_thb ELSE 0 END)     AS gmv_7d
    FROM credited GROUP BY 1
),
clicks AS (
    SELECT campaign_id, MAX(media_source) AS media_source,
           COUNT(*) AS valid_clicks, SUM(app_open) AS app_opens
    FROM click_funnel WHERE user_segment = 'EXISTING' AND campaign_type = 'RT' GROUP BY 1
),
cost AS (
    SELECT "Campaign ID" AS campaign_id, SUM("Cost (THB)") AS cost_thb
    FROM daily_campaign_report GROUP BY 1
)
SELECT c.campaign_id, c.media_source,
       ROUND(k.cost_thb)                                   AS cost_thb,
       c.valid_clicks, c.app_opens, p.buyers,
       ROUND(c.app_opens * 1.0 / c.valid_clicks, 3)        AS open_rate,
       ROUND(p.buyers * 1.0 / NULLIF(c.app_opens, 0), 3)   AS buyer_rate,
       ROUND(p.gmv_7d)                                     AS gmv_7d,
       ROUND(k.cost_thb / NULLIF(p.buyers, 0), 1)          AS cost_per_buyer,
       ROUND(p.gmv_7d / NULLIF(k.cost_thb, 0), 2)          AS roas_7d
FROM clicks c
JOIN cost k USING (campaign_id)
LEFT JOIN per_campaign p USING (campaign_id)
ORDER BY cost_per_buyer;

-- ---------------------------------------------------------------------
-- 2.5  Who transacts: buyer rate after the app open by profile
-- ---------------------------------------------------------------------
WITH opened AS (
    SELECT f.user_segment, f.platform, f.transacted_7d,
           COALESCE(u.gender, 'Unknown') AS gender,
           COALESCE(u.region, 'Unknown') AS region,
           CASE WHEN u.birth_year IS NULL THEN 'Unknown'
                WHEN YEAR(f.click_time) - u.birth_year < 25 THEN '18-24'
                WHEN YEAR(f.click_time) - u.birth_year < 35 THEN '25-34'
                WHEN YEAR(f.click_time) - u.birth_year < 45 THEN '35-44'
                WHEN YEAR(f.click_time) - u.birth_year < 55 THEN '45-54'
                ELSE '55+' END AS age_band
    FROM click_funnel f
    LEFT JOIN user_profile u ON u.user_id = f.user_id
    WHERE f.app_open = 1
),
long AS (
    SELECT user_segment, 'Age'      AS dimension, age_band AS grp, transacted_7d FROM opened
    UNION ALL SELECT user_segment, 'Gender',   gender,   transacted_7d FROM opened
    UNION ALL SELECT user_segment, 'Region',   region,   transacted_7d FROM opened
    UNION ALL SELECT user_segment, 'Platform', platform, transacted_7d FROM opened
)
SELECT user_segment, dimension, grp AS "group",
       COUNT(*)                         AS app_openers,
       SUM(transacted_7d)               AS buyers,
       ROUND(AVG(transacted_7d), 3)     AS buyer_rate,
       ROUND(SUM(transacted_7d) * 1.0 / SUM(SUM(transacted_7d)) OVER (PARTITION BY user_segment, dimension), 3) AS share_of_buyers
FROM long
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 2, 3;

-- 2.5b  Existing members: buyer rate by purchases in the 90 days before the click
WITH ex AS (
    SELECT f.click_id, f.transacted_7d,
           (SELECT COUNT(*) FROM txn_event t
             WHERE t.user_id = f.user_id
               AND t.txn_time <  f.click_time
               AND t.txn_time >= f.click_time - INTERVAL 90 DAY) AS txns_prior_90d
    FROM click_funnel f
    WHERE f.user_segment = 'EXISTING' AND f.app_open = 1
)
SELECT CASE WHEN txns_prior_90d = 0 THEN '0'
            WHEN txns_prior_90d <= 2 THEN '1-2'
            WHEN txns_prior_90d <= 5 THEN '3-5'
            ELSE '6+' END                AS txns_prior_90d,
       COUNT(*)                         AS app_openers,
       ROUND(AVG(transacted_7d), 3)     AS buyer_rate
FROM ex
GROUP BY 1 ORDER BY 1;
