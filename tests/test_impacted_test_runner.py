"""Selection infrastructure tested without running a nested application suite."""
import json
import subprocess
import sys

import pytest

from scripts import run_impacted_tests as runner, test_impact_map as impact
from scripts import update_test_baseline as promotion


@pytest.mark.parametrize("path", ["README.md", "docs/TESTING_STRATEGY.md", "docs/release.txt"])
def test_docs_only_does_not_run_runtime_suite(path):
    plan = runner.select_tests([path])
    assert plan.risk == "LOW" and not plan.full and plan.tests == ()


@pytest.mark.parametrize("paths", [
    ["test_baseline.json"],
    ["test_baseline.json", "docs/TESTING_STRATEGY.md"],
    ["docs/baseline-promotion.md"],
])
def test_baseline_metadata_and_documentation_do_not_request_full_regression(paths):
    plan = runner.select_tests(paths)
    assert plan.risk == "LOW" and not plan.full and plan.tests == ()


@pytest.mark.parametrize("path", [
    "camellia/database.py", "camellia/app.py", "camellia/config.py",
    "camellia/admin_security.py", "camellia/session_memory.py",
    "camellia/schema.py", "camellia/migrations/001.py", "camellia/unknown_runtime.py",
    "data/test_baseline.json",
])
def test_low_risk_baseline_metadata_cannot_downgrade_core_or_unknown_runtime(path):
    plan = runner.select_tests(["test_baseline.json", "docs/baseline-promotion.md", path])
    assert plan.risk == "CRITICAL" and plan.full


@pytest.mark.parametrize("path,area,risk", [
    ("camellia/admin_menu_review.py", "menu_admin", "MEDIUM"),
    ("camellia/admin_weekly_savoury.py", "menu_admin", "MEDIUM"),
    ("camellia/menu_catalogue.py", "menu_admin", "MEDIUM"),
    ("camellia/menu_review.py", "menu_authority", "HIGH"),
    ("camellia/weekly_savoury.py", "menu_authority", "HIGH"),
    ("camellia/menu_images.py", "menu_authority", "HIGH"),
    ("camellia/responder.py", "responder", "HIGH"),
    ("camellia/rag_fallback.py", "responder", "HIGH"),
    ("camellia/instagram_routing.py", "instagram", "HIGH"),
    ("camellia/order_calculator.py", "ordering", "HIGH"),
    ("camellia/handoff.py", "handoff", "HIGH"),
])
def test_feature_mapping_always_includes_safety_smoke(path, area, risk):
    plan = runner.select_tests([path])
    assert plan.risk == risk and not plan.full
    assert set(impact.AREA_TESTS[area]) <= set(plan.tests)
    for target in impact.SMOKE_TESTS:
        assert target in plan.tests or target.split("::")[0] in plan.tests


@pytest.mark.parametrize("path", [
    "camellia/database.py", "camellia/app.py", "camellia/config.py",
    "camellia/admin_security.py", "camellia/session_memory.py", "camellia/bootstrap.py",
    "camellia/new_unknown.py", "camellia/new_webhook.py", "data/new-schema.json",
    "data/menu.json", "data/client_approved_qa_source.txt", "requirements.txt",
    "Dockerfile", "railway.toml", "main.py", "docs/executable.py",
    "tests/conftest.py", ".github/workflows/ci.yml", "scripts/run_impacted_tests.py",
])
def test_core_unknown_configuration_and_deleted_runtime_escalate(path):
    # Classification is path based, not existence based (also covers deletion).
    plan = runner.select_tests([path])
    assert plan.risk == "CRITICAL" and plan.full


def test_multiple_files_union_and_deduplication():
    files = ["camellia/admin_menu_review.py", "camellia/menu_review.py"]
    plan = runner.select_tests(files + files)
    assert plan.files == tuple(sorted(files))
    assert len(plan.tests) == len(set(plan.tests))
    assert set(impact.AREA_TESTS["menu_admin"] + impact.AREA_TESTS["menu_authority"]) <= set(plan.tests)
    assert runner.deduplicate(["tests/test_bot.py", "tests/test_bot.py::test_one", "tests/test_bot.py"]) == ("tests/test_bot.py",)


def test_empty_feature_mapping_cannot_be_success(monkeypatch):
    monkeypatch.setitem(impact.AREA_TESTS, "menu_admin", ())
    assert runner.select_tests(["camellia/admin_menu_review.py"]).full


def test_empty_smoke_list_escalates_runtime(monkeypatch):
    monkeypatch.setattr(impact, "SMOKE_TESTS", ())
    assert runner.select_tests(["camellia/admin_menu_review.py"]).full


