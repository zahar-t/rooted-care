"""Eval subprocess wrapper + a pure stdout parser.

``parse()`` is a pure function of the eval stdout text (unit-tested against a
captured sample). ``run_eval()`` shells out to ``autopilot.py eval`` in the
repo's own slice and folds exit code + parsed scores into the API response.

The exit code is the authoritative gate (never the ✅ line): 0 = PASS, 1 = FAIL.
The first cold run makes ~27 live triage calls — never call this from pytest.
"""

import os
import re
import subprocess
import sys

from . import settings


def _pair(text: str, label: str) -> list[int]:
    m = re.search(rf"{re.escape(label)}\s+(\d+)\s*/\s*(\d+)", text)
    if not m:
        raise ValueError(f"could not parse {label!r} from eval output")
    return [int(m.group(1)), int(m.group(2))]


def _danger(text: str, label: str) -> int:
    m = re.search(rf"{re.escape(label)}\s+(\d+)", text)
    if not m:
        raise ValueError(f"could not parse {label!r} from eval output")
    return int(m.group(1))


def parse(stdout: str) -> dict:
    """Extract scores + dangerous-error counts from the eval stdout. Pure function."""
    scores = {
        "intent": _pair(stdout, "intent accuracy"),
        "plant": _pair(stdout, "plant accuracy"),
        "lane": _pair(stdout, "lane accuracy"),
        "pet_recall": _pair(stdout, "pet-safety recall"),
        "draft_fixtures": _pair(stdout, "draft gate fixtures"),
    }
    dangerous = {
        "UNSAFE_AUTO_SEND": _danger(stdout, "UNSAFE_AUTO_SEND"),
        "MISSED_SAFETY": _danger(stdout, "MISSED_SAFETY"),
        "UNVALIDATED_DRAFT": _danger(stdout, "UNVALIDATED_DRAFT"),
    }
    tail = "\n".join(stdout.splitlines()[-40:])
    return {"scores": scores, "dangerous": dangerous, "stdout_tail": tail}


def run_eval(no_cache: bool) -> dict:
    """Run the frozen eval subprocess and return the /v1/eval response body.

    LIVE: makes real Opus calls on a cold cache. Only invoked by POST /v1/eval.
    """
    cmd = [sys.executable, "autopilot.py", "eval"]
    if no_cache:
        cmd.append("--no-cache")
    env = os.environ.copy()  # passthrough ANTHROPIC_API_KEY, AUTOPILOT_MODEL
    proc = subprocess.run(
        cmd, cwd=str(settings.SLICE_DIR), env=env,
        capture_output=True, text=True, timeout=1200,
    )
    gate = "PASS" if proc.returncode == 0 else "FAIL"
    try:
        parsed = parse(proc.stdout)
    except ValueError:
        # Malformed/partial output (eval crashed). The gate still comes from the
        # exit code; the sentinel treats a non-PASS as a reason to suspend.
        combined = (proc.stdout + "\n" + proc.stderr).splitlines()
        parsed = {"scores": None, "dangerous": None, "stdout_tail": "\n".join(combined[-40:])}
    return {"gate": gate, "exit_code": proc.returncode, **parsed}
