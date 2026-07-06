# SIMULATE — demo the whole loop without Instagram

Everything here runs against the local stack (`docker compose up -d`). The six
DM bodies are lifted from the repo's `slice/inbox/*.txt`.

> **These calls are LIVE.** `/v1/route` runs the real brain (≈2 Opus calls per
> DM) and `/v1/eval` runs ≈27 on a cold cache. They need a real
> `ANTHROPIC_API_KEY` in `.env` and cost tokens. `scripts/smoke_live.sh` does the
> six-DM run for you; this file is the manual, copy-paste version.

```bash
# Point at the API and load your key from .env (never commit the key).
export API=http://localhost:8000
export ROOTED_API_KEY="$(grep -E '^ROOTED_API_KEY=' .env | cut -d= -f2-)"
auth=(-H "X-Api-Key: $ROOTED_API_KEY" -H "Content-Type: application/json")
```

## A. Route each sample DM directly (`POST /v1/route`)

Expected `action` / `lane` per DM (from the frozen brain's fact sheet):

| DM | handle | expected action | expected lane |
|----|--------|-----------------|---------------|
| 01 | sofia.grows | `auto_send` | `AUTO_SEND_CARE` |
| 02 | kat.and.cats | `queue` | `URGENT_PET_SAFETY` |
| 03 | dan_greenthumb | `queue` | `REPLACEMENT` |
| 04 | leafy.lou | `ack_and_queue` | `NEEDS_SOFIA` |
| 05 | priya.plants | `queue` | `PAUSE_OR_CANCEL` |
| 06 | marcus.w | `queue` | `BILLING_DISPUTE` |

```bash
# 01 — care question, monstera, calm -> auto_send
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"sim-msg-01","handle":"sofia.grows",
  "text":"hey!! quick q — the monstera from my march box has a couple of yellow leaves at the bottom. the rest looks super happy and it even has a new leaf coming. am i overwatering it? i have been doing a little every few days"
}' | python3 -m json.tool

# 02 — cat chewed the plant -> queue URGENT_PET_SAFETY
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"sim-msg-02","handle":"kat.and.cats",
  "text":"HELP my cat has been chewing on the trailing plant from this months box. she is drooling a bit and keeps pawing her mouth. is it poisonous??? do I need to go to the vet right now??"
}' | python3 -m json.tool

# 03 — replacement request, calathea, photo -> queue REPLACEMENT (notes: APPROVE)
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"sim-msg-03","handle":"dan_greenthumb",
  "text":"hey sofia, bit gutted — the calathea from junes box arrived with about half the leaves brown and crispy round the edges. i have attached a photo. any chance of a replacement? [photo attached: calathea_damage.jpg]"
}' | python3 -m json.tool

# 04 — fiddle leaf fig (not in KB) -> ack_and_queue NEEDS_SOFIA
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"sim-msg-04","handle":"leafy.lou",
  "text":"hiya! not about a box plant this time — my fiddle leaf fig (had it years, long before i found you) has started dropping leaves like mad since i moved flat. one or two a day. any ideas??"
}' | python3 -m json.tool

# 05 — pause/cancel, budget -> queue PAUSE_OR_CANCEL
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"sim-msg-05","handle":"priya.plants",
  "text":"hey sofia, this is a bit awkward but money is really tight and i think i need to cancel my subscription. or maybe pause it? honestly not a complaint, i LOVE the boxes, its just budget stuff. how does cancelling work?"
}' | python3 -m json.tool

# 06 — double charge -> queue BILLING_DISPUTE
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"sim-msg-06","handle":"marcus.w",
  "text":"You have charged me twice this month?? £58 has come out instead of £29. I like the boxes but this makes me want to cancel. Please sort it out and confirm you have refunded the extra."
}' | python3 -m json.tool
```

Idempotent replay — the same `message_id` returns the stored decision with
`"duplicate": true` and adds nothing to the queue:

```bash
curl -s "${auth[@]}" $API/v1/route -d '{"message_id":"sim-msg-06","handle":"marcus.w","text":"(same as before)"}' | python3 -m json.tool
```

## B. Same thing through n8n (WF1 webhook)

WF1 must be **active** in n8n. The webhook responds `{"ok":true}` instantly, then
routes in the background and messages Sofia on Telegram for the queued ones.

```bash
curl -s -X POST http://localhost:5678/webhook/rooted-dm -H "Content-Type: application/json" -d '{
  "message_id":"wf-msg-02","handle":"kat.and.cats",
  "text":"HELP my cat chewed the trailing plant from this months box, she is drooling. is it poisonous?? vet now??"
}'
# -> {"ok":true} immediately; a ‼️ URGENT Telegram card with Approve/Edit/Reject follows.
```

## C. The approval queue

```bash
curl -s "${auth[@]}" "$API/v1/queue?status=pending" | python3 -m json.tool
# Approve a drafted item (replace q00N):
curl -s "${auth[@]}" $API/v1/approve -d '{"queue_id":"q002","decision":"approve"}' | python3 -m json.tool
# Reject needs a reason (422 without one):
curl -s "${auth[@]}" $API/v1/approve -d '{"queue_id":"q005","decision":"reject","reason":"handling in DMs"}' | python3 -m json.tool
```

## D. Kill-switch drill (demote-never-promote)

```bash
# 1. Suspend model-written auto-sends.
curl -s "${auth[@]}" $API/v1/config -d '{"auto_send_enabled":false,"reason":"drill"}' | python3 -m json.tool
# 2. Route a care DM that WOULD auto-send — it now queues instead.
curl -s "${auth[@]}" $API/v1/route -d '{"message_id":"drill-01","handle":"sofia.grows","text":"quick q — my monstera has a yellow leaf, am i overwatering?"}' | python3 -m json.tool
#    -> "action":"queue", "killswitch_applied":true, queued.type "NEEDS_SOFIA"
# 3. Re-enable (deliberately manual).
curl -s "${auth[@]}" $API/v1/config -d '{"auto_send_enabled":true}' | python3 -m json.tool
```

## E. Eval gate (LIVE — ~27 Opus calls on a cold cache)

```bash
curl -s "${auth[@]}" $API/v1/eval -d '{"no_cache":false}' | python3 -m json.tool
# -> {"gate":"PASS","exit_code":0,"scores":{...},"dangerous":{...},"stdout_tail":"..."}
```

## F. Health

```bash
curl -s $API/healthz | python3 -m json.tool   # no auth; brain_ok must be true
```
