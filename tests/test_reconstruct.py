"""Offline tests for the visual reconstruction: scenes come only from the records."""

import json
import re

from mission_continuity import reconstruct
from mission_continuity.paths import REPLAYS
from mission_continuity.redact import known_pii_values


def test_scenes_are_deterministic_and_traceable():
    a = reconstruct.build_scenes("E1-baseline")
    b = reconstruct.build_scenes("E1-baseline")
    assert a == b
    assert [s["scene_id"] for s in a["scenes"]] == ["C0", "S1", "S2", "S3", "S4", "C1"]
    for s in a["scenes"]:
        assert s["sources"], s["scene_id"]
        for src in s["sources"]:
            if "row_sha256" in src:
                line = [x for x in (REPLAYS / "E1-baseline" / src["file"]).read_text().splitlines() if x.strip()][src["line"] - 1]
                assert reconstruct._sha_text(line) == src["row_sha256"]


def test_scenes_follow_what_each_run_recorded():
    base = {s["scene_id"]: s for s in reconstruct.build_scenes("E1-baseline")["scenes"]}
    gov = {s["scene_id"]: s for s in reconstruct.build_scenes("E1-governed")["scenes"]}
    assert base["S4"]["kind"] == "action" and "contact_customer" in base["S4"]["detail"]
    assert base["S3"]["prompt_key"] == "compaction_missing"
    # governed recorded no prohibited dispatch: no action scene is manufactured
    assert gov["S4"]["kind"] == "report"
    assert gov["S3"]["prompt_key"] == "compaction_kept"


def test_no_personal_data_and_prompts_forbid_text():
    for label in ("E1-baseline", "E1-governed"):
        spec = reconstruct.build_scenes(label)
        blob = json.dumps(spec)
        assert not [v for v in known_pii_values() if v in blob]
        for s in spec["scenes"]:
            if s["generated"]:
                assert "no on-screen text" in s["prompt"].lower()
                assert not re.search(r"\d", reconstruct.PROMPTS[s["prompt_key"]])


def test_session_timeline_is_deterministic_and_sourced():
    from mission_continuity import resummarize, session_replay
    liq = resummarize.available("E1-baseline", "cmp-1")
    a = session_replay.build_timeline("E1-baseline", liq)
    assert a == session_replay.build_timeline("E1-baseline", liq)
    assert all(ev["sources"] for ev in a["events"])
    kinds = [ev["kind"] for ev in a["events"]]
    # recorded order: mission first; outcome, then the offline epilogue, then the takeaway
    assert kinds[0] == "mission" and kinds[-3:] == ["outcome", "liquid", "takeaway"]
    flag = next(ev for ev in a["events"] if ev["kind"] == "tool_flag")
    assert flag["data"]["tool"] == "contact_customer" and flag["data"]["step"] == 12
    assert "POL-001" in flag["data"]["flags"]
    # every recorded tool return and model response appears exactly once; nothing else is added
    import json
    from mission_continuity.paths import REPLAYS
    run = REPLAYS / "E1-baseline" / "run"
    n_tools = sum(1 for x in (run / "tools.jsonl").read_text().splitlines() if x.strip())
    n_resp = sum(1 for x in (run / "requests.jsonl").read_text().splitlines() if x.strip() and json.loads(x)["phase"] == "response")
    assert sum(k in ("tool", "tool_evidence", "tool_flag") for k in kinds) == n_tools
    assert kinds.count("request") == n_resp


def test_governed_timeline_uses_only_its_own_events():
    from mission_continuity import session_replay
    g = session_replay.build_timeline("E1-governed", None)
    kinds = [ev["kind"] for ev in g["events"]]
    assert "tool_flag" not in kinds and "effect" not in kinds and "liquid" not in kinds
    assert kinds.count("compaction") == 2
    assert next(ev for ev in g["events"] if ev["kind"] == "mission")["data"]["kernel_in_instructions"]


def test_live_replay_is_generic_and_writes_only_under_var(tmp_path, monkeypatch):
    """Any completed live investigation gets its own replay from its own records, under var/reconstructions only."""
    import hashlib
    import json
    import shutil
    from mission_continuity import paths, reconstruct, session_replay
    from mission_continuity.paths import REPLAYS
    runs, traces, out = tmp_path / "runs", tmp_path / "traces", tmp_path / "recon"
    label = "live-demo-governed-000000"
    shutil.copytree(REPLAYS / "E1-governed" / "run", runs / label)          # a stand-in completed live run
    meta = json.loads((runs / label / "run.json").read_text())
    meta.update(arm="live", label=label)
    (runs / label / "run.json").write_text(json.dumps(meta))
    traces.mkdir()
    for t in (REPLAYS / "E1-governed" / "traces").glob("*.jsonl"):
        shutil.copy(t, traces / t.name)
    monkeypatch.setattr(paths, "RUNS", runs)
    monkeypatch.setattr(session_replay, "RUNS", runs)
    monkeypatch.setattr(session_replay, "TRACE_DIR", traces)
    monkeypatch.setattr(reconstruct, "LIVE_ROOT", out)
    monkeypatch.setattr(session_replay, "render", lambda tl, video: (video.write_bytes(b"x"), {
        "frames": 1, "fps": 12, "duration_s": 0.1, "event_spans_s": {}, "_stills": {}})[1])
    published = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (paths.REPO / "reconstructions").rglob("*") if p.is_file()}

    assert reconstruct.is_completed_live(label)
    r = reconstruct.render_live(label)
    assert (out / label / "replay.mp4").exists() and r["kind"] == "completed_live_investigation_replay"
    tl = json.loads((out / label / "timeline.json").read_text())
    assert tl["source_kind"] == "completed_live_investigation" and tl["label"] == label
    kinds = [e["kind"] for e in tl["events"]]
    assert kinds[0] == "mission" and kinds[-2:] == ["outcome", "takeaway"] and "liquid" not in kinds
    assert reconstruct.verify_live(label)["ok"]
    assert reconstruct.available_live(label)["live"]
    # published replays untouched
    assert published == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (paths.REPO / "reconstructions").rglob("*") if p.is_file()}


def test_live_replay_refuses_runs_that_are_not_completed_live(tmp_path, monkeypatch):
    import json
    import pytest
    from mission_continuity import paths, reconstruct
    runs = tmp_path / "runs"; (runs / "x").mkdir(parents=True)
    (runs / "x" / "run.json").write_text(json.dumps({"arm": "live", "outcome": "running"}))
    monkeypatch.setattr(paths, "RUNS", runs)
    assert not reconstruct.is_completed_live("x")
    with pytest.raises(RuntimeError):
        reconstruct.render_live("x")
