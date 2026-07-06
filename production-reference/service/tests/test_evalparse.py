"""Unit tests for evalrunner.parse — a pure function of the eval stdout.

The sample below is the scores block captured verbatim from the repo's
slice/PROOF.txt (a real `python3 autopilot.py eval --no-cache` run). No subprocess
is invoked here; the live eval path is exercised only by POST /v1/eval when a
human asks for it.
"""

from service import evalrunner

# Captured from slice/PROOF.txt (§ "3) python autopilot.py eval --no-cache").
SAMPLE = """\
  draft gate fixtures  12/12

  Scores
  ----------------------------------------
  intent accuracy      26/27  (96%)
  plant accuracy       16/16  (100%)   (scored where a plant is expected)
  lane accuracy        25/27  (93%)
  pet-safety recall    5/5  (100%)   <- must be 100%: never miss a safety case
  pet-safety precision 5/6  (83%)   (over-flagging is the safe direction)

  Errors by real-world risk
  ----------------------------------------
  UNSAFE_AUTO_SEND   0   (dangerous — model facts sent with no human)
  MISSED_SAFETY      0   (dangerous — safety case not escalated)
  UNVALIDATED_DRAFT  0   (dangerous — the auto-send draft gate disagreed with a labelled fixture)
  safe_escalation    1   (acceptable — sent to a human unnecessarily)
  misroute           1   (quality — wrong human lane, still reviewed)

  ====================================================
  QUALITY GATE: PASS ✅   (0 dangerous error(s); gate blocks only those)
  ====================================================
"""


def test_parse_scores():
    parsed = evalrunner.parse(SAMPLE)
    assert parsed["scores"] == {
        "intent": [26, 27],
        "plant": [16, 16],
        "lane": [25, 27],
        "pet_recall": [5, 5],
        "draft_fixtures": [12, 12],
    }


def test_parse_dangerous_counts():
    parsed = evalrunner.parse(SAMPLE)
    assert parsed["dangerous"] == {
        "UNSAFE_AUTO_SEND": 0,
        "MISSED_SAFETY": 0,
        "UNVALIDATED_DRAFT": 0,
    }


def test_parse_stdout_tail_is_last_40_lines():
    parsed = evalrunner.parse(SAMPLE)
    assert parsed["stdout_tail"].splitlines() == SAMPLE.splitlines()[-40:]


def test_parse_detects_dangerous_failure():
    bad = SAMPLE.replace("UNSAFE_AUTO_SEND   0", "UNSAFE_AUTO_SEND   2")
    assert evalrunner.parse(bad)["dangerous"]["UNSAFE_AUTO_SEND"] == 2


def test_parse_raises_on_garbage():
    import pytest
    with pytest.raises(ValueError):
        evalrunner.parse("nothing useful here")
