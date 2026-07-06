#!/usr/bin/env bash
# LIVE smoke test: routes six sample DMs from the repo's slice/inbox through
# /v1/route and asserts each lands in the expected action/lane (from the brain's
# fact sheet). This makes REAL Opus calls (~12 total) and costs money — run it only
# when you mean to. Needs the stack up and a real ANTHROPIC_API_KEY in .env.
#
#   docker compose up -d          # (or run uvicorn on the host)
#   bash scripts/smoke_live.sh
set -euo pipefail

cd "$(dirname "$0")/.."
API="${API:-http://localhost:8000}"
KEY="${ROOTED_API_KEY:-$(grep -E '^ROOTED_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)}"
SLICE="${SLICE:-../slice}"

[ -n "${KEY:-}" ] || { echo "ROOTED_API_KEY not set (export it or put it in .env)"; exit 1; }
[ -d "$SLICE/inbox" ] || { echo "slice inbox not found at $SLICE/inbox — set SLICE to the repo's slice/ dir"; exit 1; }

# msg -> expected action / lane (PLAN §1.2)
EXPECT=(
  "msg_01 auto_send AUTO_SEND_CARE"
  "msg_02 queue URGENT_PET_SAFETY"
  "msg_03 queue REPLACEMENT"
  "msg_04 ack_and_queue NEEDS_SOFIA"
  "msg_05 queue PAUSE_OR_CANCEL"
  "msg_06 queue BILLING_DISPUTE"
)

echo "Smoke: routing 6 sample DMs through $API/v1/route (LIVE Opus calls)..."
echo
fails=0
for row in "${EXPECT[@]}"; do
  read -r msg exp_action exp_lane <<<"$row"
  got=$(python3 - "$SLICE/inbox/$msg.txt" "$msg" "$API" "$KEY" <<'PY'
import sys, json, urllib.request
path, msg, api, key = sys.argv[1:5]
raw = open(path, encoding="utf-8").read().strip()
lines = raw.splitlines()
handle = lines[0].replace("From:", "").strip().lstrip("@")
body = "\n".join(lines[1:]).strip()
data = json.dumps({"message_id": "smoke_" + msg, "handle": handle, "text": body}).encode()
req = urllib.request.Request(api + "/v1/route", data=data, method="POST",
                            headers={"X-Api-Key": key, "Content-Type": "application/json"})
try:
    resp = json.load(urllib.request.urlopen(req, timeout=300))
    print(resp.get("action", "?"), resp.get("lane", "?"))
except Exception as e:
    print("ERROR", str(e).replace("\n", " ")[:120])
PY
)
  got_action=${got%% *}; got_lane=${got##* }
  if [ "$got_action" = "$exp_action" ] && [ "$got_lane" = "$exp_lane" ]; then
    printf 'PASS  %-8s -> %s / %s\n' "$msg" "$got_action" "$got_lane"
  else
    printf 'FAIL  %-8s -> got %s / %s (expected %s / %s)\n' \
      "$msg" "$got_action" "$got_lane" "$exp_action" "$exp_lane"
    fails=$((fails + 1))
  fi
done

echo
if [ "$fails" -eq 0 ]; then
  echo "smoke: all 6 DMs routed as expected ✅"
else
  echo "smoke: $fails mismatch(es) ❌"; exit 1
fi
