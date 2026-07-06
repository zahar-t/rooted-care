"""The ONLY module that touches the brain.

It imports ``autopilot`` straight from this repository's ``slice/`` (same commit
as the service, by construction), redirects its queue/outbox writes into the
service data dir, and folds the ``route()`` action list into the decision shape
the API returns. No brain logic is reimplemented here — every prompt, lane
decision, gate check and the holding-ack text run from the slice's own file. The
single exception is the ~10 file-writing lines that mirror ``cmd_review``'s
approve/edit outbox formats (that CLI can't be called programmatically); those
are plumbing, not brain.
"""

import datetime as dt
import os
import sys
from pathlib import Path

from . import settings

# --------------------------------------------------------------- import shim
# autopilot.py calls sys.stdout.reconfigure() at import; pytest's capture object
# may not support it, so swap in the real stdout (or a stub) just for the import.
sys.path.insert(0, str(settings.SLICE_DIR))


class _Reconfigurable:
    def reconfigure(self, *a, **k):
        pass

    def write(self, *a, **k):
        pass

    def flush(self):
        pass


_saved_stdout = sys.stdout
sys.stdout = sys.__stdout__ if sys.__stdout__ is not None else _Reconfigurable()
import autopilot  # noqa: E402  (the brain, imported from this repo's slice/)

sys.stdout = _saved_stdout


def configure_data_dir(data_dir: Path | str) -> None:
    """Point the frozen brain's file writes at ``data_dir`` and create the layout.

    ``autopilot`` reads QUEUE_FILE/OUTBOX at call time, so reassigning the module
    globals is enough to keep every write inside the service data dir — the
    repo's ``slice/queue.json`` and ``slice/outbox/`` are never touched.
    """
    settings.DATA_DIR = Path(data_dir)
    autopilot.QUEUE_FILE = settings.DATA_DIR / "queue.json"
    autopilot.OUTBOX = settings.DATA_DIR / "outbox"
    for sub in ("inbox", "outbox", "held", "decisions", "flags"):
        (settings.DATA_DIR / sub).mkdir(parents=True, exist_ok=True)


if os.environ.get("LIVE_CLOCK") == "1":
    autopilot.TODAY = dt.date.today()

configure_data_dir(settings.DATA_DIR)


# --------------------------------------------------------------- brain locator
#
# The brain ships in this same repository (../slice). There is nothing to vendor
# and no SHA to pin: the service and the brain are the same commit by
# construction, so a history rewrite can never orphan anything. The one failure
# left to guard is a mis-set ROOTED_SLICE_DIR, so the check is a path check.

def brain_ok() -> bool:
    """True iff the in-repo brain is where settings says it is."""
    return (settings.SLICE_DIR / "autopilot.py").is_file()


def assert_brain() -> None:
    """Raise if the brain path is wrong (a mis-set ROOTED_SLICE_DIR)."""
    if not brain_ok():
        raise RuntimeError(
            f"brain not found at {settings.SLICE_DIR} — point ROOTED_SLICE_DIR "
            "at the repo's slice/ directory"
        )


# ---------------------------------------------------------------- thin passthroughs

def load_queue() -> list[dict]:
    return autopilot.load_queue()


def save_queue(q: list[dict]) -> None:
    autopilot.save_queue(q)


def validate(text: str) -> list[str]:
    """Frozen deterministic draft gate. Empty list == clean."""
    return autopilot.validate_draft(text)


def autosend_disabled() -> bool:
    return (settings.DATA_DIR / "flags" / "autosend_disabled").exists()


def already_handled(message_id: str) -> str | None:
    """Frozen idempotency guard, keyed by the message-id filename."""
    msg_file = settings.DATA_DIR / "inbox" / f"{message_id}.txt"
    return autopilot.already_handled(msg_file, autopilot.load_queue())


# ------------------------------------------------------------------ routing

def _project_queued(item: dict | None) -> dict | None:
    if item is None:
        return None
    return {
        "id": item["id"],
        "type": item["type"],
        "urgency": item["urgency"],
        "summary": item["summary"],
        "notes": item["notes"],
        "has_draft": bool(item["draft_reply"]),
        "draft_reply": item["draft_reply"],
    }


