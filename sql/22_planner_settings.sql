-- 22_planner_settings.sql
-- Database-level planner settings for this workload.
--
-- jit = off: every Part A query runs in 20 ms - 2 s. On A1 JIT compilation and the JIT-compiled
-- plan cost ~1 s (optimized A1: ~1,820 ms with JIT, ~790 ms without; naive A1: ~1,580 vs ~1,400 ms).
-- JIT pays off for long CPU-bound aggregations over many millions of rows, which this
-- workload does not have at its current size. Revisit at 100x data volume.
-- Takes effect for new connections.

ALTER DATABASE ecommerce SET jit = off;
