"""rooted-api — FastAPI surface over the frozen brain.

The boundary rule: this layer routes on ``decision.action`` (a three-value enum
the Python brain emitted) and never on message content. Friction-by-design lives
here (approve/edit/reject rules), not in the workflow.

Concurrency: handlers are sync ``def`` (FastAPI runs them in a threadpool), so
every load→mutate→save of queue/decision/flag state holds the single module-level
LOCK. --workers 1 keeps that lock authoritative. Eval has its own non-blocking
lock so a second concurrent run 409s instead of stacking live Opus calls.
"""

import hmac
import json
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import adapter, evalrunner, settings
from .audit import audit

LOCK = threading.Lock()          # guards queue/decision/flag mutations
EVAL_LOCK = threading.Lock()     # non-blocking: serialises live eval runs

MESSAGE_ID_RE = r"^[A-Za-z0-9._-]{1,64}$"
HANDLE_RE = r"^[A-Za-z0-9._]{1,64}$"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Refuse to start if the in-repo brain isn't where settings points.
    adapter.assert_brain()
    adapter.configure_data_dir(settings.DATA_DIR)
    yield


app = FastAPI(title="rooted-api", version="1.0.0", lifespan=lifespan)


# ------------------------------------------------------------------- auth

def require_key(x_api_key: str | None = Header(default=None, alias="X-Api-Key")) -> None:
    expected = settings.ROOTED_API_KEY
    if not expected or x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Api-Key")


# --------------------------------------------------------------- models

class RouteReq(BaseModel):
    message_id: str = Field(pattern=MESSAGE_ID_RE)
    handle: str
    text: str = Field(min_length=1, max_length=4000)
    channel: str = "simulated"

    @field_validator("handle", mode="before")
    @classmethod
    def _clean_handle(cls, v):
        if not isinstance(v, str):
            raise ValueError("handle must be a string")
        import re
        v = v.strip().lstrip("@")
        if not re.match(HANDLE_RE, v):
            raise ValueError("invalid handle")
        return v


class ApproveReq(BaseModel):
    queue_id: str
    decision: Literal["approve", "edit", "reject"]
    edited_text: str | None = None
    reason: str | None = None
    approver: str = "sofia"


class ConfigReq(BaseModel):
    auto_send_enabled: bool
    reason: str | None = None


class EvalReq(BaseModel):
    no_cache: bool = False


class SentReq(BaseModel):
    message_id: str | None = None
    queue_id: str | None = None
    channel: str
    ok: bool
    detail: str | None = None


# --------------------------------------------------------------- /v1/route

