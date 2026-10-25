-- =====================================================================
-- Q1.1  CLICK FUNNEL  (one row per valid click, 1 Jul - 30 Sep 2026, Bangkok time)
-- Dialect: DuckDB / PostgreSQL-style SQL (window functions, INTERVAL arithmetic)
-- =====================================================================
CREATE OR REPLACE TABLE click_funnel AS
WITH
-- ---------------------------------------------------------------------
-- 1) Clean clicks
--    * ad_click has 367 exact duplicate rows (same click_id)  -> keep 1 per click_id
--    * is_blocked = 1 (fraud tool)                             -> not valid
--    * device_id blank (tracking not allowed)                  -> cannot be attributed -> not valid
--    * C701 is missing from campaign_master                    -> keep, campaign_type inferred = 'UA'
--      (Social Ads, launched 5 Sep, ~80% of its clickers are non-members, like other UA campaigns)
--    NOTE: attribution competition (last click) is run on ALL clean clicks, incl. dates
--          outside the reporting window, so a 30 Jun / 1 Oct click can still win credit.
-- ---------------------------------------------------------------------
clicks_dedup AS (
    SELECT *
    FROM (
        SELECT a.*,
               ROW_NUMBER() OVER (PARTITION BY click_id ORDER BY click_time) AS rn
        FROM ad_click a
    ) t
    WHERE rn = 1
),
clicks AS (
    SELECT c.click_id,
           c.click_time,
           c.device_id,
           c.platform,
           c.media_source,
           c.campaign_id,
           COALESCE(cm.campaign_type, 'UA')            AS campaign_type,   -- C701 inferred
           CASE WHEN cm.campaign_id IS NULL THEN 1 ELSE 0 END AS campaign_type_inferred
    FROM clicks_dedup c
    LEFT JOIN campaign_master cm ON cm.campaign_id = c.campaign_id
    WHERE c.is_blocked = 0
      AND c.device_id IS NOT NULL
),
-- ---------------------------------------------------------------------
-- 2) Clean app events
--    * event_ts_ms is epoch ms in UTC  -> convert to Bangkok time to match click/txn tables
--    * 280 duplicated event_id rows    -> DISTINCT
--    * 158 devices have >1 first_open (re-installs) -> install = FIRST first_open of the device
-- ---------------------------------------------------------------------
events AS (
    SELECT DISTINCT
           event_id,
           -- epoch ms (UTC) -> Bangkok time. Thailand has no daylight saving, so +7h is exact
           -- and needs no time-zone extension.
           make_timestamp(event_ts_ms * 1000) + INTERVAL 7 HOUR AS event_time,
           device_id,
           event_name
    FROM app_event
),
installs AS (
    SELECT device_id, event_id AS install_event_id, event_time AS install_time
    FROM (
        SELECT e.*, ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY event_time, event_id) AS rn
        FROM events e
        WHERE event_name = 'first_open'
    ) t
    WHERE rn = 1
),
-- ---------------------------------------------------------------------
-- 3) Who is the user behind the click?
--    device_user_map can hold >1 user per device (shared / handed-down phones).
--    user = latest user linked to the device AT OR BEFORE the click,
--    if none yet (brand-new device) -> the first user linked AFTER the click.
-- ---------------------------------------------------------------------
user_before AS (
    SELECT click_id, user_id
    FROM (
        SELECT c.click_id, m.user_id,
               ROW_NUMBER() OVER (PARTITION BY c.click_id ORDER BY m.first_linked_at DESC) AS rn
        FROM clicks c
        JOIN device_user_map m
          ON m.device_id = c.device_id AND m.first_linked_at <= c.click_time
    ) t WHERE rn = 1
),
user_after AS (
    SELECT click_id, user_id
    FROM (
        SELECT c.click_id, m.user_id,
               ROW_NUMBER() OVER (PARTITION BY c.click_id ORDER BY m.first_linked_at ASC) AS rn
        FROM clicks c
        JOIN device_user_map m
          ON m.device_id = c.device_id AND m.first_linked_at > c.click_time
    ) t WHERE rn = 1
),
clicks_user AS (
    SELECT c.*,
           COALESCE(ub.user_id, ua.user_id) AS click_user_id,
           CASE WHEN up.member_type = 'Amaze Member'
                 AND up.amaze_register_date <= c.click_time
                THEN 'EXISTING' ELSE 'NEW' END AS user_segment
    FROM clicks c
    LEFT JOIN user_before  ub ON ub.click_id = c.click_id
    LEFT JOIN user_after   ua ON ua.click_id = c.click_id
    LEFT JOIN user_profile up ON up.user_id  = COALESCE(ub.user_id, ua.user_id)
),
-- ---------------------------------------------------------------------
-- 4) Candidate app open per click
--    NEW      : device's first first_open within 7 days after the click
--    EXISTING : first app open (session_start or first_open) within 24 hours after the click
-- ---------------------------------------------------------------------
cand_new AS (
    SELECT cu.click_id, i.install_event_id AS open_event_id, i.install_time AS app_open_time
    FROM clicks_user cu
    JOIN installs i
      ON i.device_id = cu.device_id
     AND i.install_time >= cu.click_time
     AND i.install_time <= cu.click_time + INTERVAL 7 DAY
    WHERE cu.user_segment = 'NEW'
),
cand_existing AS (
    SELECT click_id, open_event_id, app_open_time
    FROM (
        SELECT cu.click_id, e.event_id AS open_event_id, e.event_time AS app_open_time,
               ROW_NUMBER() OVER (PARTITION BY cu.click_id ORDER BY e.event_time, e.event_id) AS rn
        FROM clicks_user cu
        JOIN events e
          ON e.device_id = cu.device_id
         AND e.event_time >= cu.click_time
         AND e.event_time <= cu.click_time + INTERVAL 24 HOUR
        WHERE cu.user_segment = 'EXISTING'
    ) t WHERE rn = 1
),
cand AS (
    SELECT * FROM cand_new
    UNION ALL
    SELECT * FROM cand_existing
),
-- ---------------------------------------------------------------------
-- 5) Last-click rule: if several clicks lead to the same app open,
--    only the latest click gets the credit.
-- ---------------------------------------------------------------------
attributed AS (
    SELECT click_id, open_event_id, app_open_time
    FROM (
        SELECT ca.*, cu.click_time,
               ROW_NUMBER() OVER (PARTITION BY ca.open_event_id
                                  ORDER BY cu.click_time DESC, ca.click_id DESC) AS rn
        FROM cand ca
        JOIN clicks_user cu ON cu.click_id = ca.click_id
    ) t WHERE rn = 1
),
-- ---------------------------------------------------------------------
-- 6) Registration (NEW only): a member linked to the device registers
--    within 7 days after the install.
-- ---------------------------------------------------------------------
registered AS (
    SELECT click_id, user_id AS reg_user_id, amaze_register_date
    FROM (
        SELECT cu.click_id, up.user_id, up.amaze_register_date,
               ROW_NUMBER() OVER (PARTITION BY cu.click_id ORDER BY up.amaze_register_date) AS rn
        FROM clicks_user cu
        JOIN attributed a      ON a.click_id  = cu.click_id
        JOIN device_user_map m ON m.device_id = cu.device_id
        JOIN user_profile up   ON up.user_id  = m.user_id
        WHERE cu.user_segment = 'NEW'
          AND up.member_type = 'Amaze Member'
          AND up.amaze_register_date >= a.app_open_time
          AND up.amaze_register_date <= a.app_open_time + INTERVAL 7 DAY
    ) t WHERE rn = 1
),
base AS (
    SELECT cu.*,
           a.open_event_id,
           a.app_open_time,
           r.reg_user_id,
           COALESCE(r.reg_user_id, cu.click_user_id) AS user_id
    FROM clicks_user cu
    LEFT JOIN attributed a ON a.click_id = cu.click_id
    LEFT JOIN registered r ON r.click_id = cu.click_id
),
-- ---------------------------------------------------------------------
-- 7) Transactions within 7 days after the app open
--    Transacted = any txn (purchase / reward / point_ex), GMV = purchase only
-- ---------------------------------------------------------------------
txn7 AS (
    SELECT b.click_id,
           COUNT(*)                                                         AS txn_cnt_7d,
           SUM(CASE WHEN t.activity = 'purchase' THEN 1 ELSE 0 END)         AS purchase_cnt_7d,
           SUM(CASE WHEN t.activity = 'purchase' THEN t.gmv_thb ELSE 0 END) AS gmv_7d
    FROM base b
    JOIN txn_event t
      ON t.user_id  = b.user_id
     AND t.txn_time >= b.app_open_time
     AND t.txn_time <= b.app_open_time + INTERVAL 7 DAY
    WHERE b.app_open_time IS NOT NULL
    GROUP BY b.click_id
)
SELECT b.click_id,
       b.click_time,
       CAST(b.click_time AS DATE)                                  AS click_date,
       b.device_id,
       b.user_id,
       b.platform,
       b.media_source,
       b.campaign_id,
       b.campaign_type,
       b.campaign_type_inferred,
       b.user_segment,
       b.app_open_time,
       CASE WHEN b.app_open_time IS NOT NULL THEN 1 ELSE 0 END     AS app_open,
       CASE WHEN b.user_segment = 'NEW' AND b.app_open_time IS NOT NULL THEN 1 ELSE 0 END AS installed,
       CASE WHEN b.user_segment = 'NEW' AND b.reg_user_id IS NOT NULL   THEN 1 ELSE 0 END AS registered_7d,
       CASE WHEN COALESCE(t.txn_cnt_7d, 0)      > 0 THEN 1 ELSE 0 END AS transacted_7d,
       CASE WHEN COALESCE(t.purchase_cnt_7d, 0) > 0 THEN 1 ELSE 0 END AS purchased_7d,
       ROUND(COALESCE(t.gmv_7d, 0), 2)                             AS gmv_7d,
       -- full funnel window = open window + 7D (NEW: 7D+7D = 14D, EXISTING: 24H+7D = 8D).
       -- Late-Sep clicks are not fully matured: compared with the last timestamp in the data.
       CASE WHEN b.click_time + CASE WHEN b.user_segment = 'NEW' THEN INTERVAL 14 DAY
                                     ELSE INTERVAL 8 DAY END
                 > d.data_end
            THEN 0 ELSE 1 END                                       AS window_complete
