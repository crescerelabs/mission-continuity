"""RunStore: the ONLY write path for app files. Everything is redacted first."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mission_continuity.paths import run_dir
from mission_continuity.redact import redact


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RunStore:
    def __init__(self, label: str, root: Path | None = None):
        self.label = label
        self.dir = root or run_dir(label)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def append(self, name: str, row: dict) -> dict:
        row = {"ts": _now(), **row}
        clean = redact(row)
        with self.path(name).open("a") as fh:
            fh.write(json.dumps(clean, default=str) + "\n")
        return clean

    def write_json(self, name: str, obj: Any) -> Any:
        clean = redact(obj)
        self.path(name).write_text(json.dumps(clean, indent=1, default=str))
        return clean

    def write_text(self, name: str, text: str) -> None:
        self.path(name).write_text(redact(text))

    def read_jsonl(self, name: str) -> list:
        p = self.dir / name
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def read_json(self, name: str, default=None):
        p = self.dir / name
        return json.loads(p.read_text()) if p.exists() else default
