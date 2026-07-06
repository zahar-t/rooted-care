#!/usr/bin/env python3
"""Author the n8n workflow JSON files, then validate them.

Why a generator: n8n's connection graph keys nodes by NAME, and a single typo
silently breaks a wire. Building the dicts in code lets us assert — for every
workflow — that every connection endpoint references a node that exists, and
that node names are unique, before we ever open the n8n UI. The emitted JSON is
the deliverable (workflows/0N-*.json); this script is how it's kept honest.

The node parameter schemas target a recent n8n (typeVersions kept conservative).
Importing may still need a small manual fix — that step is expected to be
iterative (schemas were written without a live n8n; see docs/n8n-setup.md).
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "workflows"

API = "http://rooted-api:8000"                 # rooted-api on the compose network
HEADER_CRED = {"httpHeaderAuth": {"id": "REPLACE_WITH_CREDENTIAL_ID", "name": "Rooted API Key"}}
TG_CRED = {"telegramApi": {"id": "REPLACE_WITH_CREDENTIAL_ID", "name": "Rooted Telegram Bot"}}


def node(name, type_, typeVersion, position, parameters, **extra):
    n = {
        "parameters": parameters,
        "id": name.lower().replace(" ", "-").replace("?", "").replace("(", "").replace(")", ""),
        "name": name,
        "type": type_,
        "typeVersion": typeVersion,
        "position": position,
    }
    n.update(extra)
    return n


def to(*targets):
    """One output slot: a list of {node,type,index} connection targets."""
    return [{"node": t, "type": "main", "index": 0} for t in targets]


def http_node(name, pos, method, path, *, json_body=None, query=None, timeout=30000,
              retry=False, on_error=None):
    params = {
        "method": method,
        "url": f"{API}{path}",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "options": {"timeout": timeout},
    }
    if json_body is not None:
        params.update({"sendBody": True, "specifyBody": "json", "jsonBody": json_body})
    if query is not None:
        params.update({
            "sendQuery": True,
            "queryParameters": {"parameters": [{"name": k, "value": v} for k, v in query.items()]},
        })
    extra = {"credentials": HEADER_CRED}
    if retry:
        extra.update({"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000})
    if on_error:
        extra["onError"] = on_error
    return node(name, "n8n-nodes-base.httpRequest", 4.2, pos, params, **extra)


def telegram_node(name, pos, chat_expr, text_expr, *, buttons=None, force_reply=False,
                  reply_to=None):
    params = {
        "resource": "message",
        "operation": "sendMessage",
        "chatId": chat_expr,
        "text": text_expr,
        "additionalFields": {},
    }
    if buttons:
        params["replyMarkup"] = "inlineKeyboard"
        params["inlineKeyboard"] = {"rows": [{"row": {"buttons": buttons}}]}
    if force_reply:
        params["replyMarkup"] = "forceReply"
        params["forceReply"] = {"forceReply": True}
    if reply_to is not None:
        params["additionalFields"]["reply_to_message_id"] = reply_to
    return node(name, "n8n-nodes-base.telegram", 1.2, pos, params, credentials=TG_CRED)


def switch_rules(*keys):
    values = []
    for k in keys:
        values.append({
            "conditions": {
                "options": {"caseSensitive": True, "typeValidation": "loose"},
                "conditions": [{
                    "leftValue": "={{ $json.action }}",
                    "rightValue": k,
                    "operator": {"type": "string", "operation": "equals"},
                }],
                "combinator": "and",
            },
            "renameOutput": True,
            "outputKey": k,
        })
    return {"mode": "rules", "rules": {"values": values}, "options": {}}


def wf(name, nodes, connections):
    return {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": {"executionOrder": "v1"},
        "active": False,
    }


# ============================================================ WF1 inbound DM

NORMALIZE_JS = r"""
// Normalise an inbound DM to the /v1/route contract. Content is NOT inspected —
// this only shapes fields and derives a stable message_id for idempotency.
const crypto = require('crypto');
const src = $input.first().json;
const body = src.body || src;                    // webhook payload lives under .body
const handle = String(body.handle || '').replace(/^@/, '');
const text = String(body.text || '');
let messageId = String(body.message_id || '');
if (!messageId) {
  messageId = crypto.createHash('sha256').update(handle + text).digest('hex').slice(0, 16);
}
return [{ json: { message_id: messageId, handle, text, channel: 'instagram' } }];
""".strip()

SEND_PLACEHOLDER_JS = r"""
// PLACEHOLDER for the real Instagram "send DM" node. Swap this out for the
// channel node later — README marks this seam. For now it just logs.
const d = $input.first().json;
console.log('PLACEHOLDER SEND (instagram):', JSON.stringify({
  handle: d.handle, reply_to_send: d.reply_to_send, reply_kind: d.reply_kind,
}));
return [{ json: d }];
""".strip()

# Notification body/keyboard read from the Route node so downstream HTTP calls
# (whose responses don't carry the decision) can't drop the fields.
Q = "$('Route').item.json"
NOTIFY_TEXT = (
    "={{ (" + Q + ".queued.urgency === 'high' ? '‼️ URGENT\\n' : '') "
    "+ '\U0001fab4 ' + " + Q + ".queued.type + ' — @' + " + Q + ".handle + '\\n' "
    "+ " + Q + ".queued.summary + '\\n\\nNotes: ' + " + Q + ".queued.notes "
    "+ (" + Q + ".queued.draft_reply ? '\\n\\nDraft:\\n' + " + Q + ".queued.draft_reply.slice(0,300) : '') }}"
)
NOTIFY_PLAIN_TEXT = (
    "={{ (" + Q + ".queued.urgency === 'high' ? '‼️ URGENT\\n' : '') "
    "+ '\U0001fab4 ' + " + Q + ".queued.type + ' — @' + " + Q + ".handle + '\\n' "
    "+ " + Q + ".queued.summary + '\\n\\nNotes: ' + " + Q + ".queued.notes "
    "+ '\\n\\n(No draft — yours to write, Sofia.)' }}"
)
QID = "={{ '%s:' + " + Q + ".queued.id }}"
APPROVE_BUTTONS = [
    {"text": "✅ Approve", "additionalFields": {"callback_data": QID % "app"}},
    {"text": "✏️ Edit", "additionalFields": {"callback_data": QID % "edit"}},
    {"text": "❌ Reject", "additionalFields": {"callback_data": QID % "rej"}},
]

wf1_nodes = [
    node("Webhook", "n8n-nodes-base.webhook", 2, [0, 300], {
        "httpMethod": "POST", "path": "rooted-dm", "responseMode": "responseNode", "options": {},
    }, webhookId="rooted-dm"),
    node("Respond to Webhook", "n8n-nodes-base.respondToWebhook", 1.1, [220, 140], {
        "respondWith": "json", "responseBody": "={{ { \"ok\": true } }}", "options": {},
    }),
    node("Normalize", "n8n-nodes-base.code", 2, [220, 420],
         {"language": "javaScript", "jsCode": NORMALIZE_JS}),
    http_node("Route", [440, 420], "POST", "/v1/route",
              json_body="={{ { \"message_id\": $json.message_id, \"handle\": $json.handle, "
                        "\"text\": $json.text, \"channel\": $json.channel } }}",
              timeout=180000, retry=True, on_error="continueErrorOutput"),
    telegram_node("Route Failed Alert", [660, 620], "={{ $env.OPERATOR_CHAT_ID }}",
                  "=⚠️ route failed — raw DM attached, nothing was sent.\\n"
                  "{{ JSON.stringify($('Normalize').item.json) }}"),
    node("Switch", "n8n-nodes-base.switch", 3, [660, 400],
         switch_rules("auto_send", "ack_and_queue", "queue", "duplicate")),
    node("Send Care", "n8n-nodes-base.code", 2, [900, 120],
         {"language": "javaScript", "jsCode": SEND_PLACEHOLDER_JS}),
    http_node("Confirm Sent Care", [1120, 120], "POST", "/v1/sent",
              json_body="={{ { \"message_id\": " + Q + ".message_id, \"channel\": \"instagram\", "
                        "\"ok\": true, \"detail\": \"care_reply\" } }}"),
    node("End OK", "n8n-nodes-base.noOp", 1, [1340, 120], {}),
    node("Send Ack", "n8n-nodes-base.code", 2, [900, 300],
         {"language": "javaScript", "jsCode": SEND_PLACEHOLDER_JS}),
    http_node("Confirm Sent Ack", [1120, 300], "POST", "/v1/sent",
              json_body="={{ { \"message_id\": " + Q + ".message_id, \"channel\": \"instagram\", "
                        "\"ok\": true, \"detail\": \"holding_ack\" } }}"),
    node("Has Draft?", "n8n-nodes-base.if", 2, [900, 480], {
        "conditions": {
            "options": {"caseSensitive": True, "typeValidation": "loose"},
            "conditions": [{
                "leftValue": "={{ " + Q + ".queued.has_draft }}",
                "rightValue": True,
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "options": {},
    }),
    telegram_node("Notify Approve", [1120, 420], "={{ $env.SOFIA_CHAT_ID }}", NOTIFY_TEXT,
                  buttons=APPROVE_BUTTONS),
    telegram_node("Notify Plain", [1120, 600], "={{ $env.SOFIA_CHAT_ID }}", NOTIFY_PLAIN_TEXT),
    node("Duplicate End", "n8n-nodes-base.noOp", 1, [900, 680], {}),
]

wf1_conn = {
    "Webhook": {"main": [to("Respond to Webhook", "Normalize")]},
    "Normalize": {"main": [to("Route")]},
    "Route": {"main": [to("Switch"), to("Route Failed Alert")]},
    "Switch": {"main": [to("Send Care"), to("Send Ack"), to("Has Draft?"), to("Duplicate End")]},
    "Send Care": {"main": [to("Confirm Sent Care")]},
    "Confirm Sent Care": {"main": [to("End OK")]},
    "Send Ack": {"main": [to("Confirm Sent Ack")]},
    "Confirm Sent Ack": {"main": [to("Has Draft?")]},
    "Has Draft?": {"main": [to("Notify Approve"), to("Notify Plain")]},
}

WF1 = wf("01 Inbound DM", wf1_nodes, wf1_conn)


# ==================================================== WF2 approvals (Telegram)

DISPATCH_JS = r"""
// Demo-grade conversational state for Telegram approvals. Reads ONLY callback
// data and reply-chain prompts (which embed <qid> and the step) — never message
// intent/sentiment. The reply-to chain carries the two-step edit state.
const u = $input.first().json;
const cq = u.callback_query;
const msg = u.message;

