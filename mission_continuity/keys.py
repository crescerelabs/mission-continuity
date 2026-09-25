"""Anthropic API key access.

The key lives in the operator's macOS keychain under the service name below.
It is read in the parent process (real HOME, so the login keychain resolves)
and handed to agent subprocesses through their environment only. It is never
printed, logged, written to disk or committed.
"""

from __future__ import annotations

import os
import subprocess

KEYCHAIN_SERVICE = "mission-continuity-anthropic"
ENV_VAR = "ANTHROPIC_API_KEY"


class MissingKey(RuntimeError):
    pass


def key_present() -> bool:
    """True if a key is available, without reading its value."""
    if os.environ.get(ENV_VAR):
        return True
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
        capture_output=True,
    )
    return result.returncode == 0


def anthropic_key() -> str:
    """The key value. Callers must never print or persist it."""
    value = os.environ.get(ENV_VAR)
    if value:
        return value
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise MissingKey(
            f"No Anthropic key found. Set {ENV_VAR} or store it with: "
            f"security add-generic-password -a \"$USER\" -s {KEYCHAIN_SERVICE} -w"
        )
    return value
