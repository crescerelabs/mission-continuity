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


def _cmd_rawtree(args) -> int:
    from mission_continuity import rawtree
    if args.action == "export":
        out = rawtree.export()
        print(json.dumps(out["summary"], indent=1))
        return 0
    client = rawtree.Client()
    if args.action == "check":
        r = rawtree.check(client)
        print(json.dumps(r, indent=1))
        return 0 if r["database_ok"] else 3
    if args.action == "push":
        r = rawtree.check(client)
        if not r["database_ok"]:
            print(f"REFUSED: requests resolve to database {r['current_database']!r}, not {client.database!r}")
            return 3
        taken = [t for t, exists in r["own_tables_exist"].items() if exists]
        if taken and not args.name == "allow-existing":
            print(f"REFUSED: table(s) already exist in {client.database!r}: {taken}")
            return 3
        out = rawtree.export()
        report = rawtree.push(client, out["per_bundle"])
        (rawtree.OUT_DIR / "push_report.json").write_text(json.dumps(report, indent=1))
        print(json.dumps({"database": client.database, "inserts": len(report),
                          "rows_sent": sum(x["rows"] for x in report)}, indent=1))
        return 0
    if args.action in ("push-run", "verify-run"):
        from mission_continuity.bundle import make_bundle, verify
        label = args.name
        bdir = rawtree.SMOKE_DIR / label
        if args.action == "push-run":
            bdir = make_bundle(label, root=rawtree.SMOKE_DIR)
            v = verify(bdir)
            print(f"bundle {bdir.relative_to(REPO)}: {v}")
            if not v["ok"]:
                return 3
            out = rawtree.push_run(client, bdir)
            (bdir.parent / f"{label}.push_report.json").write_text(json.dumps(out, indent=1))
            print(json.dumps({"run_id": out["run_id"], "database": client.database, "sent": out["sent"],
                              "inserted": {x["table"]: x["inserted"] for x in out["inserts"]}}, indent=1))
            return 0
        res = rawtree.verify_run(client, bdir)
        ok = all(x["match"] for x in res.values())
        for k, x in res.items():
            print(f"{'PASS' if x['match'] else 'FAIL'} {k}: RawTree {x['rawtree']} | local {x['local']}")
        print("RUN VERIFICATION", "PASS" if ok else "FAIL")
        return 0 if ok else 4
    if args.action == "query":
        data = rawtree.run_sql(client, args.name)
        (rawtree.OUT_DIR / f"{args.name}.result.json").write_text(json.dumps(data, indent=1))
        rows = data.get("data", [])
        if rows:
            cols = list(rows[0])
            print(" | ".join(cols))
            for r in rows:
                print(" | ".join(str(r[c]) for c in cols))
        print(f"-- {data.get('rows')} rows from RawTree database {client.database!r}; statistics {data.get('statistics')}")
        return 0
    if args.action == "reconcile":
        from mission_continuity.rawtree import EXPORT_VERSION  # noqa: F401
        expected = json.loads((rawtree.OUT_DIR / "export_manifest.json").read_text())["row_counts"]
        counts = {r["tbl"]: int(r["distinct_rows"]) for r in rawtree.run_sql(client, "q0_reconcile")["data"]}
        ok = True
        for t, n in expected.items():
            good = counts.get(t) == n
            ok &= good
            print(f"{'PASS' if good else 'FAIL'} rows {t}: RawTree {counts.get(t)} vs export {n}")
        local = {}
        for f in ("results_summary.json", "exploratory_summary.json"):
            for r in json.loads((REPO / "experiment" / f).read_text()):
                local[r["label"]] = r
        for r in rawtree.run_sql(client, "q3b_whole_run")["data"]:
            L = local.get(r["run_id"])
            if L is None:
                ok = False
                print(f"FAIL unexpected run in experiment query: {r['run_id']}")
                continue
            checks = {"score": (int(r["score"]), L["score"]),
                      "input_tokens": (int(r["input_tokens_all_sessions"]), L["input_tokens"]),
                      "compactions": (int(r["compactions"]), L["compactions"]),
                      "summarizer_tokens": (int(r["summarizer_tokens"]), L["summarizer_tokens"])}
            for k, (a, b) in checks.items():
                good = a == b
                ok &= good
                if not good:
                    print(f"FAIL {r['run_id']} {k}: RawTree {a} vs summary {b}")
            saved = [float(x) for x in str(r["saved_pct_each_compaction"]).split(", ") if x]
            good = saved == [float(x) for x in L["reduction_pct"]]
            ok &= good
            print(f"{'PASS' if all(a == b for a, b in checks.values()) and good else 'FAIL'} {r['run_id']}: "
                  f"score {r['score']}, input tokens {r['input_tokens_all_sessions']}, saved {saved}")
        print("RECONCILIATION", "PASS" if ok else "FAIL")
        return 0 if ok else 4
    return 1


