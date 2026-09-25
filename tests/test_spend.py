import json

import pytest

from mission_continuity import spend


def _write_trace(path, turns):
    """A trace in the shape the pydantic-ai-governor library writes."""
    with path.open("w") as fh:
        fh.write(json.dumps({"event_type": "AGENT_REGISTERED", "payload": {}}) + "\n")
        for i, (inp, out) in enumerate(turns):
            fh.write(json.dumps({
                "event_type": "CONTEXT_SNAPSHOT",
                "payload": {"llm_turn_id": f"msg_{i}", "llm_prompt_tokens": inp,
                            "llm_completion_tokens": out, "context_size_tokens": inp},
            }) + "\n")
            # A tool snapshot: no llm_turn_id, must not be counted.
            fh.write(json.dumps({
                "event_type": "CONTEXT_SNAPSHOT",
                "payload": {"context_size_tokens": 99, "tool_use_id": "t"},
            }) + "\n")


PRICED = spend.Pricing("m", 3.0, 15.0, "test", "2026-09-25")


def test_counts_only_token_snapshots_across_sessions(tmp_path):
    _write_trace(tmp_path / "a.jsonl", [(1000, 100), (2000, 200)])
    _write_trace(tmp_path / "b.jsonl", [(500, 50)])
    totals = spend.measured_tokens(tmp_path)
    assert (totals.input_tokens, totals.output_tokens) == (3500, 350)
    assert (totals.turns, totals.sessions) == (3, 2)


def test_estimate_uses_recorded_prices(tmp_path):
    _write_trace(tmp_path / "a.jsonl", [(1_000_000, 100_000)])
    usd = spend.estimate_usd(spend.measured_tokens(tmp_path), PRICED)
    assert usd == pytest.approx(3.0 + 1.5)


def test_fails_closed_without_pricing(tmp_path):
    unpriced = spend.Pricing("m", None, None, None, None)
    with pytest.raises(spend.SpendStop):
        spend.check_before_run(0.01, tmp_path, unpriced)


def test_soft_stop_refuses_run_that_would_exceed_80(tmp_path):
    # 20M input tokens at $3/MTok = $60 spent; projecting $25 more exceeds $80.
    _write_trace(tmp_path / "a.jsonl", [(20_000_000, 0)])
    assert spend.check_before_run(19.0, tmp_path, PRICED) == pytest.approx(60.0)
    with pytest.raises(spend.SpendStop):
        spend.check_before_run(25.0, tmp_path, PRICED)


def test_empty_trace_dir_is_zero(tmp_path):
    assert spend.measured_tokens(tmp_path / "missing").turns == 0
