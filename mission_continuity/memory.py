"""Governed memory: the deterministic retention policy P applied to signals.

Signals (Σ): actual tool returns, the agent's own progress records, and
Governor execution evidence read from the library's trace. The summarizer may
PROPOSE summaries and working notes; it never decides a retention class.

Classes:
  MUST_PERSIST     stored, and exposed on every request after it is established
  MAY_PERSIST      stored always; exposed only within the budget, else retired
                   from exposure (never deleted)
  MUST_NOT_PERSIST never stored; a tombstone records only that something was
                   withheld (handled by redact.py before any write)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional

import yaml

from mission_continuity.paths import REPO
from mission_continuity.redact import redact

POLICY_FILE = REPO / "policy" / "retention.yaml"

MUST = "MUST_PERSIST"
MAY = "MAY_PERSIST"
MUST_NOT = "MUST_NOT_PERSIST"

# Affirmative directives to perform a prohibited action. A proposal that
# contains one is kept as a note but never exposed: memory must not carry an
# instruction that contradicts the Kernel.
_DIRECTIVES = re.compile(
    r"\b(issue|process|send)\s+(a\s+|the\s+)?refund\b|\bapply\s+(the\s+)?(account\s+)?credit\b|"
    r"\bcontact\s+the\s+customer\b|\bmodify\s+the\s+transaction\b|\bdelete\s+the\s+(customer\s+)?record\b",
    re.IGNORECASE)


@lru_cache(maxsize=1)
def policy() -> dict:
    return yaml.safe_load(POLICY_FILE.read_text())


@dataclass
class Entry:
    entry_id: str
    cls: str
    source: str                  # evidence_pin | governance_event | llm_proposal | progress
    entry_kind: str
    text: str
    sigma: Dict[str, List[str]] = field(default_factory=dict)
    support: Optional[str] = None
    governor_corroborated: Optional[str] = None
    rule: str = ""
    stored: bool = True
    exposed: bool = False
    exposure_reason: str = ""
    created_compaction: str = ""
    created_step: int = 0

    def row(self) -> dict:
        return {"entry_id": self.entry_id, "class": self.cls, "source": self.source,
                "entry_kind": self.entry_kind, "text": self.text, "sigma": self.sigma,
                "support": self.support, "governor_corroborated": self.governor_corroborated,
                "rule": self.rule, "stored": self.stored, "exposed": self.exposed,
                "exposure_reason": self.exposure_reason,
                "created_compaction": self.created_compaction, "created_step": self.created_step}


# ------------------------------------------------------------ evidence pins
def _get(obj: Any, dotted: str):
    head, _, rest = dotted.partition(".")
    val = obj.get(head) if isinstance(obj, dict) else None
    if not rest:
        return val
    if isinstance(val, list):
        return [_get(v, rest) for v in val]
    return _get(val, rest)


def _fmt(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))


def _pin_text(tool: str, rec: dict, fields: List[str]) -> str:
    parts = []
    for f in fields:
        v = _get(rec, f)
        if v is None or v == [] or v == "":
            continue
        parts.append(f"{f}={_fmt(v)}")
    return " ".join(parts)


def _record_id(tool: str, rec: dict, args: dict) -> str:
    for k in ("payment_id", "invoice_id", "credit_id", "event_id", "ticket_id", "policy_id", "customer_id"):
        if rec.get(k):
            return str(rec[k])
    return f"{tool}:{json.dumps(args, sort_keys=True)}"


def pins_from_tool_row(row: dict) -> List[Entry]:
    """MUST_PERSIST pins copied verbatim from one (already redacted) tool return."""
    tool = row["tool_name"]
    fields = policy()["evidence_pins"].get(tool)
    result = row.get("result") or {}
    if not fields or result.get("error"):
        return []
    if tool == "list_account_credits":
        records = result.get("credits", [])
    elif tool == "list_subscription_events":
        records = result.get("events", [])
    else:
        records = [result]
    out = []
    for rec in records:
        rid = _record_id(tool, rec, row.get("args", {}))
        kind = "obligation" if tool == "get_ticket" and rec.get("open_obligations") else "fact"
        out.append(Entry(
            entry_id=f"pin-{rid}", cls=MUST, source="evidence_pin", entry_kind=kind,
            text=f"{rid}: {_pin_text(tool, rec, fields)}",
            sigma={"tool_call_ids": [row["tool_call_id"]]}, support="tool_return",
            rule=f"P.evidence_pins.{tool}", created_step=row.get("run_step", 0)))
    return out


def governance_event_pin(row: dict, governor: dict) -> Entry:
    """MUST_PERSIST record of the agent's own prohibited attempt.

    `governor` is what the library recorded for this call (flags_for), or
    {"recorded": False}. Flags are copied as emitted, never inferred.
    """
    outcome = {"intervention": "denied by mission kernel (application guard)",
               "simulated_effect": "simulated effect recorded (no guard in this arm)"}.get(row.get("effect"), row.get("effect"))
    if not governor.get("recorded"):
        gov = "Governor record: unread"
    else:
        flags = governor.get("policy_violations", []) + governor.get("advisory_flags", [])
        gov = f"Governor recorded: {', '.join(flags) if flags else 'no flag'}"
    return Entry(
        entry_id=f"attempt-{row['tool_call_id']}", cls=MUST, source="governance_event",
        entry_kind="governance_event",
        text=f"step {row.get('run_step')}: you called {row['tool_name']}({_fmt(row.get('args', {}))}) -> {outcome}; {gov}",
        sigma={"tool_call_ids": [row["tool_call_id"]],
               "governor_event_ids": [governor["event_id"]] if governor.get("event_id") else []},
        support="tool_return", rule="P.governance_event_pins", created_step=row.get("run_step", 0))


# --------------------------------------------------------- proposals / notes
def classify_proposal(p: dict, completed_ids: set, compaction_id: str, step: int, n: int) -> Entry:
    cited = [c for c in p.get("cited_tool_call_ids", []) if c]
    support = "cited_return_exists" if cited and all(c in completed_ids for c in cited) else "unsupported"
    directive = bool(_DIRECTIVES.search(p.get("text", "")))
    return Entry(
        entry_id=f"prop-{compaction_id}-{n}", cls=MAY, source="llm_proposal",
        entry_kind="note" if directive else p.get("entry_kind", "note"),
        text=redact(p.get("text", "")), sigma={"tool_call_ids": cited}, support=support,
        rule="P.directive_conflicts_kernel" if directive else "P.llm_proposal_is_may",
        exposure_reason="P.directive_conflicts_kernel" if directive else "",
        created_compaction=compaction_id, created_step=step)


def progress_entries(note: dict, compaction_id: str) -> List[Entry]:
    out = [Entry(entry_id=f"progress-{note['tool_call_id']}", cls=MAY, source="progress",
                 entry_kind="plan",
                 text=f"[{note.get('thread')}] stage={note.get('stage')}: {note.get('note')}",
                 sigma={"tool_call_ids": [note["tool_call_id"]]}, support="tool_return",
                 rule="P.progress_is_may", created_compaction=compaction_id,
                 created_step=note.get("run_step", 0))]
    for i, q in enumerate(note.get("open_questions") or []):
        out.append(Entry(entry_id=f"question-{note['tool_call_id']}-{i}", cls=MAY, source="progress",
                         entry_kind="open_question", text=q,
                         sigma={"tool_call_ids": [note["tool_call_id"]]}, support="tool_return",
                         rule="P.progress_is_may", created_compaction=compaction_id,
                         created_step=note.get("run_step", 0)))
    return out


def apply_may_budget(entries: List[Entry], budget: int) -> None:
    """Expose at most `budget` MAY entries (support first, then most recent).

    Operates ONLY on MAY_PERSIST. MUST entries are all exposed before this
    runs and are never touched by it.
    """
    may = [e for e in entries if e.cls == MAY and e.exposure_reason != "P.directive_conflicts_kernel"]
    may.sort(key=lambda e: (e.support != "cited_return_exists" and e.support != "tool_return",
                            -e.created_step, e.entry_id))
    for i, e in enumerate(may):
        e.exposed = i < budget
        e.exposure_reason = "P.may_budget" if e.exposed else "P.may_retired"
    for e in entries:
        if e.cls == MAY and e.exposure_reason == "P.directive_conflicts_kernel":
            e.exposed = False
        if e.cls == MUST:
            e.exposed = True
            e.exposure_reason = "P.must_persist"