def _read_sent_reply(message_id: str) -> str:
    """Read the reply text send_now() wrote (strip the To:/Status: header)."""
    reply_path = settings.DATA_DIR / "outbox" / f"reply_{message_id}.txt"
    return reply_path.read_text(encoding="utf-8").split("\n\n", 1)[1].strip()


def build_decision(message_id: str, handle: str, text: str) -> dict:
    """Route one message through the frozen brain and fold the result.

    Writes ``inbox/<id>.txt``, calls the frozen ``route()``, folds its action
    list into a decision, then applies the kill switch (demote-never-promote).
    Callers hold the app lock: this both reads and appends queue.json.
    """
    inbox_path = settings.DATA_DIR / "inbox" / f"{message_id}.txt"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    inbox_path.write_text(f"From: @{handle}\n\n{text}", encoding="utf-8")

    actions = autopilot.route(inbox_path)

    queued_item: dict | None = None
    reply_to_send: str | None = None
    reply_kind: str | None = None
    for a in actions:
        if a["action"] == "QUEUED":
            queued_item = a["item"]
        elif a["action"] == "AUTO-SENT":
            reply_to_send = _read_sent_reply(message_id)
            reply_kind = "holding_ack" if "holding ack" in a["detail"] else "care_reply"

    # Derive the top-level action strictly from what the brain actually did.
    if reply_kind == "care_reply" and queued_item is None:
        action = "auto_send"
    elif reply_kind == "holding_ack" and queued_item is not None:
        action = "ack_and_queue"
    elif reply_kind is None and queued_item is not None:
        action = "queue"
    else:
        raise RuntimeError(f"unexpected action fold for {message_id!r}: {actions}")

    # Kill switch: the plumbing may demote autonomy, never widen it. A model-
    # written auto-send is suspended to the queue; the deterministic holding ack
    # in ack_and_queue is untouched (no model wrote it).
    killswitch_applied = False
    if action == "auto_send" and autosend_disabled():
        queued_item = _demote_auto_send(inbox_path, message_id, handle, text, reply_to_send)
        action = "queue"
        reply_to_send = None
        reply_kind = None
        killswitch_applied = True

    return {
        "handle": handle,
        "action": action,
        "lane": queued_item["type"] if queued_item else autopilot.LANE_CARE_AUTO,
        "reply_to_send": reply_to_send,
        "reply_kind": reply_kind,
        "queued": _project_queued(queued_item),
        "killswitch_applied": killswitch_applied,
    }


def _demote_auto_send(inbox_path: Path, message_id: str, handle: str,
                      text: str, reply_text: str | None) -> dict:
    """Move the gate-passed reply out of the send path and queue it for Sofia.

    The queue item keeps already_handled() true, so idempotency still holds even
    though the outbox file moved to held/.
    """
    src = settings.DATA_DIR / "outbox" / f"reply_{message_id}.txt"
    dst = settings.DATA_DIR / "held" / f"reply_{message_id}.txt"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        src.replace(dst)
    return autopilot.queue_item(
        inbox_path, handle, "NEEDS_SOFIA", "normal",
        summary=f"(auto-send suspended) {text[:80]}",
        notes="Auto-send suspended by eval sentinel — this draft PASSED the gate; "
              "held for human review.",
        draft_reply=reply_text,
    )["item"]


# --------------------------------------------- outbox writers (cmd_review mirror)
# The single permitted re-implementation: the ~10 file-writing lines of the
# interactive CLI's approve/edit. Byte-for-byte the same formats as §1.2.

def _outbox_path(message_name: str) -> Path:
    stem = Path(message_name).stem
    return settings.DATA_DIR / "outbox" / f"reply_{stem}.txt"


def write_approved_outbox(item: dict) -> str:
    out = _outbox_path(item["message"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"To: @{item['handle']}\nStatus: APPROVED by Sofia ({item['type']})\n\n"
        f"{item['draft_reply']}\n",
        encoding="utf-8",
    )
    return item["draft_reply"]


def write_edited_outbox(item: dict, edited_text: str, reason: str) -> str:
    out = _outbox_path(item["message"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"To: @{item['handle']}\nStatus: EDITED by Sofia (reason: {reason})\n\n"
        f"{edited_text}\n",
        encoding="utf-8",
    )
    return edited_text
