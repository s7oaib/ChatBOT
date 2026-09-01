"""
Prepares writable storage before any configuration or data is loaded.

The container filesystem does not survive a deploy. Anything the bot writes at
runtime — the SQLite database, a menu edited through /admin, a candidate answer
a human approved — is therefore lost on every restart unless it lives on a
mounted volume.

Pointing DB_PATH, MENU_JSON_PATH and QA_TEMPLATES_PATH at that volume is not
enough on its own, because a fresh volume is empty: the bot would start with no
menu and no templates and answer every customer with the fallback. The seed
copies that ship inside the image have to be placed there once, on first boot.

This runs at import time of `camellia.config`, which is earlier than it looks
like it needs to. `Config` resolves those paths in its class body, so by the
time any module holds a config value the decision is already made — and
`_resolve_path(must_exist=True)` quietly falls back to the seed file when the
configured path is missing, so seeding from `lifespan` would leave the whole
process reading the image's copy while believing it was on the volume.

Deliberately silent about failure. If a volume is read-only or absent, the
fallback in `_resolve_path` still yields a bot that answers from the image;
raising here would turn a durability problem into an outage.
"""

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Runtime path -> the copy baked into the image it should be seeded from.
_SEEDED_FILES = (
    ("MENU_JSON_PATH", DATA_DIR / "menu.json"),
    ("QA_TEMPLATES_PATH", DATA_DIR / "qa_templates.json"),
)

# Written by SQLite itself, so it needs a directory rather than a seed file.
_CREATED_DIRS = ("DB_PATH",)


def _configured(env_var: str) -> Path | None:
    """The path this environment sets, or None when it is left at the default."""
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def prepare_writable_storage() -> list[str]:
    """
    Creates parent directories and seeds missing runtime files.

    Returns a list of human-readable actions taken, for logging once a logger
    exists — this runs before logging is configured.
    """
    actions: list[str] = []

    for env_var in _CREATED_DIRS:
        target = _configured(env_var)
        if target is None:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            actions.append(f"could not create the directory for {env_var} ({target.parent}): {exc}")

    for env_var, seed in _SEEDED_FILES:
        target = _configured(env_var)
        # No override means the bot is already reading the image's own copy.
        if target is None or target == seed:
            continue
        try:
            if target.exists():
                continue
            if not seed.exists():
                actions.append(f"no seed file at {seed} to populate {env_var}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(seed, target)
            actions.append(f"seeded {env_var} at {target} from {seed}")
        except Exception as exc:
            actions.append(f"could not seed {env_var} at {target}: {exc}")

    return actions


# Recorded rather than logged: logging is configured further down the import
# chain, so `camellia.app` reports these once it can.
STARTUP_ACTIONS = prepare_writable_storage()