FROM base b
LEFT JOIN txn7 t ON t.click_id = b.click_id
CROSS JOIN (SELECT LEAST((SELECT MAX(event_time) FROM events),
                         (SELECT MAX(txn_time)   FROM txn_event)) AS data_end) d
WHERE b.click_time >= TIMESTAMP '2026-07-01 00:00:00'
  AND b.click_time <  TIMESTAMP '2026-10-01 00:00:00'
ORDER BY b.click_time, b.click_id;

-- =====================================================================
-- Q1.1 RESULT in the "Example result" layout (all values from click_funnel)
-- Columns follow the example, plus the click details asked for in point 1
-- (device_id, user_id, campaign_type).
-- =====================================================================
CREATE OR REPLACE VIEW click_funnel_result AS
SELECT click_id                             AS "Click ID",
       click_time                           AS "Click Time",
       device_id                            AS "Device ID",
       user_id                              AS "User ID",
       media_source                         AS "Media Source",
       campaign_id                          AS "Campaign ID",
       campaign_type                        AS "Campaign Type",
       user_segment                         AS "User Segment",
       date_trunc('second', app_open_time)  AS "App Open Time",
       registered_7d                        AS "Registered 7D",
       transacted_7d                        AS "Transacted 7D",
       gmv_7d                               AS "GMV 7D"
FROM click_funnel;

SELECT * FROM click_funnel_result ORDER BY "Click Time", "Click ID";