@app.post("/v1/route")
def route(req: RouteReq, _=Depends(require_key)):
    with LOCK:
        decision_path = settings.DATA_DIR / "decisions" / f"{req.message_id}.json"

        # 1. Idempotent replay: return the stored decision verbatim.
        if decision_path.exists():
            stored = json.loads(decision_path.read_text(encoding="utf-8"))
            stored["duplicate"] = True
            audit("route", "n8n", req.message_id, {"duplicate": True})
            return stored

        # 2. Crash window: handled by the brain, but no decision was stored.
        handled = adapter.already_handled(req.message_id)
        if handled:
            audit("error", "n8n", req.message_id,
                  {"error": "handled_but_no_decision", "reason": handled})
            return JSONResponse(
                status_code=409,
                content={"error": "handled_but_no_decision", "message_id": req.message_id},
            )

        # 3. Fresh: route through the frozen brain, apply the kill switch, persist.
        decision = adapter.build_decision(req.message_id, req.handle, req.text)
        response = {"message_id": req.message_id, "duplicate": False, **decision}
        decision_path.parent.mkdir(parents=True, exist_ok=True)
        decision_path.write_text(json.dumps(response, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        audit("route", "n8n", req.message_id, {
            "action": decision["action"], "lane": decision["lane"],
            "killswitch_applied": decision["killswitch_applied"],
        })
        return response


# ------------------------------------------------------------- /v1/approve

@app.post("/v1/approve")
def approve(req: ApproveReq, _=Depends(require_key)):
    with LOCK:
        q = adapter.load_queue()
        item = next((i for i in q if i["id"] == req.queue_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail=f"queue item {req.queue_id} not found")
        if item["status"] != "pending":
            raise HTTPException(status_code=409,
                                detail=f"item {req.queue_id} is {item['status']}, not pending")

        warnings: list[str] = []
        if req.decision == "approve":
            if not item["draft_reply"]:
                raise HTTPException(
                    status_code=422,
                    detail="no draft to approve — use decision=edit with your own text",
                )
            reply = adapter.write_approved_outbox(item)
            item["status"] = "approved"
            audit("approve", "sofia", req.queue_id, {"approver": req.approver})

        elif req.decision == "edit":
            if not (req.edited_text and req.edited_text.strip()) or not (req.reason and req.reason.strip()):
                raise HTTPException(status_code=422,
                                    detail="edit requires non-empty edited_text AND reason")
            reply = adapter.write_edited_outbox(item, req.edited_text, req.reason)
            warnings = adapter.validate(req.edited_text)  # human text is the human decision
            item["status"] = "approved_with_edits"
            audit("edit", "sofia", req.queue_id,
                  {"approver": req.approver, "reason": req.reason, "warnings": warnings})

        else:  # reject
            if not (req.reason and req.reason.strip()):
                raise HTTPException(status_code=422, detail="reject requires a non-empty reason")
            reply = ""
            item["status"] = "rejected"
            audit("reject", "sofia", req.queue_id, {"approver": req.approver, "reason": req.reason})

        adapter.save_queue(q)
        return {
            "queue_id": req.queue_id, "status": item["status"], "handle": item["handle"],
            "reply_to_send": reply, "warnings": warnings,
        }


# --------------------------------------------------------------- /v1/queue

@app.get("/v1/queue")
def queue(status: str = "pending", _=Depends(require_key)):
    with LOCK:
        q = adapter.load_queue()
    items = [i for i in q if i["status"] == status] if status else list(q)
    items = sorted(items, key=lambda i: 0 if i["urgency"] == "high" else 1)
    counts = {
        "pending": sum(1 for i in q if i["status"] == "pending"),
        "high": sum(1 for i in items if i["urgency"] == "high"),
    }
    return {"items": items, "counts": counts}


# ---------------------------------------------------------------- /v1/eval

@app.post("/v1/eval")
def eval_endpoint(req: EvalReq, _=Depends(require_key)):
    if not EVAL_LOCK.acquire(blocking=False):
        return JSONResponse(status_code=409, content={"error": "eval_already_running"})
    try:
        result = evalrunner.run_eval(req.no_cache)
    finally:
        EVAL_LOCK.release()
    with LOCK:
        audit("eval", "sentinel", None, {
            "gate": result["gate"], "exit_code": result["exit_code"],
            "dangerous": result["dangerous"],
        })
    return result


# -------------------------------------------------------------- /v1/config

@app.get("/v1/config")
def get_config(_=Depends(require_key)):
    return {"auto_send_enabled": not adapter.autosend_disabled()}


@app.post("/v1/config")
def set_config(req: ConfigReq, _=Depends(require_key)):
    with LOCK:
        flag = settings.DATA_DIR / "flags" / "autosend_disabled"
        flag.parent.mkdir(parents=True, exist_ok=True)
        if req.auto_send_enabled:
            flag.unlink(missing_ok=True)       # re-enabling is deliberately manual
        else:
            flag.write_text(req.reason or "disabled", encoding="utf-8")
        audit("config", "operator", None,
              {"auto_send_enabled": req.auto_send_enabled, "reason": req.reason})
        return {"auto_send_enabled": not adapter.autosend_disabled()}


# ---------------------------------------------------------------- /v1/sent

@app.post("/v1/sent")
def sent(req: SentReq, _=Depends(require_key)):
    with LOCK:
        audit("sent", "n8n", req.message_id or req.queue_id,
              {"channel": req.channel, "ok": req.ok, "detail": req.detail})
    return {"recorded": True}


# ----------------------------------------------------------------- /healthz

def _data_dir_writable() -> bool:
    try:
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = settings.DATA_DIR / ".healthz"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


@app.get("/healthz")
def healthz():
    brain = adapter.brain_ok()
    body = {
        "ok": brain and _data_dir_writable(),
        "brain_ok": brain,
        "data_dir_writable": _data_dir_writable(),
        "anthropic_key_present": settings.anthropic_key_present(),
        "auto_send_enabled": not adapter.autosend_disabled(),
    }
    return JSONResponse(status_code=200 if brain else 503, content=body)
