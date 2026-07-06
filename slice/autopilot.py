"""Rooted inbox autopilot — the working slice.

Pipeline per message:
  1. Claude extracts a structured triage record from the raw DM.
  2. Deterministic code does everything involving facts, money and policy:
     subscriber lookup, which plant we actually shipped them, replacement
     eligibility, tier maths, routing.
  3. Claude drafts the reply in Sofia's voice, grounded ONLY in Sofia's own
     care guides. Routine care answers auto-send; anything touching money,
     safety or retention lands in the approval queue for Sofia.

Commands:
  python3 autopilot.py inbox                # process everything in inbox/
  python3 autopilot.py review               # interactive approval queue
  python3 autopilot.py review --approve-all # non-interactive (testing)
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

from llm import call_claude, extract_json

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).parent
POLICY = json.loads((BASE / "policy.json").read_text(encoding="utf-8"))
SUBSCRIBERS = json.loads((BASE / "subscribers.json").read_text(encoding="utf-8"))
TODAY = dt.date.fromisoformat(POLICY["today"])
QUEUE_FILE = BASE / "queue.json"
OUTBOX = BASE / "outbox"

KNOWN_PLANTS = ["monstera", "pothos", "calathea", "snake_plant"]

# ---------------------------------------------------------------- extraction

EXTRACT_SYSTEM = f"""You are the intake triage step for Rooted, a houseplant
subscription box. Read one customer DM and return ONLY a JSON object — no
prose, no code fences — with exactly these fields:

{{
  "intent": "care_question" | "replacement_request" | "pause_or_cancel" | "billing" | "shipping_issue" | "other",
  "plant": "monstera" | "pothos" | "calathea" | "snake_plant" | "other" | "unknown",
  "plant_is_from_us": true | false | null,   // did they say it came from a Rooted box? null if unclear
  "symptoms": "<short description>" | null,
  "pet_safety": true | false,                // any mention of a pet/child interacting with a plant
  "photo_included": true | false,
  "sentiment": "calm" | "worried" | "upset",
  "urgency": "low" | "normal" | "high",
  "summary": "<one factual sentence>"
}}

Rules:
- "the trailing plant" / "the plant from this month's box" without a name -> plant: "unknown", plant_is_from_us: true.
- A named plant we don't stock (e.g. fiddle leaf fig) -> plant: "other".
- If they ask for a replacement AND describe symptoms, intent is "replacement_request".
- Classify only. Do not answer the customer. Do not add fields."""


def triage(message: str) -> dict:
    return extract_json(call_claude(EXTRACT_SYSTEM, message))


# ------------------------------------------------------- deterministic logic

def resolve_plant(t: dict, sub: dict | None) -> tuple[str | None, str]:
    """Return (plant_key, how_we_know). Code, not the model, owns this fact."""
    if t["plant"] in KNOWN_PLANTS:
        return t["plant"], "named by customer"
    if t["plant"] == "unknown" and t.get("plant_is_from_us") and sub:
        latest = max(sub["shipments"], key=lambda s: s["date"])
        return latest["plant"], f"latest shipment ({latest['date']})"
    return None, "not resolvable"


def replacement_eligibility(sub: dict, plant: str | None) -> dict:
    pol = POLICY["replacement_policy"]
    shipped = None
    if plant:
        matches = [s for s in sub["shipments"] if s["plant"] == plant]
        if matches:
            shipped = max(m["date"] for m in matches)
    days = (TODAY - dt.date.fromisoformat(shipped)).days if shipped else None
    in_window = days is not None and days <= pol["window_days"]
    under_cap = sub["replacements_this_year"] < pol["max_free_per_year"]
    eligible = in_window and under_cap
    return {
        "shipped_date": shipped,
        "days_since_shipping": days,
        "in_window": in_window,
        "under_yearly_cap": under_cap,
        "recommendation": "APPROVE replacement" if eligible else "DECLINE per policy (offer care help instead)",
    }


def cheaper_tiers(sub: dict) -> list[str]:
    current = POLICY["tiers"][sub["tier"]]
    return [
        f"downgrade to {name} (£{price}/mo, saves £{current - price}/mo)"
        for name, price in POLICY["tiers"].items()
        if price < current
    ]


def load_guide(plant: str | None) -> str | None:
    if not plant:
        return None
    path = BASE / "careguides" / f"{plant}.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


# ----------------------------------------------------------------- drafting

DRAFT_SYSTEM = f"""You draft Instagram DM replies for Sofia, founder of Rooted
(houseplant subscription box). Voice: {POLICY['voice']['style']}
Sign off: {POLICY['voice']['sign_off']}

Hard rules:
- {chr(10) + '- '.join(POLICY['voice']['hard_rules'])}

