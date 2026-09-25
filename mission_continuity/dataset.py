"""The synthetic environment. Tools read from here; ground truth is never exposed."""

from __future__ import annotations

import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from mission_continuity.paths import REPO

DATA_FILE = REPO / "data" / "c1042.json"


@lru_cache(maxsize=1)
def _raw() -> dict:
    return json.loads(DATA_FILE.read_text())


def data() -> dict:
    """A copy of the environment WITHOUT ground truth."""
    d = copy.deepcopy(_raw())
    d.pop("ground_truth", None)
    return d


def ground_truth() -> dict:
    """For the evaluator only. Never reachable from any tool."""
    return copy.deepcopy(_raw()["ground_truth"])


def sha256(path: Path = DATA_FILE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
