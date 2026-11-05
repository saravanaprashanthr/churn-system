-- 14_events_time_window.sql
-- Supporting query for the BRIN index: events in a 90-day time window.
-- This is the access pattern of the pipeline extract step and the DQ freshness/volume checks
-- (Parts B and C), which read recent events by time, not by customer.

SELECT event_type, COUNT(*) AS events, COUNT(DISTINCT customer_id) AS customers
FROM events
WHERE event_ts >= TIMESTAMP '2026-04-01'
  AND event_ts <  TIMESTAMP '2026-07-01'
GROUP BY event_type
ORDER BY event_type;