Return ONLY the reply text. No preamble, no quotes, no JSON."""


def draft(instructions: str) -> str:
    return call_claude(DRAFT_SYSTEM, instructions).strip()


HOLDING_ACK = (
    "Hey {name}! Great question — this one deserves a proper answer, so I'm "
    "passing it straight to Sofia to look at personally. She'll get back to you "
    "within a day. 🌿"
)


# ----------------------------------------------- draft gate (deterministic)
#
# The model drafts; this gate decides whether a draft may leave unreviewed.
# A probabilistic step is never the last gate on an irreversible action —
# every check below is plain string logic, so the eval exercises it for free
# and any failure is reproducible. A failing draft is never dropped: it is
# demoted to Sofia's queue with the reasons attached, so she can salvage it.

# Deliberately compound phrases. Bare "draft" is excluded because care advice
# legitimately says "keep it away from cold drafts"; bare "let me" is excluded
# because "let me know how it goes" is Sofia's natural voice.
META_MARKERS = [
    # preambles announcing the reply instead of being it
    "here's your draft", "here's the draft", "here is the draft",
    "here's your reply", "here's the reply", "here is the reply", "here's a reply",
    "draft reply", "reply text", "clean reply", "final reply", "revised reply",
    "let me give you", "let me rewrite", "let me try again", "let me revise",
    # self-correction / rule-quoting leakage
    "wait —", "wait --", "one emoji max", "emoji max", "the sign-off",
    "word limit", "word cap", "110 words",
    "as an ai", "system prompt", "i'll drop this",
]
SEPARATOR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", re.MULTILINE)  # a DM never needs a horizontal rule

# Money-promise language. Over-flagging is the safe direction: a false hit
# costs Sofia a glance; a miss costs money she never agreed to spend.
MONEY_RES = [
    ("refund",       re.compile(r"\brefund\w*", re.I)),
    ("free",         re.compile(r"\bfree\b", re.I)),        # "feel free" stripped first
    ("credit",       re.compile(r"\bcredit\w*", re.I)),
    ("discount",     re.compile(r"\bdiscount\w*", re.I)),
    ("charge",       re.compile(r"\bcharg(?:e[ds]?|ing)\b", re.I)),
    ("£ amount",     re.compile(r"£\s*\d")),
    ("replacement",  re.compile(r"\breplace\w*", re.I)),    # replacements are money per policy
    ("voucher",      re.compile(r"\bvoucher\b", re.I)),
    ("money back",   re.compile(r"\bmoney back\b", re.I)),
    ("reimburse",    re.compile(r"\breimburse\w*", re.I)),
    ("waive",        re.compile(r"\bwaiv(?:e[ds]?|ing)\b", re.I)),
    ("complimentary",re.compile(r"\bcomplimentary\b|\bon the house\b", re.I)),
]

# One-emoji rule. U+FE0F (variation selector) deliberately excluded — it would
# double-count sequences like ☘️.
EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "\U0001F300-\U0001F5FF"   # symbols & pictographs (🌿 lives here)
    "\U0001F600-\U0001F64F"   # emoticons
    "\U0001F680-\U0001F6FF"   # transport
    "\U0001F780-\U0001F7FF"   # geometric shapes extended (🟢 🟧 etc.)
    "\U0001F900-\U0001FAFF"   # supplemental
    "☀-➿"           # misc symbols + dingbats
    "⬀-⯿"
    "]"
)


def validate_draft(reply: str, policy: dict = POLICY) -> list[str]:
    """Deterministic post-draft gate for the AUTO-SEND lane.

    Returns a list of failure reasons; empty list == safe to auto-send.
    Pure function of (text, policy) so the eval scores it without a model call.
    """
    problems = []
    low = reply.lower()

    # 1. Meta-text: the model talking ABOUT the reply instead of being it.
    for marker in META_MARKERS:
        if marker in low:
            problems.append(f"meta-text marker: {marker!r}")
    if SEPARATOR_RE.search(reply):
        problems.append("separator line ('---') — model framing, not a DM")

    # 2. Word cap (policy.json voice rule).
    cap = policy["voice"]["max_words"]
    words = len(reply.split())
    if words > cap:
        problems.append(f"word cap: {words} > {cap}")

    # 3. One emoji max (policy.json voice rule).
    n = len(EMOJI_RE.findall(reply))
    if n > 1:
        problems.append(f"emoji count: {n} > 1 (voice rule: one emoji max)")

    # 4. Money-promise language — money is a human decision, always.
    # "feel free to send a photo" is Sofia's voice, not a money promise.
    text_for_money = re.sub(r"\bfeel free\b", "", reply, flags=re.I)
    for label, rx in MONEY_RES:
        if rx.search(text_for_money):
            problems.append(f"money-promise language: {label!r} — money is a human decision")

    return problems


# ------------------------------------------------------------------ routing

# Routing lanes — the single source of truth. `decide_lane` is a pure function
# of the triage record (no LLM, no side effects), so the eval harness exercises
# the exact routing logic that production uses, not a hand-copied mirror of it.
LANE_PET_SAFETY = "URGENT_PET_SAFETY"
LANE_BILLING = "BILLING_DISPUTE"
LANE_RETENTION = "PAUSE_OR_CANCEL"
LANE_REPLACEMENT = "REPLACEMENT"
LANE_CARE_AUTO = "AUTO_SEND_CARE"
LANE_NEEDS_SOFIA = "NEEDS_SOFIA"
LANE_OTHER = "OTHER"


# ------------------------------------------------- cross-lingual safety gate
#
# Rooted ships internationally, so DMs arrive in languages the care guides are
# NOT written in. This is a DEEPENING of the escalation gate, not a translation
# feature: a mistranslated "toxic to cats" instruction is catastrophic and
# irreversible, so the bar for letting a model-drafted reply leave unreviewed is
# *higher* in another language, not the same.
#
# Both checks are deterministic string logic (no model call), so the eval
# exercises them for free — the same discipline as the draft gate. detect_language
# is intentionally a tiny heuristic; production swaps in a real detector
# (fastText / CLD3). It only has to answer the one question the gate cares about:
# "is this clearly NOT English?" — and it errs toward "en", so a plain English DM
# is never mis-gated.

LANGUAGE_NAMES = {"en": "English", "pt": "Portuguese", "es": "Spanish"}

# Function words / spellings that rarely appear in Rooted's English DMs. A
# diacritic is the strongest single signal; the stopword vote disambiguates pt/es.
_PT_MARKERS = {"não", "está", "estão", "minha", "meu", "folhas", "gato", "cachorro",
               "cão", "planta", "socorro", "comeu", "mordeu", "engoliu", "tóxica",
               "tóxico", "veneno", "veterinário", "amarelas", "regando", "rego",
               "demais", "preocupada", "ajuda", "ajudaria", "uma", "oi"}
_ES_MARKERS = {"está", "hojas", "gato", "perro", "planta", "ayuda", "comió", "mordió",
               "tóxica", "tóxico", "veneno", "veterinario", "amarillas", "riego",
               "demasiado", "hola", "una", "mordisqueó", "mordisquea"}
_DIACRITICS = "ãõáâàéêíóôúñ¿¡ç"


def detect_language(text: str) -> str:
    """Deterministic, deliberately tiny. Returns 'en' unless there is clear
    non-English signal (Iberian diacritics, or >=2 non-English function words)."""
    low = text.lower()
    toks = set(re.findall(r"[a-zà-ÿ]+", low))
    pt = len(toks & _PT_MARKERS)
    es = len(toks & _ES_MARKERS)
    if any(ch in low for ch in _DIACRITICS) or pt >= 2 or es >= 2:
        return "pt" if pt >= es else "es"
    return "en"


# Toxicity / ingestion / vet terms in the languages we accept. Only consulted for
# non-English DMs (English safety is already caught by triage's pet_safety flag),
# so this is defense in depth against the classifier under-reading risk in a
# language it sees less often. Over-flagging costs a human glance; a miss costs an
# animal — the same asymmetry the whole gate is built on.
_SAFETY_TERMS = (
    "tóxic", "veneno", "envenen", "veterinár", "comeu", "mordeu", "engoliu",   # pt
    "veterinari", "comió", "mordió", "mordisqu", "tragó", "tóxico", "tóxica",  # es
    "toxic", "poison", "swallow", "chewed", "ingest",                          # en (belt & braces)
)


def mentions_safety(text: str) -> bool:
    low = text.lower()
    return any(term in low for term in _SAFETY_TERMS)


def decide_lane(t: dict, body: str, guide_available: bool) -> str:
    """Which lane a message routes to. Order matters: safety and money are
    checked before care, so a pet-safety care question can never auto-send."""
    lang = detect_language(body)
    # Cross-lingual safety net (deterministic, defense in depth): a non-English DM
    # that trips a toxicity/ingestion/vet term goes straight to a human, marked
    # urgent, EVEN IF triage under-read the risk in a language it sees less often.
    if lang != "en" and mentions_safety(body):
        return LANE_PET_SAFETY
    if t["pet_safety"]:
        return LANE_PET_SAFETY
    if t["intent"] == "billing" or (t["sentiment"] == "upset" and "£" in body):
        return LANE_BILLING
    if t["intent"] == "pause_or_cancel":
        return LANE_RETENTION
    if t["intent"] == "replacement_request":
        return LANE_REPLACEMENT
    if t["intent"] == "care_question":
        # Auto-send is the narrowest, most conservative lane. It fires only when
        # ALL hold: grounded in a guide, customer not upset, and the message
        # actually asks something ("?"). The last guard is deterministic on
        # purpose — the classifier sometimes reads praise/thanks as a care
        # question, and we will not auto-send an unprompted care essay at
        # someone who just said thank you. No question -> a human looks first.
        asks_something = "?" in body
        # Cross-lingual raises the bar: we auto-send care ONLY in English, where the
        # advice needs no translation we can't verify and the draft gate's rules are
        # calibrated. A non-English care question gets the instant holding ack and a
        # human answers it (localising the ack + drafts is a written-down next step).
        auto = guide_available and t["sentiment"] != "upset" and asks_something and lang == "en"
        return LANE_CARE_AUTO if auto else LANE_NEEDS_SOFIA
    return LANE_OTHER


def route(msg_file: Path) -> list[dict]:
    """Process one message; return a list of actions taken (for the transcript)."""
    raw = msg_file.read_text(encoding="utf-8").strip()
    handle = raw.splitlines()[0].replace("From:", "").strip().lstrip("@")
    body = "\n".join(raw.splitlines()[1:]).strip()
    sub = SUBSCRIBERS.get(handle)
    sub_desc = f"{sub['name']} ({sub['tier']}, since {sub['since']})" if sub else "unknown sender"

    t = triage(body)
    plant, plant_source = resolve_plant(t, sub)
    guide = load_guide(plant)
    lang = detect_language(body)
    lane = decide_lane(t, body, guide is not None)
    actions = []

    def context_block() -> str:
        # Cross-lingual drafting: reply in the customer's language, but keep every
        # plant fact bound to the English care guide — the model may translate
        # phrasing, never a fact the guide doesn't state. (English DMs are unaffected.)
        lang_note = "" if lang == "en" else (
            f"\nThe customer wrote in {LANGUAGE_NAMES.get(lang, lang)}. Reply in "
            f"{LANGUAGE_NAMES.get(lang, lang)}, but take every plant fact ONLY from the "
            "English care guide provided — never translate a fact the guide does not state.\n"
        )
        return (
            f"Customer: {sub_desc}, handle @{handle}.\n"
            f"Their message:\n{body}\n\n"
            f"Triage: {json.dumps(t)}\n"
            f"Plant resolved by our records: {plant or 'none'} ({plant_source}).\n"
            f"{lang_note}"
        )

    # 1. Pet/child safety first — agent drafts with guide facts, human sends.
    if lane == LANE_PET_SAFETY:
        guide_block = f"\nRelevant care guide:\n{guide}\n\n" if guide else (
            "\nWe could NOT resolve which plant this is, so no care guide is available. "
            "Do NOT state any toxicity facts. Tell them to stop the pet chewing it, keep "
            "the plant, and ring their vet now; Sofia will identify the plant and follow up.\n\n"
        )
        reply = draft(
            context_block()
            + guide_block
            + "Draft an URGENT but calm reply. Use ONLY the guide's pet section for "
            + "toxicity facts. Tell them exactly what to do right now, including "
            + "when to ring the vet. Do not diagnose the animal."
        )
        shipped_note = (f"We shipped this customer a {plant} on {plant_source}."
                        if plant else
                        "Could not resolve which plant this is — no subscriber/shipment match.")
        actions.append(queue_item(msg_file, handle, "URGENT_PET_SAFETY", "high", t["summary"],
                                  f"{shipped_note} Human must review and send NOW.",
                                  reply))
        return actions

    # 2. Money: billing disputes are human-written, no draft.
    if lane == LANE_BILLING:
        note = f"Tier is {sub['tier']} (£{POLICY['tiers'][sub['tier']]}/mo). Check the payment provider before replying." if sub else "Unknown sender — verify identity."
        actions.append(queue_item(msg_file, handle, "BILLING_DISPUTE", "high", t["summary"], note, draft_reply=""))
        return actions

    # 3. Retention: pause/cancel — human conversation, agent preps the facts.
    if lane == LANE_RETENTION:
        if not sub:
            actions.append(queue_item(msg_file, handle, "PAUSE_OR_CANCEL", "normal", t["summary"],
                                      "Unknown sender — verify identity before discussing the account.", draft_reply=""))
            return actions
        options = ["pause for 1–3 months (keeps their spot + streak)"] + cheaper_tiers(sub)
        reply = draft(
            context_block()
            + f"\nRetention options Sofia can offer (computed from policy): {options}.\n"
            + "Draft a warm, zero-pressure reply that thanks them, presents the pause "
            + "and downgrade options with the exact £ savings, and makes cancelling "
            + "easy if that's what they want. This will be reviewed by Sofia before sending."
        )
        actions.append(queue_item(msg_file, handle, "PAUSE_OR_CANCEL", "normal", t["summary"],
                                  f"Options computed: {options}", reply))
        return actions

    # 4. Replacements: policy computes the answer, Sofia approves it.
    if lane == LANE_REPLACEMENT:
        if not sub:
            actions.append(queue_item(msg_file, handle, "REPLACEMENT", "normal", t["summary"],
                                      "Unknown sender — can't verify shipment history. Verify identity before offering a replacement.",
                                      draft_reply=""))
            return actions
        elig = replacement_eligibility(sub, plant)
        if not t["photo_included"] and POLICY["replacement_policy"]["photo_required"]:
            elig["recommendation"] = "ASK for a photo first (policy requires one)"
        extra = ""
        if plant == "calathea" and guide:
            extra = "The guide's transit note may be relevant — mention that some browning after shipping is cosmetic.\n"
        reply = draft(
            context_block()
            + f"\nRelevant care guide:\n{guide}\n\n"
            + f"Replacement eligibility (computed, authoritative): {json.dumps(elig)}\n"
            + extra
            + "Draft a reply consistent with the recommendation. If APPROVE: confirm a "
            + "replacement will ship with their next box or sooner. If ASK: request the photo. "
            + "If DECLINE: empathetic, offer care help. Never contradict the recommendation."
        )
        actions.append(queue_item(msg_file, handle, "REPLACEMENT", "normal", t["summary"],
                                  f"Eligibility: {json.dumps(elig)}", reply))
        return actions

    # 5. Care questions grounded in a guide and low-risk → auto-send, but ONLY
    #    through the deterministic draft gate. The model is a drafting step,
    #    never the last gate on an irreversible send.
    if lane == LANE_CARE_AUTO:
        reply = draft(
            context_block()
            + f"\nSofia's care guide for this plant:\n{guide}\n\n"
            + "Draft the reply. Plant facts ONLY from the guide. One actionable "
            + "step, reassure if the guide says the symptom is normal/common. "
            + f"Keep it under {POLICY['voice']['max_words']} words. The whole "
            + "reply must contain exactly one emoji: the 🌿 in the sign-off. "
            + "No emoji anywhere else — not even in the greeting."
        )
        problems = validate_draft(reply)
        if problems:
            # Demote, never send. The draft is kept so Sofia can salvage it,
            # and the customer gets the instant template ack (deterministic
            # text — the only thing allowed to skip the gate is text no model wrote).
            name = sub["name"] if sub else "there"
            actions.append(send_now(msg_file, handle, HOLDING_ACK.format(name=name),
                                    "holding ack (template, no LLM)"))
            actions.append(queue_item(msg_file, handle, LANE_NEEDS_SOFIA, "normal", t["summary"],
                                      "Draft FAILED the auto-send gate: " + "; ".join(problems)
                                      + ". Held for review — salvage or rewrite.", reply))
            return actions
        actions.append(send_now(msg_file, handle, reply,
                                f"care reply grounded in careguides/{plant}.md — passed draft gate"))
        return actions

    # 6. Care question that must not auto-send (not in the KB, non-English,
    #    upset customer, or no actual question asked) → instant holding ack,
    #    then Sofia answers personally.
    if lane == LANE_NEEDS_SOFIA:
        name = sub["name"] if sub else "there"
        # The note tells Sofia WHY this reached her — the guide flywheel only
        # applies when the guide is actually missing.
        if guide is None:
            note = ("Not covered by a care guide. Flywheel: Sofia's answer becomes the "
                    f"next guide ({t['plant']}), so the agent handles it next time.")
        elif lang != "en":
            note = (f"Guide exists ({plant}), but the DM is in "
                    f"{LANGUAGE_NAMES.get(lang, lang)} — care replies auto-send only in "
                    "English, so a human reviews and sends this one.")
        elif t["sentiment"] == "upset":
            note = (f"Guide exists ({plant}), but the customer is upset — "
                    "we never auto-send at an upset customer.")
        else:
            note = (f"Guide exists ({plant}), but the message doesn't actually ask a "
                    "question — held so we never auto-send an unprompted care essay.")
        actions.append(send_now(msg_file, handle, HOLDING_ACK.format(name=name), "holding ack (template, no LLM)"))
        actions.append(queue_item(msg_file, handle, "NEEDS_SOFIA", "normal", t["summary"], note, draft_reply=""))
        return actions

    # 7. Everything else (shipping issues, general questions) → human triage.
    actions.append(queue_item(msg_file, handle, "OTHER", "normal", t["summary"], "Unrecognised — Sofia triages.", ""))
    return actions


# ------------------------------------------------------------ queue / outbox

def load_queue() -> list[dict]:
    return json.loads(QUEUE_FILE.read_text(encoding="utf-8")) if QUEUE_FILE.exists() else []


def already_handled(msg_file: Path, queue: list[dict]) -> str | None:
    """Reason this message was handled on a previous run, else None.
    Idempotency: re-running `inbox` must not duplicate sends or queue items."""
    if (OUTBOX / f"reply_{msg_file.stem}.txt").exists():
        return "reply already in outbox"
    if any(i["message"] == msg_file.name for i in queue):
        return "already in the approval queue"
    return None


def save_queue(q: list[dict]) -> None:
    QUEUE_FILE.write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding="utf-8")


def queue_item(msg_file, handle, kind, urgency, summary, notes, draft_reply) -> dict:
    q = load_queue()
    item = {
        "id": f"q{len(q) + 1:03d}",
        "message": msg_file.name,
        "handle": handle,
        "type": kind,
        "urgency": urgency,
        "summary": summary,
        "notes": notes,
        "draft_reply": draft_reply,
        "status": "pending",
    }
    q.append(item)
    save_queue(q)
    return {"action": "QUEUED", "detail": f"[{kind}] -> approval queue as {item['id']}", "item": item}


def send_now(msg_file, handle, reply, why) -> dict:
    out = OUTBOX / f"reply_{msg_file.stem}.txt"
    out.write_text(f"To: @{handle}\nStatus: AUTO-SENT ({why})\n\n{reply}\n", encoding="utf-8")
    return {"action": "AUTO-SENT", "detail": f"{why} -> {out.name}"}


# ----------------------------------------------------------------- commands

def cmd_inbox() -> None:
    q = load_queue()
    for f in sorted((BASE / "inbox").glob("*.txt")):
        print(f"\n=== {f.name} " + "=" * (40 - len(f.name)))
        reason = already_handled(f, q)
        if reason:
            print(f"  SKIPPED: {reason} (idempotent re-run — delete queue.json/outbox to reprocess)")
            continue
        for a in route(f):
            print(f"  {a['action']}: {a['detail']}")
    pending = [i for i in load_queue() if i["status"] == "pending"]
    print(f"\nDone. {len(pending)} item(s) waiting for Sofia -> python3 autopilot.py review")


def cmd_review(approve_all: bool) -> None:
    q = load_queue()
    pending = sorted([i for i in q if i["status"] == "pending"],
                     key=lambda i: 0 if i["urgency"] == "high" else 1)
    if not pending:
        print("Queue is empty. Go water something.")
        return
    for item in pending:
        print(f"\n{'!' * 60 if item['urgency'] == 'high' else '-' * 60}")
        print(f"[{item['id']}] {item['type']} — @{item['handle']} ({item['message']})")
        print(f"  Summary: {item['summary']}")
        print(f"  Notes:   {item['notes']}")
        if item["draft_reply"]:
            print(f"  Draft:\n    " + item["draft_reply"].replace("\n", "\n    "))
        else:
            print("  Draft:   (none — this one is yours to write, Sofia)")

        if approve_all:
            choice = "a" if item["draft_reply"] else "s"
        else:
            choice = input("\n[a]pprove & send  [e]dit  [r]eject  [s]kip > ").strip().lower()

        if choice == "a" and item["draft_reply"]:
            out = OUTBOX / f"reply_{Path(item['message']).stem}.txt"
            out.write_text(f"To: @{item['handle']}\nStatus: APPROVED by Sofia ({item['type']})\n\n{item['draft_reply']}\n",
                           encoding="utf-8")
            item["status"] = "approved"
            print(f"  -> sent ({out.name})")
        elif choice == "a":
            # Same rule the API states as a 422: approving needs a draft to send.
            print("  -> no draft to approve — use [e]dit to write and send this one")
        elif choice == "e":
            print("  Enter replacement text, end with a single '.' line:")
            lines = []
            while (line := input()) != ".":
                lines.append(line)
            reason = input("  Reason for deviating from the draft/policy: ").strip()
            out = OUTBOX / f"reply_{Path(item['message']).stem}.txt"
            out.write_text(f"To: @{item['handle']}\nStatus: EDITED by Sofia (reason: {reason})\n\n" + "\n".join(lines) + "\n",
                           encoding="utf-8")
            item["status"] = "approved_with_edits"
            print(f"  -> sent ({out.name})")
        elif choice == "r":
            item["status"] = "rejected"
        else:
            print("  -> skipped")
    save_queue(q)


# -------------------------------------------------------------------- eval
#
# The auto-send lane is only as trustworthy as the triage classifier feeding it.
# This harness runs the REAL triage + routing logic (decide_lane is pure, so we
# test production behaviour, not a copy) against labelled DMs and scores it.
#
# Crucially, not all routing errors are equal, so we don't gate on raw accuracy:
#   UNSAFE_AUTO_SEND  — model-written facts sent with no human (gold wanted a human). Forbidden.
#   MISSED_SAFETY     — a pet/child-safety message not caught. Forbidden.
#   UNVALIDATED_DRAFT — the deterministic draft gate disagreed with a labelled fixture. Forbidden.
#   safe_escalation   — sent to a human when it could have auto-sent. Wasteful, not harmful.
#   misroute          — wrong human lane (both get human review). A quality issue, not a risk.
# The gate blocks only the three dangerous classes; the safe ones are reported, not punished.

EVAL_DIR = BASE / "eval"
TRIAGE_CACHE = EVAL_DIR / ".triage_cache.json"


def classify_error(gold_lane: str, pred_lane: str) -> str | None:
    """Bucket a routing mismatch by real-world risk. None if it matched."""
    if pred_lane == gold_lane:
        return None
    if pred_lane == LANE_CARE_AUTO:          # auto-sent model facts that should've had a human
        return "UNSAFE_AUTO_SEND"
    if gold_lane == LANE_PET_SAFETY:         # a safety message routed somewhere non-urgent
        return "MISSED_SAFETY"
    if gold_lane == LANE_CARE_AUTO:          # would've auto-sent, but we escalated to a human
        return "safe_escalation"
    return "misroute"                         # human lane A vs human lane B — both reach Sofia


def _load_cases() -> list[dict]:
    lines = (EVAL_DIR / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def _load_draft_cases() -> list[dict]:
    lines = (EVAL_DIR / "draft_cases.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def _load_crosslingual_cases() -> list[dict]:
    path = EVAL_DIR / "crosslingual_cases.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _triage_cached(body: str, use_cache: bool) -> tuple[dict, bool]:
    """Triage a message, memoised by body hash so re-running eval is free."""
    cache = json.loads(TRIAGE_CACHE.read_text(encoding="utf-8")) if TRIAGE_CACHE.exists() else {}
    key = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    if use_cache and key in cache:
        return cache[key], True
    t = triage(body)
    cache[key] = t
    TRIAGE_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    return t, False


def cmd_eval(no_cache: bool) -> None:
    cases = _load_cases()
    draft_cases = _load_draft_cases()
    xl_cases = _load_crosslingual_cases()
    print(f"\nRunning triage + routing against {len(cases)} labelled DMs "
          f"({'live, cache off' if no_cache else 'cache on'}), plus "
          f"{len(draft_cases)} draft-gate and {len(xl_cases)} cross-lingual "
          f"fixtures (both deterministic, no model)...\n")

    rows, intent_ok, plant_ok, plant_n, lane_ok = [], 0, 0, 0, 0
    pet = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    errors = {"UNSAFE_AUTO_SEND": 0, "MISSED_SAFETY": 0, "UNVALIDATED_DRAFT": 0,
              "UNSAFE_CROSSLINGUAL": 0, "safe_escalation": 0, "misroute": 0}
    confusion: dict[str, dict[str, int]] = {}

    for c in cases:
        gold = c["gold"]
        sub = SUBSCRIBERS.get(c.get("handle", ""))
        t, cached = _triage_cached(c["text"], use_cache=not no_cache)
        plant, _ = resolve_plant(t, sub)
        guide = load_guide(plant)
        lane = decide_lane(t, c["text"], guide is not None)

        i_ok = t["intent"] == gold["intent"]
        l_ok = lane == gold["lane"]
        err = classify_error(gold["lane"], lane)
        if err:
            errors[err] += 1
        p_ok = None
        if gold["plant"] is not None:
            plant_n += 1
            p_ok = t["plant"] == gold["plant"]
            plant_ok += p_ok
        intent_ok += i_ok
        lane_ok += l_ok

        g, pr = bool(gold["pet_safety"]), bool(t["pet_safety"])
        pet["tp" if (g and pr) else "fn" if (g and not pr) else "fp" if pr else "tn"] += 1

        confusion.setdefault(gold["lane"], {}).setdefault(lane, 0)
        confusion[gold["lane"]][lane] += 1

        flags = []
        if not i_ok:
            flags.append(f"intent:{t['intent']}!={gold['intent']}")
        if p_ok is False:
            flags.append(f"plant:{t['plant']}!={gold['plant']}")
        if g != pr:
            flags.append(f"pet_safety:{pr}!={g}")
        rows.append((l_ok, err, c["id"], gold["lane"], lane, cached, flags))

    n = len(cases)
    print(f"{'':7}{'case':<34}{'expected lane':<20}{'got lane':<20}{'notes'}")
    print("  " + "-" * 96)
    for l_ok, err, cid, glane, lane, cached, flags in rows:
        mark = "PASS" if l_ok else "FAIL"
        note = ("" if l_ok else f"[{err}]  ") + ("; ".join(flags))
        cbadge = "·" if cached else "*"
        print(f"{cbadge} {mark}  {cid:<34}{glane:<20}{lane:<20}{note}")

    # Draft-gate fixtures: labelled drafts (including the two leaked auto-sends
    # from earlier committed runs, byte-for-byte) scored against validate_draft.
    # Pure string logic — this whole block costs zero model calls.
    print("\n  Draft gate (deterministic validator fixtures — zero model calls)")
    print("  " + "-" * 64)
    draft_ok = 0
    for c in draft_cases:
        problems = validate_draft(c["text"])
        got_send = not problems
        ok = got_send == c["should_send"]
        if not ok:
            errors["UNVALIDATED_DRAFT"] += 1
        draft_ok += ok
        exp = "SEND " if c["should_send"] else "BLOCK"
        got = "SEND " if got_send else "BLOCK"
        reasons = f"  [{'; '.join(problems)}]" if problems else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {c['id']:<32}expected {exp} got {got}{reasons}")
    print(f"\n  draft gate fixtures  {draft_ok}/{len(draft_cases)}")

    # Cross-lingual gate fixtures: decide_lane run on non-English DMs with a
    # CONTROLLED triage record (same idea as the draft-gate block — we test the
    # deterministic router, not the classifier). Zero model calls. The thesis:
    # cross-lingual RAISES the bar. A toxicity term in another language escalates
    # to a human even when the (simulated) triage missed it; a routine care
    # question that would auto-send in English does not auto-send in Portuguese —
    # and the English twin, identical triage, still does (isolating language).
    if xl_cases:
        print("\n  Cross-lingual gate (deterministic — zero model calls)")
        print("  " + "-" * 72)
        xl_ok = 0
        for c in xl_cases:
            lane = decide_lane(c["triage"], c["text"], c["guide_available"])
            ok = lane == c["gold_lane"]
            xl_ok += ok
            # Dangerous == a non-English DM that needed a human but would auto-send.
            if not ok and c["gold_lane"] != LANE_CARE_AUTO and lane == LANE_CARE_AUTO:
                errors["UNSAFE_CROSSLINGUAL"] += 1
            lng = detect_language(c["text"])
            note = c.get("note", "")[:44]
            print(f"  {'PASS' if ok else 'FAIL'}  {c['id']:<24}[{lng}] exp {c['gold_lane']:<17} got {lane:<17} {note}")
        print(f"\n  cross-lingual fixtures  {xl_ok}/{len(xl_cases)}")

    pet_recall = pet["tp"] / (pet["tp"] + pet["fn"]) if (pet["tp"] + pet["fn"]) else 1.0
    pet_prec = pet["tp"] / (pet["tp"] + pet["fp"]) if (pet["tp"] + pet["fp"]) else 1.0
    lane_acc = lane_ok / n

    print("\n  Scores")
    print("  " + "-" * 40)
    print(f"  intent accuracy      {intent_ok}/{n}  ({intent_ok / n:.0%})")
    print(f"  plant accuracy       {plant_ok}/{plant_n}  ({plant_ok / plant_n:.0%})   (scored where a plant is expected)")
    print(f"  lane accuracy        {lane_ok}/{n}  ({lane_acc:.0%})")
    print(f"  pet-safety recall    {pet['tp']}/{pet['tp'] + pet['fn']}  ({pet_recall:.0%})   <- must be 100%: never miss a safety case")
    print(f"  pet-safety precision {pet['tp']}/{pet['tp'] + pet['fp']}  ({pet_prec:.0%})   (over-flagging is the safe direction)")

    print("\n  Errors by real-world risk")
    print("  " + "-" * 40)
    print(f"  UNSAFE_AUTO_SEND   {errors['UNSAFE_AUTO_SEND']}   (dangerous — model facts sent with no human)")
    print(f"  MISSED_SAFETY      {errors['MISSED_SAFETY']}   (dangerous — safety case not escalated)")
    print(f"  UNVALIDATED_DRAFT  {errors['UNVALIDATED_DRAFT']}   (dangerous — the auto-send draft gate disagreed with a labelled fixture)")
    print(f"  UNSAFE_CROSSLINGUAL {errors['UNSAFE_CROSSLINGUAL']}   (dangerous — a non-English DM would auto-send when it needed a human)")
    print(f"  safe_escalation    {errors['safe_escalation']}   (acceptable — sent to a human unnecessarily)")
    print(f"  misroute           {errors['misroute']}   (quality — wrong human lane, still reviewed)")

    print("\n  Lane confusion (rows = expected, cols = predicted)")
    labels = [LANE_PET_SAFETY, LANE_BILLING, LANE_RETENTION, LANE_REPLACEMENT,
              LANE_CARE_AUTO, LANE_NEEDS_SOFIA, LANE_OTHER]
    seen = [x for x in labels if x in confusion or any(x in row for row in confusion.values())]
    short = {LANE_PET_SAFETY: "PET", LANE_BILLING: "BILL", LANE_RETENTION: "RETN",
             LANE_REPLACEMENT: "REPL", LANE_CARE_AUTO: "AUTO", LANE_NEEDS_SOFIA: "SOFIA", LANE_OTHER: "OTHR"}
    print("  " + " " * 12 + "".join(f"{short[c]:>6}" for c in seen))
    for r in seen:
        cells = "".join(f"{confusion.get(r, {}).get(c, 0):>6}" for c in seen)
        print(f"  {short[r] + ' (exp)':<12}{cells}")

    dangerous = (errors["UNSAFE_AUTO_SEND"] + errors["MISSED_SAFETY"]
                 + errors["UNVALIDATED_DRAFT"] + errors["UNSAFE_CROSSLINGUAL"])
    gate_ok = dangerous == 0
    print("\n  " + "=" * 52)
    print(f"  QUALITY GATE: {'PASS ✅' if gate_ok else 'FAIL ❌'}"
          f"   ({dangerous} dangerous error(s); gate blocks only those)")
    print("  " + "=" * 52 + "\n")
    sys.exit(0 if gate_ok else 1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["inbox", "review", "eval"])
    p.add_argument("--approve-all", action="store_true", help="review: approve every drafted item non-interactively")
    p.add_argument("--no-cache", action="store_true", help="eval: force fresh triage calls instead of the cache")
    args = p.parse_args()
    OUTBOX.mkdir(exist_ok=True)
    if args.command == "inbox":
        cmd_inbox()
    elif args.command == "review":
        cmd_review(args.approve_all)
    else:
        cmd_eval(args.no_cache)
