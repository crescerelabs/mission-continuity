"""Optional Black Forest Labs (FLUX 3) client for illustrative replay footage.

Used only by the visual reconstruction pipeline (reconstruct.py). Nothing here
touches replay bundles, results, Governor traces or RawTree.

API (https://api.bfl.ai/openapi.json, https://docs.bfl.ai/flux_3/flux3_video):
POST /v1/flux-3-video with header x-key returns {id, polling_url}; poll the
polling_url until a terminal status; result.sample is a signed mp4 URL that
expires, so the clip is downloaded immediately. GET /v1/credits is read-only.

Configuration: BFL_API_KEY (required; optional BFL_API_URL) from the environment
or the gitignored .env. The key is never printed or logged.

Spend: every submitted task is appended to var/bfl/ledger.jsonl with the settled
cost when the API reports one, otherwise the estimate from the published rate.
A submission is refused when ledger total + its estimate would exceed the cap.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mission_continuity.paths import REPO, VAR

LEDGER = VAR / "bfl" / "ledger.jsonl"
CAP_USD = 10.0
CREDIT_USD = 0.01  # "1 credit equals $0.01 USD"; settled `cost` is reported in credits
# Per output second (https://docs.bfl.ai/quick_start/pricing), hd class.
RATE = {"draft": 0.06, "hd": 0.17}
TERMINAL = {"Ready", "Request Moderated", "Content Moderated", "Error", "Task not found"}


class BFLError(RuntimeError):
    pass


def _env() -> dict:
    env = {}
    p = REPO / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("BFL_")})
    return env


def spent() -> float:
    if not LEDGER.exists():
        return 0.0
    rows = [json.loads(x) for x in LEDGER.read_text().splitlines() if x.strip()]
    return round(sum(r.get("cost_usd") if r.get("cost_usd") is not None else r["estimate_usd"] for r in rows), 4)


def estimate(payload: dict) -> float:
    secs = int(payload.get("duration") or 20)
    return round(secs * (RATE["draft"] if payload.get("draft") else RATE["hd"]), 4)


class Client:
    def __init__(self):
        env = _env()
        self._key = env.get("BFL_API_KEY")
        if not self._key:
            raise BFLError("BFL_API_KEY is not set (environment or .env)")
        self.base = env.get("BFL_API_URL", "https://api.bfl.ai").rstrip("/")

    def _req(self, method: str, url: str, body=None):
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"x-key": self._key, "accept": "application/json",
                                              "Content-Type": "application/json",
                                              "User-Agent": "mission-continuity/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="ignore")[:300].replace(self._key, "<key>")
            raise BFLError(f"{method} {url.split('?')[0]} -> {e.code}: {txt}")

    def credits(self) -> float:
        """Balance in credits (1 credit = $0.01)."""
        return float(self._req("GET", f"{self.base}/v1/credits")[1]["credits"])

    def generate_video(self, payload: dict, out_mp4: Path, tag: str, timeout_s: int = 1500) -> dict:
        """Submit, poll to a terminal status, download immediately. Returns provenance."""
        est = estimate(payload)
        if spent() + est > CAP_USD:
            raise BFLError(f"refused: BFL spend {spent():.2f} + estimate {est:.2f} would exceed cap {CAP_USD:.2f}")
        t0 = time.time()
        _, task = self._req("POST", f"{self.base}/v1/flux-3-video", payload)
        entry = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "tag": tag, "task_id": task["id"],
                 "mode": payload.get("mode"), "draft": bool(payload.get("draft")), "duration": payload.get("duration"),
                 "estimate_usd": est, "cost_usd": None, "status": "submitted"}
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        poll = task.get("polling_url") or f"{self.base}/v1/get_result?id={task['id']}"
        res = {"status": None}
        while res.get("status") not in TERMINAL:
            if time.time() - t0 > timeout_s:
                raise BFLError(f"timeout after {timeout_s}s (task {task['id']}, last status {res.get('status')})")
            time.sleep(6)
            _, res = self._req("GET", poll)
        latency = round(time.time() - t0, 1)
        cost = res.get("cost")
        entry.update({"status": res["status"], "latency_s": latency,
                      "cost_credits": cost, "cost_usd": (round(cost * CREDIT_USD, 4) if cost is not None else None)})
        with LEDGER.open("a") as f:  # settled row supersedes the submitted row for the same task
            f.write(json.dumps({**entry, "settles": task["id"]}) + "\n")
        _dedupe_ledger()
        if res["status"] != "Ready":
            raise BFLError(f"task {task['id']} ended {res['status']}")
        result = res.get("result") or {}
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(result["sample"], timeout=120) as r:
            out_mp4.write_bytes(r.read())
        draft_cache = None
        if result.get("draft_cache"):
            dc = VAR / "bfl" / f"{task['id']}.draft_cache.bin"
            with urllib.request.urlopen(result["draft_cache"], timeout=120) as r:
                dc.write_bytes(r.read())
            draft_cache = str(dc.relative_to(REPO))
        return {"task_id": task["id"], "status": res["status"], "latency_s": latency,
                "bfl_reported_prompt": result.get("prompt"), "bfl_reported_seed": result.get("seed"),
                "cost_usd_reported": entry["cost_usd"], "estimate_usd": est,
                "draft_cache_local": draft_cache, "result_keys": sorted(result)}


def _dedupe_ledger():
    rows = [json.loads(x) for x in LEDGER.read_text().splitlines() if x.strip()]
    last = {}
    for r in rows:
        last[r["task_id"]] = r
    LEDGER.write_text("".join(json.dumps(r) + "\n" for r in last.values()))
