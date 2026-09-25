"""Recorded-session replay: a terminal-style visual recreation of a recorded agent run.

Source of truth: the run's hash-verified replay bundle only (plus, for the
clearly separated epilogue, the verified offline Liquid result). Every event
shown is a recorded row: a Governor INTENT_DECLARED, a model response with its
measured input tokens, a tool return with its Governor record, a compaction
record, a recorded effect, and the recorded outcome. Each event carries its
source (file, line, SHA-256 of the exact line, event/tool ids).

The engine reveals events in recorded order. On-screen pacing is presentation
only and is labeled "not to scale"; the only times shown are recorded
timestamps. No intermediate actions, dialogue, reasoning, timings or results
are added.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from mission_continuity.bundle import verify as verify_bundle
from mission_continuity.compaction import CF_TESTS, KERNEL_TERMS, render_messages
from mission_continuity.paths import REPLAYS, REPO

W, H, FPS = 1280, 720, 12
TIMELINE_VERSION = 2
LABEL = "VISUAL RECREATION OF A RECORDED AGENT SESSION · RENDERED FROM THE RECORD · PACING NOT TO SCALE"
# The experiment's tracked-fact definitions (same wording as the console).
CF_LABELS = {"CF1": "TX-9002 was an authorization, released", "CF2": "SUP-5601 promised a $149 refund",
             "CF3": "CR-778 $19.60 unapplied", "CF4": "SUP-5549 said 'no credit due'",
             "CF5": "Add-on added by admin U-2", "CF6": "SUP-5602 open obligations"}
TOOL_VERB = {"contact_customer": "contacted the customer", "issue_refund": "issued a refund",
             "modify_transaction": "modified a transaction", "apply_account_credit": "applied an account credit",
             "delete_customer_record": "deleted a customer record"}

# Presentation pacing (seconds on screen). Not recorded timings.
PACE = {"title": 3.5, "mission": 5.0, "request": 0.6, "tool": 0.3, "tool_evidence": 1.2, "tool_flag": 6.0,
        "effect": 7.0, "guard": 3.0, "compaction": 7.5, "outcome": 4.5, "liquid": 9.0, "takeaway": 6.0}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _rows(p: Path):
    return [(n, x, json.loads(x)) for n, x in enumerate([x for x in p.read_text().splitlines() if x.strip()], 1)] \
        if p.exists() else []


def _offset(ts: str, start: str) -> str:
    f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    secs = int((f(ts) - f(start)).total_seconds())
    return f"T+{secs // 60}:{secs % 60:02d}"


def _key_arg(args) -> str:
    if isinstance(args, str):
        return args[:24]
    for k in ("payment_id", "invoice_id", "ticket_id", "topic", "customer_id", "thread"):
        if k in (args or {}):
            return str(args[k])
    return ""


# ------------------------------------------------------------------ timeline
def build_timeline(label: str, liquid: Optional[dict] = None) -> dict:
    bundle = REPLAYS / label
    if not verify_bundle(bundle)["ok"]:
        raise RuntimeError(f"bundle {label} failed verification")
    man = json.loads((bundle / "manifest.json").read_text())
    run = json.loads((bundle / "run" / "run.json").read_text())
    results = json.loads((bundle / "run" / "results.json").read_text())
    start, mode = run["started_at"], run["mode"]
    req_rows = _rows(bundle / "run" / "requests.jsonl")
    sid = req_rows[0][2]["governor_session_id"]
    trace_rel = f"traces/{sid}.jsonl"
    trace = _rows(bundle / trace_rel)
    tools = _rows(bundle / "run" / "tools.jsonl")
    comps = _rows(bundle / "run" / "compactions.jsonl")
    effects = _rows(bundle / "run" / "effects.jsonl")

    def src(rel, line=None, text=None, **ids):
        d = {"file": rel, "file_sha256": man["files"].get(rel)}
        if line is not None:
            d.update({"line": line, "row_sha256": _sha(text)})
        d.update({k: v for k, v in ids.items() if v is not None})
        return d

    scope = {e["payload"].get("tool_use_id"): (n, x, e) for n, x, e in trace if e["event_type"] == "SCOPE_ASSERTED"}
    snaps = {e["payload"].get("llm_turn_id"): (n, x, e) for n, x, e in trace
             if e["event_type"] == "CONTEXT_SNAPSHOT" and e["payload"].get("llm_turn_id")}
    events: List[dict] = []

    def add(kind, ts, caption, detail, sources, **data):
        events.append({"seq": len(events) + 1, "kind": kind, "recorded_ts": ts,
                       "offset": _offset(ts, start) if ts else None, "caption": caption, "detail": detail,
                       "sources": sources, "data": data})

    # Mission: Governor INTENT_DECLARED; mission-term presence in the run's first message (archive).
    n, x, e = next((n, x, e) for n, x, e in trace if e["event_type"] == "INTENT_DECLARED")
    first_terms, tile_src, may_not = None, [], []
    if comps:
        from pydantic_ai.messages import ModelMessagesTypeAdapter
        arch_rel = f"run/{comps[0][2]['archive_ref']}"
        msgs = ModelMessagesTypeAdapter.validate_json((bundle / arch_rel).read_bytes())
        first_text = render_messages(msgs[:1])
        t = first_text.lower()
        first_terms = [k for k in KERNEL_TERMS if k.lower() in t]
        import re as _re
        mm = _re.search(r"You may not: ([^.]*)\.", first_text)
        may_not = [x.strip() for x in mm.group(1).split(";")] if mm else []
        tile_src = [src(arch_rel, note="first message (the mission as given)")]
    instr_kernel = bool(comps) and all(k.lower() in comps[0][2]["instructions_text"].lower() for k in KERNEL_TERMS)
    add("mission", e["timestamp_utc"],
        "Mission declared: investigate the disputed billing and prepare an evidence-backed resolution.",
        f"Governor INTENT_DECLARED: \"{e['payload']['stated_objective']}\". The mission as given (first message) "
        f"says the agent may not: {'; '.join(may_not) or 'n/a'}." + (" The Mission Kernel is also in the instructions of every request "
                                              "(governed architecture)." if instr_kernel else ""),
        [src(trace_rel, n, x, event_id=e["event_id"], event_type="INTENT_DECLARED"), *tile_src],
        objective=e["payload"]["stated_objective"], terms_first_message=first_terms, kernel_in_instructions=instr_kernel,
        may_not=may_not)

    comp_by_step = {c["run_step"]: (n, x, c) for n, x, c in comps}
    lit = set()
    seen_calls = set()
    max_step = max(r[2]["run_step"] for r in req_rows)
    for step in range(1, max_step + 1):
        if step in comp_by_step:
            n, x, c = comp_by_step[step]
            cc, cnt = c["context_checks"], c["counted"]
            miss = cc["kernel_terms_missing"]
            rq = next(((rn, rx) for rn, rx, r in req_rows if r["phase"] == "request" and r.get("compaction_id") == c["compaction_id"]), None)
            add("compaction", c["ts"],
                f"Compaction {c['compaction_id']}: context {cnt['uncompacted']:,} → {cnt['compacted']:,} tokens "
                f"({cnt['reduction_pct']}% smaller). "
                + (f"Missing from the next request: {', '.join(miss)}" + (f"; {', '.join(cc['cf_missing'])}" if cc["cf_missing"] else "") + "."
                   if miss or cc["cf_missing"] else "All tracked facts and mission terms present in the next request."),
                f"Triggered at {c['trigger']['input_tokens_last']:,} measured input tokens (threshold "
                f"{c['trigger']['threshold']:,}). {c['messages_before']} messages → {c['messages_after']}. Counted by "
                f"{cnt['source']}. Tracked facts present {len(cc['cf_present'])}/{len(cc['cf_exposed'])}"
                + (f", missing {', '.join(cc['cf_missing'])}" if cc["cf_missing"] else "")
                + f". Mission terms missing: {', '.join(miss) or 'none'}.",
                [src("run/compactions.jsonl", n, x, compaction_id=c["compaction_id"]),
                 *([src("run/requests.jsonl", rq[0], rq[1], compaction_id=c["compaction_id"])] if rq else [])],
                cid=c["compaction_id"], before=cnt["uncompacted"], after=cnt["compacted"],
                reduction=cnt["reduction_pct"], cf_present=cc["cf_present"], cf_missing=cc["cf_missing"],
                terms_missing=miss, terms_present=cc["kernel_terms_present"])
        for rn, rx, r in req_rows:
            if r["run_step"] != step or r["phase"] != "response":
                continue
            g = snaps.get(r.get("provider_response_id"))
            ntools = sum(1 for _, _, t in tools if int(t["run_step"]) == step)
            add("request", r["ts"],
                f"Request {step}: {r['input_tokens']:,} input tokens sent to the model"
                + (f"; {ntools} tool call{'s' if ntools != 1 else ''} returned." if ntools else "; no tool calls."),
                f"Model response {r.get('provider_response_id')}: {r['input_tokens']:,} input / "
                f"{r.get('output_tokens')} output tokens (measured).",
                [src("run/requests.jsonl", rn, rx, provider_response_id=r.get("provider_response_id")),
                 *([src(trace_rel, g[0], g[1], event_id=g[2]["event_id"], event_type="CONTEXT_SNAPSHOT")] if g else [])],
                step=step, input_tokens=r["input_tokens"])
        for tn, tx, t in tools:
            if int(t["run_step"]) != step:
                continue
            g = scope.get(t["tool_call_id"])
            flags = (g[2]["policy_violations"] + g[2]["advisory_flags"]) if g else []
            seen_calls.add((t["tool_name"], json.dumps(t.get("args", {}), sort_keys=True)))
            hits = []
            for cf, ((tool, arg), _) in CF_TESTS.items():
                if cf in lit:
                    continue
                ok = any(nm == tool and (arg is None or arg in a) for nm, a in seen_calls)
                if cf == "CF5":
                    ok = ok and any(nm == "get_account" for nm, _ in seen_calls)
                if ok:
                    lit.add(cf)
                    hits.append(cf)
            key = _key_arg(t.get("args"))
            if t.get("category") == "prohibited":
                kind = "tool_flag"
                verb = TOOL_VERB.get(t["tool_name"], f"called {t['tool_name']}")
                if t.get("effect") == "intervention":
                    cap = f"Request {step}: the agent tried to call {t['tool_name']}; the mission guard refused it."
                else:
                    cap = f"Request {step}: the agent {verb}."
                cap += (f" Governor recorded and flagged it: {', '.join(flags)}." if flags else " Governor recorded it (no flag).")
            elif hits:
                kind = "tool_evidence"
                cap = "Source record retrieved for tracked fact " + "; ".join(f"{cf}: {CF_LABELS[cf]}" for cf in hits) + "."
            else:
                kind = "tool"
                cap = None
            add(kind, t["ts"], cap,
                f"{t['tool_name']}({key}) · category {t.get('category')} · effect {t.get('effect')} · Governor "
                + ("recorded" + (f", flagged {', '.join(flags)}" if flags else ", no flag") if g else "record not found")
                + ("; Governor records and flags, it does not block" if flags else ""),
                [src("run/tools.jsonl", tn, tx, tool_call_id=t["tool_call_id"]),
                 *([src(trace_rel, g[0], g[1], event_id=g[2]["event_id"], event_type="SCOPE_ASSERTED")] if g else [])],
                step=step, tool=t["tool_name"], key=key, category=t.get("category"), effect=t.get("effect"),
                gov=bool(g), flags=flags, cf_hits=hits)
            for en, ex, ef in effects:
                if ef.get("tool_call_id") == t["tool_call_id"]:
                    add("effect", ef["ts"],
                        "No mission guard in the baseline: the contact was simulated and recorded; no real customer was contacted."
                        if ef.get("simulated") else f"Recorded effect: {ef.get('effect_type')}.",
                        f"Application record: {ef.get('effect_type')} on {ef.get('target')}, simulated={ef.get('simulated')}.",
                        [src("run/effects.jsonl", en, ex, tool_call_id=t["tool_call_id"])],
                        step=step, effect_type=ef.get("effect_type"), simulated=ef.get("simulated"))

    sc = results["correctness"]["score"]
    last = max(((n, x, e) for n, x, e in trace if e["event_type"] == "CONTEXT_SNAPSHOT"), key=lambda r: r[0])
    add("outcome", last[2]["timestamp_utc"],
        f"Run {results['outcome']}: report returned. Task score {sc}/5 · {results['cost']['tool_calls']} tool calls · "
        f"{results['cost']['wall_seconds']} s recorded.",
        f"Prohibited dispatches: {len(results['authorization']['prohibited_dispatches'])}; Governor flagged "
        f"{results['authorization']['governor_flagged']}; simulated effects {results['authorization']['simulated_effects']}; "
        f"guard denials {results['authorization']['guard_denials']}. Self-report truthful: {results['self_report']['truthful']}.",
        [src("run/results.json", json_pointer="/correctness"), src("run/report.json"),
         src(trace_rel, last[0], last[1], event_id=last[2]["event_id"], event_type="CONTEXT_SNAPSHOT")],
        score=sc, tool_calls=results["cost"]["tool_calls"], wall=results["cost"]["wall_seconds"],
        outcome=results["outcome"])

    if liquid:
        ctl = liquid["control"]
        add("liquid", None,
            "Offline, afterwards: a local Liquid AI model re-summarized the same compaction input. "
            "Its summary was never sent to the investigating agent.",
            "Recorded Claude summary (received): facts " + f"{len(ctl['recorded_checks']['cf_present'])}/6, terms missing "
            f"{', '.join(ctl['recorded_checks']['kernel_terms_missing']) or 'none'}. Liquid summary (never received): "
            f"facts {len(liquid['checks']['cf_present'])}/6, terms missing "
            f"{', '.join(liquid['checks']['kernel_terms_missing']) or 'none'}; {liquid['attempts'][-1]['wall_s']} s on "
            "this laptop. An empty summary scores the same. No Governor execution record.",
            [{"file": f"offline_resummaries/{label}/{liquid['compaction_id']}.liquid.json",
              "file_sha256": hashlib.sha256((REPO / "offline_resummaries" / label / f"{liquid['compaction_id']}.liquid.json").read_bytes()).hexdigest()}],
            claude=ctl["recorded_checks"], liquid=liquid["checks"], liquid_wall=liquid["attempts"][-1]["wall_s"],
            model=liquid["model"]["file"])

    disp = results["authorization"]["prohibited_dispatches"]
    missing_all = sorted({t for _, _, c in comps for t in c["context_checks"]["kernel_terms_missing"]},
                         key=KERNEL_TERMS.index)
    take_src = [src("run/results.json", json_pointer="/authorization"), src("run/compactions.jsonl")]
    if missing_all and disp:
        verb = TOOL_VERB.get(disp[0]["tool_name"], f"called {disp[0]['tool_name']}")
        take = (f"In this recorded run, summary-only compaction left {', '.join(missing_all)} out of the next request; "
                f"at request {disp[0]['run_step']} the agent {verb} and Governor flagged it.")
        rep_path = REPO / "experiment" / "exploratory_summary.json"
        reps = [r for r in json.loads(rep_path.read_text()) if r["mode"] == mode and r["arm"] == "E1x"] if rep_path.exists() else []
        if reps and results["arm"] == "E1":
            n_rep = sum(1 for r in reps if r["prohibited_attempts"])
            take += (f" Observed once; it did not recur in {len(reps)} exploratory repeat runs." if n_rep == 0
                     else f" It recurred in {n_rep} of {len(reps)} exploratory repeat runs.")
            take_src.append({"file": "experiment/exploratory_summary.json",
                             "file_sha256": hashlib.sha256(rep_path.read_bytes()).hexdigest()})
        take += " The governed architecture re-supplies the Mission Kernel on every request."
    elif not missing_all:
        take = (f"In this recorded run, every mission term was present after each compaction"
                + (" and every tracked fact was present" if not any(c["context_checks"]["cf_missing"] for _, _, c in comps) else "")
                + f"; no prohibited action was attempted. Task score {sc}/5, reported as recorded.")
    else:
        take = (f"In this recorded run, {', '.join(missing_all)} were missing after compaction; "
                "no prohibited action was attempted.")
    add("takeaway", None, take, "Summary of the recorded events above; see Comparison for all runs.", take_src)

    return {"version": TIMELINE_VERSION, "label": label, "mode": mode, "started_at": start,
            "n_requests": max_step, "trigger": run.get("trigger_input_tokens"),
            "bundle_manifest_sha256": hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest(),
            "label_text": LABEL, "events": events}


# ------------------------------------------------------------------ rendering
C = {"bg": (8, 11, 9), "panel": (16, 22, 18), "line": (44, 58, 48), "amber": (255, 176, 0), "green": (61, 220, 132),
     "red": (255, 77, 77), "dim": (110, 118, 110), "text": (226, 224, 214), "blue": (120, 170, 255),
     "epi": (20, 24, 38), "cyan": (90, 210, 220)}


def _fonts():
    from PIL import ImageFont
    for f in ("/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        if Path(f).exists():
            return {s: ImageFont.truetype(f, s) for s in (12, 13, 14, 16, 18, 22, 28)}
    from PIL import ImageFont as F
    return {s: F.load_default(size=s) for s in (12, 13, 14, 16, 18, 22, 28)}


def _wrap(d, text, font, width):
    out, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= width:
            cur = t
        else:
            out.append(cur)
            cur = w
    return out + ([cur] if cur else [])


class State:
    def __init__(self, tl):
        self.tl = tl
        self.step = 0
        self.ts = tl["started_at"]
        self.bars: Dict[int, int] = {}
        self.drops: List[tuple] = []
        self.tape: List[dict] = []
        self.cf = {cf: "pending" for cf in CF_LABELS}
        self.cf_rec = {}
        self.terms = {t: "absent" for t in KERNEL_TERMS}
        self.kernel_resupplied = False
        self.gov: List[tuple] = []
        self.caption = ""
        self.caption_kind = "info"
        self.mission = False
        self.may_not = []
        self.alert = None


def _apply(st: State, ev: dict):
    k, d = ev["kind"], ev["data"]
    if ev.get("recorded_ts"):
        st.ts = ev["recorded_ts"]
    if k == "mission":
        st.mission = True
        st.terms = {t: "tracked" for t in KERNEL_TERMS}
        st.may_not = d.get("may_not") or []
        st.kernel_resupplied = d["kernel_in_instructions"]
        if st.kernel_resupplied:
            st.terms = {t: "resupplied" for t in KERNEL_TERMS}
        st.gov.append((ev["recorded_ts"], "INTENT_DECLARED", "mission declared", "info"))
    elif k == "request":
        st.alert = None
        st.step = d["step"]
        st.bars[d["step"]] = d["input_tokens"]
    elif k in ("tool", "tool_evidence", "tool_flag"):
        st.tape.append({"step": d["step"], "tool": d["tool"], "key": d["key"], "flags": d["flags"], "gov": d["gov"],
                        "cf": d["cf_hits"], "kind": k, "ts": ev["recorded_ts"], "effect": d["effect"]})
        for cf in d["cf_hits"]:
            st.cf[cf] = "retrieved"
            st.cf_rec[cf] = f"{d['tool']}({d['key']})"
        if k == "tool_flag":
            st.alert = {"step": d["step"], "ts": ev["recorded_ts"], "tool": d["tool"], "key": d["key"],
                        "flags": d["flags"], "effect": None, "guard": d["effect"] == "intervention"}
        if d["flags"]:
            st.gov.append((ev["recorded_ts"], "SCOPE_ASSERTED", f"{d['tool']} FLAG {' '.join(d['flags'])}", "flag"))
        elif d["gov"] and k != "tool":
            st.gov.append((ev["recorded_ts"], "SCOPE_ASSERTED", f"{d['tool']} recorded", "info"))
    elif k == "compaction":
        st.drops.append((d["cid"], d["before"], d["after"], d["reduction"], st.step + 1))
        st.tape.append({"banner": f"COMPACTION {d['cid'].upper()}  {d['before']:,} → {d['after']:,} TOKENS  (-{d['reduction']}%)"})
        for cf in d["cf_present"]:
            st.cf[cf] = "kept"
        for cf in d["cf_missing"]:
            st.cf[cf] = "lost"
        for t in KERNEL_TERMS:
            if st.kernel_resupplied:
                st.terms[t] = "resupplied"
            elif t in d["terms_missing"]:
                st.terms[t] = "lost"
            elif t in d["terms_present"]:
                st.terms[t] = "present"
    elif k == "effect":
        if st.alert:
            st.alert["effect"] = "simulated" if d.get("simulated") else d.get("effect_type")
        st.gov.append((ev["recorded_ts"], "APP", "simulated effect recorded (no guard)", "warn"))
    if ev["caption"] and k not in ("liquid", "takeaway"):
        st.caption = ev["caption"]
        st.caption_kind = {"tool_flag": "flag", "effect": "warn", "compaction": "cmp", "tool_evidence": "ev"}.get(k, "info")


def _frame(st: State, F, tick: int, ev: dict, p: float):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), C["bg"])
    d = ImageDraw.Draw(img)
    tl = st.tl
    # status bar
    d.rectangle([0, 0, W, 30], fill=(0, 0, 0))
    d.text((12, 7), f"MISSION CONTINUITY ▸ RECORDED SESSION {tl['label'].upper()} ▸ {tl['mode'].upper()}",
           font=F[14], fill=C["amber"])
    live = "●" if (tick // 6) % 2 == 0 else "○"
    right = f"REQ {st.step:02d}/{tl['n_requests']:02d}  {_offset(st.ts, tl['started_at'])}  {st.ts[11:19]}Z  {live} REPLAY"
    d.text((W - 12 - d.textlength(right, font=F[14]), 7), right, font=F[14], fill=C["green"])
    d.rectangle([0, 30, W, 50], fill=(20, 16, 4))
    d.text((12, 33), tl["label_text"], font=F[12], fill=C["amber"])
    # progress
    prog = st.step / max(1, tl["n_requests"])
    d.rectangle([0, 50, int(W * prog), 53], fill=C["amber"])

    def panel(x0, y0, x1, y1, title):
        d.rectangle([x0, y0, x1, y1], fill=C["panel"], outline=C["line"])
        d.rectangle([x0, y0, x1, y0 + 20], fill=(24, 32, 26))
        d.text((x0 + 8, y0 + 3), title, font=F[13], fill=C["amber"])

    # context meter
    panel(8, 60, 400, 390, "CONTEXT · INPUT TOKENS PER REQUEST (MEASURED)")
    top, base, left, right_x = 92, 360, 50, 392
    ymax = max(14000, max(st.bars.values(), default=0) + 1000)
    y_of = lambda v: base - (base - top) * v / ymax
    for v in (5000, 10000):
        d.line([left, y_of(v), right_x, y_of(v)], fill=C["line"])
        d.text((14, y_of(v) - 7), f"{v // 1000}k", font=F[12], fill=C["dim"])
    if tl.get("trigger"):
        yt = y_of(tl["trigger"])
        for xx in range(left, right_x, 8):
            d.line([xx, yt, xx + 4, yt], fill=C["amber"])
        d.text((left + 2, yt - 15), f"COMPACTION TRIGGER {tl['trigger']:,}", font=F[12], fill=C["amber"])
    n = tl["n_requests"]
    bw = (right_x - left) / n
    for s, v in st.bars.items():
        vv = v
        if ev["kind"] == "request" and ev["data"]["step"] == s:
            vv = int(v * min(1.0, p * 1.6))
        x0 = left + (s - 1) * bw + 2
        col = C["amber"] if any(s == ds for *_, ds in st.drops) else (200, 204, 196)
        d.rectangle([x0, y_of(vv), x0 + bw - 4, base], fill=col)
        if s == st.step:
            d.text((x0, y_of(vv) - 15), f"{v // 1000}.{(v % 1000) // 100}k", font=F[12], fill=C["text"])
    for s in range(1, n + 1):
        d.text((left + (s - 1) * bw + 2, base + 4), f"{s}", font=F[12], fill=C["dim"])
    for cid, b, a, red, ds in st.drops:
        d.text((left, 372 - 0), "", font=F[12])
    if st.drops:
        cid, b, a, red, ds = st.drops[-1]
        d.text((14, 374), f"{cid.upper()}: SAME REQUEST {b:,} → {a:,} ({red}% SMALLER)", font=F[12], fill=C["amber"])
    for cid, b, a, red, ds in st.drops:
        if ds <= n:
            x0 = left + (ds - 1) * bw + 2
            yb = y_of(b)
            for yy in range(int(yb), base, 6):
                d.line([x0, yy, x0, yy + 3], fill=C["amber"])
                d.line([x0 + bw - 4, yy, x0 + bw - 4, yy + 3], fill=C["amber"])
            d.line([x0, yb, x0 + bw - 4, yb], fill=C["amber"])

    # mission limits
    panel(8, 398, 400, 660, "MISSION TERMS · CHECKED IN THE NEXT REQUEST")
    if st.mission:
        for i, t in enumerate(KERNEL_TERMS):
            x0, y0 = 18 + (i % 2) * 190, 426 + (i // 2) * 56
            state = st.terms[t]
            col = {"present": C["green"], "resupplied": C["green"], "lost": C["red"], "tracked": C["text"]}[state]
            reveal = ev["kind"] != "mission" or p > (i + 1) / 8
            if not reveal:
                continue
            d.rectangle([x0, y0, x0 + 178, y0 + 48], outline=col, width=2)
            d.text((x0 + 10, y0 + 6), t.upper(), font=F[16], fill=col)
            sub = {"present": "PRESENT after compaction", "resupplied": "Kernel re-supplied", "lost": "MISSING after compaction",
                   "tracked": "checked at compaction"}[state]
            d.text((x0 + 10, y0 + 28), sub, font=F[12], fill=col)
            if state == "lost":
                d.line([x0 + 8, y0 + 16, x0 + 12 + d.textlength(t.upper(), font=F[16]), y0 + 16], fill=C["red"], width=2)

    if st.may_not:
        yy = 596
        d.text((18, yy), "MAY NOT (mission as given):", font=F[12], fill=C["dim"])
        for ln in _wrap(d, " · ".join(st.may_not), F[12], 370)[:3]:
            yy += 15
            d.text((18, yy), ln, font=F[12], fill=C["text"])

    # tool tape
    panel(408, 60, 890, 660, "TOOL CALLS · GOVERNOR RECORD")
    rows = st.tape[-24:]
    y = 86
    for i, r in enumerate(rows):
        newest = i == len(rows) - 1 and ev["kind"] in ("tool", "tool_evidence", "tool_flag", "compaction")
        if "banner" in r:
            d.rectangle([414, y - 1, 884, y + 19], fill=(48, 36, 4))
            d.text((420, y + 2), r["banner"], font=F[13], fill=C["amber"])
            y += 23
            continue
        col = C["red"] if r["flags"] else (C["green"] if r["cf"] else C["text"])
        if newest and (tick // 3) % 2 == 0 and r["kind"] != "tool":
            d.rectangle([414, y - 1, 884, y + 19], fill=(60, 18, 18) if r["flags"] else (14, 44, 26))
        d.text((420, y + 2), f"R{r['step']:02d} {r['ts'][11:19]} {r['tool'][:22]:<22} {r['key'][:11]:<11}",
               font=F[13], fill=col)
        tag = ("FLAG " + " ".join(r["flags"]))[:12] if r["flags"] else ("GOV REC" if r["gov"] else "GOV ?")
        d.text((884 - d.textlength(tag, font=F[12]) - 4, y + 3), tag, font=F[12], fill=C["red"] if r["flags"] else C["green"])
        y += 23
    if (tick // 4) % 2 == 0:
        d.rectangle([420, y + 4, 430, y + 18], fill=C["text"])

    if st.alert:
        a = st.alert
        bx0, by0, bx1, by1 = 414, 360, 884, 544
        d.rectangle([bx0, by0, bx1, by1], fill=(34, 10, 10), outline=C["red"], width=3)
        d.text((bx0 + 12, by0 + 8), f"RECORDED ACTION · REQUEST {a['step']} · {a['ts'][11:19]}Z", font=F[14], fill=C["red"])
        d.text((bx0 + 12, by0 + 32), f"{a['tool']} → {a['key']}", font=F[22], fill=C["text"])
        d.text((bx0 + 12, by0 + 68), "GOVERNOR  FLAGGED " + " · ".join(a["flags"]), font=F[14], fill=C["red"])
        d.text((bx0 + 12, by0 + 88), "          recorded and flagged; did not block", font=F[13], fill=C["text"])
        if a["effect"] or a["guard"]:
            eff = ("MISSION GUARD  refused the call" if a["guard"] else
                   "EFFECT    SIMULATED · no mission guard in the baseline")
            d.text((bx0 + 12, by0 + 118), eff, font=F[14], fill=C["amber"])
            if not a["guard"]:
                d.text((bx0 + 12, by0 + 138), "          no real customer was contacted", font=F[13], fill=C["text"])

    # evidence
    panel(898, 60, 1272, 400, "EVIDENCE · TRACKED FACTS")
    for i, (cf, lab) in enumerate(CF_LABELS.items()):
        y0 = 88 + i * 51
        stt = st.cf[cf]
        col = {"pending": C["dim"], "retrieved": C["green"], "kept": C["green"], "lost": C["red"]}[stt]
        d.text((906, y0), f"{cf}", font=F[16], fill=col)
        d.text((952, y0 + 2), lab[:40], font=F[13], fill=col if stt != "pending" else C["dim"])
        sub = {"pending": "source not yet retrieved", "retrieved": f"source retrieved · {st.cf_rec.get(cf, '')}"[:44],
               "kept": "PRESENT after compaction", "lost": "MISSING after compaction"}[stt]
        d.text((952, y0 + 21), sub, font=F[12], fill=col)

    # governor log
    panel(898, 408, 1272, 660, "GOVERNOR · RECORDS AND FLAGS, NEVER BLOCKS")
    lines_g = []
    for ts, et, txt, lvl in st.gov:
        col = {"info": C["text"], "flag": C["red"], "warn": C["amber"]}[lvl]
        lines_g.append((f"{ts[11:19]} {et}", col))
        for i in range(0, len(txt), 44):
            lines_g.append((f"  {txt[i:i + 44]}", col))
    for i, (txt, col) in enumerate(lines_g[-14:]):
        d.text((906, 432 + i * 16), txt[:46], font=F[12], fill=col)

    # caption line
    d.rectangle([0, 668, W, H], fill=(0, 0, 0))
    ccol = {"flag": C["red"], "warn": C["amber"], "cmp": C["amber"], "ev": C["green"], "info": C["text"]}[st.caption_kind]
    lines = _wrap(d, st.caption, F[16], W - 60)[:2]
    for i, ln in enumerate(lines):
        d.text((24, 674 + i * 21), ("▶ " if i == 0 else "  ") + ln, font=F[16], fill=ccol)
    return img


def _card(tl, F, title: str, lines: List[str], epilogue: bool = False, rows=None):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), C["epi"] if epilogue else C["bg"])
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 30], fill=(0, 0, 0))
    hdr = ("EXPLORATORY · OFFLINE · NOT PART OF THE RECORDED SESSION" if epilogue
           else f"MISSION CONTINUITY ▸ RECORDED SESSION {tl['label'].upper()} ▸ {tl['mode'].upper()}")
    d.text((12, 7), hdr, font=F[14], fill=C["blue"] if epilogue else C["amber"])
    if not epilogue:
        d.rectangle([0, 30, W, 50], fill=(20, 16, 4))
        d.text((12, 33), tl["label_text"], font=F[12], fill=C["amber"])
    d.text((60, 110), title, font=F[28], fill=C["blue"] if epilogue else C["amber"])
    y = 170
    for ln in lines:
        for w in _wrap(d, ln, F[18], W - 120):
            d.text((60, y), w, font=F[18], fill=C["text"])
            y += 28
        y += 10
    if rows:
        y += 10
        colx = [60, 380, 720, 1000]
        for r_i, row in enumerate(rows):
            for c_i, cell in enumerate(row):
                red = r_i and c_i and row[0] == "mission terms missing" and cell not in ("", "none", "same")
                d.text((colx[c_i], y), cell, font=F[16] if r_i else F[14],
                       fill=(C["blue"] if r_i == 0 else (C["text"] if c_i == 0 else (C["red"] if red else C["amber"]))))
            y += 34
    return img


def frames(tl: dict):
    """Yield (image, event_seq) frames in recorded order."""
    F = _fonts()
    st = State(tl)
    tick = 0
    title = [f"Architecture: {tl['mode']}. {tl['n_requests']} model requests, recorded {tl['started_at']}.",
             "Every event, number and time on screen is read from the hash-verified replay bundle and its Governor "
             "trace. Pacing is for viewing and not to scale; the clock shows recorded time.",
             "No footage is generated. This is not a screen recording."]
    img = _card(tl, F, "RECORDED AGENT SESSION REPLAY", title)
    for _ in range(int(PACE["title"] * FPS)):
        yield img, 0
    for ev in tl["events"]:
        k = ev["kind"]
        if k == "liquid":
            d = ev["data"]
            rows = [["", "RECORDED CLAUDE SUMMARY", "LIQUID OFFLINE SUMMARY", "EMPTY SUMMARY"],
                    ["received by the agent", "yes", "no, never", "(reference)"],
                    ["tracked facts", f"{len(d['claude']['cf_present'])}/6", f"{len(d['liquid']['cf_present'])}/6", "5/6"],
                    ["mission terms missing", ", ".join(d["claude"]["kernel_terms_missing"]),
                     ", ".join(d["liquid"]["kernel_terms_missing"]), "same"],
                    ["where it ran", "Anthropic API", f"this laptop, {d['liquid_wall']} s", ""]]
            img = _card(tl, F, "Offline re-summarization (Liquid AI)", [ev["caption"],
                        "Automated checks cannot tell the summaries apart here: the verbatim tail still carries "
                        "CF1–CF5. No Governor execution record exists for this offline run. In the governed "
                        "architecture the Mission Kernel does not pass through any summarizer."], epilogue=True, rows=rows)
            for _ in range(int(PACE["liquid"] * FPS)):
                yield img, ev["seq"]
            continue
        if k == "takeaway":
            img = _card(tl, F, "What the record shows", [ev["caption"]])
            for _ in range(int(PACE["takeaway"] * FPS)):
                yield img, ev["seq"]
            continue
        _apply(st, ev)
        nfr = max(1, int(PACE.get(k, 0.5) * FPS))
        for f in range(nfr):
            yield _frame(st, F, tick, ev, f / max(1, nfr - 1)), ev["seq"]
            tick += 1


def render(tl: dict, out_mp4: Path) -> dict:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is required (brew install ffmpeg)")
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                             "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_mp4)], stdin=subprocess.PIPE)
    spans: Dict[int, List[int]] = {}
    n = 0
    stills = {}
    for img, seq in frames(tl):
        proc.stdin.write(img.tobytes())
        spans.setdefault(seq, [n, n])[1] = n
        stills[seq] = img
        n += 1
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return {"frames": n, "fps": FPS, "duration_s": round(n / FPS, 2),
            "event_spans_s": {str(k): [round(a / FPS, 2), round((b + 1) / FPS, 2)] for k, (a, b) in spans.items()},
            "_stills": stills}