function out(o) { return [{ json: o }]; }

if (cq && cq.data) {
  const [op, qid] = cq.data.split(':');
  if (op === 'app')  return out({ kind: 'approve',      qid, cb_id: cq.id, chat: cq.message.chat.id, msg_id: cq.message.message_id });
  if (op === 'rej')  return out({ kind: 'reject_prompt', qid, cb_id: cq.id, chat: cq.message.chat.id });
  if (op === 'edit') return out({ kind: 'edit_prompt',   qid, cb_id: cq.id, chat: cq.message.chat.id });
  return out({ kind: 'ignore' });
}

if (msg && msg.reply_to_message && typeof msg.reply_to_message.text === 'string') {
  const prompt = msg.reply_to_message.text;
  const chat = msg.chat.id;
  let m;
  if ((m = prompt.match(/^Reason for rejecting (\S+)\?/)))
    return out({ kind: 'reject_submit', qid: m[1], reason: msg.text, chat });
  if ((m = prompt.match(/^Replacement text for (\S+):/)))
    return out({ kind: 'edit_text_prompt', qid: m[1], edited_text: msg.text, chat });
  if ((m = prompt.match(/^Reason for editing (\S+)\?/))) {
    // the edited text lives one hop up the reply chain (the message we quoted)
    const edited = (msg.reply_to_message.reply_to_message && msg.reply_to_message.reply_to_message.text) || '';
    return out({ kind: 'edit_submit', qid: m[1], reason: msg.text, edited_text: edited, chat });
  }
}
return out({ kind: 'ignore' });
""".strip()

wf2_nodes = [
    node("Telegram Trigger", "n8n-nodes-base.telegramTrigger", 1.1, [0, 400], {
        "updates": ["callback_query", "message"], "additionalFields": {},
    }, credentials=TG_CRED, webhookId="rooted-approvals"),
    node("Dispatch", "n8n-nodes-base.code", 2, [220, 400],
         {"language": "javaScript", "jsCode": DISPATCH_JS}),
    node("Route Callback", "n8n-nodes-base.switch", 3, [440, 400], {
        "mode": "rules",
        "rules": {"values": [
            {"conditions": {"options": {"caseSensitive": True, "typeValidation": "loose"},
                            "conditions": [{"leftValue": "={{ $json.kind }}", "rightValue": k,
                                            "operator": {"type": "string", "operation": "equals"}}],
                            "combinator": "and"},
             "renameOutput": True, "outputKey": k}
            for k in ["approve", "reject_prompt", "edit_prompt",
                      "reject_submit", "edit_text_prompt", "edit_submit"]
        ]},
        "options": {"fallbackOutput": "extra"},
    }),
    # approve
    http_node("Approve", [700, 40], "POST", "/v1/approve",
              json_body="={{ { \"queue_id\": $json.qid, \"decision\": \"approve\" } }}",
              on_error="continueErrorOutput"),
    node("Send Approved", "n8n-nodes-base.code", 2, [920, 0],
         {"language": "javaScript", "jsCode": SEND_PLACEHOLDER_JS}),
    http_node("Confirm Approved Sent", [1140, 0], "POST", "/v1/sent",
              json_body="={{ { \"queue_id\": $('Dispatch').item.json.qid, \"channel\": \"instagram\", "
                        "\"ok\": true, \"detail\": \"approved\" } }}"),
    telegram_node("Approved Confirm", [1360, 0], "={{ $('Dispatch').item.json.chat }}",
                  "=✅ Approved & sent ({{ $('Dispatch').item.json.qid }})."),
    telegram_node("Approve Error", [920, 160], "={{ $('Dispatch').item.json.chat }}",
                  "=⚠️ Can't approve {{ $('Dispatch').item.json.qid }}: "
                  "{{ $json.error || 'no draft — use Edit to write your own.' }}"),
    # reject prompt / submit
    telegram_node("Reject Prompt", [700, 240], "={{ $json.chat }}",
                  "=Reason for rejecting {{ $json.qid }}?", force_reply=True),
    http_node("Reject Submit", [700, 360], "POST", "/v1/approve",
              json_body="={{ { \"queue_id\": $json.qid, \"decision\": \"reject\", "
                        "\"reason\": $json.reason } }}"),
    telegram_node("Reject Confirm", [920, 360], "={{ $('Dispatch').item.json.chat }}",
                  "=❌ Rejected {{ $('Dispatch').item.json.qid }}."),
    # edit two-step
    telegram_node("Edit Prompt", [700, 480], "={{ $json.chat }}",
                  "=Replacement text for {{ $json.qid }}:", force_reply=True),
    telegram_node("Edit Reason Prompt", [700, 600], "={{ $json.chat }}",
                  "=Reason for editing {{ $json.qid }}? (text saved)", force_reply=True),
    http_node("Edit Submit", [700, 720], "POST", "/v1/approve",
              json_body="={{ { \"queue_id\": $json.qid, \"decision\": \"edit\", "
                        "\"edited_text\": $json.edited_text, \"reason\": $json.reason } }}"),
    node("Edit Send", "n8n-nodes-base.code", 2, [920, 720],
         {"language": "javaScript", "jsCode": SEND_PLACEHOLDER_JS}),
    http_node("Confirm Edit Sent", [1140, 720], "POST", "/v1/sent",
              json_body="={{ { \"queue_id\": $('Dispatch').item.json.qid, \"channel\": \"instagram\", "
                        "\"ok\": true, \"detail\": \"approved_with_edits\" } }}"),
    telegram_node("Edit Confirm", [1360, 720], "={{ $('Dispatch').item.json.chat }}",
                  "=✏️ Edited & sent ({{ $('Dispatch').item.json.qid }})."),
    node("Ignore", "n8n-nodes-base.noOp", 1, [700, 860], {}),
]

wf2_conn = {
    "Telegram Trigger": {"main": [to("Dispatch")]},
    "Dispatch": {"main": [to("Route Callback")]},
    "Route Callback": {"main": [
        to("Approve"), to("Reject Prompt"), to("Edit Prompt"),
        to("Reject Submit"), to("Edit Reason Prompt"), to("Edit Submit"), to("Ignore"),
    ]},
    "Approve": {"main": [to("Send Approved"), to("Approve Error")]},
    "Send Approved": {"main": [to("Confirm Approved Sent")]},
    "Confirm Approved Sent": {"main": [to("Approved Confirm")]},
    "Reject Submit": {"main": [to("Reject Confirm")]},
    "Edit Submit": {"main": [to("Edit Send")]},
    "Edit Send": {"main": [to("Confirm Edit Sent")]},
    "Confirm Edit Sent": {"main": [to("Edit Confirm")]},
}

WF2 = wf("02 Approvals (Telegram)", wf2_nodes, wf2_conn)


# ======================================================= WF3 eval sentinel

ALERT_EVAL_JS = r"""
// Format the eval result for the alert. Reads only scores/dangerous counts.
const r = $('Run Eval').item.json;
const s = r.scores || {};
const d = r.dangerous || {};
const line = (k, v) => v ? `${k} ${v[0]}/${v[1]}` : `${k} n/a`;
const text =
  `\u{1F6A8} Eval gate: ${r.gate || 'ERROR'} (exit ${r.exit_code ?? '?'})\n` +
  [line('intent', s.intent), line('lane', s.lane), line('pet-recall', s.pet_recall),
   line('draft', s.draft_fixtures)].join('  ') + '\n' +
  `dangerous: UNSAFE_AUTO_SEND ${d.UNSAFE_AUTO_SEND ?? '?'}, ` +
  `MISSED_SAFETY ${d.MISSED_SAFETY ?? '?'}, UNVALIDATED_DRAFT ${d.UNVALIDATED_DRAFT ?? '?'}\n` +
  'Auto-send suspended — re-enable manually after review.';
