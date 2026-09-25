-- Q0 Reconciliation: distinct rows per table (duplicates collapsed by event_id).
SELECT tbl, distinct_rows, stored_rows FROM (
  SELECT 'sentience_mc_runs' AS tbl, uniqExact(toString(event_id)) AS distinct_rows, count() AS stored_rows FROM sentience_mc_runs
  UNION ALL SELECT 'sentience_mc_compactions', uniqExact(toString(event_id)), count() FROM sentience_mc_compactions
  UNION ALL SELECT 'sentience_mc_retention', uniqExact(toString(event_id)), count() FROM sentience_mc_retention
  UNION ALL SELECT 'sentience_mc_tool_calls', uniqExact(toString(event_id)), count() FROM sentience_mc_tool_calls
  UNION ALL SELECT 'sentience_mc_governor_events', uniqExact(toString(event_id)), count() FROM sentience_mc_governor_events
)
ORDER BY tbl
