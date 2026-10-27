"""Viewer adapter for the separately installed synchronization completion helper."""
from __future__ import annotations

import os
import re
import socket
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SYNC_LIB = Path(os.environ.get("ASSETFURNACE_SYNC_LIB", REPO.parent / "managed" / "sync"))
if str(SYNC_LIB) not in sys.path:
    sys.path.insert(0, str(SYNC_LIB))
from assetfurnace_completion import CompletionError, invalidate, seal


def owner() -> str:
    value = os.environ.get("ASSETFURNACE_SYNC_OWNER") or socket.gethostname().split(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9_-]", "-", value)[:32] or "host"


def host_name(name: str) -> str:
    """Add exactly one stable host namespace to newly-created independent runs."""
    host = owner()
    match = re.match(r"^(ash|rowan)__(.*)$", name, re.IGNORECASE)
    if match:
        return name[:80]
    return f"{host}__{name}"[:80]

def _state_root() -> Path:
    return Path(os.environ.get("ASSETFURNACE_SYNC_STATE", REPO.parent / "managed" / "sync-state"))


def revoke(output_root: Path, bundle: Path) -> None:
    try:
        relative = bundle.resolve().relative_to(output_root.resolve()).as_posix()
    except ValueError as exc:
        raise CompletionError("bundle is outside output root") from exc
    invalidate(_state_root(), relative)


def complete(output_root: Path, bundle: Path) -> None:
    try:
        relative = bundle.resolve().relative_to(output_root.resolve()).as_posix()
    except ValueError as exc:
        raise CompletionError("bundle is outside output root") from exc
    seal(output_root, _state_root(), relative, owner())
