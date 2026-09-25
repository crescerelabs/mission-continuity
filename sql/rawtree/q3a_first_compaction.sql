-- Q3a Cost at the FIRST compaction: same pending request counted uncompacted vs
-- compacted (Anthropic token-counting endpoint, recorded per compaction), per group.
SELECT toString(condition) AS condition, toString(run_group) AS run_group, toString(mode) AS mode,
       count() AS runs,
       round(min(accurateCastOrNull(reduction_pct, 'Float64')), 1) AS min_saved_pct,
       round(max(accurateCastOrNull(reduction_pct, 'Float64')), 1) AS max_saved_pct,
       round(avg(accurateCastOrNull(reduction_pct, 'Float64')), 1) AS avg_saved_pct,
       sum(accurateCastOrNull(tokens_saved, 'Int64')) AS tokens_saved,
       countIf(toString(a2_met) = 'true') AS runs_meeting_a2
FROM (SELECT * FROM sentience_mc_compactions LIMIT 1 BY toString(event_id))
WHERE toString(first_compaction) = 'true'
  AND toString(run_group) IN ('preregistered', 'exploratory')
GROUP BY condition, run_group, mode
ORDER BY condition, run_group, mode
