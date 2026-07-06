"""Append-only JSONL audit log — one event per line, never rewritten.

Every mutating endpoint appends exactly one event. Callers hold the module-level
lock in app.py while writing, so lines never interleave. The file is the record
of everything the automation layer did on the operator's behalf.
"""

import datetime as dt
import json
from typing import Any

from . import settings

_EVENTS = {"route", "approve", "reject", "edit", "sent", "eval", "config", "error"}
_ACTORS = {"n8n", "sofia", "sentinel", "operator"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def audit(event: str, actor: str, ref: str | None, detail: dict[str, Any] | None = None) -> None:
    """Append one audit event. Unknown event/actor values are allowed but tagged,
    so a typo shows up in the log instead of being silently swallowed."""
    line = {
        "ts": _now(),
        "event": event if event in _EVENTS else f"?{event}",
        "actor": actor if actor in _ACTORS else f"?{actor}",
        "ref": ref,
        "detail": detail or {},
    }
    path = settings.DATA_DIR / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
