# Demo runbook

Presenter script for Mission Continuity. Written from the running console, not
from the plan. Where the console and this runbook disagree, the console wins
and this file is corrected.

| | |
| :-- | :-- |
| App commit | see `git log -1` (runbook verified against the console at the commit recorded in `replays/*/manifest.json`) |
| Golden bundles | `replays/E1-baseline`, `replays/E1-governed`, `replays/E2-baseline`, `replays/E2-governed` (also `E0-*`) |
| Shipped | live runs, replay with hash verification, Compaction view, evaluation, checkpoint files |
| Not in this build | pause and resume (checkpoint files are saved, but resuming is not demonstrated), memory heat |

Rules for the presenter:
- Read results off the screen; do not announce outcomes in advance.
- Sentience Governor records and flags. It never blocks. Refusals come only from the application's mission guard.
- Injected omissions are always introduced as injected.

## 0. Before the demo

```bash
cd ~/mission-continuity
make demo
```

Open `http://localhost:8501`. On the **Mission** tab, the Readiness panel should show green rows for `pydantic-ai-governor 0.1.1`, `sentience-governor 0.3.2.1`, `pydantic-ai-slim 2.37.0`, `anthropic`, `streamlit`, and "Anthropic key set (value never shown)".

## 1. Readiness (Mission tab)

- **Do:** click **Readiness check (one small live call)**.
- **Expect:** "Claude ✓ · Pydantic AI tool call ✓ · Governor evidence ✓ (N events, session …)".
- **Say:** "This is a real Claude agent running through Pydantic AI, and the Sentience Governor library is recording what it does. We just confirmed all three are live."
- **If not:** no key or no network means skip to stage 3; everything else runs from recorded runs.

## 2. Start a live governed run

- **Where:** sidebar, Mode **Live**, Configuration **governed**, Fault injection **none**.
- **Do:** click **Start investigation**, then open the **Investigation** tab.
- **Expect:** the caption "Live · refreshing", the context meter, and the first request with its tool calls.
- **Say:** "Perpetuity & Co. runs the Archive of Everything. Their customer, Moth & Lantern, says they were charged twice and billed for an add-on they never ordered. The agent has to work out which complaints are real, without taking any action it isn't authorized to take. Let's leave it working."
- **If not:** if the run shows outcome `error`, leave it and go to stage 3.

## 3. Switch to a verified recorded run

- **Where:** sidebar, Mode **Replay**, Recorded run **E1-governed**.
- **Expect:** a green banner, "Recorded run · artifacts verified (N files, hashes match)". The live run keeps going in the background.
- **Say:** "Live runs take a couple of minutes, so here's a complete run we recorded earlier. The console checked every file against its hash; nothing here is regenerated."
- **If not:** a bundle that fails its hash check is refused by the console. Pick the other governed bundle; never open raw files as a substitute.

## 4. The investigation (Investigation tab)

- **Do:** drag **Replay up to model request** from 1 to the end.
- **Expect:** the context meter climbing toward the trigger line; tool calls with GOVERNOR chips; the compaction banner.
- **Say:** "Every request re-sends the whole working context, so it grows every step. The agent pulls the account, payments, invoices, credits, policies and support tickets. The payment list makes the $149 look like a double charge; only the payment detail shows one of them was an authorization that was released."

## 5. Compaction (Compaction tab)

Walk the seven bands top to bottom.
- **Say (band 1):** "This is what the agent is about to forget."
- **Say (band 2):** "The model proposed this summary."
- **Say (band 3):** "This is what our retention policy required the application to keep word for word, including the agent's own attempts at prohibited actions with the flags Governor actually recorded, and what it is never allowed to keep."
- **Say (band 5):** "This is exactly what the model receives next."
- **Say (band 6):** "Did the key facts and mission limits survive?"
- **Say (band 7):** "And this is what it cost, counted by Anthropic's token-counting endpoint."

Outcome-bound variants (use one only if the golden bundle's results support it): filled in at rehearsal from `results.json`.

## 6. Baseline vs governed

- **Where:** Compaction tab, **Compare side by side with** set to `E1-baseline`; then the **Comparison** tab.
- **Say:** "Same mission, same model, same data, same trigger; two continuity architectures. These token numbers come from Governor's records. Governor records and flags; any refusal you see came from our application's mission guard, and it's counted separately. Natural runs and the injected-omission runs are shown separately."

## 7. Final report and verified artifacts (Report tab)

- **Expect:** R1 to R5 scored against an answer key the agent never saw; corrections; actions requiring human authorization; "what the agent says it did vs the tool ledger".
- **Do:** in a terminal, run `.venv/bin/mc verify-bundle replays/E1-governed`.
- **Say:** "Here's the agent's report, scored against an answer key it never saw. These are the raw recorded artifacts, committed to the repo, that you can replay yourselves."

## 8. Back to the live run (if time)

- **Where:** Mode **Live**, pick the run started in stage 2.
- **Say:** "And here's the live run we started. Whatever it has done so far is what's on screen."

## Fallbacks

- **No API access:** skip stages 1, 2 and 8. "API access isn't available here, so I'll show a recorded run. It's the same application and genuine artifacts."
- **Slow or failed live run:** don't restart it on stage. "That run failed. Failures are recorded and evaluated like any other run."
- **Feature not in this build:** "The state is saved after every compaction; resuming from it isn't demonstrated in this build." Heat is described only as a documented extension.