def _cmd_reconstruct(args) -> int:
    from mission_continuity import reconstruct
    if args.action == "build" and args.live:
        r = reconstruct.render_live(args.label)
        print(json.dumps({k: r[k] for k in ("out_dir", "kind", "source_root", "frames", "duration_s", "video_sha256",
                                            "video_bytes")}, indent=1))
        return 0
    if args.action == "verify" and args.live:
        r = reconstruct.verify_live(args.label)
        for k, v in r["checks"].items():
            print(f"{'PASS' if v else 'FAIL'} {k}")
        print("LIVE REPLAY", "PASS" if r["ok"] else "FAIL")
        return 0 if r["ok"] else 4
    if args.action == "build":
        r = reconstruct.render(args.label, placeholder=args.placeholder)
        print(json.dumps({k: r[k] for k in ("out_dir", "kind", "footage", "frames", "fps", "duration_s", "video_sha256",
                                            "video_bytes", "experimental_material_not_used")}, indent=1))
        return 0
    if args.action == "verify":
        r = reconstruct.verify(args.label, placeholder=args.placeholder)
        for k, v in r["checks"].items():
            print(f"{'PASS' if v else 'FAIL'} {k}")
        print("RECONSTRUCTION", "PASS" if r["ok"] else "FAIL")
        return 0 if r["ok"] else 4
    if args.action == "scenes":
        for s in reconstruct.build_scenes(args.label)["scenes"]:
            print(f"{s['scene_id']} {s['kind']:<10} {s.get('offset') or '':<7} {s['caption']}")
        return 0
    from mission_continuity import bfl
    if args.action == "bfl-check":
        c = bfl.Client()
        credits = c.credits()
        print(f"BFL key: set (not shown); credits: {credits:g} (= ${credits * bfl.CREDIT_USD:.2f}); "
              f"ledger spend so far: ${bfl.spent():.2f} of ${bfl.CAP_USD:.2f} cap")
        return 0
    if args.action in ("draft", "accept"):
        if not args.scene:
            print("scene id required (e.g. S4)", file=sys.stderr)
            return 1
        d = reconstruct.RECON / args.label
        fp = d / "footage.json"
        footage = json.loads(fp.read_text()) if fp.exists() else {}
        if args.action == "accept":
            footage[args.scene]["accepted"] = True
            fp.write_text(json.dumps(footage, indent=1))
            print(f"accepted {args.label} {args.scene}")
            return 0
        scene = next(s for s in reconstruct.build_scenes(args.label)["scenes"] if s["scene_id"] == args.scene)
        if footage.get(args.scene, {}).get("accepted"):
            print(f"{args.scene} is already accepted; not regenerating", file=sys.stderr)
            return 1
        payload = {"mode": "t2v", "prompt": scene["prompt"], "aspect_ratio": "16:9", "duration": reconstruct.SCENE_S,
                   "resolution": "hd", "generate_audio": False, "draft": True, "safety_tolerance": 2}
        reference = None
        if args.reference:
            # Style and continuity reference: the first frame of an accepted clip opens this clip (i2v).
            ref = footage.get(args.reference) or {}
            ref_clip = d / "clips" / f"{args.reference}.mp4"
            if not (ref.get("accepted") and ref_clip.exists() and reconstruct._sha_file(ref_clip) == ref.get("clip_sha256")):
                print(f"reference {args.reference} is not an accepted, hash-verified clip", file=sys.stderr)
                return 1
            import base64
            import subprocess as sp
            frame = d / "clips" / f"{args.reference}_frame0.png"
            sp.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(ref_clip), "-frames:v", "1", str(frame)], check=True)
            payload.update({"mode": "i2v", "keyframes": base64.b64encode(frame.read_bytes()).decode()})
            reference = {"scene": args.reference, "clip_sha256": ref["clip_sha256"],
                         "keyframe": f"clips/{frame.name}", "keyframe_sha256": reconstruct._sha_file(frame),
                         "use": "opening frame (i2v keyframe) for style and continuity"}
        clip = d / "clips" / f"{args.scene}.mp4"
        prov = bfl.Client().generate_video(payload, clip, tag=f"{args.label}/{args.scene}/draft")
        request = {k: v for k, v in payload.items() if k != "keyframes"}
        if reference:
            request["keyframes"] = "<first frame of " + reference["scene"] + ", sha256 " + reference["keyframe_sha256"] + ">"
        footage[args.scene] = {"scene_id": args.scene, "model": "FLUX 3 (/v1/flux-3-video)", "request": request,
                               "reference": reference,
                               "clip": f"clips/{args.scene}.mp4", "clip_sha256": reconstruct._sha_file(clip),
                               "accepted": False, **prov,
                               "bfl_reported_prompt_matches_request": prov.get("bfl_reported_prompt") == payload["prompt"]}
        fp.write_text(json.dumps(footage, indent=1))
        print(json.dumps({k: footage[args.scene][k] for k in ("task_id", "status", "latency_s", "cost_usd_reported",
                                                               "estimate_usd", "clip", "clip_sha256")}, indent=1))
        print(f"ledger spend: ${bfl.spent():.2f} of ${bfl.CAP_USD:.2f}")
        return 0
    return 1


