"""Infrastructure contracts: exact coverage, isolation, and failure propagation."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import run_impacted_tests as runner, test_execution as execution
from scripts import test_impact_map as impact


def policy(root, parallel=()):
    (root / "scripts").mkdir(exist_ok=True)
    (root / "isolation.py").write_text("# isolation\n")
    data = {"isolation_dependencies": {"isolation.py": execution.digest(root / "isolation.py")},
            "parallel_files": {p: execution.digest(root / p) for p in parallel}}
    (root / "scripts/test_parallel_policy.json").write_text(json.dumps(data))


def phase(collected, selected, failed=False):
    return {"collected": collected, "selected": selected, "error": None, "exitstatus": int(failed),
            "wall_seconds": 1, "reports": [{"nodeid": n, "phase": "call", "duration": .1,
                "outcome": "failed" if failed else "passed"} for n in selected]}


def test_policy_only_audited_unchanged_files_are_parallel(tmp_path):
    (tmp_path / "safe.py").write_text("original\n")
    policy(tmp_path, ["safe.py"])
    assert execution.safe_files(tmp_path) == {"safe.py"}
    (tmp_path / "new.py").write_text("unknown\n")
    assert "new.py" not in execution.safe_files(tmp_path)
    (tmp_path / "safe.py").write_text("changed\n")
    assert execution.safe_files(tmp_path) == set()


def test_changed_isolation_dependency_serializes_everything(tmp_path):
    (tmp_path / "safe.py").write_text("original\n")
    policy(tmp_path, ["safe.py"])
    (tmp_path / "isolation.py").write_text("changed\n")
    assert not execution.safe_files(tmp_path)


def test_hash_is_line_ending_independent(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"a\nb\n")
    b.write_bytes(b"a\r\nb\r\n")
    assert execution.digest(a) == execution.digest(b)


def test_policy_rejects_path_escape(tmp_path):
    policy(tmp_path)
    path = tmp_path / "scripts/test_parallel_policy.json"
    path.write_text(json.dumps({"isolation_dependencies": {}, "parallel_files": {"../unsafe.py": "hash"}}))
    with pytest.raises(ValueError, match="Unsafe"):
        execution.safe_files(tmp_path)


def test_aggregate_exact_union_and_preserves_failure():
    result = execution.aggregate([phase(["a", "b"], ["a"]), phase(["a", "b"], ["b"], True)], 2)
    assert result["collected"] == ["a", "b"]
    assert result["test_count"] == 2 and result["passed"] == 1 and result["failed"] == 1
    assert result["skipped"] == 0


@pytest.mark.parametrize("phases", [
    [phase(["a", "b"], ["a"])],
    [phase(["a"], ["a"]), phase(["a"], ["a"])],
    [phase(["a"], ["a"]), phase(["b"], ["b"])],
    [{**phase(["a"], ["a"]), "error": "worker crashed"}],
    [{**phase(["a"], ["a"]), "reports": []}],
    [],
])
def test_aggregate_rejects_missing_duplicate_or_inconsistent_execution(phases):
    with pytest.raises(ValueError):
        execution.aggregate(phases, 1)


def test_aggregate_rejects_duplicate_execution_inside_one_phase():
    report = phase(["a"], ["a"])
    report["reports"] *= 2
    with pytest.raises(ValueError, match="more than once"):
        execution.aggregate([report], 1)


@pytest.mark.parametrize("codes", [(0, 0), (1, 0), (0, 1), (2, 1), (0, 5)])
def test_both_phases_run_and_propagate_real_failures(tmp_path, monkeypatch, codes):
    (tmp_path / "safe.py").write_text("original\n")
    policy(tmp_path, ["safe.py"])
    monkeypatch.setattr(execution, "find_spec", lambda name: True)
    commands = []

    def run(command, cwd):
        commands.append(command)
        index = len(commands) - 1
        report = phase(["a", "b"], [["a"], ["b"]][index], codes[index] != 0)
        Path(command[command.index("--execution-report") + 1]).write_text(json.dumps(report))
        return subprocess.CompletedProcess(command, codes[index])

    monkeypatch.setattr(execution.subprocess, "run", run)
    assert execution.execute(root=tmp_path, workers=2) == next((c for c in codes if c), 0)
    assert len(commands) == 2
    assert commands[0][commands[0].index("-n") + 1] == "2"
    assert "loadfile" in commands[0] and "--max-worker-restart" in commands[0]
    assert commands[1][commands[1].index("-n") + 1] == "0"


def test_missing_report_cannot_pass(tmp_path, monkeypatch):
    policy(tmp_path)
    monkeypatch.setattr(execution.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    assert execution.execute(root=tmp_path, serial=True) == 2


def test_worker_inventory_disagreement_is_failure():
    from types import SimpleNamespace
    config = SimpleNamespace(_execution_state={"collected": ["a"], "selected": ["a"], "error": None})
    node = SimpleNamespace(config=config, workeroutput={"execution_inventory": {"collected": ["a"], "selected": []}})
    execution.pytest_testnodedown(node, None)
    assert config._execution_state["error"] == "Worker collection mismatch"


def test_current_contract_preserves_exact_security_cases_and_all_files(monkeypatch):
    commands = []
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **kw: commands.append(command) or subprocess.CompletedProcess(command, 0))
    assert runner.main(["--current-contract", "--workers", "2"]) == 0
    assert commands[0][5:] == list(impact.CURRENT_CONTRACT_TESTS)
    security = [p for p in impact.CURRENT_CONTRACT_TESTS if p.startswith("tests/test_admin_portal.py::")]
    assert len(security) == 8
    assert "tests/test_admin_portal.py::test_instagram_get_challenge_rejects_untrusted_verify_tokens" in security
    assert "tests/test_instagram_ads_inventory.py" in impact.CURRENT_CONTRACT_TESTS


@pytest.mark.parametrize("failure_phase", [None, "parallel", "serial"])
def test_real_process_execution_is_isolated_and_exact_once(tmp_path, monkeypatch, failure_phase):
    """Tiny isolated suite proves xdist/plugin lifecycle on Windows and Linux."""
    monkeypatch.setenv("PYTHONPATH", str(execution.ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    # Real process-local SQLite files, including a serial-sensitive phase.
    source = '''import os, sqlite3, tempfile
from pathlib import Path
import pytest
@pytest.mark.parametrize("i", range(2))
def test_isolated(i, tmp_path):
    path = tmp_path / "fixture.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items (n INTEGER)")
        conn.execute("INSERT INTO items VALUES (?)", (i,))
        assert conn.execute("SELECT n FROM items").fetchone()[0] == i
    assert os.getpid() != PARENT_PID
    assert SHOULD_PASS
'''
    parallel = [f"test_safe_{i}.py" for i in range(3)]
    for name in [*parallel, "test_sensitive.py"]:
        current_phase = "parallel" if name in parallel else "serial"
        text = source.replace("PARENT_PID", str(os.getpid())).replace("SHOULD_PASS", repr(current_phase != failure_phase))
        (tmp_path / name).write_text(text)
    policy(tmp_path, parallel)
    result = execution.execute(root=tmp_path, workers=2)
    assert result == (0 if failure_phase is None else 1)
    summary = json.loads((tmp_path / ".test-results/latest.json").read_text())
    assert summary["test_count"] == 8 and summary["skipped"] == 0
    assert [p["selected_count"] for p in summary["phases"]] == [6, 2]
    assert summary["passed"] == (8 if failure_phase is None else 2 if failure_phase == "parallel" else 6)


def test_cached_setup_covers_runtime_and_test_dependencies():
    text = (execution.ROOT / ".github/actions/setup-tests/action.yml").read_text()
    assert "cache: pip" in text and "cache-dependency-path:" in text
    assert "requirements.txt" in text and "requirements-test.txt" in text
    assert "pip install -r requirements.txt -r requirements-test.txt" in text


@pytest.mark.parametrize("is_parallel", [True, False])
def test_real_empty_phase_is_allowed_only_with_complete_union(tmp_path, monkeypatch, is_parallel):
    monkeypatch.setenv("PYTHONPATH", str(execution.ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert True\n")
    # A second audited file makes hybrid mode possible even if the only test is serial.
    (tmp_path / "audited.py").write_text("# audited helper\n")
    policy(tmp_path, ["test_one.py"] if is_parallel else ["audited.py"])
    assert execution.execute(root=tmp_path, workers=2) == 0
    result = json.loads((tmp_path / ".test-results/latest.json").read_text())
    assert result["test_count"] == result["passed"] == 1 and result["skipped"] == 0
    assert [p["selected_count"] for p in result["phases"]] == ([1, 0] if is_parallel else [0, 1])
