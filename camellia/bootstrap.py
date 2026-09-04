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

import hashlib
import json
import os
import shutil
import time
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


def seed_fingerprint(path: Path) -> str | None:
    """
    A hash of the templates a seed file carries, ignoring formatting.

    Used to tell two very different situations apart once the file is live:
    the operator editing a template through /admin (expected), versus the
    repository's seed having moved on without anyone delivering it (the silent
    staleness this whole mechanism exists to catch).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    payload = json.dumps(data.get("templates"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _seed_version(path: Path) -> int | None:
    """The seed_version a file declares, or None when it predates the field."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f).get("seed_version")
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _stamp(target: Path, seed: Path) -> None:
    """
    Records which seed the live file came from, without disturbing content.

    /admin/templates rewrites this file by loading it, replacing the templates
    list and dumping it again, so both keys survive an operator's edits — which
    is what lets /health distinguish those edits from an undelivered seed.
    """
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["seed_version"] = _seed_version(seed)
        data["seeded_from_hash"] = seed_fingerprint(seed)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


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
                # Reconcile rather than skip. The old behaviour — seed once,
                # never again — meant an edited template in the repository was
                # a first-boot seed and nothing more: it was committed, pushed,
                # deployed, and customers carried on reading the original file
                # with no error anywhere. A confirmed bento price sat undelivered
                # that way for two days.
                #
                # Only a *higher* seed_version replaces the live file, so an
                # ordinary deploy leaves /admin edits alone; delivering a
                # repository change is the deliberate act of bumping it.
                #
                # Files whose seed declares no seed_version are never touched.
                # menu.json is one of them, and must stay that way: it is
                # rewritten at runtime by menu uploads, so reconciling it would
                # throw away the week the kitchen just posted.
                seed_v = _seed_version(seed)
                if seed_v is None:
                    continue
                live_v = _seed_version(target)
                if live_v is not None and live_v >= seed_v:
                    continue
                backup = target.with_name(f"{target.stem}.superseded-{int(time.time())}{target.suffix}")
                try:
                    shutil.copyfile(target, backup)
                except Exception:
                    backup = None
                shutil.copyfile(seed, target)
                _stamp(target, seed)
                actions.append(
                    f"updated {env_var} at {target} from seed_version {live_v} to {seed_v}"
                    + (f" (previous copy kept at {backup.name})" if backup else "")
                )
                continue
            if not seed.exists():
                actions.append(f"no seed file at {seed} to populate {env_var}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(seed, target)
            _stamp(target, seed)
            actions.append(f"seeded {env_var} at {target} from {seed}")
        except Exception as exc:
            actions.append(f"could not seed {env_var} at {target}: {exc}")

    return actions


# Recorded rather than logged: logging is configured further down the import
# chain, so `camellia.app` reports these once it can.
STARTUP_ACTIONS = prepare_writable_storage()
