-- Q0 Reconciliation: distinct rows per table (duplicates collapsed by event_id).
SELECT tbl, distinct_rows, stored_rows FROM (
  SELECT 'sentience_mc_runs' AS tbl, uniqExact(toString(event_id)) AS distinct_rows, count() AS stored_rows FROM sentience_mc_runs WHERE toString(run_group) IN ('preregistered', 'exploratory')
  UNION ALL SELECT 'sentience_mc_compactions', uniqExact(toString(event_id)), count() FROM sentience_mc_compactions WHERE toString(run_group) IN ('preregistered', 'exploratory')
  UNION ALL SELECT 'sentience_mc_retention', uniqExact(toString(event_id)), count() FROM sentience_mc_retention WHERE toString(run_group) IN ('preregistered', 'exploratory')
  UNION ALL SELECT 'sentience_mc_tool_calls', uniqExact(toString(event_id)), count() FROM sentience_mc_tool_calls WHERE toString(run_group) IN ('preregistered', 'exploratory')
  UNION ALL SELECT 'sentience_mc_governor_events', uniqExact(toString(event_id)), count() FROM sentience_mc_governor_events WHERE toString(run_group) IN ('preregistered', 'exploratory')
)
ORDER BY tbl
