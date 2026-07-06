"""Canned triage records + drafts for the mocked LLM. Zero live calls.

The fake ``call_claude`` (conftest.fake_llm) dispatches on the marker embedded in
each test message body:
  * triage system prompt  -> return json.dumps(TRIAGE_BY_MARKER[marker])
  * draft system prompt    -> return DRAFT_BY_MARKER[marker], else GOOD_DRAFT

Markers are deliberately non-overlapping (no marker is a substring of another) so
first-match dispatch is unambiguous. Each triage record is built to satisfy the
brain's ``decide_lane`` for exactly one lane; handles/shipments are real values
read from the repo's ``slice/subscribers.json`` (never invented).
"""

import json

from .. import settings

# Real CRM values — the frozen brain looks subscribers up by these handles.
SUBSCRIBERS = json.loads((settings.SLICE_DIR / "subscribers.json").read_text(encoding="utf-8"))

HANDLE_CARE = "sofia.grows"      # Sofia — Grower, shipped a monstera 2026-03-24
HANDLE_PET = "kat.and.cats"      # Kat   — Sprout, latest shipment pothos 2026-06-26
HANDLE_BILL = "marcus.w"         # Marcus— Grower (£29/mo)
HANDLE_PAUSE = "priya.plants"    # Priya — Grower
HANDLE_REPL = "dan_greenthumb"   # Dan   — Jungle, shipped a calathea 2026-06-26
HANDLE_OTHER = "sofia.grows"     # any known sender
HANDLE_UNKNOWN = "nobody.here"   # deliberately absent from subscribers.json


def _t(**over) -> dict:
    """A triage record matching EXTRACT_SYSTEM's schema, with sensible defaults."""
    base = {
        "intent": "other",
        "plant": "unknown",
        "plant_is_from_us": None,
        "symptoms": None,
        "pet_safety": False,
        "photo_included": False,
        "sentiment": "calm",
        "urgency": "normal",
        "summary": "A customer message.",
    }
    base.update(over)
    return base


TRIAGE_BY_MARKER: dict[str, dict] = {
    # care question, named plant, calm, asks "?" -> AUTO_SEND_CARE (gate decides)
    "MK_CAREOK": _t(intent="care_question", plant="monstera", plant_is_from_us=True,
                    symptoms="a couple of yellow lower leaves", sentiment="calm",
                    summary="Care question about a monstera with yellow lower leaves."),
    # same lane, but the draft trips the gate (meta-text) -> ack_and_queue
    "MK_CAREFAIL": _t(intent="care_question", plant="monstera", plant_is_from_us=True,
                      symptoms="yellow leaves", sentiment="calm",
                      summary="Care question about a monstera; the draft fails the gate."),
    # same lane, but the draft breaches the word cap -> ack_and_queue
    "MK_CAREWORDCAP": _t(intent="care_question", plant="monstera", plant_is_from_us=True,
                         symptoms="yellow leaves", sentiment="calm",
                         summary="Care question about a monstera; the draft is too long."),
    # care question, plant not in the KB (fiddle leaf fig) -> NEEDS_SOFIA
    "MK_CARENOGUIDE": _t(intent="care_question", plant="other", plant_is_from_us=False,
                         symptoms="dropping leaves after moving flat", sentiment="calm",
                         summary="Care question about a fiddle leaf fig not in the KB."),
    # pet-safety mention -> URGENT_PET_SAFETY (checked before everything)
    "MK_PET": _t(intent="care_question", plant="unknown", plant_is_from_us=True,
                 symptoms="cat chewed the trailing plant, now drooling", pet_safety=True,
                 sentiment="worried", urgency="high",
                 summary="Customer's cat chewed the trailing plant and is drooling."),
    # billing intent -> BILLING_DISPUTE (no draft)
    "MK_BILL": _t(intent="billing", plant="unknown", sentiment="upset", urgency="high",
                  summary="Customer charged twice this month and wants the extra refunded."),
    # pause/cancel, known sender -> PAUSE_OR_CANCEL with a retention draft
    "MK_PAUSEKNOWN": _t(intent="pause_or_cancel", plant="unknown", sentiment="calm",
                        summary="Customer wants to pause or cancel due to budget."),
    # pause/cancel, unknown sender -> PAUSE_OR_CANCEL, empty draft
    "MK_PAUSEUNK": _t(intent="pause_or_cancel", plant="unknown", sentiment="calm",
                      summary="Unknown sender wants to cancel their subscription."),
    # replacement, known sender, photo attached -> REPLACEMENT with eligibility JSON
    "MK_REPLKNOWN": _t(intent="replacement_request", plant="calathea", plant_is_from_us=True,
                       symptoms="about half the leaves brown and crispy", photo_included=True,
                       summary="Customer wants a replacement calathea; photo attached."),
    # replacement, unknown sender -> REPLACEMENT, empty draft
    "MK_REPLUNK": _t(intent="replacement_request", plant="calathea", plant_is_from_us=True,
                     symptoms="crispy leaves", photo_included=True,
                     summary="Unknown sender wants a replacement."),
    # anything else -> OTHER
    "MK_OTHER": _t(intent="shipping_issue", plant="unknown", sentiment="calm",
                   summary="Customer asks where this month's box is."),
    # not a billing intent, but upset AND a £ figure in the body -> BILLING_DISPUTE
    "MK_UPSETMONEY": _t(intent="shipping_issue", plant="unknown", sentiment="upset",
                        urgency="high",
                        summary="Upset customer references a £ amount; billing via upset+£."),
}


