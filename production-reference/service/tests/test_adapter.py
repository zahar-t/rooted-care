"""Adapter contract tests — the brain's decision mapping, gate, kill switch,
and idempotency.

Every test mocks call_claude via the fake_llm fixture: zero live LLM calls.
"""

import pytest

from service import adapter, settings
from service.tests import fixtures

# marker -> (handle, action, lane, reply_kind, has_draft)
MAPPING = [
    ("MK_CAREOK",     fixtures.HANDLE_CARE, "auto_send",     "AUTO_SEND_CARE",    "care_reply",  None),
    ("MK_CAREFAIL",   fixtures.HANDLE_CARE, "ack_and_queue", "NEEDS_SOFIA",        "holding_ack", True),
    ("MK_CAREWORDCAP",fixtures.HANDLE_CARE, "ack_and_queue", "NEEDS_SOFIA",        "holding_ack", True),
    ("MK_CARENOGUIDE","leafy.lou",          "ack_and_queue", "NEEDS_SOFIA",        "holding_ack", False),
    ("MK_PET",        fixtures.HANDLE_PET,  "queue",         "URGENT_PET_SAFETY", None,          True),
    ("MK_BILL",       fixtures.HANDLE_BILL, "queue",         "BILLING_DISPUTE",   None,          False),
    ("MK_PAUSEKNOWN", fixtures.HANDLE_PAUSE,"queue",         "PAUSE_OR_CANCEL",   None,          True),
    ("MK_PAUSEUNK",   fixtures.HANDLE_UNKNOWN,"queue",       "PAUSE_OR_CANCEL",   None,          False),
    ("MK_REPLKNOWN",  fixtures.HANDLE_REPL, "queue",         "REPLACEMENT",       None,          True),
    ("MK_REPLUNK",    fixtures.HANDLE_UNKNOWN,"queue",       "REPLACEMENT",       None,          False),
    ("MK_OTHER",      fixtures.HANDLE_OTHER,"queue",         "OTHER",             None,          False),
    ("MK_UPSETMONEY", fixtures.HANDLE_BILL, "queue",         "BILLING_DISPUTE",   None,          False),
]


@pytest.mark.parametrize("marker,handle,action,lane,reply_kind,has_draft", MAPPING,
                         ids=[m[0] for m in MAPPING])
def test_mapping_table(data_dir, fake_llm, marker, handle, action, lane, reply_kind, has_draft):
    d = adapter.build_decision(f"m-{marker}", handle, fixtures.text_for(marker))

    assert d["action"] == action
    assert d["lane"] == lane
    assert d["reply_kind"] == reply_kind
    assert d["killswitch_applied"] is False

    if action == "auto_send":
        assert d["queued"] is None
        assert d["reply_to_send"] == fixtures.GOOD_DRAFT
    else:
        assert d["queued"] is not None
        assert d["queued"]["type"] == lane
        assert d["queued"]["urgency"] in ("high", "normal")
        assert d["queued"]["has_draft"] is has_draft
        assert d["queued"]["has_draft"] == bool(d["queued"]["draft_reply"])

    if reply_kind == "holding_ack":
        assert d["action"] == "ack_and_queue"
        assert "passing it straight to Sofia" in d["reply_to_send"]
    if action == "queue":
        assert d["reply_to_send"] is None


def test_pet_safety_is_high_urgency(data_dir, fake_llm):
    d = adapter.build_decision("m-pet", fixtures.HANDLE_PET, fixtures.text_for("MK_PET"))
    assert d["queued"]["urgency"] == "high"


def test_replacement_notes_carry_eligibility_json(data_dir, fake_llm):
    d = adapter.build_decision("m-repl", fixtures.HANDLE_REPL, fixtures.text_for("MK_REPLKNOWN"))
    notes = d["queued"]["notes"]
    assert "Eligibility:" in notes
    # dan_greenthumb's calathea shipped 2026-06-26; fixture clock is 2026-07-01 -> in window
    assert "APPROVE replacement" in notes


def test_gate_fail_demotes_to_ack_and_queue(data_dir, fake_llm):
    d = adapter.build_decision("m-gf", fixtures.HANDLE_CARE, fixtures.text_for("MK_CAREFAIL"))
    assert d["action"] == "ack_and_queue"
    assert d["queued"]["type"] == "NEEDS_SOFIA"
    assert "FAILED the auto-send gate" in d["queued"]["notes"]
    assert d["reply_kind"] == "holding_ack"
    # the ack carries the subscriber's real first name (Sofia), not a placeholder
    assert fixtures.first_name(fixtures.HANDLE_CARE) in d["reply_to_send"]
    # the failed draft is kept so Sofia can salvage it
    assert d["queued"]["has_draft"] is True


