"""API contract tests. Every test uses the mocked LLM (via the client fixture);
zero live calls. The eval endpoint is tested for auth + 409 concurrency + the
success shape (run_eval monkeypatched) — never the real subprocess.
"""

import json

from service.tests import fixtures


def _route(client, auth, message_id, handle, marker):
    return client.post("/v1/route", headers=auth, json={
        "message_id": message_id, "handle": handle, "text": fixtures.text_for(marker),
    })


# ------------------------------------------------------------------ auth

def test_missing_api_key_401(client):
    r = client.post("/v1/route", json={
        "message_id": "dm-1", "handle": "sofia.grows", "text": "hi?"})
    assert r.status_code == 401


def test_bad_api_key_401(client):
    r = client.get("/v1/queue", headers={"X-Api-Key": "wrong-key"})
    assert r.status_code == 401


# --------------------------------------------------------------- /v1/route

def test_route_auto_send(client, auth):
    r = _route(client, auth, "dm-care", fixtures.HANDLE_CARE, "MK_CAREOK")
    assert r.status_code == 200
    d = r.json()
    assert d["action"] == "auto_send"
    assert d["lane"] == "AUTO_SEND_CARE"
    assert d["reply_kind"] == "care_reply"
    assert d["duplicate"] is False
    assert d["reply_to_send"] == fixtures.GOOD_DRAFT
    assert d["queued"] is None


def test_route_queue_billing(client, auth):
    d = _route(client, auth, "dm-bill", fixtures.HANDLE_BILL, "MK_BILL").json()
    assert d["action"] == "queue"
    assert d["lane"] == "BILLING_DISPUTE"
    assert d["reply_to_send"] is None
    assert d["queued"]["has_draft"] is False
    assert d["queued"]["urgency"] == "high"


def test_route_ack_and_queue(client, auth):
    d = _route(client, auth, "dm-fig", "leafy.lou", "MK_CARENOGUIDE").json()
    assert d["action"] == "ack_and_queue"
    assert d["reply_kind"] == "holding_ack"
    assert "Lou" in d["reply_to_send"]  # real first name for leafy.lou
    assert d["queued"]["type"] == "NEEDS_SOFIA"


def test_message_id_path_traversal_422(client, auth):
    r = client.post("/v1/route", headers=auth, json={
        "message_id": "../evil", "handle": "sofia.grows", "text": "hi?"})
    assert r.status_code == 422


def test_empty_text_422(client, auth):
    r = client.post("/v1/route", headers=auth, json={
        "message_id": "dm-2", "handle": "sofia.grows", "text": ""})
    assert r.status_code == 422


def test_handle_leading_at_is_stripped(client, auth):
    d = _route(client, auth, "dm-at", "@sofia.grows", "MK_CAREOK").json()
    assert d["handle"] == "sofia.grows"


def test_route_duplicate_is_idempotent(client, auth):
    r1 = _route(client, auth, "dm-dup", fixtures.HANDLE_BILL, "MK_BILL")
    assert r1.status_code == 200
    before = client.get("/v1/queue", headers=auth).json()["items"]

    r2 = _route(client, auth, "dm-dup", fixtures.HANDLE_BILL, "MK_BILL")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["duplicate"] is True

    expected = r1.json()
    expected["duplicate"] = True
    assert d2 == expected  # verbatim except the flag

    after = client.get("/v1/queue", headers=auth).json()["items"]
    assert len(after) == len(before)  # no new queue item


# ------------------------------------------------------------- /v1/approve

