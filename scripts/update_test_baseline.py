"""Deliberate operator attestation of a validated commit, never run by CI."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.run_impacted_tests import ROOT, SelectionError, git, resolve_commit


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--test-count", required=True, type=int)
    parser.add_argument("--confirm-full-regression", action="store_true",
                        help="attest full regression passed on THIS SHA")
    parser.add_argument("--production-validated", action="store_true",
                        help="explicitly attest production acceptance, never inferred")
    parser.add_argument("--deployment-id")
    args = parser.parse_args(argv)
    try:
        if git(root, "status", "--porcelain", "--untracked-files=all").strip():
            raise SelectionError("Baseline promotion requires a clean working tree")
        sha = resolve_commit(root, args.sha)
        if args.test_count <= 0 or not args.confirm_full_regression:
            raise SelectionError("Positive test count and --confirm-full-regression attestation required")
        if bool(args.deployment_id) != args.production_validated:
            raise SelectionError("Production attestation and deployment ID must be supplied together")
        path = root / "test_baseline.json"
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(previous, dict):
            raise SelectionError("Existing baseline must be an object")
        record = {
            "known_good_sha": sha,
            "full_regression_passed": True,
            "full_regression_test_count": args.test_count,
            "production_validated": args.production_validated,
            "production_deployment_id": args.deployment_id,
            "evidence_source": "Explicit operator attestation; utility does not execute tests or verify production",
            "promoted_at_utc": datetime.now(timezone.utc).isoformat(),
            "previous_known_good_sha": previous.get("known_good_sha"),
        }
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(f"Baseline promoted explicitly to {sha}; review and commit test_baseline.json separately.")
        return 0
    except (SelectionError, OSError, ValueError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