def test_wordcap_fail_also_demotes(data_dir, fake_llm):
    d = adapter.build_decision("m-wc", fixtures.HANDLE_CARE, fixtures.text_for("MK_CAREWORDCAP"))
    assert d["action"] == "ack_and_queue"
    assert "word cap" in d["queued"]["notes"]


# --------------------------------------------------------------- kill switch

def _disable_autosend(data_dir):
    (data_dir / "flags" / "autosend_disabled").write_text("", encoding="utf-8")


def test_kill_switch_demotes_auto_send(data_dir, fake_llm):
    _disable_autosend(data_dir)
    d = adapter.build_decision("m-ks", fixtures.HANDLE_CARE, fixtures.text_for("MK_CAREOK"))

    assert d["action"] == "queue"
    assert d["killswitch_applied"] is True
    assert d["reply_to_send"] is None
    assert d["queued"]["type"] == "NEEDS_SOFIA"
    assert "suspended" in d["queued"]["notes"].lower()
    # the gate-passed draft is preserved for human review
    assert d["queued"]["has_draft"] is True
    # the model-written reply moved out of the send path into held/
    assert (data_dir / "held" / "reply_m-ks.txt").exists()
    assert not (data_dir / "outbox" / "reply_m-ks.txt").exists()
    # idempotency still holds via the queue item
    assert adapter.already_handled("m-ks") is not None


def test_kill_switch_off_allows_auto_send(data_dir, fake_llm):
    d = adapter.build_decision("m-on", fixtures.HANDLE_CARE, fixtures.text_for("MK_CAREOK"))
    assert d["action"] == "auto_send"
    assert d["killswitch_applied"] is False
    assert d["reply_to_send"] == fixtures.GOOD_DRAFT


def test_kill_switch_never_promotes(data_dir, fake_llm):
    """The flag may demote autonomy, never widen it: queued/ack lanes are untouched."""
    _disable_autosend(data_dir)
    # a queued lane stays queued, not touched by the switch
    d1 = adapter.build_decision("m-b", fixtures.HANDLE_BILL, fixtures.text_for("MK_BILL"))
    assert d1["action"] == "queue"
    assert d1["killswitch_applied"] is False
    # ack_and_queue: the deterministic template ack still sends; still not auto_send
    d2 = adapter.build_decision("m-ng", "leafy.lou", fixtures.text_for("MK_CARENOGUIDE"))
    assert d2["action"] == "ack_and_queue"
    assert d2["reply_kind"] == "holding_ack"
    assert d2["killswitch_applied"] is False


# --------------------------------------------------------------- idempotency

def test_already_handled_tracks_queue_and_outbox(data_dir, fake_llm):
    assert adapter.already_handled("m-q") is None
    adapter.build_decision("m-q", fixtures.HANDLE_BILL, fixtures.text_for("MK_BILL"))
    assert adapter.already_handled("m-q") == "already in the approval queue"

    adapter.build_decision("m-a", fixtures.HANDLE_CARE, fixtures.text_for("MK_CAREOK"))
    assert adapter.already_handled("m-a") == "reply already in outbox"


def test_queue_ids_increment_never_collide(data_dir, fake_llm):
    adapter.build_decision("m-1", fixtures.HANDLE_BILL, fixtures.text_for("MK_BILL"))
    adapter.build_decision("m-2", fixtures.HANDLE_PAUSE, fixtures.text_for("MK_PAUSEKNOWN"))
    ids = [i["id"] for i in adapter.load_queue()]
    assert ids == ["q001", "q002"]


# ------------------------------------------------------------- brain locator

def test_brain_ok(data_dir):
    assert adapter.brain_ok() is True
    adapter.assert_brain()  # must not raise


# ---------------------------------------------------------------------------
# RETIRED AT CONSOLIDATION — a deliberate decision, not a silently dropped test.
#
# Two tests lived here:
#     test_vendored_clone_stays_git_clean  — asserted the service never dirties
#                                            the vendored copy of the brain
#     test_frozen_original_untouched       — asserted the original working-slice repo
#                                            is never written to
#
# They enforced the frozen-vendor boundary: when this automation layer was a
# SEPARATE repo, the brain was a reviewed artifact, vendored at a pinned SHA,
# that the wrapper was forbidden to touch. Folding both into ONE repository (the
# brain now lives at ../../slice, beside this reference layer) dissolves that
# boundary on purpose. These two therefore asserted an invariant we chose to
# retire, and they failed only because the deliberate consolidation rename made
# the brain git-dirty — not because of any regression. Left here as a visible
# record of the decision rather than deleted outright.
#
# (The pinned-SHA guard itself was later retired with the rest of the vendoring
# machinery: the service now imports the brain from ../../slice directly, so
# service and brain are the same commit by construction and there is no pin to
# drift or orphan. test_brain_ok above covers the one failure mode left — a
# mis-set ROOTED_SLICE_DIR.)
# ---------------------------------------------------------------------------