def test_empty_smoke_cli_is_error(monkeypatch):
    monkeypatch.setattr(impact, "SMOKE_TESTS", ())
    monkeypatch.setattr(runner, "read_baseline", lambda *a: "fixture-sha")
    monkeypatch.setattr(runner, "changed_files", lambda *a: [])
    assert runner.main(["--smoke-only"]) == 2


def test_every_selected_test_file_exists():
    runner.validate_targets(runner.ROOT, impact.SMOKE_TESTS)
    for tests in impact.AREA_TESTS.values():
        runner.validate_targets(runner.ROOT, tests)


@pytest.mark.parametrize("target", ["tests/absent.py", "../unsafe.py", "/absolute.py"])
def test_invalid_target_fails_closed(tmp_path, target):
    with pytest.raises(runner.SelectionError):
        runner.validate_targets(tmp_path, [target])


def _git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "tests@example.invalid")
    _git(tmp_path, "config", "user.name", "Isolation Test")
    (tmp_path / "camellia").mkdir()
    for name in ("responder.py", "database.py", "admin_menu_review.py"):
        (tmp_path / "camellia" / name).write_text("# baseline\n", encoding="utf-8")
    _git(tmp_path, "add", "camellia")
    _git(tmp_path, "commit", "-m", "fixture baseline")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def test_committed_staged_unstaged_untracked_and_deleted_detection(git_repo):
    root, baseline = git_repo
    (root / "camellia/responder.py").write_text("# committed\n")
    _git(root, "add", "camellia/responder.py")
    _git(root, "commit", "-m", "fixture committed change")
    (root / "camellia/admin_menu_review.py").write_text("# staged\n")
    _git(root, "add", "camellia/admin_menu_review.py")
    (root / "camellia/database.py").unlink()
    (root / "camellia/new file.py").write_text("# untracked\n")
    assert runner.changed_files(root, baseline) == [
        "camellia/admin_menu_review.py", "camellia/database.py",
        "camellia/new file.py", "camellia/responder.py",
    ]
    assert runner.select_tests(runner.changed_files(root, baseline)).full


def test_rename_keeps_old_risk_path(git_repo):
    root, baseline = git_repo
    _git(root, "mv", "camellia/database.py", "harmless.md")
    paths = runner.changed_files(root, baseline)
    assert "camellia/database.py" in paths and "harmless.md" in paths
    assert runner.select_tests(paths).full


def _baseline(root, sha, **extra):
    record = {"known_good_sha": sha, "full_regression_passed": True, "full_regression_test_count": 2597}
    record.update(extra)
    (root / "test_baseline.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("content", [None, "broken", "[]", "{}", '{"full_regression_passed":false}'])
def test_missing_or_invalid_baseline_is_error_not_empty_success(tmp_path, content):
    if content is not None:
        (tmp_path / "test_baseline.json").write_text(content)
    assert runner.main(["--list"], root=tmp_path) == 2


@pytest.mark.parametrize("count", [0, -1, True, "2597"])
def test_invalid_baseline_count(git_repo, count):
    root, sha = git_repo
    _baseline(root, sha, full_regression_test_count=count)
    with pytest.raises(runner.SelectionError):
        runner.read_baseline(root)


def test_baseline_override_resolves_without_writing(git_repo):
    root, sha = git_repo
    _baseline(root, "f" * 40)
    original = (root / "test_baseline.json").read_bytes()
    assert runner.read_baseline(root, sha) == sha
    assert (root / "test_baseline.json").read_bytes() == original
    with pytest.raises(runner.SelectionError):
        runner.read_baseline(root)


@pytest.mark.parametrize("ref", ["HEAD", "main", "1234567"])
def test_record_cannot_silently_follow_a_moving_ref(git_repo, ref):
    root, _ = git_repo
    _baseline(root, ref)
    with pytest.raises(runner.SelectionError):
        runner.read_baseline(root)


@pytest.mark.parametrize("ref", ["-unsafe", "missing-commit", ""])
def test_invalid_commit_cannot_be_used(git_repo, ref):
    root, _ = git_repo
    with pytest.raises(runner.SelectionError):
        runner.resolve_commit(root, ref)


@pytest.mark.parametrize("exit_code", [0, 1, 2, 4, 5])
def test_pytest_real_exit_code_is_preserved(monkeypatch, exit_code):
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, exit_code))
    assert runner.main(["--full"]) == exit_code


