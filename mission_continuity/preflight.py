"""Readiness check: Claude via Pydantic AI, one real tool call, real Governor evidence.

Launched by `mc preflight` with HOME=var/home. Prints one JSON line.
"""

from __future__ import annotations

import asyncio
import json
import sys

from pydantic_ai import Agent
from pydantic_ai_governor import SentienceGovernor

from mission_continuity import governor_evidence
from mission_continuity.agent import MODEL
from mission_continuity.kernel import load_kernel
from mission_continuity.store import RunStore
from mission_continuity.tools import Deps, build_tools


async def main() -> int:
    kernel = load_kernel()
    store = RunStore("preflight")
    tools = [t for t in build_tools() if t.name == "get_billing_policy"]
    out = {"model": MODEL}
    for settings in ({"temperature": 0.0, "max_tokens": 300}, {"max_tokens": 300}):
        agent = Agent(MODEL, deps_type=Deps, tools=tools, model_settings=settings,
                      capabilities=[SentienceGovernor(agent_id="mc-preflight")])
        try:
            r = await agent.run("Call get_billing_policy with topic 'refunds', then reply with the policy_id only.",
                                deps=Deps(store=store, kernel=kernel, mode="baseline", guard=False),
                                metadata=kernel.governor_metadata())
        except Exception as exc:
            out.setdefault("attempts", []).append({"settings": settings, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            continue
        events = governor_evidence.read_trace(r.run_id)
        kinds = [e["event_type"] for e in events]
        tokens = list(governor_evidence.token_snapshots(events).values())
        out.update(ok=True, settings=settings, reply=str(r.output)[:80], session_id=r.run_id,
                   events=kinds, scope_asserted=kinds.count("SCOPE_ASSERTED"),
                   token_snapshots=len(tokens),
                   cached_read_tokens=[t["payload"].get("llm_cached_read_tokens") for t in tokens],
                   model_identifier=tokens[0]["payload"].get("model_identifier") if tokens else None)
        break
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
