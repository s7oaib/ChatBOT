"""Deterministic changed-file selection; pytest failures are never hidden."""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import test_impact_map as impact

ROOT = Path(__file__).resolve().parent.parent
RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


class SelectionError(ValueError):
    pass


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if result.returncode:
        # Do not echo Git stderr, which can contain credential-bearing remote URLs.
        raise SelectionError(f"git {args[0]} failed (exit {result.returncode}); use --full if baseline is unavailable")
    return result.stdout.decode("utf-8", errors="surrogateescape")


def resolve_commit(root, revision):
    if not isinstance(revision, str) or not revision or revision.startswith("-"):
        raise SelectionError("Baseline SHA/ref is invalid")
    return git(root, "rev-parse", "--verify", revision + "^{commit}").strip()


def read_baseline(root, override=None):
    try:
        data = json.loads((root / "test_baseline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SelectionError("Missing/invalid test_baseline.json; explicitly use --full") from exc
    if not isinstance(data, dict) or data.get("full_regression_passed") is not True:
        raise SelectionError("Baseline must record a passing full regression")
    count = data.get("full_regression_test_count")
    if type(count) is not int or count <= 0:
        raise SelectionError("Baseline test count must be a positive integer")
    frozen_sha = data.get("known_good_sha")
    if not isinstance(frozen_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", frozen_sha):
        raise SelectionError("Baseline must store an immutable full commit SHA, not a moving branch/ref")
    return resolve_commit(root, override if override is not None else frozen_sha)


def changed_files(root, baseline):
    # --no-renames includes BOTH old and new paths; deleted code still has risk.
    outputs = (
        git(root, "diff", "--name-only", "-z", "--no-renames", baseline + "...HEAD", "--"),
        git(root, "diff", "--name-only", "-z", "--no-renames", "--"),
        git(root, "diff", "--cached", "--name-only", "-z", "--no-renames", "--"),
        git(root, "ls-files", "--others", "--exclude-standard", "-z"),
    )
    return sorted({name for output in outputs for name in output.split("\0") if name})


def classify(path):
    path = path.replace("\\", "/")
    if path in impact.CORE_FILES or path.startswith(".github/workflows/"):
        return "CRITICAL", "core / test infrastructure", ()
    area = impact.FILE_AREAS.get(path)
    if area:
        return impact.AREA_RISK[area], area, impact.AREA_TESTS[area]
    if path.startswith("tests/") and Path(path).name.startswith("test_") and path.endswith(".py"):
        return "MEDIUM", "changed test", (path,)
    if path.startswith("tests/"):
        return "CRITICAL", "shared fixture / test data", ()
    # Documentation only: executable scripts under docs are NOT low risk.
    if path.endswith((".md", ".rst", ".txt")) and not Path(path).name.startswith("requirements"):
        if path.startswith("docs/") or Path(path).name.lower().startswith("readme"):
            return "LOW", "documentation", ()
    if path in impact.LOW_ROOT_FILES:
        return "LOW", "non-runtime metadata", ()
    return "CRITICAL", "unknown runtime/configuration input: full escalation", ()


def deduplicate(targets):
    # A whole-file target subsumes node targets within it.
    files = {target for target in targets if "::" not in target}
    return tuple(sorted({target for target in targets if "::" not in target or target.split("::")[0] not in files}))


@dataclass(frozen=True)
class Plan:
    risk: str
    files: tuple
    reasons: tuple
    tests: tuple
    full: bool = False


def select_tests(paths):
    paths = tuple(sorted(set(paths)))
    risk = "LOW"
    reasons, tests = [], []
    for path in paths:
        level, reason, targets = classify(path)
        reasons.append((path, level, reason))
        if RISK_ORDER[level] > RISK_ORDER[risk]:
            risk = level
        if level in ("MEDIUM", "HIGH") and not targets:
            return Plan("CRITICAL", paths, tuple(reasons), (), True)
        tests.extend(targets)
    if risk == "CRITICAL":
        return Plan(risk, paths, tuple(reasons), (), True)
    if risk != "LOW":
        if not impact.SMOKE_TESTS:
            return Plan("CRITICAL", paths, tuple(reasons), (), True)
        tests.extend(impact.SMOKE_TESTS)
    return Plan(risk, paths, tuple(reasons), deduplicate(tests))


def validate_targets(root, targets):
    for target in targets:
        path = Path(target.split("::")[0])
        if path.is_absolute() or ".." in path.parts or not (root / path).is_file():
            raise SelectionError(f"Missing/unsafe selected test: {target}; repair map or use --full")


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print selection without executing")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--smoke-only", action="store_true", help="explicit smoke check, NOT a release/core gate")
    modes.add_argument("--full", action="store_true", help="full regression even without a resolvable baseline")
    modes.add_argument("--current-contract", action="store_true", help="existing CI contract/security gate; NOT full regression")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4, help="bounded parallel workers (default 4)")
    parser.add_argument("--serial", action="store_true", help="explicit serial fallback")
    parser.add_argument("--baseline", help="override comparison commit, never writes the baseline record")
    args = parser.parse_args(argv)
    try:
        if args.current_contract:
            plan = Plan("HIGH", (), (("explicit CI current-contract", "HIGH", "not a full-regression substitute"),), impact.CURRENT_CONTRACT_TESTS)
        elif args.full:
            plan = Plan("CRITICAL", (), (("explicit --full", "CRITICAL", "full regression requested"),), (), True)
        else:
            baseline = read_baseline(root, args.baseline)
            print(f"Comparison baseline: {baseline}", flush=True)
            paths = changed_files(root, baseline)
            plan = select_tests(paths)
            if args.smoke_only:
                if not impact.SMOKE_TESTS:
                    raise SelectionError("Smoke list is empty; cannot report smoke success")
                print("Explicit smoke-only check; does NOT satisfy critical/high-risk/release validation.", flush=True)
                plan = Plan(plan.risk, plan.files, plan.reasons, deduplicate(impact.SMOKE_TESTS))
        print("Changed files:", flush=True)
        for path in plan.files:
            print(f"  {path}", flush=True)
        if not plan.files:
            print("  (none; or explicit --full bypasses detection)", flush=True)
        print(f"Risk: {plan.risk}", flush=True)
        for path, risk, reason in plan.reasons:
            print(f"  {risk}: {path} - {reason}", flush=True)
        if plan.full:
            command = [sys.executable, "-m", "pytest", "-q"]
        else:
            validate_targets(root, plan.tests)
            if not plan.tests:
                print("No runtime pytest required: only reviewed non-runtime inputs or no changes.", flush=True)
                return 0
            command = [sys.executable, "-m", "pytest", "-q", *plan.tests]
        print("Will run:", flush=True)
        print("  " + " ".join(command), flush=True)
        if args.list:
            return 0
        command = [sys.executable, "-m", "scripts.test_execution", "--workers", str(args.workers),
                   *(["--serial"] if args.serial else []), *([] if plan.full else plan.tests)]
        return subprocess.run(command, cwd=root).returncode
    except (SelectionError, OSError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
