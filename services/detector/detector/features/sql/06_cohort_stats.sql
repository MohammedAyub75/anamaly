-- Feature block 6 -- cohort keys and aggregates.
--
-- The five fallback levels from `policy/fusion.yaml`, most specific first, each
-- precomputed with median, MAD, P01, P99 and count for every peer metric. Layer
-- 2 walks down the ladder until a cohort reaches `min_size` and records which
-- level it stopped at, so the evidence can say "compared against 412 peers at
-- grade 12, Process Ops, plant sites" rather than quoting a bare number.
--
-- Median and MAD, never mean and sigma: outliers are what we are looking for,
-- and they poison the mean (docs/specs/detector.md, layer 2).
--
-- Long format -- one row per (level, cohort key, metric) -- because the set of
-- metrics is going to grow and a new one must not mean a schema migration.

CREATE OR REPLACE TEMP TABLE cohort_input AS
UNPIVOT (
    SELECT employee_id,
           coalesce(grade, -1)                         AS grade,
           coalesce(job_family, 'unknown')             AS job_family,
           coalesce(site_class, 'unknown')             AS site_class,
           coalesce(nationality_class, 'unknown')      AS nationality_class,
           coalesce(service_band, 'unknown')           AS service_band,
           base_salary,
           allowance_ratio,
           allowance_total_monthly,
           net_mean,
           overtime_ratio,
           band_position
    FROM employee_features
)
ON base_salary, allowance_ratio, allowance_total_monthly, net_mean,
   overtime_ratio, band_position
INTO NAME metric VALUE value;

CREATE OR REPLACE TEMP TABLE cohort_stats AS
$cohort_levels;