def test_full_list_does_not_execute_and_does_not_require_baseline(monkeypatch, tmp_path, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("--list must not run subprocess")
    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    assert runner.main(["--full", "--list"], root=tmp_path) == 0
    assert "-m pytest -q" in capsys.readouterr().out


def test_cli_smoke_only_and_list(monkeypatch, capsys):
    monkeypatch.setattr(runner, "read_baseline", lambda *a: "fixture-sha")
    monkeypatch.setattr(runner, "changed_files", lambda *a: ["camellia/app.py"])
    commands = []
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **kw: commands.append(command) or subprocess.CompletedProcess(command, 0))
    assert runner.main(["--smoke-only", "--list"]) == 0
    assert commands == []
    assert "does NOT satisfy" in capsys.readouterr().out
    assert runner.main(["--smoke-only"]) == 0
    assert commands == [[sys.executable, "-m", "scripts.test_execution", "--workers", "4", *runner.deduplicate(impact.SMOKE_TESTS)]]


def test_docs_only_cli_never_runs_pytest(monkeypatch):
    monkeypatch.setattr(runner, "read_baseline", lambda *a: "fixture-sha")
    monkeypatch.setattr(runner, "changed_files", lambda *a: ["docs/strategy.md"])
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: pytest.fail("docs-only called pytest"))
    assert runner.main([]) == 0


def test_baseline_only_cli_never_executes_full_pytest(monkeypatch, capsys):
    monkeypatch.setattr(runner, "read_baseline", lambda *a: "fixture-sha")
    monkeypatch.setattr(runner, "changed_files", lambda *a: ["test_baseline.json", "docs/TESTING_STRATEGY.md"])
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: pytest.fail("metadata-only called pytest"))
    assert runner.main(["--list"]) == 0
    assert runner.main([]) == 0
    output = capsys.readouterr().out
    assert "Risk: LOW" in output and "No runtime pytest required" in output
    assert "-m pytest" not in output


def test_missing_mapped_file_returns_safe_error(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "read_baseline", lambda *a: "fixture-sha")
    monkeypatch.setattr(runner, "changed_files", lambda *a: ["camellia/admin_menu_review.py"])
    assert runner.main([], root=tmp_path) == 2


def _clean_baseline(git_repo):
    root, sha = git_repo
    _baseline(root, sha)
    _git(root, "add", "test_baseline.json")
    _git(root, "commit", "-m", "fixture baseline metadata")
    return root, sha


def test_promotion_requires_clean_tree_and_never_changes_dirty_data(git_repo):
    root, sha = _clean_baseline(git_repo)
    original = (root / "test_baseline.json").read_bytes()
    (root / "unexpected.txt").write_text("dirty")
    assert promotion.main(["--sha", sha, "--test-count", "100", "--confirm-full-regression"], root) == 2
    assert (root / "test_baseline.json").read_bytes() == original


def test_promotion_does_not_infer_production_acceptance(git_repo):
    root, sha = _clean_baseline(git_repo)
    assert promotion.main(["--sha", sha, "--test-count", "100", "--confirm-full-regression"], root) == 0
    record = json.loads((root / "test_baseline.json").read_text())
    assert record["known_good_sha"] == sha and record["full_regression_test_count"] == 100
    assert record["production_validated"] is False and record["production_deployment_id"] is None
    assert record["previous_known_good_sha"] == sha and record["promoted_at_utc"]


@pytest.mark.parametrize("extra", [[], ["--production-validated"], ["--deployment-id", "fixture-deploy"]])
def test_promotion_requires_explicit_consistent_attestations(git_repo, extra):
    root, sha = _clean_baseline(git_repo)
    args = ["--sha", sha, "--test-count", "100", *extra]
    if extra:
        args.append("--confirm-full-regression")
    assert promotion.main(args, root) == 2
    assert _git(root, "status", "--porcelain") == ""


def test_promotion_records_explicit_production_evidence(git_repo):
    root, sha = _clean_baseline(git_repo)
    assert promotion.main(["--sha", sha, "--test-count", "100", "--confirm-full-regression",
                           "--production-validated", "--deployment-id", "fixture-deploy"], root) == 0
    record = json.loads((root / "test_baseline.json").read_text())
    assert record["production_validated"] is True and record["production_deployment_id"] == "fixture-deploy"


def test_ci_fast_and_full_policy_remains_separate():
    content = (runner.ROOT / ".github/workflows/ci.yml").read_text()
    assert 'cron: "23 2 * * *"' in content
    assert "github.event_name == 'push' || github.event_name == 'pull_request'" in content
    assert "github.event_name == 'workflow_dispatch' || github.event_name == 'schedule' || github.event_name == 'release'" in content
    assert "run: python scripts/run_impacted_tests.py --full --workers 2" in content
    assert "run: python scripts/run_impacted_tests.py --current-contract --workers 2" in content
    assert content.count("uses: ./.github/actions/setup-tests") == 2
    assert "${{ github.event_name }}-${{ github.ref }}" in content
    assert content.count("include-hidden-files: true") == 2
    assert "update_test_baseline" not in content