return [{ json: { text } }];
""".strip()

wf3_nodes = [
    node("Schedule 07:30", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 300], {
        "rule": {"interval": [{"field": "days", "triggerAtHour": 7, "triggerAtMinute": 30}]},
    }),
    node("Decide Cache", "n8n-nodes-base.code", 2, [220, 300], {
        "language": "javaScript",
        "jsCode": "// Monday forces a fresh (cache-off) run — a weekly live drift check.\n"
                  "return [{ json: { no_cache: new Date().getDay() === 1 } }];",
    }),
    http_node("Run Eval", [440, 300], "POST", "/v1/eval",
              json_body="={{ { \"no_cache\": $json.no_cache } }}",
              timeout=1200000, on_error="continueErrorOutput"),
    node("Gate Not PASS?", "n8n-nodes-base.if", 2, [660, 200], {
        "conditions": {
            "options": {"caseSensitive": True, "typeValidation": "loose"},
            "conditions": [{
                "leftValue": "={{ $json.gate }}", "rightValue": "PASS",
                "operator": {"type": "string", "operation": "notEquals"},
            }],
            "combinator": "and",
        },
        "options": {},
    }),
    http_node("Suspend Auto-send", [900, 320], "POST", "/v1/config",
              json_body="={{ { \"auto_send_enabled\": false, \"reason\": \"eval gate FAIL\" } }}"),
    node("Format Alert", "n8n-nodes-base.code", 2, [1120, 320],
         {"language": "javaScript", "jsCode": ALERT_EVAL_JS}),
    telegram_node("Alert Sofia", [1340, 240], "={{ $env.SOFIA_CHAT_ID }}", "={{ $json.text }}"),
    telegram_node("Alert Operator", [1340, 400], "={{ $env.OPERATOR_CHAT_ID }}", "={{ $json.text }}"),
    node("Quiet Log", "n8n-nodes-base.noOp", 1, [900, 100], {}),
]

wf3_conn = {
    "Schedule 07:30": {"main": [to("Decide Cache")]},
    "Decide Cache": {"main": [to("Run Eval")]},
    "Run Eval": {"main": [to("Gate Not PASS?"), to("Suspend Auto-send")]},
    "Gate Not PASS?": {"main": [to("Suspend Auto-send"), to("Quiet Log")]},
    "Suspend Auto-send": {"main": [to("Format Alert")]},
    "Format Alert": {"main": [to("Alert Sofia", "Alert Operator")]},
}

WF3 = wf("03 Eval Sentinel", wf3_nodes, wf3_conn)


# ======================================================= WF4 daily digest

DIGEST_JS = r"""
// Format the pending queue into a digest. Reads counts + ids/types/handles only.
const r = $input.first().json;
const items = r.items || [];
const counts = r.counts || { pending: 0, high: 0 };
let text;
if (items.length === 0) {
  text = 'Queue is empty. Go water something. \u{1F33F}';
} else {
  const urgent = counts.high ? ` (${counts.high} ‼️)` : '';
  const lines = items.slice(0, 10).map(i =>
    `${i.urgency === 'high' ? '‼️ ' : ''}${i.id} ${i.type} @${i.handle}`);
  text = `\u{1FAB4} ${counts.pending} waiting${urgent}:\n` + lines.join('\n');
}
return [{ json: { text } }];
""".strip()

wf4_nodes = [
    node("Schedule 09:00", "n8n-nodes-base.scheduleTrigger", 1.2, [0, 300], {
        "rule": {"interval": [{"field": "days", "triggerAtHour": 9, "triggerAtMinute": 0}]},
    }),
    http_node("Get Pending Queue", [220, 300], "GET", "/v1/queue", query={"status": "pending"}),
    node("Format Digest", "n8n-nodes-base.code", 2, [440, 300],
         {"language": "javaScript", "jsCode": DIGEST_JS}),
    telegram_node("Digest to Sofia", [660, 300], "={{ $env.SOFIA_CHAT_ID }}", "={{ $json.text }}"),
]

wf4_conn = {
    "Schedule 09:00": {"main": [to("Get Pending Queue")]},
    "Get Pending Queue": {"main": [to("Format Digest")]},
    "Format Digest": {"main": [to("Digest to Sofia")]},
}

WF4 = wf("04 Daily Digest", wf4_nodes, wf4_conn)


# ============================================ WF5 Telegram demo (customer round-trip)
# A self-contained demo of the SAME frozen brain: you DM the bot as if you were a
# customer, and the AI's customer-facing reply comes straight back to that chat —
# the auto_send care reply, or the ack_and_queue holding ack (the hand-off
# message). Telegram stands in for the Instagram customer channel so the whole
# loop is visible in one window. Same boundary rule as WF1: it switches ONLY on
# decision.action and relays brain-authored text; it never inspects the DM.
#
# Runs on its own bot webhook, so activate this ALONE (Telegram allows one
# webhook consumer per bot token — it can't be active alongside WF2 on the same
# bot). Needs no SOFIA_CHAT_ID: it replies to the sender's own chat.

DEMO_SHAPE_JS = r"""
// Shape an inbound Telegram DM into the /v1/route contract. Content is NOT
// inspected — this only maps Telegram fields and derives a stable message_id.
// The frozen brain makes every judgement; this workflow just relays its
// customer-facing reply back to the same chat.
const u = $input.first().json;
const msg = u.message || {};
const from = msg.from || {};
const chat = msg.chat || {};
const text = typeof msg.text === 'string' ? msg.text.trim() : '';

