"""Visual reconstruction ("Watch Replay") of a recorded run.

An AI-generated visual reconstruction of recorded execution, parameterized by
run label. The execution timeline, every caption and every number come from the
run's hash-verified replay bundle; generated footage (FLUX 3, Black Forest Labs)
is an illustrative backdrop only and is never evidence.

Pipeline
  1. build_scenes(label): deterministic scene selection from bundle records.
     Scenes are chosen by what the run actually recorded; a scene is never
     manufactured to match another run.
       S1 mission   first INTENT_DECLARED in the investigator's Governor session
       S2 evidence  first payment record retrieved whose record_type is authorization
       S3 compaction the first compaction record, with its measured context checks
       S4 outcome   the first prohibited dispatch recorded in results.json, or, when
                    none, the returned report
     plus two local cards (C0 title, C1 end). Each scene carries a short on-video
     caption, the complete caption for the console table, and its source records
     (file, line, SHA-256 of the exact JSONL line, event/tool ids).
  2. Footage per generated scene: a BFL clip under reconstructions/<label>/clips/,
     or a solid placeholder.
  3. render(): Pillow overlays (a reconstruction label on every frame, one caption
     per scene) composited by ffmpeg, then concatenated.
  4. verify(): re-verifies the bundle, rebuilds the scenes from the records and
     compares them to the stored scenes.json, and checks clip and video hashes.

Outputs: reconstructions/<label>/ for generated footage; placeholder renders go to
var/reconstructions/<label>/ (gitignored). Bundles, results, Governor traces and
RawTree are only read, never written.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from mission_continuity.bundle import verify as verify_bundle
from mission_continuity.paths import REPLAYS, REPO, VAR
from mission_continuity.redact import known_pii_values

RECON = REPO / "reconstructions"
PLACEHOLDER_ROOT = VAR / "reconstructions"
MISSION_FILE = REPO / "missions" / "MC-BILL-001.yaml"
W, H, FPS = 1280, 704, 24          # FLUX 3 hd at 16:9
SCENE_S, CARD_S = 5, 3
GENERATED = ["S1", "S2", "S3", "S4"]
LABEL_TEXT = "AI-generated visual reconstruction of recorded run {label} · footage is illustrative, not evidence · captions from the record"
VERSION = 1

PROHIBITED_PHRASES = {  # mission prohibited_actions keys -> plain words
    "issue_refunds": "refunds",
    "modify_transactions_or_billing_records": "changing records",
    "delete_customer_records": "deleting records",
    "contact_customers": "contacting the customer",
}
TOOL_PHRASES = {"contact_customer": "contacted the customer", "issue_refund": "issued a refund",
                "modify_transaction": "modified a transaction", "apply_account_credit": "applied an account credit",
                "delete_customer_record": "deleted a customer record"}

# Shared visual vocabulary so baseline and governed replays read side by side.
STYLE = ("Muted ivory and deep green paper-cut illustration, soft window light, a quiet archive office "
         "with wooden drawers and a corkboard, one investigator seen from behind with no facial detail, "
         "slow gentle camera movement. Absolutely no on-screen text, letters, numbers, captions, logos, "
         "signage or writing anywhere; every paper card and document is blank. Silent.")
PROMPTS = {
    "mission": "The investigator opens a sealed envelope and pins four blank rule cards to the corkboard, "
               "each marked only with a simple icon: a coin, a pencil, an eraser, a telephone.",
    "evidence": "The investigator pulls two identical blank receipt cards from an archive drawer and lays them "
                "side by side; one of them slowly fades into a faint translucent outline while the other stays solid.",
    "compaction_missing": "A desk piled high with blank papers is swept together into one small folded note. "
                          "On the corkboard, the rule cards with the telephone, eraser and pencil icons come loose "
                          "and drift out of frame; the coin card stays pinned.",
    "compaction_kept": "A desk piled high with blank papers is swept together into one small folded note. "
                       "On the corkboard, all four rule cards (coin, pencil, eraser, telephone icons) stay firmly pinned.",
    "action_simulated": "The corkboard behind is missing its telephone card. The investigator lifts a vintage "
                        "telephone receiver and dials. On the wall, a small green recorder light blinks steadily and "
                        "an amber lamp lights up while the call carries on uninterrupted.",
    "action_guarded": "The investigator reaches for a vintage telephone, but a glass cover closes over it and stays "
                      "shut. On the wall, a small green recorder light blinks steadily and an amber lamp lights up.",
    "report": "The investigator ties a blank folder closed with string and places it in a wooden out-tray. "
              "The rule cards remain pinned on the corkboard behind.",
}


# ------------------------------------------------------------------ records
def _sha_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _lines(p: Path) -> List[str]:
    return [x for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _src(bundle: Path, rel: str, line: Optional[int] = None, **ids) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text())
    d = {"file": rel, "file_sha256": manifest["files"].get(rel)}
    if line is not None:
        d.update({"line": line, "row_sha256": _sha_text(_lines(bundle / rel)[line - 1])})
    d.update({k: v for k, v in ids.items() if v is not None})
    return d


def _offset(ts: str, start: str) -> str:
    f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    secs = int((f(ts) - f(start)).total_seconds())
    return f"T+{secs // 60}:{secs % 60:02d}"


def _mission_prohibited() -> List[str]:
    keys, inside = [], False
    for line in MISSION_FILE.read_text().splitlines():
        if line.startswith("prohibited_actions:"):
            inside = True
            continue
        if inside:
            if line.startswith("  ") and ":" in line:
                keys.append(line.strip().split(":")[0])
            elif line and not line.startswith(" "):
                break
    return keys


def build_scenes(label: str) -> dict:
    bundle = REPLAYS / label
    v = verify_bundle(bundle)
    if not v["ok"]:
        raise RuntimeError(f"bundle {label} failed verification: {v}")
    run = json.loads((bundle / "run" / "run.json").read_text())
    results = json.loads((bundle / "run" / "results.json").read_text())
    start = run["started_at"]
    main_sid = json.loads(_lines(bundle / "run" / "requests.jsonl")[0])["governor_session_id"]
    trace_rel = f"traces/{main_sid}.jsonl"
    trace = [json.loads(x) for x in _lines(bundle / trace_rel)]
    tools = [json.loads(x) for x in _lines(bundle / "run" / "tools.jsonl")]
    cmps = [json.loads(x) for x in _lines(bundle / "run" / "compactions.jsonl")]
    mode = run["mode"]
    scenes = []

    # C0 title card
    scenes.append({"scene_id": "C0", "kind": "title", "generated": False,
                   "caption": f"Recorded run {label} ({mode} architecture)",
                   "detail": f"Visual reconstruction of recorded run {label}, {mode} architecture. "
                             f"Footage generated by FLUX 3 (Black Forest Labs) is illustrative only; every caption, "
                             f"number and time is taken from the recorded run.",
                   "recorded_ts": start, "offset": "T+0:00",
                   "sources": [_src(bundle, "run/run.json")]})

    # S1 mission declared
    i, ev = next((n, e) for n, e in enumerate(trace, 1) if e["event_type"] == "INTENT_DECLARED")
    forbidden = [PROHIBITED_PHRASES.get(k, k.replace("_", " ")) for k in _mission_prohibited()]
    scenes.append({"scene_id": "S1", "kind": "mission", "generated": True, "prompt_key": "mission",
                   "caption": "Mission declared. Not allowed: " + ", ".join(forbidden) + ".",
                   "detail": f"Governor recorded the declared mission: \"{ev['payload']['stated_objective']}\". "
                             f"Mission limits (Mission Kernel, sha256 {run['kernel_sha256'][:12]}…): "
                             + ", ".join(_mission_prohibited()) + ".",
                   "recorded_ts": ev["timestamp_utc"], "offset": _offset(ev["timestamp_utc"], start),
                   "sources": [_src(bundle, trace_rel, i, event_id=ev["event_id"], event_type=ev["event_type"]),
                               {"file": "missions/MC-BILL-001.yaml", "kernel_sha256": run["kernel_sha256"]}]})

    # S2 evidence retrieved: first authorization payment record
    hit = next(((n, t) for n, t in enumerate(tools, 1) if t["tool_name"] == "get_payment_detail"
                and isinstance(t.get("result"), dict) and t["result"].get("record_type") == "authorization"), None)
    if hit:
        n, t = hit
        r = t["result"]
        g = next(((k, e) for k, e in enumerate(trace, 1) if e["event_type"] == "SCOPE_ASSERTED"
                  and e["payload"].get("tool_use_id") == t["tool_call_id"]), (None, None))
        cap = str(r.get("capture_status", "")).replace("_", " ")
        scenes.append({"scene_id": "S2", "kind": "evidence", "generated": True, "prompt_key": "evidence",
                       "caption": f"Payment record {r['payment_id']}: ${r['amount']:.2f} authorization, {cap}.",
                       "detail": f"Request {t['run_step']}: get_payment_detail returned {r['payment_id']}, "
                                 f"{r['currency']} {r['amount']:.2f}, record type {r['record_type']}, capture status "
                                 f"{r.get('capture_status')}, invoice {r.get('invoice_id')}.",
                       "recorded_ts": t["ts"], "offset": _offset(t["ts"], start),
                       "sources": [_src(bundle, "run/tools.jsonl", n, tool_call_id=t["tool_call_id"]),
                                   *([_src(bundle, trace_rel, g[0], event_id=g[1]["event_id"],
                                           event_type="SCOPE_ASSERTED")] if g[0] else [])]})

    # S3 first compaction
    if cmps:
        c = cmps[0]
        cc, cnt = c["context_checks"], c["counted"]
        missing = cc["kernel_terms_missing"]
        n_exp, n_pres = len(cc["cf_exposed"]), len(cc["cf_present"])
        caption = (f"Context compacted, {cnt['reduction_pct']}% smaller. "
                   + (f"Missing afterwards: mission terms {', '.join(missing)}." if missing
                      else "All mission terms still present."))
        scenes.append({"scene_id": "S3", "kind": "compaction", "generated": True,
                       "prompt_key": "compaction_missing" if missing else "compaction_kept",
                       "caption": caption,
                       "detail": f"Compaction {c['compaction_id']} at request {c['run_step']}: triggered at "
                                 f"{c['trigger']['input_tokens_last']:,} measured input tokens (threshold "
                                 f"{c['trigger']['threshold']:,}). Context {cnt['uncompacted']:,} → {cnt['compacted']:,} "
                                 f"tokens, {cnt['tokens_saved']:,} saved ({cnt['reduction_pct']}%), counted by "
                                 f"{cnt['source']}. Tracked facts in the next request: {n_pres}/{n_exp}"
                                 + (f" (missing {', '.join(cc['cf_missing'])})" if cc["cf_missing"] else "")
                                 + ". Mission terms missing: " + (", ".join(missing) if missing else "none") + ".",
                       "recorded_ts": c["ts"], "offset": _offset(c["ts"], start),
                       "sources": [_src(bundle, "run/compactions.jsonl", 1, compaction_id=c["compaction_id"])]})

    # S4 outcome: first prohibited dispatch (results.json is authoritative), else the report
    disp = results["authorization"]["prohibited_dispatches"]
    if disp:
        d = disp[0]
        n, t = next((n, t) for n, t in enumerate(tools, 1)
                    if t["tool_name"] == d["tool_name"] and int(t["run_step"]) == int(d["run_step"]))
        g = next(((k, e) for k, e in enumerate(trace, 1) if e["event_type"] == "SCOPE_ASSERTED"
                  and e["payload"].get("tool_use_id") == t["tool_call_id"]), (None, None))
        simulated = d["application_outcome"] == "simulated_effect"
        flags = ", ".join(d["governor_violations"] + d["governor_flags"])
        verb = TOOL_PHRASES.get(d["tool_name"], f"called {d['tool_name']}")
        caption = (f"The agent {verb}. Governor recorded and flagged it." if simulated
                   else f"The agent tried to call {d['tool_name']}; the mission guard refused it.")
        srcs = [_src(bundle, "run/tools.jsonl", n, tool_call_id=t["tool_call_id"])]
        if g[0]:
            srcs.append(_src(bundle, trace_rel, g[0], event_id=g[1]["event_id"], event_type="SCOPE_ASSERTED"))
        eff = bundle / "run" / "effects.jsonl"
        for k, x in enumerate(_lines(eff), 1):
            if json.loads(x).get("tool_call_id") == t["tool_call_id"]:
                srcs.append(_src(bundle, "run/effects.jsonl", k, tool_call_id=t["tool_call_id"]))
        srcs.append(_src(bundle, "run/results.json", json_pointer="/authorization/prohibited_dispatches/0"))
        scenes.append({"scene_id": "S4", "kind": "action", "generated": True,
                       "prompt_key": "action_simulated" if simulated else "action_guarded",
                       "caption": caption,
                       "detail": f"Request {d['run_step']}: {d['tool_name']} dispatched"
                                 + (" after the first compaction" if d.get("after_first_compaction") else "")
                                 + f". Sentience Governor recorded it{' and flagged ' + flags if flags else ''}; "
                                 f"Governor records and does not block. Application outcome: {d['application_outcome']}"
                                 + (" (no mission guard in the baseline; the effect is simulated)." if simulated else "."),
                       "recorded_ts": t["ts"], "offset": _offset(t["ts"], start), "sources": srcs})
    else:
        last_i, last = max(((n, e) for n, e in enumerate(trace, 1) if e["event_type"] == "CONTEXT_SNAPSHOT"),
                           key=lambda x: x[0])
        scenes.append({"scene_id": "S4", "kind": "report", "generated": True, "prompt_key": "report",
                       "caption": "Report returned. No prohibited action was attempted.",
                       "detail": "The run returned its investigation report. results.json records no prohibited "
                                 "tool dispatches in this run.",
                       "recorded_ts": last["timestamp_utc"], "offset": _offset(last["timestamp_utc"], start),
                       "sources": [_src(bundle, trace_rel, last_i, event_id=last["event_id"], event_type="CONTEXT_SNAPSHOT"),
                                   _src(bundle, "run/report.json"),
                                   _src(bundle, "run/results.json", json_pointer="/authorization/prohibited_dispatches")]})

    # C1 end card
    score = results["correctness"]["score"]
    scenes.append({"scene_id": "C1", "kind": "end", "generated": False,
                   "caption": f"Run {results['outcome']}. Task score {score}/5.",
                   "detail": f"Run outcome {results['outcome']}, task score {score}/5, "
                             f"{results['cost']['tool_calls']} tool calls, {results['cost']['wall_seconds']} s. "
                             f"Source: replay bundle {label}, verified by SHA-256.",
                   "recorded_ts": None, "offset": None,
                   "sources": [_src(bundle, "run/results.json", json_pointer="/correctness"),
                               _src(bundle, "run/report.json")]})

    for s in scenes:
        if s.get("prompt_key"):
            s["prompt"] = f"{STYLE} {PROMPTS[s['prompt_key']]}"
    return {"version": VERSION, "label": label, "mode": mode,
            "bundle_manifest_sha256": _sha_file(bundle / "manifest.json"),
            "label_text": LABEL_TEXT.format(label=label), "scenes": scenes}


def leak_check(obj) -> List[str]:
    blob = json.dumps(obj)
    return [v for v in known_pii_values() if v in blob]


# ------------------------------------------------------------------ render
def _font(size: int):
    from PIL import ImageFont
    for f in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def _wrap(draw, text: str, font, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def overlay_png(path: Path, label_text: str, caption: str, card: bool = False):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    f_label, f_cap = _font(19), _font(40 if card else 32)
    d.rectangle([0, 0, W, 40], fill=(12, 28, 22, 205))
    d.text((18, 10), label_text, font=f_label, fill=(236, 230, 214, 255))
    lines = _wrap(d, caption, f_cap, W - 140)
    lh = f_cap.size + 12
    if card:
        y = (H - lh * len(lines)) // 2
        for ln in lines:
            d.text(((W - d.textlength(ln, font=f_cap)) // 2, y), ln, font=f_cap, fill=(236, 230, 214, 255))
            y += lh
    else:
        box_h = lh * len(lines) + 30
        d.rectangle([40, H - box_h - 30, W - 40, H - 30], fill=(12, 28, 22, 215))
        y = H - box_h - 15
        for ln in lines:
            d.text((64, y), ln, font=f_cap, fill=(236, 230, 214, 255))
            y += lh
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _ffmpeg(*args):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is required to render a reconstruction (brew install ffmpeg)")
    subprocess.run([exe, "-y", "-loglevel", "error", *args], check=True)


def _segment(bg: Optional[Path], png: Path, secs: int, out: Path):
    src = ["-i", str(bg)] if bg else ["-f", "lavfi", "-i", f"color=c=0x1d3a2f:s={W}x{H}:r={FPS}:d={secs}"]
    _ffmpeg(*src, "-i", str(png), "-filter_complex",
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},"
            f"trim=duration={secs},setpts=PTS-STARTPTS[b];[b][1:v]overlay=0:0,format=yuv420p[v]",
            "-map", "[v]", "-an", "-t", str(secs), "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            "-r", str(FPS), str(out))


def render(label: str, placeholder: bool = False) -> dict:
    spec = build_scenes(label)
    leaks = leak_check(spec)
    if leaks:
        raise RuntimeError(f"refused: {len(leaks)} personal-data value(s) in scene text")
    out_dir = (PLACEHOLDER_ROOT if placeholder else RECON) / label
    clips = RECON / label / "clips"
    footage = {}
    if not placeholder:
        fp = RECON / label / "footage.json"
        footage = json.loads(fp.read_text()) if fp.exists() else {}
        # A published reconstruction never silently falls back to placeholder footage.
        pending = [s["scene_id"] for s in spec["scenes"] if s["generated"] and not (
            (clips / f"{s['scene_id']}.mp4").exists() and footage.get(s["scene_id"], {}).get("accepted")
            and footage[s["scene_id"]].get("clip_sha256") == _sha_file(clips / f"{s['scene_id']}.mp4"))]
        if pending:
            raise RuntimeError(f"refused: scenes without an accepted generated clip: {', '.join(pending)} "
                               f"(use --placeholder for a local preview under var/)")
    out_dir.mkdir(parents=True, exist_ok=True)
    timeline, t, segs = [], 0, []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for s in spec["scenes"]:
            secs = SCENE_S if s["generated"] else CARD_S
            png = out_dir / "overlays" / f"{s['scene_id']}.png"
            overlay_png(png, spec["label_text"], s["caption"], card=not s["generated"])
            bg = None
            src = "card" if not s["generated"] else "placeholder"
            if s["generated"] and not placeholder:
                clip = clips / f"{s['scene_id']}.mp4"
                f = footage.get(s["scene_id"])
                if clip.exists() and f and f.get("clip_sha256") == _sha_file(clip) and f.get("accepted"):
                    bg, src = clip, "bfl"
            seg = tmp / f"{len(segs):02d}_{s['scene_id']}.mp4"
            _segment(bg, png, secs, seg)
            segs.append(seg)
            timeline.append({"scene_id": s["scene_id"], "start_s": t, "end_s": t + secs, "footage": src,
                             "overlay_sha256": _sha_file(png)})
            t += secs
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in segs))
        video = out_dir / "replay.mp4"
        _ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(video))
    (out_dir / "scenes.json").write_text(json.dumps(spec, indent=1, ensure_ascii=False))
    manifest = {"label": label, "version": VERSION, "placeholder": placeholder,
                "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "scenes_sha256": _sha_file(out_dir / "scenes.json"), "timeline": timeline,
                "duration_s": t, "video": "replay.mp4", "video_sha256": _sha_file(video),
                "video_bytes": video.stat().st_size}
    (out_dir / "render.json").write_text(json.dumps(manifest, indent=1))
    return {"out_dir": str(out_dir.relative_to(REPO)), **manifest}


# ------------------------------------------------------------------ verify
def verify(label: str, placeholder: bool = False) -> dict:
    out_dir = (PLACEHOLDER_ROOT if placeholder else RECON) / label
    checks = {}
    checks["bundle_verified"] = verify_bundle(REPLAYS / label)["ok"]
    stored = json.loads((out_dir / "scenes.json").read_text())
    rebuilt = build_scenes(label)
    checks["scenes_rebuilt_from_records_match"] = stored == json.loads(json.dumps(rebuilt, ensure_ascii=False))
    rows_ok = True
    for s in stored["scenes"]:
        for src in s["sources"]:
            p = REPLAYS / label / src["file"]
            if "row_sha256" in src:
                rows_ok &= _sha_text(_lines(p)[src["line"] - 1]) == src["row_sha256"]
            if src.get("file_sha256"):
                rows_ok &= _sha_file(p) == src["file_sha256"]
    checks["source_rows_and_files_match"] = rows_ok
    render_m = json.loads((out_dir / "render.json").read_text())
    checks["scenes_json_hash_matches_render"] = _sha_file(out_dir / "scenes.json") == render_m["scenes_sha256"]
    checks["video_hash_matches"] = _sha_file(out_dir / render_m["video"]) == render_m["video_sha256"]
    fp = RECON / label / "footage.json"
    if fp.exists() and not placeholder:
        footage = json.loads(fp.read_text())
        checks["clip_hashes_match"] = all(_sha_file(RECON / label / "clips" / f"{sid}.mp4") == f["clip_sha256"]
                                          for sid, f in footage.items() if f.get("clip_sha256"))
    checks["no_personal_data"] = not leak_check(stored) and not (fp.exists() and leak_check(fp.read_text()))
    return {"label": label, "ok": all(checks.values()), "checks": checks}


def available(label: str, allow_placeholder: bool = False) -> Optional[dict]:
    """For the console: the reconstruction of a run, if one exists.

    Only reconstructions/<label> counts. allow_placeholder (local preview only,
    MC_REPLAY_PLACEHOLDER=1) also accepts var/reconstructions/<label>, which the
    console labels as placeholder footage.
    """
    for root, placeholder in ((RECON, False), (PLACEHOLDER_ROOT, True)):
        if placeholder and not allow_placeholder:
            break
        d = root / label
        if (d / "render.json").exists() and (d / "replay.mp4").exists():
            fp = RECON / label / "footage.json"
            return {"dir": d, "placeholder": placeholder, "render": json.loads((d / "render.json").read_text()),
                    "scenes": json.loads((d / "scenes.json").read_text()),
                    "footage": json.loads(fp.read_text()) if fp.exists() and not placeholder else {}}
    return None
