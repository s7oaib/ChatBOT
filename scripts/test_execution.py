"""Bounded, audited parallel files followed by serial tests; verify coverage union."""
import argparse
from collections import Counter
import hashlib
from importlib.util import find_spec
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
_config = None


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def safe_files(root):
    """New/edited tests or changed isolation dependencies fail closed to serial."""
    policy = json.loads((root / "scripts/test_parallel_policy.json").read_text())
    paths = (*policy["isolation_dependencies"], *policy["parallel_files"])
    if any(Path(path).is_absolute() or ".." in Path(path).parts for path in paths):
        raise ValueError("Unsafe parallel policy path")
    for path, expected in policy["isolation_dependencies"].items():
        if not (root / path).is_file() or digest(root / path) != expected:
            return set()
    return {path for path, expected in policy["parallel_files"].items()
            if (root / path).is_file() and digest(root / path) == expected}


def pytest_addoption(parser):
    group = parser.getgroup("camellia-execution")
    group.addoption("--execution-phase", choices=("all", "parallel", "serial"), default="all")
    group.addoption("--execution-report")


def pytest_configure(config):
    global _config
    _config = config
    config._execution_state = {"started": time.monotonic(), "collected": [], "selected": [], "reports": [], "error": None}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    state = config._execution_state
    state["collected"] = sorted(item.nodeid for item in items)
    phase = config.getoption("execution_phase")
    allowed = safe_files(Path(config.rootpath)) if phase != "all" else set()
    selected, deselected = [], []
    for item in items:
        parallel = item.nodeid.split("::")[0] in allowed
        (selected if phase == "all" or parallel == (phase == "parallel") else deselected).append(item)
    state["selected"] = sorted(item.nodeid for item in selected)
    items[:] = selected
    if deselected:
        config.hook.pytest_deselected(items=deselected)


def pytest_runtest_logreport(report):
    _config._execution_state["reports"].append({"nodeid": report.nodeid, "phase": report.when,
        "duration": report.duration, "outcome": report.outcome})


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    state = node.config._execution_state
    inventory = node.workeroutput.get("execution_inventory")
    if error or inventory is None:
        state["error"] = "Worker did not finish its collection inventory"
        return
    if state["collected"] and any(state[key] != inventory[key] for key in ("collected", "selected")):
        state["error"] = "Worker collection mismatch"
    state["collected"] = inventory["collected"]
    state["selected"] = inventory["selected"]


def pytest_sessionfinish(session, exitstatus):
    config, state = session.config, session.config._execution_state
    if hasattr(config, "workeroutput"):
        config.workeroutput["execution_inventory"] = {key: state[key] for key in ("collected", "selected")}
        return
    destination = config.getoption("execution_report")
    if destination:
        report = {key: state[key] for key in ("collected", "selected", "reports", "error")}
        report.update(exitstatus=int(exitstatus), wall_seconds=time.monotonic() - state["started"])
        Path(destination).write_text(json.dumps(report), encoding="utf-8")


def aggregate(phases, wall_seconds):
    """Neither missing nor duplicate cases can turn a successful subprocess green."""
    expected, selected, executed, reports = None, [], [], []
    for phase in phases:
        inventory = set(phase["collected"])
        if phase.get("error") or expected is not None and inventory != expected:
            raise ValueError("Collection inventory changed between phases/workers")
        expected = inventory
        selected.extend(phase["selected"])
        reports.extend(phase["reports"])
        if any(count != 1 for count in Counter((r["nodeid"], r["phase"]) for r in phase["reports"]).values()):
            raise ValueError("A case phase executed more than once")
        executed.extend({r["nodeid"] for r in phase["reports"]})
        if set(phase["selected"]) != {r["nodeid"] for r in phase["reports"]}:
            raise ValueError("A selected case did not execute")
    if not expected or Counter(selected) != Counter(expected) or Counter(executed) != Counter(expected):
        raise ValueError("Execution phases omitted or duplicated cases")
    calls = [r for r in reports if r["phase"] == "call"]
    skipped = {r["nodeid"] for r in reports if r["outcome"] == "skipped"}
    return {"wall_seconds": wall_seconds, "collected": sorted(expected), "test_count": len(expected),
            "passed": sum(r["outcome"] == "passed" for r in calls), "skipped": len(skipped),
            "failed": len({r["nodeid"] for r in reports if r["outcome"] == "failed"}),
            "phases": [{"selected_count": len(p["selected"]), "wall_seconds": p["wall_seconds"],
                        "exitstatus": p["exitstatus"]} for p in phases], "reports": reports}


def execute(targets=(), *, root=ROOT, workers=4, serial=False):
    if not 1 <= workers <= 4:
        raise ValueError("Worker count must be between 1 and 4")
    started = time.monotonic()
    report_dir = root / ".test-results" / uuid.uuid4().hex
    report_dir.mkdir(parents=True)
    xdist = find_spec("xdist") is not None
    hybrid = not serial and workers > 1 and xdist and bool(safe_files(root))
    if not hybrid:
        print("Serial execution (explicit mode, missing xdist, or unaudited isolation dependencies).", flush=True)
    phases, failure = [], 0
    for phase in (("parallel", "serial") if hybrid else ("all",)):
        report_path = report_dir / (phase + ".json")
        command = [sys.executable, "-m", "pytest", "-q", "-p", "scripts.test_execution",
                   "--execution-phase", phase, "--execution-report", str(report_path)]
        if xdist:
            command += ["-n", str(workers if phase == "parallel" else 0)]
            if phase == "parallel":
                command += ["--dist", "loadfile", "--max-worker-restart", "0"]
        command += list(targets)
        print(f"Execution phase: {phase}; workers: {workers if phase == 'parallel' else 1}", flush=True)
        result = subprocess.run(command, cwd=root)
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            # Pytest reports 5 when a legitimate phase has no selected tests.
            empty = result.returncode == 5 and data["selected"] == [] and data["reports"] == [] and bool(data["collected"])
            if result.returncode and not empty and not failure:
                failure = result.returncode
            phases.append(data)
        except (OSError, ValueError, KeyError):
            failure = failure or result.returncode or 2
            print("Execution report missing/invalid; validation cannot pass.", file=sys.stderr)
    try:
        summary = aggregate(phases, time.monotonic() - started)
        (report_dir / "aggregate.json").write_text(json.dumps(summary), encoding="utf-8")
        (root / ".test-results/latest.json").write_text(json.dumps(summary), encoding="utf-8")
        print(f"Aggregate: {summary['test_count']} cases, {summary['passed']} passed, {summary['failed']} failed, "
              f"{summary['skipped']} skipped; {summary['wall_seconds']:.2f}s. Report: {report_dir / 'aggregate.json'}", flush=True)
        return failure or (1 if summary["failed"] else 0)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return failure or 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args(argv)
    try:
        return execute(args.targets, workers=args.workers, serial=args.serial)
    except (OSError, ValueError, KeyError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
