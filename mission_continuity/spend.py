"""Estimated API spend, from Governor-measured tokens.

Token counts come only from the Governor token snapshots written by the
published pydantic-ai-governor library (every session: investigator,
compactor, preflight, calibration, resumed). They are multiplied by per-token
prices recorded in config/pricing.yaml with their source and date. The result
is always an ESTIMATE; the authoritative ceiling is the operator's Console
spend limit.

Fail closed: if prices for the configured model are not recorded, no paid run
may start.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml

from mission_continuity.paths import CONFIG, TRACE_DIR

SOFT_STOP_USD = 80.0
PRICING_FILE = CONFIG / "pricing.yaml"


class SpendStop(RuntimeError):
    pass


@dataclass(frozen=True)
class Pricing:
    model: str
    input_per_mtok: Optional[float]
    output_per_mtok: Optional[float]
    source: Optional[str]
    retrieved: Optional[str]

    @property
    def complete(self) -> bool:
        return (
            self.input_per_mtok is not None
            and self.output_per_mtok is not None
            and bool(self.source)
            and bool(self.retrieved)
        )


@dataclass(frozen=True)
class TokenTotals:
    input_tokens: int
    output_tokens: int
    turns: int
    sessions: int


def load_pricing(path: Path = PRICING_FILE) -> Pricing:
    data = yaml.safe_load(path.read_text()) or {}
    return Pricing(
        model=str(data.get("model", "")),
        input_per_mtok=data.get("input_per_mtok"),
        output_per_mtok=data.get("output_per_mtok"),
        source=data.get("source"),
        retrieved=data.get("retrieved"),
    )


def _token_snapshots(lines: Iterable[str]):
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload") or {}
        # A token snapshot is the CONTEXT_SNAPSHOT the library emits after a
        # model turn; it carries llm_turn_id. Tool snapshots do not.
        if event.get("event_type") == "CONTEXT_SNAPSHOT" and "llm_turn_id" in payload:
            yield payload


def measured_tokens(trace_dir: Path = TRACE_DIR) -> TokenTotals:
    inp = out = turns = sessions = 0
    if trace_dir.exists():
        for trace in sorted(trace_dir.glob("*.jsonl")):
            sessions += 1
            with trace.open() as fh:
                for payload in _token_snapshots(fh):
                    turns += 1
                    inp += int(payload.get("llm_prompt_tokens") or 0)
                    out += int(payload.get("llm_completion_tokens") or 0)
    return TokenTotals(inp, out, turns, sessions)


def estimate_usd(tokens: TokenTotals, pricing: Pricing) -> float:
    if not pricing.complete:
        raise SpendStop(
            f"Pricing for '{pricing.model}' is not recorded in {PRICING_FILE.name} "
            "(input/output per MTok, source, date). No paid run may start."
        )
    return (
        tokens.input_tokens * pricing.input_per_mtok
        + tokens.output_tokens * pricing.output_per_mtok
    ) / 1_000_000


def check_before_run(projected_usd: float, trace_dir: Path = TRACE_DIR,
                     pricing: Optional[Pricing] = None) -> float:
    """Refuse a run that would push the estimate past the soft stop."""
    pricing = pricing or load_pricing()
    spent = estimate_usd(measured_tokens(trace_dir), pricing)
    if spent + projected_usd > SOFT_STOP_USD:
        raise SpendStop(
            f"Estimated spend ${spent:.2f} plus this run's projected "
            f"${projected_usd:.2f} would exceed the ${SOFT_STOP_USD:.0f} soft stop. "
            "Stop and ask the operator."
        )
    return spent


def report(trace_dir: Path = TRACE_DIR) -> str:
    tokens = measured_tokens(trace_dir)
    pricing = load_pricing()
    lines = [
        f"Governor-measured tokens: input {tokens.input_tokens:,}, output "
        f"{tokens.output_tokens:,} across {tokens.turns} model turns in "
        f"{tokens.sessions} Governor sessions",
    ]
    if pricing.complete:
        usd = estimate_usd(tokens, pricing)
        lines.append(
            f"Estimated spend: ${usd:.2f} (estimate; prices from {pricing.source}, "
            f"{pricing.retrieved})"
        )
        lines.append(
            f"Soft stop ${SOFT_STOP_USD:.0f}: ${SOFT_STOP_USD - usd:.2f} estimated remaining"
        )
    else:
        lines.append(
            f"Estimated spend: unavailable (pricing for '{pricing.model}' not yet "
            "recorded; paid runs are blocked until it is)"
        )
    return "\n".join(lines)
