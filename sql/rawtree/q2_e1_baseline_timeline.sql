-- Q2 What happened after compaction in E1-baseline? Recorded sequence only; it does
-- not establish that the missing restriction caused the later call.
SELECT request_step, kind, what, detail FROM (
  SELECT toInt64(request_step) AS request_step, 'compaction' AS kind, toString(compaction_id) AS what,
         concat('missing from next request: ',
                arrayStringConcat(arraySort(groupArrayIf(toString(item), toString(status) = 'missing')), ', ')) AS detail
  FROM (SELECT * FROM sentience_mc_retention LIMIT 1 BY toString(event_id))
  WHERE toString(run_id) = 'E1-baseline'
  GROUP BY request_step, compaction_id
  UNION ALL
  SELECT toInt64(t.request_step), 'prohibited tool call', toString(t.tool_name),
         concat('application: ', toString(t.app_outcome),
                ' | Governor ', toString(g.event_type), ' seq ', toString(g.sequence), ' at ', toString(g.timestamp_utc),
                ': target ', toString(g.target_system), ', flags ', toString(g.advisory_flags),
                ', violations ', toString(g.policy_violations), ', pass_through ', toString(g.pass_through))
  FROM (SELECT * FROM sentience_mc_tool_calls LIMIT 1 BY toString(event_id)) AS t
  INNER JOIN (SELECT * FROM sentience_mc_governor_events LIMIT 1 BY toString(event_id)) AS g
    ON toString(g.tool_use_id) = toString(t.tool_call_id) AND toString(g.run_id) = toString(t.run_id)
  WHERE toString(t.run_id) = 'E1-baseline' AND toString(t.category) = 'prohibited'
    AND toString(g.event_type) = 'SCOPE_ASSERTED'
)
ORDER BY request_step
