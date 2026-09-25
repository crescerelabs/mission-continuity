"""MUST_NOT_PERSIST redaction. Every app write passes through here first."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, List, Tuple

import yaml

from mission_continuity.paths import REPO

POLICY_FILE = REPO / "policy" / "retention.yaml"


@lru_cache(maxsize=1)
def _policy() -> dict:
    return yaml.safe_load(POLICY_FILE.read_text())


@lru_cache(maxsize=1)
def _known_values() -> Tuple[Tuple[str, str], ...]:
    """Exact synthetic PII values from the dataset, so a regex miss still redacts."""
    from mission_continuity.dataset import data

    acct = data()["account"]
    vals = [("owner_phone", acct["owner_phone"]), ("billing_address", acct["billing_address"]),
            ("card_number", acct["payment_method"]["card_number"])]
    for seat in acct["seats"]:
        vals += [("name", seat["name"]), ("email", seat["email"])]
    vals.append(("phone", "(207) 555-0142"))
    return tuple(sorted(vals, key=lambda kv: -len(kv[1])))


def _patterns():
    return [(k, re.compile(p)) for k, p in _policy()["pii_patterns"].items()]


def redact_text(text: str, withheld: List[str] | None = None) -> str:
    for field, value in _known_values():
        if value in text:
            text = text.replace(value, f"<withheld:{field}>")
            if withheld is not None:
                withheld.append(field)
    for field, pat in _patterns():
        def sub(m, field=field):
            if withheld is not None:
                withheld.append(field)
            return f"<withheld:{field}>"
        text = pat.sub(sub, text)
    return text


def redact(obj: Any, withheld: List[str] | None = None) -> Any:
    """Deep copy of obj with every MUST_NOT_PERSIST value withheld."""
    fields = set(_policy()["pii_fields"])
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in fields and v not in (None, ""):
                out[k] = f"<withheld:{k}>"
                if withheld is not None:
                    withheld.append(k)
            else:
                out[k] = redact(v, withheld)
        return out
    if isinstance(obj, list):
        return [redact(v, withheld) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj, withheld)
    return obj


def known_pii_values() -> List[str]:
    """For the A7 leak scan."""
    return [v for _, v in _known_values()]
