"""Environment and paths for rooted-api.

Nothing here imports the brain — adapter.py owns that. These are just the
knobs the rest of the service reads. Paths are resolved once from this file's
location so the service works the same whether launched from the repo root, from
``service/``, or inside the container.
"""

import os
from pathlib import Path

# This layer's root = parent of the ``service`` package directory.
ROOT = Path(__file__).resolve().parent.parent

# The brain: the working slice that ships in THIS repository, one directory up.
# Nothing is vendored and no SHA is pinned — the service and the brain are the
# same commit by construction, so a history rewrite can never orphan a pin.
# Override for other layouts (the Docker bind mount sets /app/slice).
SLICE_DIR = Path(os.environ.get("ROOTED_SLICE_DIR", str(ROOT.parent / "slice")))

# Runtime state. Gitignored, auto-created. Overridable so tests can point it at a
# tmp dir (see adapter.configure_data_dir).
DATA_DIR = Path(os.environ.get("ROOTED_DATA_DIR", str(ROOT / "service" / "data")))

# Shared header secret for every /v1/* endpoint (constant-time compared).
ROOTED_API_KEY = os.environ.get("ROOTED_API_KEY", "")

# Model + clock knobs passed through to the frozen brain / eval subprocess.
AUTOPILOT_MODEL = os.environ.get("AUTOPILOT_MODEL", "claude-opus-4-8")
LIVE_CLOCK = os.environ.get("LIVE_CLOCK", "0")


def anthropic_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
