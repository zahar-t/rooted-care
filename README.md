# Rooted Autopilot

> A grounded care-advice agent for a houseplant subscription box: it answers what it
> safely can, and escalates to a human wherever judgment or safety is at stake.

Putting a tiny business on autopilot: **Rooted**, a houseplant subscription box
run by my friend Sofia (over-explains everything, can't say no). AI agents read
and draft; deterministic code owns facts, money and policy; Sofia approves what
matters.

## Deliverables

| Brief asks for | Where |
|---|---|
| 1. The map (thinking) | [`MAP.md`](MAP.md), before/after diagrams, agent roster, human gates, never-automate list, Sofia-specific design |
| 2. One working slice (proof) | [`slice/`](slice/), the inbox autopilot, runs end-to-end |
| 3. The walkthrough (judgment) | [`WALKTHROUGH.md`](WALKTHROUGH.md), the live demo + the three questions |

**One-page proof sheet:** [`docs/proof-sheet.html`](docs/proof-sheet.html), the
architecture, the six routing lanes, and the live eval quality gate on a single
page (open it in a browser). A captured text run is in [`slice/PROOF.txt`](slice/PROOF.txt).

## The judgment

- **What stayed human, and why.** Pet-safety sends — the agent makes it *fast*
  (draft in seconds, urgent flag), the human makes it *safe*; every money
  decision (replacements, refunds, billing disputes — the policy engine computes
  the recommendation, Sofia one-taps it); and cancel/pause conversations,
  because retention at 40 subscribers is a relationship, not a funnel. Anything
  not covered by a care guide gets an instant holding ack and Sofia answers
  personally — and her answer becomes the next guide. The guides themselves stay
  human-written: they're the product's expertise, so agents quote Sofia, they
  never replace her.

- **Where the AI surprised me (good and bad).** Good: extraction was flawless on
  genuinely messy input first try — "the trailing plant from this month's box"
  became `plant: unknown, plant_is_from_us: true`, letting deterministic code
  resolve it to the actual shipped pothos (toxic to cats, the one message where
  that lookup really mattered). Bad, caught by measurement: the classifier
  sometimes reads a plain "thanks, it's thriving!" as a care question — which
  would auto-send an unprompted care essay at someone who just said thank you —
  so the auto-send lane now deterministically requires an actual question. And
  an early committed run leaked model framing ("Here's your draft reply:") into
  a customer DM; that exact text, byte for byte, is now regression fixtures for
  the draft gate every auto-send must pass. Both fixes are structural, not
  prompt pleas: a probabilistic step is never the last gate on an irreversible
  action.

- **What I'd automate next — and refuse to.** Next: the Retention & Billing
  agent (dunning sequence, the biggest silent money leak), then Box Curation
  (pet-safe filter, no-repeats from shipment history, personalised care cards).
  Refuse: auto-sending anything about an animal's health; auto-approving refunds
  or credits; automated "win-back" pressure on people who cancel — one
  manipulative email costs more than a churn at 40 subscribers; and replacing
  Sofia's voice in the guides with generated content. Sofia is the product; the
  system automates around her, never through her.

Fuller version in [`WALKTHROUGH.md`](WALKTHROUGH.md).

## Run it

Requirements: Python 3.10+, and **either** `ANTHROPIC_API_KEY` set (uses the
`anthropic` SDK) **or** a logged-in Claude Code CLI (falls back to `claude -p`).
No other dependencies.

```bash
cd slice
python3 autopilot.py inbox                # triage + route all 8 sample DMs
python3 autopilot.py review               # Sofia's approval queue (urgent first)
python3 autopilot.py review --approve-all # non-interactive, for testing
python3 autopilot.py eval                 # score triage + routing on 27 labelled DMs + the deterministic draft gate fixtures
python3 autopilot.py eval --no-cache      # ...forcing fresh model calls
```

Outputs: sent replies land in `slice/outbox/`, pending decisions in
`slice/queue.json`. A captured end-to-end run is in
[`slice/PROOF.txt`](slice/PROOF.txt) if you'd rather not run it yourself. To
re-run from scratch: delete `queue.json` and `outbox/*`.

## How the slice works

```
DM (inbox/*.txt)
  → Claude: structured triage (intent, plant, symptoms, pet-safety, sentiment)
  → Python: subscriber lookup, which plant we ACTUALLY shipped them,
            replacement eligibility, tier maths, routing   ← no LLM here, ever
  → Claude: reply drafted in Sofia's voice, plant facts ONLY from her care guides
  → lane:   AUTO-SEND (routine care) | approval queue (money/safety/retention)
```

Config is data, not code: `policy.json` (pricing tiers, replacement policy,
routing rules, voice), `subscribers.json` (CRM stand-in), `careguides/*.md`
(Sofia's knowledge base, the only source of plant facts the model may use).

## The quality gate: you don't widen autonomy without measuring it

`python3 autopilot.py eval` runs the **real** triage + routing logic (the router
is a pure function, so the eval tests production behaviour, not a copy) against
27 labelled DMs in [`slice/eval/cases.jsonl`](slice/eval/cases.jsonl), every
lane plus deliberately adversarial cases: a calmly-worded pet-safety message, a
*child* (not pet) chewing a leaf, an upset care question that must *not*
auto-send, a replacement-vs-care tie-break, a billing question wrapped in a
friendly care remark, an angry cancel with no money mention, a plain thank-you
that must not trigger a reply, an unknown sender, and a plant named as
out-of-reach.

The insight the harness encodes: **not all routing errors are equal.** It buckets
every mismatch by real-world risk and gates on only the dangerous ones:

| Error class | Meaning | Gated? |
|---|---|---|
| `UNSAFE_AUTO_SEND` | Model-written facts sent with no human | **Blocks release** |
| `MISSED_SAFETY` | A pet/child-safety message not escalated | **Blocks release** |
| `UNVALIDATED_DRAFT` | The auto-send draft gate disagreed with a labelled fixture | **Blocks release** |
| `safe_escalation` | Sent to a human when it could have auto-sent | Reported, allowed |
| `misroute` | Wrong human lane (still gets reviewed) | Reported, allowed |

Latest live run (`claude-opus-4-8`): **intent 96%, plant 100%, lane 93%,
pet-safety recall 100%, draft gate fixtures 12/12, 0 dangerous errors → gate
PASS.** "0 dangerous errors" is scoped to what the harness actually measures:
triage, routing, *and* the draft-gate fixtures. The remaining misses
are both safe: a `safe_escalation` (the model over-flagged safety on a plant the
customer said was out of reach, the *correct* direction to err; over-caution
costs a human glance, under-caution costs an animal) and a `misroute` between two
human-reviewed lanes.

**The eval earned its keep the day it was written.** Growing the set from 19 to
27 caught a real `UNSAFE_AUTO_SEND`: the classifier sometimes reads a plain
"thanks, it's thriving!" as a care question and would have auto-sent an unprompted
care essay. The fix was structural, not a prompt plea: the auto-send lane now
*deterministically* requires the message to actually ask a question (`"?" in
body`), so praise routes to a human no matter how the model labels it. Dangerous
error found by measurement, closed in code. Full output in `slice/PROOF.txt`.

### The draft gate: routing isn't enough

Routing a message to the auto-send lane correctly still leaves one unguarded
step: the draft itself is model output. An earlier committed run proved the
point: the auto-sent reply in `outbox/reply_msg_01.txt` **did leak model
framing into a customer DM** ("Here's your draft reply:", a `---` separator,
and in a later run a self-correction quoting the voice rules mid-message).
That exact leaked text, byte for byte, is now a pair of regression fixtures in
[`slice/eval/draft_cases.jsonl`](slice/eval/draft_cases.jsonl).

The fix is structural: before anything auto-sends, `validate_draft()` (a pure
function, plain string logic, zero model calls) checks the draft for leaked
meta-commentary, the 110-word cap and one-emoji rule from `policy.json`, and
money-promise language (refunds, freebies, £ amounts; money is a human
decision). A failing draft is never sent and never dropped: it demotes to
Sofia's approval queue with the failure reasons attached, and the customer gets
the deterministic holding ack. The eval scores the validator against 11
labelled drafts on every run. The philosophy in one line: **a probabilistic
step is never the last gate on an irreversible action.**

## How I used coding tools (Claude Code) to build this

I built the whole thing with Claude Code as a pair, and the division of labour
mirrors the product's own principle (*I own the judgment, the tool does the
legwork*):

- **Scaffolding & boilerplate**, the CLI arg-parsing, JSON I/O, queue/outbox
  plumbing, and the LLM adapter's SDK↔CLI fallback were dictated by me and typed
  by the tool, which let me spend my time on the routing design instead.
- **The eval harness was co-designed in the loop.** I asked for a scorer; the
  first version gated on raw lane accuracy. Running it surfaced that a "safe"
  over-escalation was being scored identically to a dangerous auto-send, so I
  had it restructure the gate around *error asymmetry*. The tool also generated
  the adversarial fixtures, which I reviewed and relabelled where I disagreed
  (one ambiguous case I reworded until it tested exactly one thing).
- **Tightening the model against itself**, I used the tool to probe candidate
  DM wordings (e.g. "does this phrasing trip the safety gate?") before committing
  them as fixtures, which is how the `safe_escalation` false-positive was found
  and documented rather than shipped blind.
- **Find → fix, in one loop**, expanding the eval to 27 cases surfaced a genuine
  `UNSAFE_AUTO_SEND` (a thank-you read as a care question). I had the tool trace
  it to the auto-send condition and add a deterministic question-mark guard, then
  re-ran the eval to confirm the dangerous error became a safe one. The measure →
  diagnose → fix → re-measure cycle took minutes.
- **Refactor with a safety net**, extracting the pure `decide_lane` router from
  the side-effecting `route()` was a tool-driven refactor I verified with a
  10-case pure-logic unit check before spending a single model call.

The meta-point: the same instinct that makes these tools useful (*let the model
draft, keep human judgment on the decisions that matter*) is the exact thesis
of the product.

## Honest time log (~5.5h against a self-imposed 6h budget)

- 1.0h, picking the business/twist, mapping before/after, deciding the human gates
- 0.5h, fixtures: care guides, subscriber records, sample DMs, policy
- 2.5h, the slice: triage prompt, routing, policy engine, drafting, review CLI
- 1.0h, testing the six lanes end-to-end, tightening prompts (word cap, "guide-only facts")
- 0.5h, writing up MAP / WALKTHROUGH / README

Built with Claude Code throughout, including the decision to make the LLM
adapter fall back to `claude -p` so the demo runs on a Claude login alone.

## What I'd do next (written down instead of built)

1. Retention & Billing agent, dunning sequence; biggest silent money leak.
2. Box Curation agent, pet-safe filter, no-repeats, personalised care cards.
3. Real interfaces, Instagram webhook → `inbox/`, approval queue → a
   one-thumb Telegram bot for Sofia, Stripe for payments. **Designed, and prototyped one
   layer deep in [`production-reference/`](production-reference/)**: FastAPI + n8n +
   Telegram approvals + kill switch + eval sentinel, built and unit-tested (52 mocked
   tests), live stack not yet stood up.
4. Harden the cross-lingual gate, swap the demo language heuristic for a real detector
   (fastText / CLD3), localise the holding ack and drafts into the customer's language,
   and add safety fixtures in more languages. The judgment (cross-lingual raises the bar
   for auto-send) is built; the breadth is next.
5. Grow the eval set (27 routing + 12 draft-gate + 3 cross-lingual fixtures today,
   `python3 autopilot.py eval`) toward ~100, and track scores over time as prompts change.
6. Structured-output extraction (forced tool use) instead of prompt-enforced JSON.
