# The Walkthrough

## The story

- Sofia runs Rooted: 40 subscribers, monthly plant box, care advice via DM.
- Two problems, both personality-shaped: she answers every DM with an essay
  (3h/day), and she can't say no (every complaint = free plant, failed payments
  never chased).
- The redesign isn't "add a chatbot". It's: **agents do the reading, drafting
  and policy maths; Sofia does judgment, safety and relationships.** Show MAP.md
  before/after diagrams here.
- One principle drove every decision: **the LLM writes words; code owns facts,
  money and dates.** The model never computes eligibility, never asserts which
  plant a customer owns, and never states a care fact that isn't in Sofia's own
  guides.

## Live demo

```
cd slice
rm -f queue.json outbox/*.txt   # start clean — otherwise the idempotent run skips the committed artifacts
python3 autopilot.py inbox      # (or show slice/sample_run_transcript.txt if time is tight)
```

Walk the eight messages — the first six each demonstrate a different lane, the
last two (in Portuguese) exercise the cross-lingual gate:

1. **msg_01, "monstera yellow leaves"** → AUTO-SENT. Point out: the reply's
   facts come from `careguides/monstera.md` (Sofia's own words), capped at 110
   words with "happy to go deeper": Sofia's depth made opt-in.
2. **msg_02, "my cat chewed the plant from this month's box"** → the customer
   never named the plant. *Code*, not the model, looked up her shipment history
   and found we sent her a pothos (toxic to cats). Drafted with the guide's
   toxicity facts, flagged URGENT, human must send. **This is the demo
   centrepiece**, grounding + safety gate in one message.
3. **msg_03, crispy calathea, wants replacement** → policy engine computed:
   shipped 5 days ago (≤14-day window), 0 prior replacements → APPROVE
   recommended. Sofia one-taps. Saying yes is now policy, not guilt.
4. **msg_04, fiddle leaf fig (not our plant, not in the KB)** → instant
   template ack auto-sent (no LLM call, a template was the right tool), and a
   queue item for Sofia. **The flywheel:** her answer becomes the next care
   guide, so the agent handles fiddle figs forever after.
5. **msg_05, "money's tight, need to cancel"** → agent computed the retention
   options (pause, or downgrade Grower→Sprout = exact £14/mo saving) and
   drafted, but this NEVER auto-sends. Retention is a relationship.
6. **msg_06, "you charged me twice"** → no draft at all. Money dispute + upset
   customer = facts prepared (tier, expected charge), human writes.
7. **msg_07 + msg_08, the Portuguese pair** → the cross-lingual gate live: the
   cat-toxicity DM escalates URGENT even if triage under-reads it (a
   deterministic safety-term net), and the routine care question that would
   auto-send in English demotes to Sofia — auto-send is English-only.

Then the human side:

```
python3 autopilot.py review     # urgent items first; approve / edit / reject
```

Point out the **friction-by-design**: approving the policy recommendation is one
keypress; overriding it demands typing a reason. That's Sofia's conflict-avoidance
handled structurally, not with a pep talk.

Then the proof it's actually safe to let anything auto-send:

```
python3 autopilot.py eval          # 27 labelled DMs, scored against the real router
```

The point to land: the auto-send lane is only as safe as the classifier feeding
it, so it's measured. The gate ignores raw accuracy and blocks only *dangerous*
errors: an unsafe auto-send, a missed safety case, or a draft-gate mismatch
(the same run also scores the deterministic `validate_draft()` that every
auto-send must pass through, against 12 labelled drafts). Today: pet-safety recall
100%, 0 dangerous errors, gate PASS. And it earned its keep: growing the set to
27 caught the classifier reading a plain "thanks!" as a care question (an unsafe
auto-send), which I fixed by making the auto-send lane deterministically require
an actual question. Remaining misses are both safe: over-escalating a "cat's
nowhere near it" message, and one wrong-but-still-human-reviewed lane.

## The three questions

**What did I deliberately leave as human work, and why?**
- Pet-safety sends (agent = fast, human = safe), all money decisions,
  cancel/pause conversations, and any question not covered by a care guide.
- The care guides themselves stay human-written: they're the product's
  expertise. Agents quote Sofia; they never replace her.

**Where did the AI surprise me?**
- *Good:* extraction was flawless on genuinely messy input first try: "the
  trailing plant from this month's box" correctly became `plant: unknown,
  plant_is_from_us: true`, letting deterministic code resolve it to the actual
  shipped pothos (toxic to cats, the one message where that lookup really
  mattered). The LLM + lookup handoff worked without iteration.
- *Good:* tie-break rules in the triage prompt were respected: the crispy
  calathea message is both a care question and a replacement ask, and it
  classified per the rule I wrote, not vibes.
- *Watch-out:* drafts drift beyond instructions in a generous direction. The
  pet-safety draft appended an unprompted "want me to share some cat-safe
  trailing plants?" offer (charming here), but the same instinct would happily
  volunteer discounts. That's exactly Sofia's failure mode in model form, and
  it's why nothing money-adjacent auto-sends and why eligibility is a Python
  function the model can only *phrase*, never decide.
- *Watch-out:* the model matches the register of whatever you feed it: hand it
  a 250-word care guide and an essay-length reply is the path of least
  resistance. The hard 110-word cap in the voice rules is load-bearing.
- *Watch-out (caught by the eval):* the classifier isn't stable on the boundary
  between a *question* and *feedback*: it sometimes tags a plain "thanks, it's
  thriving!" as a `care_question`, which would auto-send an unprompted care essay
  at someone who just said thank you. This only showed up because the eval set
  grew to include a thank-you. The lesson: don't let a probabilistic classifier
  be the last gate on an irreversible action. The auto-send lane now has a
  deterministic check (the message must actually ask a question) *behind* the
  model's call, so praise reaches a human however the model labels it.
- *Watch-out (the drafting-side twin):* an early committed run leaked model
  framing into an auto-sent customer DM — a "Here's your draft reply:" preamble,
  a `---` separator, and in a later run a self-correction quoting the voice
  rules mid-message. That exact text, byte for byte, is now regression fixtures
  for the deterministic draft gate every auto-send must pass. Same lesson as
  the classifier, applied to drafting: the model writes, code decides what leaves.

**Another week: what next, and what would I refuse to automate?**
- *Next:* the Retention & Billing agent (dunning sequence, biggest silent
  money leak), then Box Curation (pet-safe filter + no-repeats from shipment
  history + personalised care card per box; the data model already supports it).
- *Refuse:* auto-sending anything about an animal's health; auto-approving
  refunds/credits; automated "win-back" pressure on people who cancel (Sofia's
  brand is trust at 40 subscribers; one manipulative email costs more than a
  churn); and replacing Sofia's voice in the guides with generated content.

## Why not n8n/Zapier?

Deliberate choice: the routing logic IS the product, and ~300
lines of Python make every decision inspectable and testable. I'd happily bolt
n8n on the outside (Instagram webhook in, send-message node out). The slice is
the brain, not the plumbing. Also: no API key needed; the LLM adapter falls
back to the locally-authenticated Claude Code CLI (`claude -p`), which is the
kind of pragmatic glue that ships real systems.

## Where I stopped (respecting a self-imposed ~6h budget)

- No real Instagram/Stripe integration. Inbox is .txt files by design; the
  interfaces (`inbox/`, `outbox/`, `queue.json`) are where webhooks would attach.
- Extraction uses prompt-enforced JSON, not the API's structured-output schema
  feature. The lenient parser was the 20-minute pragmatic version. With API
  access (vs CLI fallback) I'd use forced tool-use / `output_config.format`.
- No conversation memory across messages from the same customer.
- Eval harness: **built** (`python3 autopilot.py eval`), 27 labelled DMs scored
  against the real router plus 12 labelled drafts scored against the draft gate,
  gated on dangerous errors only (no unsafe auto-send, no missed safety, no
  draft-gate mismatch), currently 0 dangerous errors / lane accuracy 93%. It
  already caught and fixed one real unsafe auto-send. Next step is growing the
  set toward ~100 and tracking scores as prompts change.