def _cmd_resummarize(args) -> int:
    from mission_continuity import resummarize as rs
    if args.action == "control":
        r = rs.control(args.label, args.cid)
        print(f"recorded: {r['recorded']}\nrebuilt:  {r['rebuilt']}")
        print("CONTROL", "PASS" if r["ok"] else "FAIL")
        return 0 if r["ok"] else 4
    if args.action == "model-check":
        m = rs.verify_model()
        print(f"model {rs.MODEL['file']}: SHA-256 {m['sha256']} ({m['bytes']:,} bytes) matches the pinned value")
        with rs.Server() as srv:
            print(f"llama-server {' / '.join(srv.version)}; n_ctx {(srv.props.get('default_generation_settings') or {}).get('n_ctx')}; "
                  f"smoke reply: {srv.smoke()!r}")
        return 0
    if args.action == "liquid":
        r = rs.run_liquid(args.label, args.cid)
        a = r["attempts"][-1]
        print(json.dumps({"valid": r["error"] is None, "error": r["error"], "attempts": len(r["attempts"]),
                          "checks": r["checks"], "caps": r["caps"], "usage": a.get("usage"),
                          "wall_s": a["wall_s"], "finish_reason": a.get("finish_reason")}, indent=1))
        return 0
    if args.action == "verify":
        r = rs.verify(args.label, args.cid)
        for k, v in r["checks"].items():
            print(f"{'PASS' if v else 'FAIL'} {k}")
        print("OFFLINE RESUMMARY", "PASS" if r["ok"] else "FAIL")
        return 0 if r["ok"] else 4
    return 1


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

    p = sub.add_parser("rawtree", help="Optional: export recorded evidence to RawTree and query it")
    p.add_argument("action", choices=["export", "check", "push", "push-run", "verify-run", "query", "reconcile"])
    p.add_argument("name", nargs="?", default="q0_reconcile")
    p.set_defaults(func=_cmd_rawtree)

    p = sub.add_parser("reconstruct", help="Optional: AI-generated visual reconstruction (Watch Replay) of a recorded run")
    p.add_argument("action", choices=["scenes", "build", "verify", "bfl-check", "draft", "accept"])
    p.add_argument("label", nargs="?", default="E1-baseline")
    p.add_argument("scene", nargs="?")
    p.add_argument("--placeholder", action="store_true", help="solid backgrounds; output under var/")
    p.add_argument("--live", action="store_true", help="completed live investigation (var/runs); output under var/reconstructions")
    p.add_argument("--reference", help="accepted scene whose first frame opens this draft (style/continuity)")
    p.set_defaults(func=_cmd_reconstruct)

    p = sub.add_parser("resummarize", help="Optional: offline exploratory re-summarization of a recorded compaction (Liquid AI, local)")
    p.add_argument("action", choices=["control", "model-check", "liquid", "verify"])
    p.add_argument("label", nargs="?", default="E1-baseline")
    p.add_argument("cid", nargs="?", default="cmp-1")
    p.set_defaults(func=_cmd_resummarize)

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