def test_approve_writes_outbox_and_status(client, auth, data_dir):
    _route(client, auth, "dm-repl", fixtures.HANDLE_REPL, "MK_REPLKNOWN")
    r = client.post("/v1/approve", headers=auth,
                    json={"queue_id": "q001", "decision": "approve"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    out = (data_dir / "outbox" / "reply_dm-repl.txt").read_text(encoding="utf-8")
    assert "Status: APPROVED by Sofia (REPLACEMENT)" in out
    assert out.startswith("To: @dan_greenthumb\n")

    # second approve of the same item -> 409
    r2 = client.post("/v1/approve", headers=auth,
                     json={"queue_id": "q001", "decision": "approve"})
    assert r2.status_code == 409


def test_approve_empty_draft_422(client, auth):
    _route(client, auth, "dm-b", fixtures.HANDLE_BILL, "MK_BILL")  # billing has no draft
    r = client.post("/v1/approve", headers=auth,
                    json={"queue_id": "q001", "decision": "approve"})
    assert r.status_code == 422


def test_approve_unknown_id_404(client, auth):
    r = client.post("/v1/approve", headers=auth,
                    json={"queue_id": "q999", "decision": "reject", "reason": "x"})
    assert r.status_code == 404


def test_edit_requires_text_and_reason(client, auth):
    _route(client, auth, "dm-e", fixtures.HANDLE_REPL, "MK_REPLKNOWN")
    r = client.post("/v1/approve", headers=auth,
                    json={"queue_id": "q001", "decision": "edit", "edited_text": "hi"})
    assert r.status_code == 422  # missing reason


def test_reject_requires_reason(client, auth):
    _route(client, auth, "dm-r", fixtures.HANDLE_REPL, "MK_REPLKNOWN")
    r = client.post("/v1/approve", headers=auth,
                    json={"queue_id": "q001", "decision": "reject"})
    assert r.status_code == 422


def test_reject_writes_nothing_but_sets_status(client, auth, data_dir):
    _route(client, auth, "dm-rej", fixtures.HANDLE_REPL, "MK_REPLKNOWN")
    r = client.post("/v1/approve", headers=auth,
                    json={"queue_id": "q001", "decision": "reject", "reason": "off-brand"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["reply_to_send"] == ""
    assert not (data_dir / "outbox" / "reply_dm-rej.txt").exists()


def test_edit_returns_validate_warnings_but_still_200(client, auth, data_dir):
    _route(client, auth, "dm-ed", fixtures.HANDLE_REPL, "MK_REPLKNOWN")
    r = client.post("/v1/approve", headers=auth, json={
        "queue_id": "q001", "decision": "edit",
        "edited_text": fixtures.WORDCAP_DRAFT, "reason": "my own words",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved_with_edits"
    assert any("word cap" in w for w in body["warnings"])  # warnings surfaced, not blocking
    out = (data_dir / "outbox" / "reply_dm-ed.txt").read_text(encoding="utf-8")
    assert "EDITED by Sofia (reason: my own words)" in out


# ---------------------------------------------------- kill switch via /v1/config

def test_kill_switch_via_config(client, auth, data_dir):
    c = client.post("/v1/config", headers=auth,
                    json={"auto_send_enabled": False, "reason": "eval gate FAIL"})
    assert c.status_code == 200
    assert c.json()["auto_send_enabled"] is False

    d = _route(client, auth, "dm-ks", fixtures.HANDLE_CARE, "MK_CAREOK").json()
    assert d["action"] == "queue"
    assert d["killswitch_applied"] is True
    assert d["reply_to_send"] is None
    assert "suspended" in d["queued"]["notes"].lower()
    assert (data_dir / "held" / "reply_dm-ks.txt").exists()

    # re-enabling is a deliberate manual action
    c2 = client.post("/v1/config", headers=auth, json={"auto_send_enabled": True})
    assert c2.json()["auto_send_enabled"] is True
    d2 = _route(client, auth, "dm-ok", fixtures.HANDLE_CARE, "MK_CAREOK").json()
    assert d2["action"] == "auto_send"


def test_get_config_reflects_flag(client, auth):
    assert client.get("/v1/config", headers=auth).json()["auto_send_enabled"] is True
    client.post("/v1/config", headers=auth, json={"auto_send_enabled": False})
    assert client.get("/v1/config", headers=auth).json()["auto_send_enabled"] is False


# --------------------------------------------------------------- /v1/queue

def test_queue_urgent_first_and_counts(client, auth):
    _route(client, auth, "dm-pause", fixtures.HANDLE_PAUSE, "MK_PAUSEKNOWN")  # normal
    _route(client, auth, "dm-pet", fixtures.HANDLE_PET, "MK_PET")            # high
    body = client.get("/v1/queue", headers=auth).json()
    assert body["items"][0]["urgency"] == "high"
    assert body["counts"]["pending"] == 2
    assert body["counts"]["high"] == 1


# ---------------------------------------------------------------- /v1/sent

def test_sent_records(client, auth):
    r = client.post("/v1/sent", headers=auth,
                    json={"message_id": "dm-1", "channel": "instagram", "ok": True})
    assert r.status_code == 200
    assert r.json() == {"recorded": True}


# ----------------------------------------------------------------- /v1/eval

def test_eval_requires_auth(client):
    r = client.post("/v1/eval", json={"no_cache": False})
    assert r.status_code == 401


def test_eval_concurrent_returns_409(client, auth):
    from service import app as app_module
    app_module.EVAL_LOCK.acquire()  # simulate a run in flight; run_eval never executes
    try:
        r = client.post("/v1/eval", headers=auth, json={"no_cache": False})
        assert r.status_code == 409
        assert r.json()["error"] == "eval_already_running"
    finally:
        app_module.EVAL_LOCK.release()


def test_eval_success_shape(client, auth, monkeypatch):
    from service import evalrunner
    canned = {
        "gate": "PASS", "exit_code": 0,
        "scores": {"intent": [26, 27], "plant": [16, 16], "lane": [25, 27],
                   "pet_recall": [5, 5], "draft_fixtures": [12, 12]},
        "dangerous": {"UNSAFE_AUTO_SEND": 0, "MISSED_SAFETY": 0, "UNVALIDATED_DRAFT": 0},
        "stdout_tail": "QUALITY GATE: PASS",
    }
    monkeypatch.setattr(evalrunner, "run_eval", lambda no_cache: canned)
    r = client.post("/v1/eval", headers=auth, json={"no_cache": False})
    assert r.status_code == 200
    assert r.json()["gate"] == "PASS"
    assert r.json()["scores"]["lane"] == [25, 27]


# ----------------------------------------------------------------- /healthz

def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    b = r.json()
    assert b["brain_ok"] is True
    assert b["data_dir_writable"] is True
    assert b["auto_send_enabled"] is True


# ------------------------------------------------------ audit log is appended

def test_audit_log_grows(client, auth, data_dir):
    _route(client, auth, "dm-a1", fixtures.HANDLE_BILL, "MK_BILL")
    client.post("/v1/approve", headers=auth,
                json={"queue_id": "q001", "decision": "reject", "reason": "spam"})
    lines = (data_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(ln)["event"] for ln in lines]
    assert "route" in events and "reject" in events
