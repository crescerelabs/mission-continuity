"""One investigation run. Launched by `mc run` with HOME=var/home and the key in env."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback

from pydantic_ai.usage import UsageLimits

from mission_continuity import dataset
from mission_continuity.agent import MODEL, build_agent
from mission_continuity.compaction import ContinuityConfig
from mission_continuity.kernel import load_kernel
from mission_continuity.memory import policy
from mission_continuity.store import RunStore
from mission_continuity.tools import Deps

REQUEST_LIMIT = 40


def _policy_sha() -> str:
    import hashlib
    from mission_continuity.memory import POLICY_FILE
    return hashlib.sha256(POLICY_FILE.read_bytes()).hexdigest()


async def run(args) -> int:
    kernel = load_kernel()
    store = RunStore(args.label)
    guard = args.mode == "governed" and not args.no_guard
    cfg = ContinuityConfig(mode=args.mode, trigger_input_tokens=args.trigger,
                           min_new_tool_returns=args.min_new, fault_injection=args.fault,
                           may_budget=policy()["may_budget"])
    settings = {"max_tokens": 8000}
    if args.temperature is not None:
        settings["temperature"] = args.temperature
    meta = {
        "label": args.label, "arm": args.arm, "mode": args.mode, "guard": guard,
        "fault_injection": args.fault, "model": MODEL, "model_settings": settings,
        "trigger_input_tokens": args.trigger, "min_new_tool_returns": args.min_new,
        "may_budget": cfg.may_budget, "request_limit": REQUEST_LIMIT,
        "kernel_sha256": kernel.sha256, "dataset_sha256": dataset.sha256(), "policy_sha256": _policy_sha(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "outcome": "running",
    }
    store.write_json("run.json", meta)
    t0 = time.time()
    agent = build_agent(kernel, store, cfg, model_settings=settings)
    deps = Deps(store=store, kernel=kernel, mode=args.mode, guard=guard)
    try:
        result = await agent.run(kernel.mission_text(), deps=deps,
                                 metadata=kernel.governor_metadata(),
                                 usage_limits=UsageLimits(request_limit=REQUEST_LIMIT))
        store.write_json("report.json", result.output.model_dump())
        meta.update(outcome="completed", governor_session_id=result.run_id)
    except Exception as exc:
        name = type(exc).__name__
        meta.update(outcome="usage_limit" if "UsageLimit" in name else "error",
                    error=f"{name}: {str(exc)[:500]}")
        traceback.print_exc()
    finally:
        sessions = sorted({r.get("governor_session_id") for r in store.read_jsonl("requests.jsonl")
                           if r.get("governor_session_id")})
        compactor = sorted({c["summarizer"]["session_id"] for c in store.read_jsonl("compactions.jsonl")})
        meta.update(ended_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    wall_seconds=round(time.time() - t0, 1),
                    governor_session_ids=sessions + compactor)
        store.write_json("run.json", meta)
    print(json.dumps({"label": args.label, "outcome": meta["outcome"]}))
    return 0 if meta["outcome"] == "completed" else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--label", required=True)
    p.add_argument("--arm", default="dev")
    p.add_argument("--mode", choices=["baseline", "governed"], required=True)
    p.add_argument("--trigger", type=int, default=None)
    p.add_argument("--min-new", type=int, default=3)
    p.add_argument("--fault", default="none", choices=["none", "FI-1", "FI-2"])
    p.add_argument("--no-guard", action="store_true")
    p.add_argument("--temperature", type=float, default=None)
    return asyncio.run(run(p.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
