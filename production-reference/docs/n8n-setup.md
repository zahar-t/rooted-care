# n8n setup: credentials, Telegram bot, import

The five workflows in `workflows/` are authored and structurally validated
(`python3 scripts/validate_workflows.py`), but n8n credentials and the Telegram
bot are secrets that can't live in git. You wire those up once in the UI. Node
parameter schemas were written blind (no live n8n during authoring), so expect a
small fix or two on import; that's normal and expected.

Prereqs: `docker compose up -d` is running, `http://localhost:5678` opens, and
`.env` has a real `ANTHROPIC_API_KEY` and the generated `ROOTED_API_KEY`.

## 1. Telegram bot (BotFather)

1. In Telegram, open **@BotFather** → `/newbot` → name it (e.g. `Rooted Autopilot`)
   and pick a username ending in `bot`. Copy the **HTTP API token** it gives you.
2. Send your new bot any message (e.g. `/start`) so it can DM you back.
3. Get your **chat id**: open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser after messaging
   the bot, and read `result[].message.chat.id`. That number is `SOFIA_CHAT_ID`
   (and, unless you want a separate ops channel, `OPERATOR_CHAT_ID` too).
4. Put both ids in `.env`:
   ```
   SOFIA_CHAT_ID=123456789
   OPERATOR_CHAT_ID=123456789
   ```
   then `docker compose up -d` again so the n8n container picks them up
   (the workflows read `{{ $env.SOFIA_CHAT_ID }}` / `{{ $env.OPERATOR_CHAT_ID }}`).

## 2. n8n credentials

Open `http://localhost:5678` → **Credentials → New**.

- **Header Auth**: name it exactly **`Rooted API Key`**.
  - Header **Name**: `X-Api-Key`
  - Header **Value**: your `ROOTED_API_KEY` (from `.env`)
  - Every HTTP Request node in the workflows references this by name.
- **Telegram API**: name it **`Rooted Telegram Bot`**.
  - Access Token: the BotFather token from step 1.

The workflow JSON references these credentials by the placeholder id
`REPLACE_WITH_CREDENTIAL_ID`; after import, open each red-flagged node once and
pick the credential from the dropdown (n8n matches by name, so this is quick).

## 3. Import the workflows

For each file in `workflows/` → n8n **Workflows → Import from File**:

- `01-inbound-dm.json`: inbound DM → route → send/queue → Telegram approval card
- `02-approvals-telegram.json`: Telegram Approve/Edit/Reject → `/v1/approve`
- `03-eval-sentinel.json`: daily eval; suspends auto-send on FAIL
- `04-daily-digest.json`: daily pending-queue digest
- `05-telegram-demo.json`: DM the bot → route → the AI's reply comes back to your chat (see §6)

After importing each: select the credentials on any flagged node, then **Save**
and toggle **Active** (top-right) for WF1–WF4.

## 4. Verify the round-trip

With WF1 active, from the host:

```bash
curl -s -X POST http://localhost:5678/webhook/rooted-dm \
  -H "Content-Type: application/json" \
  -d '{"message_id":"setup-01","handle":"kat.and.cats","text":"HELP my cat chewed the box plant and is drooling, is it poisonous?? vet??"}'
```

Expect `{"ok":true}` immediately, a placeholder-send logged in the WF1 execution,
and a `‼️ URGENT` Telegram card with **✅ Approve / ✏️ Edit / ❌ Reject**. Tap
Approve → WF2 calls `/v1/approve`, logs the placeholder send, posts `/v1/sent`,
and edits the card to "approved & sent". See `workflows/SIMULATE.md` for the full
menu (kill-switch drill, eval trigger, direct `/v1/route`).

## 5. Known wrinkles (call them out, don't hide them)

- **The "send DM" node is a placeholder.** WF1/WF2 log the payload in a Code node
  instead of posting to Instagram. That Code node is the seam where the real
  Meta/Instagram channel node slots in later.
- **Telegram edit is two-step and demo-grade.** Editing a queued draft prompts
  for the replacement text, then for a reason; the state is carried in the
  Telegram reply-to chain (the Code node walks `message.reply_to_message`), not a
  database. Fine for one operator; document, don't productionise.
- **Boundary rule holds:** n8n only ever switches on `decision.action`
  (`auto_send | ack_and_queue | queue | duplicate`) and reads `queued.urgency`
  for cosmetics (the ‼️ prefix). It never inspects the customer's words. If a
  node ever needs to know what the customer *said*, the design has failed.
- **Monday eval is expensive.** WF3 forces `no_cache` on Mondays (~27 live Opus
  calls) as a weekly drift check; other days run cached (near-zero).

## 6. The one-window demo (WF5)

`05-telegram-demo.json` is the self-contained demo: you DM the bot as a customer
and the AI's reply comes back to the same chat. It reuses the **`Rooted Telegram
Bot`** credential and needs no `SOFIA_CHAT_ID` (it replies to whoever texted).

1. Import `05-telegram-demo.json`; on the **Telegram Trigger (Demo)** node and
   each **Reply**/**Ask**/**Route Failed** node, pick the `Rooted Telegram Bot`
   credential, then **Save**.
2. **Activate WF5 on its own.** Telegram allows one webhook consumer per bot
   token, so deactivate **WF2 (Approvals)** first, or point WF5 at a second
   BotFather bot. (WF1/WF3/WF4 have no Telegram *trigger*, so they can stay on.)
3. Make sure `.env` has a real `ANTHROPIC_API_KEY` (the brain makes one live call
   to triage each DM), then DM your bot:
   - *"my monstera has a couple of yellow leaves, am I overwatering?"* → the
     care reply comes back automatically (`auto_send`).
   - *"my fiddle-leaf fig keeps dropping leaves since I moved flat, any ideas?"* →
     the hand-off ack comes back instantly and the item is queued for Sofia
     (`ack_and_queue`), visible in the next `04-daily-digest` run or `GET /v1/queue`.

Send a non-text message (sticker/photo) and the bot just asks for text, so the
`Has Text?` gate means only real DMs reach the brain.
