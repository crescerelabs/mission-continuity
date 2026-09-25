"""Optional RawTree export of genuine recorded evidence.

Reads ONLY the committed, hash-verified replay bundles (replays/*). Builds flat
rows from explicit field allowlists: no prompts, model text, summaries, tool
arguments other than record ids, tool results, or free-text Governor fields.
Every outgoing row is scanned for the synthetic personal-data values and the
export refuses on any hit. Bundles are never modified.

Database selection on RawTree's shared cluster: every request sends BOTH
`?database=<name>` and the `x-rawtree-database` header, which RawTree requires
to agree (agent-skills references/api.md). Omitting them would fall back to the
key's stored default database.

Configuration: RAWTREE_API_KEY and RAWTREE_DATABASE (required; optional RAWTREE_API_URL)
from the environment or the gitignored .env. The key is never printed or logged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List

from mission_continuity.bundle import verify
from mission_continuity.compaction import KERNEL_TERMS
from mission_continuity.paths import REPLAYS, REPO, VAR
from mission_continuity.redact import known_pii_values

EXPORT_VERSION = 1
OUT_DIR = VAR / "rawtree"
SQL_DIR = REPO / "sql" / "rawtree"
TABLES = ["sentience_mc_runs", "sentience_mc_compactions", "sentience_mc_retention", "sentience_mc_tool_calls", "sentience_mc_governor_events"]
CF_IDS = ["CF1", "CF2", "CF3", "CF4", "CF5", "CF6"]
RECORD_ID = re.compile(r"^(TX|INV|CR|SE|SUP|POL-B)-[A-Z0-9-]+$|^C-\d+$")


class RawTreeError(RuntimeError):
    pass


def _eid(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _jl(p: Path) -> list:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _csv(xs) -> str:
    return ",".join(str(x) for x in (xs or []))


def bundle_rows(bundle: Path) -> Dict[str, List[dict]]:
    """All rows for one verified bundle. Explicit allowlists only."""
    v = verify(bundle)
    if not v["ok"]:
        raise RawTreeError(f"bundle {bundle.name} failed verification: {v}")
    manifest_sha = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
    man = json.loads((bundle / "manifest.json").read_text())
    run = bundle / "run"
    meta = json.loads((run / "run.json").read_text())
    res = json.loads((run / "results.json").read_text())
    comps = _jl(run / "compactions.jsonl")
    tools = _jl(run / "tools.jsonl")
    rid = bundle.name
    group = "exploratory" if res["arm"] == "E1x" else "preregistered"
    fault = res.get("fault_injection") or "none"
    condition = "natural" if fault == "none" else f"induced:{fault}"
    common = {"run_id": rid, "run_group": group, "mode": res["mode"], "condition": condition,
              "bundle_manifest_sha256": manifest_sha, "app_commit": man.get("app_commit"),
              "export_version": EXPORT_VERSION}

    # Governor events, from the library-written traces in the bundle.
    gov = []
    for tr in sorted((bundle / "traces").glob("*.jsonl")):
        for e in _jl(tr):
            p = e.get("payload") or {}
            gov.append({
                "event_id": _eid(rid, "gov", e["event_id"]), **common,
                "governor_event_id": e["event_id"], "session_id": e["session_id"], "agent_id": e.get("agent_id"),
                "sequence": e.get("event_sequence_number"), "event_type": e["event_type"],
                "timestamp_utc": e.get("timestamp_utc"), "tool_use_id": p.get("tool_use_id") or "",
                "target_system": p.get("target_system") or "", "operation_type": p.get("operation_type") or "",
                "advisory_flags": _csv(e.get("advisory_flags")), "policy_violations": _csv(e.get("policy_violations")),
                "pass_through": bool(e.get("pass_through")),
                "context_size_tokens": p.get("context_size_tokens"), "llm_turn_id": p.get("llm_turn_id") or "",
                "llm_prompt_tokens": p.get("llm_prompt_tokens"), "llm_completion_tokens": p.get("llm_completion_tokens"),
            })
    asserted = {g["tool_use_id"]: g for g in gov if g["event_type"] == "SCOPE_ASSERTED" and g["tool_use_id"]}

    first_step = comps[0]["run_step"] if comps else None
    tool_rows = []
    for t in tools:
        g = asserted.get(t["tool_call_id"])
        refs = [str(v) for v in (t.get("args") or {}).values() if isinstance(v, str) and RECORD_ID.match(v)]
        tool_rows.append({
            "event_id": _eid(rid, "tool", t["tool_call_id"]), **common,
            "tool_call_id": t["tool_call_id"], "request_step": t.get("run_step"), "tool_name": t["tool_name"],
            "category": t.get("category"),
            "app_outcome": {"intervention": "guard_denial"}.get(t.get("effect"), t.get("effect") or "none"),
            "record_ref": _csv(refs),
            "after_first_compaction": first_step is not None and t.get("run_step", 0) >= first_step,
            "governor_recorded": g is not None, "target_system": g["target_system"] if g else "",
            "governor_flags": g["advisory_flags"] if g else "", "governor_violations": g["policy_violations"] if g else "",
            "governor_event_id": g["governor_event_id"] if g else "",
        })

    comp_rows, ret_rows = [], []
    for i, c in enumerate(comps):
        cc, ct, sm = c["context_checks"], c.get("counted") or {}, c.get("summarizer") or {}
        ratio = ct.get("ratio")
        comp_rows.append({
            "event_id": _eid(rid, "cmp", c["compaction_id"]), **common,
            "compaction_id": c["compaction_id"], "request_step": c["run_step"], "first_compaction": i == 0,
            "trigger_input_tokens_last": c["trigger"]["input_tokens_last"], "threshold": c["trigger"]["threshold"],
            "counted_uncompacted": ct.get("uncompacted"), "counted_compacted": ct.get("compacted"),
            "fixed_overhead": ct.get("fixed_overhead"), "tokens_saved": ct.get("tokens_saved"),
            "reduction_pct": ct.get("reduction_pct"), "ratio": ratio,
            "a2_met": ratio is not None and ratio <= 0.60,
            "facts_tracked": len(cc["cf_exposed"]), "facts_present": len(cc["cf_present"]),
            "mission_terms_missing": len(cc["kernel_terms_missing"]),
            "summarizer_input_tokens": sm.get("input_tokens"), "summarizer_output_tokens": sm.get("output_tokens"),
            "stubbed_results": len(c.get("tail_stubbed_tool_call_ids") or []),
        })
        for cf in CF_IDS:
            status = ("present" if cf in cc["cf_present"] else "missing" if cf in cc["cf_missing"]
                      else "not_compacted_yet")
            ret_rows.append({"event_id": _eid(rid, "ret", c["compaction_id"], cf), **common,
                             "compaction_id": c["compaction_id"], "request_step": c["run_step"],
                             "first_compaction": i == 0, "item_type": "fact", "item": cf, "status": status})
        for term in KERNEL_TERMS:
            status = "missing" if term in cc["kernel_terms_missing"] else "present"
            ret_rows.append({"event_id": _eid(rid, "ret", c["compaction_id"], term), **common,
                             "compaction_id": c["compaction_id"], "request_step": c["run_step"],
                             "first_compaction": i == 0, "item_type": "mission_term", "item": term, "status": status})

    corr, cost = res["correctness"], res["cost"]
    runs = [{"event_id": _eid(rid, "run"), **common, "arm": res["arm"], "outcome": res["outcome"],
             "score": corr["score"], **{k.lower(): int(bool(corr[k])) for k in ("R1", "R2", "R3", "R4", "R5")},
             "input_tokens": cost["input_tokens"], "output_tokens": cost["output_tokens"],
             "summarizer_input_tokens": cost["summarizer_input_tokens"],
             "summarizer_output_tokens": cost["summarizer_output_tokens"],
             "estimated_usd": cost["estimated_usd"], "wall_seconds": cost["wall_seconds"],
             "compactions": len(comps), "tool_calls": len(tools), "governor_sessions": cost["governor_sessions"],
             "kernel_sha256": meta.get("kernel_sha256"), "dataset_sha256": meta.get("dataset_sha256"),
             "policy_sha256": meta.get("policy_sha256")}]
    return {"sentience_mc_runs": runs, "sentience_mc_compactions": comp_rows, "sentience_mc_retention": ret_rows,
            "sentience_mc_tool_calls": tool_rows, "sentience_mc_governor_events": gov}


def leak_scan(rows_by_table: Dict[str, List[dict]]) -> List[str]:
    blob = json.dumps(rows_by_table)
    return [v for v in known_pii_values() if v in blob]


def bundles() -> List[Path]:
    return sorted(p for p in REPLAYS.iterdir() if (p / "manifest.json").exists())


def export(out_dir: Path = OUT_DIR) -> dict:
    per_bundle = {b.name: bundle_rows(b) for b in bundles()}
    merged = {t: [r for rows in per_bundle.values() for r in rows[t]] for t in TABLES}
    leaks = leak_scan(merged)
    if leaks:
        raise RawTreeError(f"export refused: {len(leaks)} personal-data value(s) found")
    out_dir.mkdir(parents=True, exist_ok=True)
    for t, rows in merged.items():
        (out_dir / f"{t}.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    summary = {"export_version": EXPORT_VERSION, "bundles": sorted(per_bundle),
               "row_counts": {t: len(r) for t, r in merged.items()}, "leak_scan": "0 values found"}
    (out_dir / "export_manifest.json").write_text(json.dumps(summary, indent=1))
    return {"summary": summary, "per_bundle": per_bundle}


# ------------------------------------------------------------------ HTTP (live)
def _env() -> dict:
    env = {}
    p = REPO / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("RAWTREE_")})
    return env


class Client:
    def __init__(self):
        env = _env()
        self._key = env.get("RAWTREE_API_KEY")
        if not self._key:
            raise RawTreeError("RAWTREE_API_KEY is not set (environment or .env)")
        self.database = env.get("RAWTREE_DATABASE")
        if not self.database:
            raise RawTreeError("RAWTREE_DATABASE is not set; the database is never chosen implicitly")
        self.base = env.get("RAWTREE_API_URL", "https://api.rawtree.com").rstrip("/")

    def _call(self, method, path, body=None, params=None, scoped=True):
        q = dict(params or {})
        headers = {"Authorization": f"Bearer {self._key}", "User-Agent": "mission-continuity/0.1",
                   "Content-Type": "application/json"}
        if scoped:
            q["database"] = self.database
            headers["x-rawtree-database"] = self.database
        url = self.base + path + ("?" + urllib.parse.urlencode(q) if q else "")
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode() or "null"), dict(r.headers)
        except urllib.error.HTTPError as e:
            txt = e.read().decode()
            raise RawTreeError(f"{method} {path} -> {e.code}: {txt[:300].replace(self._key, '<key>')}")

    def health(self):
        return self._call("GET", "/health", scoped=False)[1]

    def tables(self):
        return self._call("GET", "/v1/tables")[1]

    def describe(self, table):
        return self._call("GET", f"/v1/tables/{table}")[1]

    def query(self, sql: str, query_id: str | None = None):
        body = {"sql": sql, "format": "JSON", **({"query_id": query_id} if query_id else {})}
        code, data, headers = self._call("POST", "/v1/query", body=body)
        return data

    def insert(self, table: str, rows: List[dict], dedup_token: str):
        code, data, headers = self._call("POST", f"/v1/tables/{table}", body=rows,
                                         params={"insert_deduplication_token": dedup_token})
        return data, headers.get("X-ClickHouse-Query-Id") or headers.get("x-clickhouse-query-id")


def existing_own_tables(client: Client) -> Dict[str, bool]:
    """Whether each of OUR table names exists, probing only our names (no listing of others)."""
    found = {}
    for t in TABLES:
        try:
            client.describe(t)
            found[t] = True
        except RawTreeError as e:
            if "-> 404" in str(e) or "not found" in str(e).lower():
                found[t] = False
            else:
                raise
    return found


def check(client: Client) -> dict:
    """R1: connectivity and database selection, read-only."""
    out = {"health": client.health()}
    db = client.query("SELECT currentDatabase() AS db")
    out["current_database"] = db["data"][0]["db"]
    out["selected_database"] = client.database
    out["database_ok"] = out["current_database"] == client.database
    out["own_tables_exist"] = existing_own_tables(client)
    return out


def push(client: Client, per_bundle: dict) -> List[dict]:
    """R2: one insert per table per bundle, each with a deterministic deduplication token."""
    report = []
    for name, tables in sorted(per_bundle.items()):
        sha = tables["sentience_mc_runs"][0]["bundle_manifest_sha256"]
        for t in TABLES:
            rows = tables[t]
            if not rows:
                continue
            token = _eid(t, sha, EXPORT_VERSION)
            data, qid = client.insert(t, rows, token)
            report.append({"bundle": name, "table": t, "rows": len(rows),
                           "inserted": (data or {}).get("inserted"), "query_id": qid})
    return report


def run_sql(client: Client, name: str) -> dict:
    sql = (SQL_DIR / f"{name}.sql").read_text()
    return client.query(sql, query_id=None)
