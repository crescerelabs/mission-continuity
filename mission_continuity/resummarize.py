"""Offline, exploratory re-summarization of a recorded compaction (Liquid AI, on-device).

The recorded run is never touched. This reads a baseline compaction's archived
input (run/archive/<cid>.json in a hash-verified replay bundle), asks a local
model to summarize it with the same instructions, schema, caps and token limit
as the recorded summarizer, rebuilds the next request exactly as baseline
compaction assembles it, and scores it with the unchanged CF_TESTS and
KERNEL_TERMS from compaction.py.

The Liquid summary was never sent to any agent: it did not affect the recorded
investigation and has no Governor execution record. Its provenance is the
output file under offline_resummaries/<run>/<cid>.liquid.json.

Control: the same rebuild applied to the recorded Claude summary must reproduce
the recorded context_checks exactly before any Liquid result is accepted.

Runtime: llama.cpp `llama-server` (Homebrew) on localhost, called with stdlib
urllib. The model file is pinned by repo revision and SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart

from mission_continuity.bundle import verify as verify_bundle
from mission_continuity.compaction import (CF_TESTS, KERNEL_TERMS, SUMMARIZER_INSTRUCTIONS,
                                           SUMMARIZER_MAX_TOKENS, CompactionCandidate, _cap, _split,
                                           _stub_tail, render_messages)
from mission_continuity.paths import REPLAYS, REPO, VAR
from mission_continuity.redact import known_pii_values, redact_text

OUT_ROOT = REPO / "offline_resummaries"
MODEL = {
    "repo": "LiquidAI/LFM2.5-1.2B-Instruct-GGUF",
    "revision": "8ed288026e23958ad9dfa92d53ed773a8eee7125",
    "file": "LFM2.5-1.2B-Instruct-Q8_0.gguf",
    "sha256": "f6b981dcb86917fa463f78a362320bd5e2dc45445df147287eedb85e5a30d26a",
    "bytes": 1246253888,
    "license": "LFM Open License v1.0",
}
MODEL_PATH = VAR / "models" / MODEL["file"]
N_CTX = 32768
# Model card recommended sampling; max_tokens matches the recorded summarizer.
SAMPLING = {"temperature": 0.1, "top_k": 50, "repeat_penalty": 1.05, "max_tokens": SUMMARIZER_MAX_TOKENS}
SEEDS = [1, 2]            # seed 2 only as the plan's single retry after an invalid output
TIMEOUT_S = 300
PORT = 8089


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ records
def load(label: str, cid: str) -> dict:
    bundle = REPLAYS / label
    if not verify_bundle(bundle)["ok"]:
        raise RuntimeError(f"bundle {label} failed verification")
    rec = next(json.loads(x) for x in (bundle / "run" / "compactions.jsonl").read_text().splitlines()
               if x.strip() and json.loads(x)["compaction_id"] == cid)
    if rec["mode"] != "baseline":
        raise RuntimeError("offline re-summarization is defined for baseline compactions (summary-only assembly)")
    raw = (bundle / "run" / rec["archive_ref"]).read_bytes()
    return {"label": label, "cid": cid, "record": rec, "archive_bytes": raw,
            "messages": ModelMessagesTypeAdapter.validate_json(raw)}


def summarizer_prompt(src: dict) -> dict:
    """The same instructions and user wrapper as MissionContinuity._summarize."""
    history = redact_text(render_messages(src["messages"]), [])
    return {"system": SUMMARIZER_INSTRUCTIONS, "user": f"Working history to compress:\n\n{history}"}


def checks_for(src: dict, summary: str) -> dict:
    """Rebuild the next request exactly as baseline compaction does, then apply the unchanged tests."""
    rec = src["record"]
    _, tail = _split(src["messages"])
    new_tail, _ = _stub_tail(tail)
    head = ModelRequest(parts=[UserPromptPart("Summary of the work so far:\n" + summary)])
    sent = (rec["instructions_text"] + "\n" + render_messages([head] + list(new_tail))).lower()
    exposed = rec["context_checks"]["cf_exposed"]          # identical prefix, so identical exposure
    present = [cf for cf in exposed if CF_TESTS[cf][1](sent)]
    kp = [t for t in KERNEL_TERMS if t.lower() in sent]
    return {"kernel_terms_present": kp, "kernel_terms_missing": [t for t in KERNEL_TERMS if t not in kp],
            "cf_exposed": exposed, "cf_present": present, "cf_missing": [cf for cf in exposed if cf not in present]}


def control(label: str, cid: str) -> dict:
    src = load(label, cid)
    rebuilt = checks_for(src, src["record"]["summary_text"])
    return {"ok": rebuilt == src["record"]["context_checks"], "rebuilt": rebuilt,
            "recorded": src["record"]["context_checks"]}


# ------------------------------------------------------------------ local model
def verify_model() -> dict:
    if not MODEL_PATH.exists():
        raise RuntimeError(f"model file missing: {MODEL_PATH.relative_to(REPO)}")
    sha = _sha_file(MODEL_PATH)
    if sha != MODEL["sha256"] or MODEL_PATH.stat().st_size != MODEL["bytes"]:
        raise RuntimeError("model checksum mismatch; delete the file and download again")
    return {"sha256": sha, "bytes": MODEL_PATH.stat().st_size}


def _http(method: str, url: str, body=None, timeout=TIMEOUT_S):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class Server:
    """llama-server on localhost for the duration of one run."""

    def __init__(self, port: int = PORT):
        exe = shutil.which("llama-server")
        if not exe:
            raise RuntimeError("llama-server not found (brew install llama.cpp)")
        self.exe, self.port, self.base = exe, port, f"http://127.0.0.1:{port}"
        self.version = subprocess.run([exe, "--version"], capture_output=True, text=True).stderr.strip().splitlines()[-2:]

    def __enter__(self):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", self.port)) == 0:
                raise RuntimeError(f"port {self.port} is already in use")
        log = (VAR / "models" / "llama-server.log").open("w")
        self.proc = subprocess.Popen([self.exe, "-m", str(MODEL_PATH), "-c", str(N_CTX), "-ngl", "99",
                                      "--host", "127.0.0.1", "--port", str(self.port), "--no-webui"],
                                     stdout=log, stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < 120:
            if self.proc.poll() is not None:
                raise RuntimeError("llama-server exited while loading the model (see var/models/llama-server.log)")
            try:
                if _http("GET", self.base + "/health", timeout=5).get("status") == "ok":
                    self.props = _http("GET", self.base + "/props", timeout=10)
                    return self
            except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
                pass
            time.sleep(1)
        raise RuntimeError("llama-server did not become ready within 120 s")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def smoke(self) -> str:
        r = _http("POST", self.base + "/v1/chat/completions",
                  {"messages": [{"role": "user", "content": "Reply with the single word: ready"}],
                   "max_tokens": 8, "temperature": 0.1, "seed": 1}, timeout=60)
        return r["choices"][0]["message"]["content"]

    def summarize(self, prompt: dict, seed: int) -> dict:
        schema = CompactionCandidate.model_json_schema()
        body = {"messages": [{"role": "system", "content": prompt["system"]},
                             {"role": "user", "content": prompt["user"]}],
                **SAMPLING, "seed": seed,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "CompactionCandidate", "schema": schema}}}
        t0 = time.time()
        r = _http("POST", self.base + "/v1/chat/completions", body)
        return {"seed": seed, "wall_s": round(time.time() - t0, 2), "raw": r["choices"][0]["message"]["content"],
                "finish_reason": r["choices"][0].get("finish_reason"), "usage": r.get("usage"),
                "timings": r.get("timings"), "request": {k: v for k, v in body.items() if k != "messages"}}


def _validate(raw: str):
    try:
        return CompactionCandidate.model_validate(json.loads(raw)), None
    except Exception as exc:   # invalid JSON or schema: a recorded outcome, never hand-edited
        return None, f"{type(exc).__name__}: {str(exc)[:300]}"


def run_liquid(label: str, cid: str) -> dict:
    ctrl = control(label, cid)
    if not ctrl["ok"]:
        raise RuntimeError("control failed: recorded Claude summary does not reproduce the recorded checks")
    out = OUT_ROOT / label / f"{cid}.liquid.json"
    if out.exists():
        raise RuntimeError(f"{out.relative_to(REPO)} exists; the plan allows one run (no re-runs for a better result)")
    src = load(label, cid)
    prompt = summarizer_prompt(src)
    model = verify_model()
    attempts, candidate, caps, error = [], None, None, None
    with Server() as srv:
        n_ctx = (srv.props.get("default_generation_settings") or {}).get("n_ctx") or srv.props.get("n_ctx")
        for seed in SEEDS:
            a = srv.summarize(prompt, seed)
            pt = (a.get("usage") or {}).get("prompt_tokens")
            if pt is None or pt >= N_CTX:
                raise RuntimeError(f"prompt tokens {pt} not below context {N_CTX}; refusing (no truncation)")
            cand, err = _validate(a["raw"])
            a.update({"valid": cand is not None, "validation_error": err})
            attempts.append(a)
            if cand is not None:
                candidate, caps = _cap(cand)
                break
        error = None if candidate else "Liquid did not produce a valid summary"
        server_info = {"llama_server_version": srv.version, "n_ctx": n_ctx}
    result = {
        "kind": "offline_exploratory_resummarization",
        "statement": ("Offline re-summarization of an archived compaction input. The recorded agent never "
                      "received this summary; it did not affect the recorded run or any recorded result, and "
                      "it has no Governor execution record."),
        "run": label, "compaction_id": cid,
        "input": {"archive": f"replays/{label}/run/{src['record']['archive_ref']}",
                  "archive_sha256": _sha_bytes(src["archive_bytes"]),
                  "prompt_sha256": _sha_bytes(json.dumps(prompt, sort_keys=True).encode())},
        "model": {**MODEL, "verified_sha256": model["sha256"], "runtime": "llama.cpp llama-server, Metal", **server_info},
        "sampling": SAMPLING, "retry_rule": "one retry with seed 2 only after invalid output",
        "attempts": attempts,
        "summary": candidate.summary if candidate else None,
        "proposals": [p.model_dump() for p in candidate.proposals] if candidate else [],
        "caps": caps, "error": error,
        "checks": checks_for(src, candidate.summary) if candidate else None,
        "control": {"recorded_summary_reproduces_recorded_checks": ctrl["ok"],
                    "recorded_checks": src["record"]["context_checks"],
                    "recorded_summary_words": len(src["record"]["summary_text"].split()),
                    "recorded_summarizer": src["record"]["summarizer"]},
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    leaks = [v for v in known_pii_values() if v in json.dumps(result)]
    if leaks:
        raise RuntimeError(f"refused to store: {len(leaks)} personal-data value(s) in the output")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return result


def verify(label: str, cid: str) -> dict:
    out = OUT_ROOT / label / f"{cid}.liquid.json"
    r = json.loads(out.read_text())
    src = load(label, cid)
    checks = {"bundle_verified": True,
              "archive_hash_matches": _sha_bytes(src["archive_bytes"]) == r["input"]["archive_sha256"],
              "prompt_hash_matches": _sha_bytes(json.dumps(summarizer_prompt(src), sort_keys=True).encode()) == r["input"]["prompt_sha256"],
              "control_reproduces_recorded_checks": control(label, cid)["ok"],
              "checks_recomputed_from_stored_summary": (r["summary"] is None and r["checks"] is None)
              or checks_for(src, r["summary"]) == r["checks"],
              "no_personal_data": not [v for v in known_pii_values() if v in json.dumps(r)]}
    return {"ok": all(checks.values()), "checks": checks}


def available(label: str, cid: str):
    out = OUT_ROOT / label / f"{cid}.liquid.json"
    return json.loads(out.read_text()) if out.exists() else None
