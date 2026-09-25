"""Read Governor evidence written by the published pydantic-ai-governor library.

This module only READS traces. The application never constructs or emits
Governor events.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from mission_continuity.paths import trace_file


def read_trace(session_id: str, path: Path | None = None) -> List[dict]:
    p = path or trace_file(session_id)
    if not p.exists():
        return []
    events = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def trace_file_text(session_id: str) -> str:
    p = trace_file(session_id)
    return p.read_text() if p.exists() else ""


def scope_assertions(events: List[dict]) -> Dict[str, dict]:
    """tool_use_id -> SCOPE_ASSERTED event."""
    return {e["payload"].get("tool_use_id"): e for e in events
            if e.get("event_type") == "SCOPE_ASSERTED" and e.get("payload", {}).get("tool_use_id")}


def tool_snapshots(events: List[dict]) -> Dict[str, dict]:
    """tool_use_id -> tool CONTEXT_SNAPSHOT (normal return)."""
    return {e["payload"]["tool_use_id"]: e for e in events
            if e.get("event_type") == "CONTEXT_SNAPSHOT" and "llm_turn_id" not in e.get("payload", {})
            and e.get("payload", {}).get("tool_use_id")}


def token_snapshots(events: List[dict]) -> Dict[str, dict]:
    """llm_turn_id -> token CONTEXT_SNAPSHOT (measured tokens for one model turn)."""
    return {e["payload"]["llm_turn_id"]: e for e in events
            if e.get("event_type") == "CONTEXT_SNAPSHOT" and "llm_turn_id" in e.get("payload", {})}


def corroborated(events: List[dict], tool_use_id: str) -> bool:
    """Governor recorded both the dispatch and a normal return for this call."""
    return tool_use_id in scope_assertions(events) and tool_use_id in tool_snapshots(events)


def flags_for(events: List[dict], tool_use_id: str) -> dict:
    """Flags and violations exactly as the library recorded them. Never inferred."""
    e = scope_assertions(events).get(tool_use_id)
    if e is None:
        return {"recorded": False}
    return {"recorded": True, "advisory_flags": e.get("advisory_flags", []),
            "policy_violations": e.get("policy_violations", []),
            "simulated_consequence": e.get("simulated_consequence"),
            "pass_through": e.get("pass_through"), "event_id": e.get("event_id")}
