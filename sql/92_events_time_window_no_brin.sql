-- 92_events_time_window_no_brin.sql
-- Same as 14_events_time_window.sql with bitmap scans disabled, so the BRIN index cannot be used.
-- Used only to measure the BRIN index (A/B on the same database).

SET enable_bitmapscan = off;

SELECT event_type, COUNT(*) AS events, COUNT(DISTINCT customer_id) AS customers
FROM events
WHERE event_ts >= TIMESTAMP '2026-04-01'
  AND event_ts <  TIMESTAMP '2026-07-01'
GROUP BY event_type
ORDER BY event_type;
