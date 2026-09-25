"""mc: the Mission Continuity command line."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from mission_continuity import __version__, keys, spend
from mission_continuity.paths import ISOLATED_HOME, REPO, run_dir
from mission_continuity.redact import redact_text

PY = sys.executable


def _child_env() -> dict:
    """Environment for agent subprocesses: isolated HOME, key from the keychain."""
    env = dict(os.environ)
    env["ANTHROPIC_API_KEY"] = keys.anthropic_key()   # read here with the real HOME
    ISOLATED_HOME.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(ISOLATED_HOME)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _projected_usd() -> float:
    """1.5x the mean estimated cost of prior completed runs, or $1.50 before any."""
    costs = []
    pricing = spend.load_pricing()
    for rj in run_dir("").glob("*/run.json"):
        meta = json.loads(rj.read_text())
        if meta.get("estimated_usd") is not None:
            costs.append(meta["estimated_usd"])
    return 1.5 * (sum(costs) / len(costs)) if costs else 1.50


def _cmd_spend(_args) -> int:
    print(spend.report())
    return 0


def _cmd_run(args) -> int:
    label = args.label or f"{args.arm}-{args.mode}-{time.strftime('%H%M%S')}"
    try:
        spent = spend.check_before_run(_projected_usd())
    except spend.SpendStop as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    cmd = [PY, "-m", "mission_continuity.worker", "--label", label, "--arm", args.arm,
           "--mode", args.mode, "--fault", args.fault, "--min-new", str(args.min_new)]
    if args.trigger is not None:
        cmd += ["--trigger", str(args.trigger)]
    if args.no_guard:
        cmd.append("--no-guard")
    if args.temperature is not None:
        cmd += ["--temperature", str(args.temperature)]
    out_dir = run_dir(label)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"run {label}: estimated spend so far ${spent:.2f}; starting", flush=True)
    proc = subprocess.run(cmd, env=_child_env(), cwd=REPO, capture_output=True, text=True)
    (out_dir / "runner.log").write_text(redact_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr))
    # Record this run's own estimated cost from its Governor sessions.
    rj = out_dir / "run.json"
    if rj.exists():
        meta = json.loads(rj.read_text())
        from mission_continuity.paths import trace_file
        pricing = spend.load_pricing()
        inp = out = 0
        for sid in meta.get("governor_session_ids", []):
            p = trace_file(sid)
            if p.exists():
                for payload in spend._token_snapshots(p.read_text().splitlines()):
                    inp += int(payload.get("llm_prompt_tokens") or 0)
                    out += int(payload.get("llm_completion_tokens") or 0)
        meta.update(measured_input_tokens=inp, measured_output_tokens=out,
                    estimated_usd=round((inp * pricing.input_per_mtok + out * pricing.output_per_mtok) / 1e6, 4))
        rj.write_text(json.dumps(meta, indent=1))
        print(f"run {label}: {meta.get('outcome')}, measured tokens in {inp:,} / out {out:,}, "
              f"estimated ${meta['estimated_usd']:.2f}")
    print(proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "(no worker output)")
    return proc.returncode


def _cmd_bundle(args) -> int:
    from mission_continuity.bundle import make_bundle, verify
    for label in args.labels:
        dst = make_bundle(label)
        print(f"{dst.relative_to(REPO)}: {verify(dst)}")
    return 0


def _cmd_verify_bundle(args) -> int:
    from pathlib import Path
    from mission_continuity.bundle import verify
    r = verify(Path(args.path))
    print(json.dumps(r))
    return 0 if r["ok"] else 1


def _cmd_summarize(args) -> int:
    from mission_continuity.store import RunStore
    rows = []
    for label in args.labels:
        if not (run_dir(label) / "results.json").exists():
            print(f"skipping {label}: not evaluated", file=sys.stderr)
            continue
        r = RunStore(label).read_json("results.json")
        cmp_ = r["compactions"]
        rows.append({
            "label": label, "arm": r["arm"], "mode": r["mode"], "failure_kind": r["failure_kind"],
            "outcome": r["outcome"], "score": r["correctness"]["score"],
            **{k: r["correctness"][k] for k in ("R1", "R2", "R3", "R4", "R5")},
            "compactions": len(cmp_),
            "reduction_pct": [c["counted"].get("reduction_pct") for c in cmp_ if c.get("counted")],
            "tokens_saved": [c["counted"].get("tokens_saved") for c in cmp_ if c.get("counted")],
            "cf_missing": sorted({x for c in cmp_ for x in c["cf_missing"]}),
            "kernel_terms_missing": sorted({x for c in cmp_ for x in c["kernel_terms_missing"]}),
            "prohibited_attempts": len(r["authorization"]["prohibited_dispatches"]),
            "governor_flagged": r["authorization"]["governor_flagged"],
            "governor_unflagged": r["authorization"]["governor_unflagged"],
            "simulated_effects": r["authorization"]["simulated_effects"],
            "guard_denials": r["authorization"]["guard_denials"],
            "repeated_retrievals": r["repeated_tool_calls_after_compaction"],
            "input_tokens": r["cost"]["input_tokens"], "output_tokens": r["cost"]["output_tokens"],
            "summarizer_tokens": r["cost"]["summarizer_input_tokens"] + r["cost"]["summarizer_output_tokens"],
            "estimated_usd": r["cost"]["estimated_usd"], "wall_seconds": r["cost"]["wall_seconds"],
            "tool_joins": f'{r["evidence_integrity"]["tool_calls_joined"]}/{r["evidence_integrity"]["tool_calls"]}',
            "token_joins": f'{r["evidence_integrity"]["token_snapshots_joined"]}/{r["evidence_integrity"]["model_responses"]}',
            "leaks": r["leak_scan"]["leaks"],
            "floor_ok": r.get("floor_check", {}).get("all_ok"),
            "consecutive_compactions": r.get("floor_check", {}).get("consecutive_compactions"),
        })
    out = REPO / "experiment" / "results_summary.json"
    out.write_text(json.dumps(rows, indent=1))
    print(json.dumps(rows, indent=1))
    return 0


def _cmd_evaluate(args) -> int:
    from mission_continuity.evaluate import evaluate
    r = evaluate(args.label)
    print(json.dumps({k: r[k] for k in ("label", "mode", "outcome", "correctness", "compactions",
                                        "evidence_integrity", "leak_scan", "cost")}, indent=1))
    return 0


def _cmd_preflight(_args) -> int:
    try:
        spend.check_before_run(0.05)
    except spend.SpendStop as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    proc = subprocess.run([PY, "-m", "mission_continuity.preflight"], env=_child_env(), cwd=REPO,
                          capture_output=True, text=True)
    print(redact_text(proc.stdout.strip() or proc.stderr.strip()[-2000:]))
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mc", description=__doc__)
    parser.add_argument("--version", action="version", version=f"mc {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("spend", help="Governor-measured tokens and estimated spend")
    p.set_defaults(func=_cmd_spend)

    p = sub.add_parser("bundle", help="Package recorded runs as hash-verified replay bundles")
    p.add_argument("labels", nargs="+")
    p.set_defaults(func=_cmd_bundle)

    p = sub.add_parser("verify-bundle", help="Recompute a replay bundle's hashes")
    p.add_argument("path")
    p.set_defaults(func=_cmd_verify_bundle)

    p = sub.add_parser("summarize", help="Write experiment/results_summary.json from evaluated runs")
    p.add_argument("labels", nargs="+")
    p.set_defaults(func=_cmd_summarize)

    p = sub.add_parser("evaluate", help="Evaluate one run (writes results.json)")
    p.add_argument("label")
    p.set_defaults(func=_cmd_evaluate)

    p = sub.add_parser("preflight", help="Live readiness check: Claude + one tool + Governor evidence")
    p.set_defaults(func=_cmd_preflight)

    p = sub.add_parser("run", help="Run one investigation")
    p.add_argument("--mode", choices=["baseline", "governed"], required=True)
    p.add_argument("--arm", default="dev")
    p.add_argument("--label")
    p.add_argument("--trigger", type=int, default=None, help="compaction trigger (input tokens); omit to disable")
    p.add_argument("--min-new", type=int, default=3)
    p.add_argument("--fault", default="none", choices=["none", "FI-1", "FI-2"])
    p.add_argument("--no-guard", action="store_true")
    p.add_argument("--temperature", type=float, default=None)
    p.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
