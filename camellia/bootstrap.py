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
import tempfile
import time
from copy import deepcopy
from pathlib import Path

from camellia.content_integrity import is_known_legacy_subscription_response

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# Runtime path -> the copy baked into the image it should be seeded from.
_SEEDED_FILES = (
    ("MENU_JSON_PATH", DATA_DIR / "menu.json"),
    ("QA_TEMPLATES_PATH", DATA_DIR / "qa_templates.json"),
)

# Written by SQLite itself, so it needs a directory rather than a seed file.
_CREATED_DIRS = ("DB_PATH",)

# These are the only seed objects that may be structurally reconciled after a
# durable QA file already exists.  Everything else remains operator-owned.
REQUIRED_QA_TEMPLATE_IDS = (
    "bengali_home_table_details",
    "bengali_home_table_pricing",
    "bengali_home_table_payment",
)

_HOME_TABLE_BACKUP_SUFFIX = ".home-table-backfill"
_SUBSCRIPTION_TEMPLATE_ID = "subscription_details"
_OPENING_HOURS_TEMPLATE_ID = "opening_hours"
_SUBSCRIPTION_BACKUP_SUFFIX = ".subscription-template-backfill"

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


def missing_required_template_ids(
    path: Path | str,
    required_ids: tuple[str, ...] = REQUIRED_QA_TEMPLATE_IDS,
) -> tuple[str, ...]:
    """Return required QA template IDs absent from a JSON template file.

    A malformed or incomplete file is treated as missing every required ID;
    callers can then fail closed without making a destructive replacement.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        templates = data.get("templates")
        if not isinstance(templates, list):
            return tuple(required_ids)
        present = {str(item.get("id")) for item in templates if isinstance(item, dict)}
    except Exception:
        return tuple(required_ids)
    return tuple(template_id for template_id in required_ids if template_id not in present)


def required_template_ids_present(
    path: Path | str,
    required_ids: tuple[str, ...] = REQUIRED_QA_TEMPLATE_IDS,
) -> bool:
    """Whether every required QA template ID exists in the active file."""
    return not missing_required_template_ids(path, required_ids)


def _atomic_json_write(target: Path, data: dict) -> None:
    """Write a JSON file beside its target, then replace the target atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _backfill_required_qa_templates(target: Path, seed: Path) -> str:
    """Reconcile only approved structural/template drift in the QA file."""
    try:
        with open(seed, "r", encoding="utf-8") as f:
            seed_data = json.load(f)
        with open(target, "r", encoding="utf-8") as f:
            live_data = json.load(f)
        seed_templates = seed_data.get("templates")
        live_templates = live_data.get("templates")
        if not isinstance(seed_templates, list) or not isinstance(live_templates, list):
            return "error: template list is not a JSON array"
        seed_by_id = {
            str(item.get("id")): item
            for item in seed_templates
            if isinstance(item, dict) and item.get("id")
        }
        live_ids = {
            str(item.get("id"))
            for item in live_templates
            if isinstance(item, dict) and item.get("id")
        }
        seed_required_ids = REQUIRED_QA_TEMPLATE_IDS
        # Older isolated fixtures may intentionally contain only the original
        # structural requirements.  The shipped seed carries subscription_details,
        # and opening_hours; only then are those durable reconciliations enabled.
        for optional_template_id in (_SUBSCRIPTION_TEMPLATE_ID, _OPENING_HOURS_TEMPLATE_ID):
            if optional_template_id in seed_by_id:
                seed_required_ids = (*seed_required_ids, optional_template_id)
        missing_from_seed = tuple(
            template_id for template_id in seed_required_ids
            if template_id not in seed_by_id
        )
        if missing_from_seed:
            return "seed-missing:" + ", ".join(missing_from_seed)
        missing = tuple(
            template_id for template_id in seed_required_ids
            if template_id not in live_ids
        )
        reconciled_templates = list(live_templates)
        actions: list[str] = []
        for template_id in missing:
            reconciled_templates.append(deepcopy(seed_by_id[template_id]))
        if missing:
            actions.append("backfilled:" + ", ".join(missing))

        subscription_replaced = False
        if _SUBSCRIPTION_TEMPLATE_ID in live_ids:
            seed_subscription = seed_by_id[_SUBSCRIPTION_TEMPLATE_ID]
            for index, template in enumerate(reconciled_templates):
                if not isinstance(template, dict) or template.get("id") != _SUBSCRIPTION_TEMPLATE_ID:
                    continue
                if template == seed_subscription:
                    continue
                if is_known_legacy_subscription_response(template.get("response")):
                    reconciled_templates[index] = deepcopy(seed_subscription)
                    subscription_replaced = True
                else:
                    actions.append("skipped_operator_override:" + _SUBSCRIPTION_TEMPLATE_ID)
            if subscription_replaced:
                actions.append("replaced:" + _SUBSCRIPTION_TEMPLATE_ID)

        if not missing and not subscription_replaced:
            return ";".join(dict.fromkeys(actions)) or "unchanged"

        if missing:
            backup = target.with_name(target.stem + _HOME_TABLE_BACKUP_SUFFIX + target.suffix)
            if not backup.exists():
                shutil.copyfile(target, backup)
        if _SUBSCRIPTION_TEMPLATE_ID in missing or subscription_replaced:
            backup = target.with_name(target.stem + _SUBSCRIPTION_BACKUP_SUFFIX + target.suffix)
            if not backup.exists():
                shutil.copyfile(target, backup)

        live_data["templates"] = reconciled_templates
        _atomic_json_write(target, live_data)
        return ";".join(dict.fromkeys(actions))
    except Exception as exc:
        return f"error: {exc}"


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
                # A valid QA seed is required before any existing-file
                # replacement or structural repair.  This keeps an incomplete
                # seed from causing a destructive whole-file update.
                if env_var == "QA_TEMPLATES_PATH":
                    missing_seed = missing_required_template_ids(seed)
                    if missing_seed:
                        actions.append(
                            f"could not reconcile {env_var}; seed missing required templates: "
                            + ", ".join(missing_seed)
                        )
                        continue

                seed_v = _seed_version(seed)
                if seed_v is None:
                    continue
                live_v = _seed_version(target)
                if live_v is not None and live_v >= seed_v:
                    if env_var != "QA_TEMPLATES_PATH":
                        continue
                    result = _backfill_required_qa_templates(target, seed)
                    if result == "unchanged":
                        continue
                    for reconciliation in result.split(";"):
                        if reconciliation.startswith("backfilled:"):
                            actions.append(
                                f"backfilled {env_var} with required templates: "
                                + reconciliation.removeprefix("backfilled:")
                            )
                        elif reconciliation.startswith("replaced:"):
                            actions.append(
                                f"replaced {env_var} template: "
                                + reconciliation.removeprefix("replaced:")
                            )
                        elif reconciliation.startswith("skipped_operator_override:"):
                            actions.append(
                                f"preserved {env_var} operator override: "
                                + reconciliation.removeprefix("skipped_operator_override:")
                            )
                        else:
                            actions.append(f"could not reconcile {env_var}: {reconciliation}")
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
