"""Thin Claude adapter.

Uses the Anthropic SDK when ANTHROPIC_API_KEY is set; otherwise shells out to
the locally-authenticated Claude Code CLI (`claude -p`). Either way the
contract is the same: system + user text in, plain text out.
"""

import json
import os
import re
import shutil
import subprocess

MODEL = os.environ.get("AUTOPILOT_MODEL", "claude-opus-4-8")


def call_claude(system: str, user: str) -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _via_sdk(system, user)
    if shutil.which("claude"):
        return _via_cli(system, user)
    raise RuntimeError(
        "No Claude access found. Set ANTHROPIC_API_KEY or install Claude Code (claude CLI)."
    )


def _via_sdk(system: str, user: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def _via_cli(system: str, user: str) -> str:
    # --system-prompt REPLACES Claude Code's own system prompt, so our
    # "return ONLY the reply text" instruction no longer competes with the
    # CLI's default persona (which is what leaked meta-commentary into sends).
    result = subprocess.run(
        ["claude", "-p", "--system-prompt", system,
         "--output-format", "json", "--model", MODEL],
        input=user,
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:500]}")
    return json.loads(result.stdout)["result"]


def extract_json(text: str) -> dict:
    """Lenient JSON extraction: tolerates code fences and surrounding prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object in model output: {text[:200]}")
    return json.loads(text[start : end + 1])
