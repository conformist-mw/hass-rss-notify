"""Tests for the GitHub Actions workflows.

These guard the CI/release configuration against drift: the Python version CI
installs must stay in step with what Home Assistant requires, and the release
workflow must keep publishing plain tags (HACS installs the repository content
directly, so no ZIP asset is built).
"""

from importlib.metadata import metadata
import json
from pathlib import Path
import re
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def load_workflow(name: str) -> dict[str, Any]:
    """Parse a workflow; PyYAML reads an unquoted ``on:`` key as ``True``."""
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    data["on"] = data.pop(True, data.get("on"))
    return data


def steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every step of every job in a workflow."""
    return [step for job in workflow["jobs"].values() for step in job["steps"]]


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflow_is_valid_yaml(name: str) -> None:
    """Each workflow parses and every job declares a runner and steps."""
    workflow = load_workflow(name)

    assert workflow["name"]
    assert workflow["on"]
    assert workflow["jobs"]
    for job_id, job in workflow["jobs"].items():
        assert job["runs-on"], job_id
        assert job["steps"], job_id
        for step in job["steps"]:
            assert "uses" in step or "run" in step, (job_id, step)


def test_ci_python_matches_the_home_assistant_requirement() -> None:
    """CI installs the Python minor release Home Assistant requires."""
    required = metadata("homeassistant")["Requires-Python"]
    major_minor = re.search(r"(\d+\.\d+)", required).group(1)

    assert load_workflow("ci.yml")["env"]["PYTHON_VERSION"] == major_minor


def test_ci_lints_and_tests_against_the_pinned_dependencies() -> None:
    """The CI test job installs the pinned deps and runs ruff plus pytest."""
    commands = " ".join(
        step["run"] for step in steps(load_workflow("ci.yml")) if "run" in step
    )

    assert "pip install -r requirements_test.txt" in commands
    assert "ruff check" in commands
    assert "ruff format --check" in commands
    assert "pytest" in commands


def test_ci_runs_the_hassfest_and_hacs_validations() -> None:
    """Structural validation is delegated to the hassfest and HACS actions."""
    used = {
        step["uses"]: step for step in steps(load_workflow("ci.yml")) if "uses" in step
    }

    assert "home-assistant/actions/hassfest@master" in used
    assert used["hacs/action@main"]["with"]["category"] == "integration"


def test_release_publishes_version_tags_without_a_zip() -> None:
    """Releases are cut from ``v*`` tags and attach no built archive."""
    workflow = load_workflow("release.yml")
    commands = " ".join(step["run"] for step in steps(workflow) if "run" in step)

    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert "gh release create" in commands
    assert "zip" not in commands
    assert "zip_release" not in json.loads((REPO_ROOT / "hacs.json").read_text())