// handle: the Telegram username if it fits the API's handle rule, else tg<id>.
let handle = String(from.username || '').replace(/^@/, '');
if (!/^[A-Za-z0-9._]{1,64}$/.test(handle)) handle = 'tg' + (from.id || 'anon');

// message_id: unique per Telegram message, sanitised to the API's id rule.
const messageId = ('tg-' + (chat.id ?? 'x') + '-' + (msg.message_id ?? 'x'))
  .replace(/[^A-Za-z0-9._-]/g, '').slice(0, 64);

return [{ json: {
  message_id: messageId, handle, text, channel: 'telegram',
  chat_id: chat.id, has_text: text.length > 0,
} }];
""".strip()

DEMO_CHAT = "={{ $('Shape DM').item.json.chat_id }}"
# queue lane has no customer-facing reply (Sofia writes it) — relay the brain's
# own summary of what it queued, so the sender still sees the decision.
DEMO_QUEUED_TEXT = (
    "=🌿 Thanks — I've passed this straight to Sofia to answer personally; "
    "she'll follow up. (Routed as {{ $json.queued.type }} · "
    "{{ $json.queued.urgency }}: {{ $json.queued.summary }})"
)

wf5_nodes = [
    node("Telegram Trigger (Demo)", "n8n-nodes-base.telegramTrigger", 1.1, [0, 300], {
        "updates": ["message"], "additionalFields": {},
    }, credentials=TG_CRED, webhookId="rooted-demo"),
    node("Shape DM", "n8n-nodes-base.code", 2, [220, 300],
         {"language": "javaScript", "jsCode": DEMO_SHAPE_JS}),
    node("Has Text?", "n8n-nodes-base.if", 2, [440, 300], {
        "conditions": {
            "options": {"caseSensitive": True, "typeValidation": "loose"},
            "conditions": [{
                "leftValue": "={{ $json.has_text }}",
                "rightValue": True,
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "options": {},
    }),
    telegram_node("Ask For Text", [660, 480], DEMO_CHAT,
                  "👋 Send me a text message — a customer DM to Rooted — and I'll show "
                  "you how the autopilot handles it."),
    http_node("Route (Demo)", [660, 200], "POST", "/v1/route",
              json_body="={{ { \"message_id\": $json.message_id, \"handle\": $json.handle, "
                        "\"text\": $json.text, \"channel\": $json.channel } }}",
              timeout=180000, retry=True, on_error="continueErrorOutput"),
    telegram_node("Route Failed (Demo)", [880, 380], DEMO_CHAT,
                  "⚠️ Couldn't route that one — is ANTHROPIC_API_KEY set and the API healthy?"),
    node("Switch (Demo)", "n8n-nodes-base.switch", 3, [880, 180],
         switch_rules("auto_send", "ack_and_queue", "queue")),
    telegram_node("Reply Care", [1120, 40], DEMO_CHAT, "={{ $json.reply_to_send }}"),
    telegram_node("Reply Handoff Ack", [1120, 200], DEMO_CHAT, "={{ $json.reply_to_send }}"),
    telegram_node("Reply Queued", [1120, 360], DEMO_CHAT, DEMO_QUEUED_TEXT),
]

wf5_conn = {
    "Telegram Trigger (Demo)": {"main": [to("Shape DM")]},
    "Shape DM": {"main": [to("Has Text?")]},
    "Has Text?": {"main": [to("Route (Demo)"), to("Ask For Text")]},
    "Route (Demo)": {"main": [to("Switch (Demo)"), to("Route Failed (Demo)")]},
    "Switch (Demo)": {"main": [to("Reply Care"), to("Reply Handoff Ack"), to("Reply Queued")]},
}

WF5 = wf("05 Telegram Demo", wf5_nodes, wf5_conn)


# ============================================================== validate + write

WORKFLOWS = {
    "01-inbound-dm.json": WF1,
    "02-approvals-telegram.json": WF2,
    "03-eval-sentinel.json": WF3,
    "04-daily-digest.json": WF4,
    "05-telegram-demo.json": WF5,
}


def validate(w: dict) -> None:
    names = [n["name"] for n in w["nodes"]]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"{w['name']}: duplicate node names {dupes}"
    nameset = set(names)
    for src, conn in w["connections"].items():
        assert src in nameset, f"{w['name']}: connection source {src!r} is not a node"
        for slot in conn.get("main", []):
            for link in slot:
                assert link["node"] in nameset, \
                    f"{w['name']}: connection target {link['node']!r} (from {src!r}) is not a node"
    # every node except triggers should be reachable from some connection
    targets = {l["node"] for c in w["connections"].values() for slot in c["main"] for l in slot}
    sources = set(w["connections"].keys())
    for n in w["nodes"]:
        is_trigger = n["type"].endswith(("Trigger", "webhook"))
        if not is_trigger and n["name"] not in targets:
            raise AssertionError(f"{w['name']}: node {n['name']!r} is unreachable")
        if n["name"] not in sources and n["name"] not in targets and not is_trigger:
            raise AssertionError(f"{w['name']}: node {n['name']!r} is disconnected")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for fname, w in WORKFLOWS.items():
        validate(w)
        text = json.dumps(w, indent=2, ensure_ascii=False)
        json.loads(text)  # round-trip: must be valid JSON
        (OUT / fname).write_text(text + "\n", encoding="utf-8")
        print(f"  OK  {fname:<28} {len(w['nodes'])} nodes, "
              f"{sum(len(s) for c in w['connections'].values() for s in c['main'])} wires")
    print(f"all {len(WORKFLOWS)} workflows: valid JSON, unique node names, every wire resolves")


if __name__ == "__main__":
    main()
