"""Replay bundles: genuine recorded runs, copied with their Governor traces and hashed.

A bundle is replays/<label>/ with
  run/        the run's redacted ledgers, compactions, checkpoints, report, results
              (not runner.log, which stays local because it can contain local paths)
  traces/     every Governor trace file of the run's sessions (investigator + compactor)
  manifest.json  label, arm, mode, app commit, kernel/dataset/policy hashes, SHA-256 per file
Nothing is regenerated or edited. verify() recomputes every hash.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from mission_continuity.paths import REPLAYS, REPO, run_dir, trace_file
from mission_continuity.redact import known_pii_values


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_bundle(label: str, root: Path = REPLAYS) -> Path:
    src = run_dir(label)
    meta = json.loads((src / "run.json").read_text())
    dst = root / label
    if dst.exists():
        shutil.rmtree(dst)
    # runner.log (worker stdout/stderr) stays local: it can contain local filesystem
    # paths. Its evaluated content, the GOVERNANCE_ERROR count, is in results.json.
    shutil.copytree(src, dst / "run", ignore=shutil.ignore_patterns("runner.log"))
    (dst / "traces").mkdir(parents=True)
    for sid in meta.get("governor_session_ids", []):
        if trace_file(sid).exists():
            shutil.copy2(trace_file(sid), dst / "traces" / f"{sid}.jsonl")
    files = {str(p.relative_to(dst)): _sha(p) for p in sorted(dst.rglob("*")) if p.is_file()}
    blob = "".join(p.read_text(errors="ignore") for p in dst.rglob("*") if p.is_file())
    leaks = [v for v in known_pii_values() if v in blob]
    if leaks:
        shutil.rmtree(dst)
        raise RuntimeError(f"bundle refused: {len(leaks)} MUST_NOT_PERSIST value(s) found")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                            capture_output=True, text=True).stdout.strip()
    manifest = {"label": label, "arm": meta.get("arm"), "mode": meta.get("mode"),
                "fault_injection": meta.get("fault_injection"), "app_commit": commit,
                "kernel_sha256": meta.get("kernel_sha256"), "dataset_sha256": meta.get("dataset_sha256"),
                "policy_sha256": meta.get("policy_sha256"), "leak_scan": "0 values found",
                "files": files}
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return dst


def verify(bundle: Path) -> dict:
    m = json.loads((bundle / "manifest.json").read_text())
    bad = [f for f, h in m["files"].items() if not (bundle / f).exists() or _sha(bundle / f) != h]
    extra = [str(p.relative_to(bundle)) for p in bundle.rglob("*")
             if p.is_file() and p.name != "manifest.json" and str(p.relative_to(bundle)) not in m["files"]]
    return {"ok": not bad and not extra, "files": len(m["files"]), "mismatched": bad, "unlisted": extra}
