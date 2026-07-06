# RUNBOOK: operating rooted-automation

Everything here is host-side against the running stack. `/v1/route` and
`/v1/eval` make **live Opus calls** and need a real `ANTHROPIC_API_KEY` in `.env`.

```bash
export API=http://localhost:8000
export ROOTED_API_KEY="$(grep -E '^ROOTED_API_KEY=' .env | cut -d= -f2-)"
auth=(-H "X-Api-Key: $ROOTED_API_KEY" -H "Content-Type: application/json")
```

## 1. Start / stop

```bash
docker compose up -d              # rooted-api :8000, n8n :5678 (the brain is ../slice, bind-mounted)
docker compose logs -f rooted-api # tail the API
docker compose down               # stop (keeps the n8n_data volume)

curl -s $API/healthz | python3 -m json.tool
# brain_ok:true and data_dir_writable:true are the ones that matter.
# anthropic_key_present:true confirms the key reached the container.
```

If `brain_ok` is false the slice mount is missing or `ROOTED_SLICE_DIR` points
somewhere wrong — check the `../slice:/app/slice` volume in docker-compose.yml.

## 2. Simulate a DM

```bash
# A queued case (cat safety) that returns a decision and queues for Sofia:
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"run-01","handle":"kat.and.cats",
  "text":"HELP my cat chewed the box plant and is drooling, is it poisonous?? vet??"
}' | python3 -m json.tool
# -> action:"queue", lane:"URGENT_PET_SAFETY", queued.urgency:"high"

curl -s "${auth[@]}" "$API/v1/queue?status=pending" | python3 -m json.tool
```

`scripts/smoke_live.sh` routes all six sample DMs and asserts each expected
action/lane (~12 live calls): the one-shot "is the brain wired up right" check.

## 3. Approve via Telegram (WF2)

With the stack up, WF1/WF2 active, and the Telegram bot wired (`docs/n8n-setup.md`):

1. A queued item posts a card to Sofia with **✅ Approve / ✏️ Edit / ❌ Reject**
   (buttons only when `queued.has_draft`).
2. **Approve** → WF2 `POST /v1/approve {decision:"approve"}` → placeholder send →
   `POST /v1/sent` → the card edits to "approved & sent". The outbox file is
   written in `cmd_review`'s exact format (`Status: APPROVED by Sofia (TYPE)`).
3. **Reject** → prompts for a reason (the API 422s a reason-less reject) →
   `{decision:"reject", reason}`.
4. **Edit** → prompts for replacement text, then a reason → `{decision:"edit",
   edited_text, reason}`. The API runs `validate_draft` on your text and returns
   `warnings` (never blocking; your words are your call).

The same, by curl (no Telegram):

```bash
curl -s "${auth[@]}" $API/v1/approve -d '{"queue_id":"q001","decision":"approve"}' | python3 -m json.tool
curl -s "${auth[@]}" $API/v1/approve -d '{"queue_id":"q002","decision":"reject","reason":"handling in DMs"}' | python3 -m json.tool
curl -s "${auth[@]}" $API/v1/approve -d '{"queue_id":"q003","decision":"edit","edited_text":"Hey! ...","reason":"warmer tone"}' | python3 -m json.tool
```

## 4. Kill-switch drill (demote-never-promote)

```bash
# 1. Suspend model-written auto-sends.
curl -s "${auth[@]}" $API/v1/config -d '{"auto_send_enabled":false,"reason":"drill"}' | python3 -m json.tool
#    -> {"auto_send_enabled": false}

# 2. Route a care DM that WOULD auto-send. It now queues instead.
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"drill-care-01","handle":"sofia.grows",
  "text":"quick q, my monstera has a yellow lower leaf, am i overwatering?"
}' | python3 -m json.tool
#    -> action:"queue", killswitch_applied:true, queued.type:"NEEDS_SOFIA",
#       notes mention "Auto-send suspended". The draft is in service/data/held/.

# 3. Confirm the gate-passed draft was held, not sent.
ls service/data/held/     # reply_drill-care-01.txt is here, NOT in outbox/

# 4. Re-enable (deliberately manual, never automatic on a later eval PASS).
curl -s "${auth[@]}" $API/v1/config -d '{"auto_send_enabled":true}' | python3 -m json.tool

# 5. Same DM shape now auto-sends again.
curl -s "${auth[@]}" $API/v1/route -d '{
  "message_id":"drill-care-02","handle":"sofia.grows",
  "text":"another quick one, new monstera leaf is pale, is that normal?"
}' | python3 -m json.tool
#    -> action:"auto_send", killswitch_applied:false
```

## 5. Eval gate (LIVE: ~27 Opus calls cold, then cached)

```bash
curl -s "${auth[@]}" $API/v1/eval -d '{"no_cache":false}' | python3 -m json.tool
# -> {"gate":"PASS","exit_code":0,"scores":{...},"dangerous":{UNSAFE_AUTO_SEND:0,...}}
```

The gate is the subprocess **exit code**, not the ✅ line. WF3 runs this daily and
flips the kill switch on any non-PASS (or crash), then alerts Sofia. A second
concurrent `/v1/eval` returns 409 `eval_already_running`.

## 6. Audit trail

```bash
tail -f service/data/audit.jsonl    # one JSON line per route/approve/edit/reject/sent/eval/config
```

## Troubleshooting

- **401 on `/v1/*`**: `X-Api-Key` missing or wrong; it must equal `ROOTED_API_KEY`.
- **409 `handled_but_no_decision`**: the brain acted but the decision write was
  lost (crash window). n8n dead-letters this to the operator; inspect
  `service/data/` and the audit log.
- **healthz 503**: `brain_ok:false`; the slice bind mount is missing or
  `ROOTED_SLICE_DIR` is mis-set — check the volumes in docker-compose.yml.
- **containers stuck in `Created`**: a Docker Desktop VM issue, not this stack;
  fully quit Docker Desktop and relaunch (or reboot). Verify with
  `docker run --rm hello-world`.
