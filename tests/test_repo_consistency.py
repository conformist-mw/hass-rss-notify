"""Guards for repository metadata that has to agree in more than one place.

These are drift guards, not behaviour tests: nothing here can fail because of a
defect in the integration, only because two files that must state the same thing
stopped doing so. Structural validation of the workflows themselves is left to
hassfest, the HACS action and GitHub, which run in CI.
"""

import json
from pathlib import Path
import tomllib

REPO_ROOT = Path(__file__).parent.parent
INTEGRATION = REPO_ROOT / "custom_components" / "rss_notify"


def test_the_python_version_ci_installs_matches_pyproject() -> None:
    """CI, ruff's target version and `requires-python` name the same release."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    required = pyproject["project"]["requires-python"].lstrip(">=~^")
    major, minor = required.split(".")[:2]

    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert f'PYTHON_VERSION: "{major}.{minor}"' in ci
    assert pyproject["tool"]["ruff"]["target-version"] == f"py{major}{minor}"


def test_ci_enforces_the_coverage_invariant() -> None:
    """The coverage floor README and CLAUDE.md promise is enforced by CI."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()

    assert "--cov=custom_components/rss_notify" in ci
    assert "--cov-branch" in ci
    assert "--cov-fail-under=100" in ci


def test_the_released_version_is_stated_once() -> None:
    """`manifest.json` (what HACS reads) and `pyproject.toml` agree."""
    manifest = json.loads((INTEGRATION / "manifest.json").read_text())
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())

    assert manifest["version"] == pyproject["project"]["version"]


def test_hacs_requires_a_home_assistant_minor_release() -> None:
    """The HACS minimum is a `x.y.0` release, not the patch the tests pin.

    Pinning the exact patch release blocks installation for everyone on an
    earlier patch of the same minor for no functional reason.
    """
    minimum = json.loads((REPO_ROOT / "hacs.json").read_text())["homeassistant"]

    assert minimum.split(".")[2] == "0", minimum
    assert minimum in (REPO_ROOT / "README.md").read_text()


def test_the_declared_license_matches_the_license_file() -> None:
    """`pyproject.toml`'s SPDX identifier and the file it points at agree."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    declared = pyproject["project"]

    assert declared["license"] == "MIT"
    assert declared["license-files"] == ["LICENSE"]
    assert (REPO_ROOT / "LICENSE").read_text().startswith("MIT License")


def test_the_english_translations_match_the_strings_file() -> None:
    """`strings.json` and `translations/en.json` are byte-identical.

    Hassfest checks this for core integrations only, so a custom integration has
    to guard it itself: the two files drifting means the UI shows stale text.
    """
    strings = (INTEGRATION / "strings.json").read_bytes()
    english = (INTEGRATION / "translations" / "en.json").read_bytes()

    assert strings == english
