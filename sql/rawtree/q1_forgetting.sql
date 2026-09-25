-- Q1 What did the agent forget? First compaction (cmp-1) of every run: tracked facts
-- kept and items missing from the next request. Preregistered vs exploratory and
-- natural vs induced are separate columns; mission terms are keyword presence.
SELECT toString(condition) AS condition, toString(run_group) AS run_group, toString(mode) AS mode,
       toString(run_id) AS run_id,
       countIf(toString(item_type) = 'fact' AND toString(status) = 'present') AS facts_kept,
       countIf(toString(item_type) = 'fact' AND toString(status) != 'not_compacted_yet') AS facts_tracked,
       countIf(toString(item_type) = 'mission_term' AND toString(status) = 'missing') AS mission_terms_missing,
       arrayStringConcat(arraySort(groupArrayIf(toString(item), toString(status) = 'missing')), ', ') AS missing_items
FROM (SELECT * FROM sentience_mc_retention LIMIT 1 BY toString(event_id))
WHERE toString(compaction_id) = 'cmp-1'
GROUP BY condition, run_group, mode, run_id
ORDER BY condition, mode, run_group, run_id
