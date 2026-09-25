"""Filesystem locations. Everything a run writes lives under var/ (gitignored)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VAR = REPO / "var"
RUNS = VAR / "runs"
REPLAYS = REPO / "replays"
CONFIG = REPO / "config"

# Isolated HOME for agent subprocesses. The published pydantic-ai-governor
# library writes its trace to ~/.sentience/traces/pydantic-ai/<run_id>.jsonl
# and the location is not configurable, so the runner points HOME here. This
# keeps Governor evidence self-contained and guarantees that no operator
# governance profile under the real ~/.sentience can bind to these runs.
ISOLATED_HOME = VAR / "home"
TRACE_DIR = ISOLATED_HOME / ".sentience" / "traces" / "pydantic-ai"


def run_dir(label: str) -> Path:
    return RUNS / label


def trace_file(session_id: str) -> Path:
    return TRACE_DIR / f"{session_id}.jsonl"
