"""Offline tests for the exploratory re-summarization harness (no model needed)."""

from mission_continuity import resummarize as rs
from mission_continuity.compaction import SUMMARIZER_INSTRUCTIONS


def test_control_reproduces_recorded_checks():
    assert rs.control("E1-baseline", "cmp-1")["ok"]


def test_prompt_matches_recorded_summarizer_wrapper():
    p = rs.summarizer_prompt(rs.load("E1-baseline", "cmp-1"))
    assert p["system"] == SUMMARIZER_INSTRUCTIONS
    assert p["user"].startswith("Working history to compress:\n\n")


def test_checks_depend_only_on_the_summary():
    src = rs.load("E1-baseline", "cmp-1")
    empty = rs.checks_for(src, "")
    assert set(["modify", "delete", "contact"]) <= set(empty["kernel_terms_missing"])
    full = rs.checks_for(src, "Limits: may not modify, delete or contact. " + src["record"]["summary_text"])
    assert not set(["modify", "delete", "contact"]) & set(full["kernel_terms_missing"])
    assert full["cf_present"] == src["record"]["context_checks"]["cf_present"]


def test_governed_compactions_are_out_of_scope():
    import pytest
    with pytest.raises(RuntimeError, match="baseline"):
        rs.load("E1-governed", "cmp-1")