# Canonical message bodies. Each embeds its marker and the punctuation the frozen
# decide_lane inspects ("?" for a care question, "£" for the upset-money path).
TEXTS: dict[str, str] = {
    "MK_CAREOK": "hey MK_CAREOK the monstera from my march box has a few yellow lower leaves — am i overwatering it?",
    "MK_CAREFAIL": "hey MK_CAREFAIL my monstera has some yellow leaves, any idea what is going on?",
    "MK_CAREWORDCAP": "hi MK_CAREWORDCAP quick monstera question — why the yellow leaves?",
    "MK_CARENOGUIDE": "hiya MK_CARENOGUIDE my fiddle leaf fig keeps dropping leaves since i moved, any ideas?",
    "MK_PET": "HELP MK_PET my cat chewed the trailing plant from this month's box and is drooling, is it poisonous??",
    "MK_BILL": "MK_BILL you have charged me £58 instead of £29 this month, please refund the extra.",
    "MK_PAUSEKNOWN": "hey MK_PAUSEKNOWN money is tight so i think i need to pause or cancel, how does that work?",
    "MK_PAUSEUNK": "MK_PAUSEUNK i want to cancel my subscription, how do i do that?",
    "MK_REPLKNOWN": "hi MK_REPLKNOWN the calathea from june's box arrived half crispy, photo attached, any chance of a replacement?",
    "MK_REPLUNK": "MK_REPLUNK my calathea came in crispy, can i get a replacement? photo attached",
    "MK_OTHER": "MK_OTHER hey, where is this month's box? it has not arrived yet",
    "MK_UPSETMONEY": "MK_UPSETMONEY honestly furious — £58 charged and no box arrived, please sort it out",
}


# A clean care reply: under the word cap, exactly one emoji (the sign-off 🌿), no
# meta-text, no money language. Passes the frozen validate_draft.
GOOD_DRAFT = (
    "Hey! A couple of yellow lower leaves on an otherwise happy monstera is "
    "usually nothing to worry about — often it is just the plant shedding its "
    "oldest leaves. One thing to try: let the top few centimetres of soil dry "
    "out fully before the next drink, then water thoroughly. Want me to talk you "
    "through a summer watering rhythm? Just say the word.\n\nSofia 🌿"
)

# Trips the gate on meta-text markers ("here's your draft", "draft reply").
GATEFAIL_DRAFT = (
    "Here's your draft reply:\n\nWater your monstera weekly and keep it in bright "
    "indirect light.\n\nSofia 🌿"
)

# Trips the gate on the word cap only (~158 words), one emoji, no money language.
WORDCAP_DRAFT = (
    "Give it bright indirect light and let the soil dry between waters. " * 13
).strip() + "\n\nSofia 🌿"

DRAFT_BY_MARKER: dict[str, str] = {
    "MK_CAREFAIL": GATEFAIL_DRAFT,
    "MK_CAREWORDCAP": WORDCAP_DRAFT,
}


def text_for(marker: str) -> str:
    return TEXTS[marker]


def first_name(handle: str) -> str:
    return SUBSCRIBERS[handle]["name"]
