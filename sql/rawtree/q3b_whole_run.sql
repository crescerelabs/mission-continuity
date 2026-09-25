-- Q3b WHOLE-RUN measurements per run: every compaction, all Governor-measured tokens
-- (investigator and summarizer sessions), and the final task score (0-5).
SELECT toString(r.condition) AS condition, toString(r.run_group) AS run_group, toString(r.mode) AS mode,
       toString(r.run_id) AS run_id, toInt64(r.score) AS score, toInt64(r.compactions) AS compactions,
       c.saved_pcts AS saved_pct_each_compaction,
       toInt64(r.input_tokens) AS input_tokens_all_sessions,
       toInt64(r.summarizer_input_tokens) + toInt64(r.summarizer_output_tokens) AS summarizer_tokens,
       round(accurateCastOrNull(r.estimated_usd, 'Float64'), 4) AS estimated_usd
FROM (SELECT * FROM sentience_mc_runs LIMIT 1 BY toString(event_id)) AS r
LEFT JOIN (
  SELECT toString(run_id) AS run_id,
         arrayStringConcat(arrayMap(x -> toString(x.2), arraySort(groupArray((toInt64(request_step),
               accurateCastOrNull(reduction_pct, 'Float64'))))), ', ') AS saved_pcts
  FROM (SELECT * FROM sentience_mc_compactions LIMIT 1 BY toString(event_id)) GROUP BY run_id
) AS c ON c.run_id = toString(r.run_id)
WHERE toString(r.run_group) IN ('preregistered', 'exploratory')
ORDER BY condition, run_group, mode, run_id
