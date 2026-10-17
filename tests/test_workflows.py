from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_key_material_and_read_only_by_default(path):
    text = path.read_text(encoding="utf-8")
    for needle in ("credentials_json", "GOOGLE_CREDENTIALS", "private_key"):
        assert needle not in text
    assert yaml.safe_load(text).get("permissions") == {"contents": "read"}


def test_the_dry_run_is_gated_uses_federation_and_the_read_only_account():
    wf = yaml.safe_load((WORKFLOWS / "dry-run.yml").read_text(encoding="utf-8"))
    job = wf["jobs"]["dry-run"]
    assert "GCP_WIF_PROVIDER" in job["if"] and job["permissions"]["id-token"] == "write"
    auth = next(s for s in job["steps"] if "google-github-actions/auth" in str(s.get("uses", "")))
    assert auth["with"]["service_account"] == "${{ vars.GCP_PLAN_SA }}", "a dry run needs read access, not the deploy account"


def test_the_ci_workflow_runs_the_three_checks_and_has_no_cloud_step():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    for cmd in ("bqci lint", "bqci cost", "bqci test"):
        assert cmd in text
    assert "google-github-actions" not in text and "id-token" not in text
