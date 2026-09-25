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


def test_published_render_refuses_without_accepted_clips():
    import pytest
    if (reconstruct.RECON / "E1-governed" / "footage.json").exists():
        pytest.skip("footage exists")
    with pytest.raises(RuntimeError, match="without an accepted generated clip"):
        reconstruct.render("E1-governed")
    assert reconstruct.available("E1-governed") is None
