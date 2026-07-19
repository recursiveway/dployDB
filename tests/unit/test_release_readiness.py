"""Release-readiness contracts for DployDB 0.1.0 Alpha."""

from __future__ import annotations

import re
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.release_notes import release_notes
from scripts.verify_clean_linux import wheel_version
from scripts.verify_distribution import DistributionError, _validate_names
from scripts.verify_pipx_install import expected_distribution_version
from scripts.verify_release_tag import TAG_PATTERN

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".github" / "workflows" / "release.yml",
)
ACTION_REFERENCE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def test_public_package_metadata_declares_alpha_license_owner_and_urls() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]

    assert metadata["build-system"]["requires"] == ["hatchling>=1.27"]
    assert project["name"] == "dploydb"
    assert project["version"] == "0.1.0"
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "NOTICE"]
    assert project["authors"] == [{"name": "RecursiveWay"}]
    assert "Development Status :: 3 - Alpha" in project["classifiers"]
    assert not any(value.startswith("License ::") for value in project["classifiers"])
    assert project["urls"] == {
        "Homepage": "https://github.com/recursiveway/dployDB",
        "Repository": "https://github.com/recursiveway/dployDB",
        "Issues": "https://github.com/recursiveway/dployDB/issues",
        "Documentation": "https://github.com/recursiveway/dployDB/tree/main/docs",
    }


def test_sdist_uses_a_public_allowlist() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = set(metadata["tool"]["hatch"]["build"]["targets"]["sdist"]["include"])

    for required in ("/.gitignore", "/LICENSE", "/NOTICE", "/README.md", "/dploydb", "/tests"):
        assert required in include
    for forbidden in (
        "/.agents",
        "/.claude",
        "/.codex",
        "/AGENTS.md",
        "/IMPLEMENTATION_PLAN.md",
    ):
        assert forbidden not in include


def test_license_notice_and_public_policies_are_present() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")

    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    assert notice.startswith("DployDB\nCopyright 2026 RecursiveWay\n")
    for name in (
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "RELEASING.md",
        "SECURITY.md",
    ):
        assert (ROOT / name).is_file()


def test_readme_is_explicitly_alpha_and_publicly_installable() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "DployDB 0.1.0 is Alpha software" in readme
    assert "pipx install dploydb==0.1.0" in readme
    assert "Apache License 2.0" in readme
    assert "1.0.0" in readme
    assert "SECURITY.md" in readme


def test_workflows_pin_actions_and_limit_publish_permissions() -> None:
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped.startswith("uses:"):
                continue
            reference = stripped.removeprefix("uses:").split("#", 1)[0].strip()
            assert ACTION_REFERENCE.fullmatch(reference), (
                f"workflow action is not pinned to a commit SHA: {reference}"
            )

    ci = WORKFLOWS[0].read_text(encoding="utf-8")
    release = WORKFLOWS[1].read_text(encoding="utf-8")
    assert "id-token: write" not in ci
    assert "environment:\n      name: testpypi" in release
    assert "environment:\n      name: pypi" in release
    assert "pypa/gh-action-pypi-publish@" in release
    assert "secrets." not in release
    assert "--draft --prerelease" in release
    assert "--draft=false --prerelease=true" in release


def test_release_workflow_recovers_an_existing_immutable_tag_from_main() -> None:
    release = WORKFLOWS[1].read_text(encoding="utf-8")

    assert "workflow_dispatch:" in release
    assert "Existing verified release tag to recover" in release
    assert "release tag is not canonical" in release
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in release
    assert '"refs/tags/${TAG}:refs/tags/${TAG}"' in release
    assert 'test "$object_type" = "tag"' in release
    assert 'test "$remote_target" = "$(git rev-parse "${TAG}^{}")"' in release
    assert ".verification.verified" in release
    assert release.count("ref: refs/tags/${{ env.RELEASE_TAG }}") == 4
    assert "${{ github.ref_name }}" not in release


def _write_wheel(path: Path, version: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"dploydb-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: dploydb\nVersion: {version}\n",
        )


def test_install_and_clean_linux_verifiers_read_version_from_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "dploydb-0.7.2-py3-none-any.whl"
    _write_wheel(wheel, "0.7.2")

    assert expected_distribution_version(wheel) == "0.7.2"
    assert wheel_version(wheel) == "0.7.2"


def test_release_notes_extract_exact_version() -> None:
    changelog = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - now\n\nAlpha.\n\n## [0.0.1]\nOld.\n"

    assert release_notes(changelog, "0.1.0") == "Alpha.\n"
    with pytest.raises(RuntimeError, match="no section"):
        release_notes(changelog, "9.9.9")


@pytest.mark.parametrize("tag", ("v0.1.0", "v0.9.0", "v1.0.0rc1", "v2.3.4b2"))
def test_release_tag_pattern_accepts_canonical_versions(tag: str) -> None:
    assert TAG_PATTERN.fullmatch(tag) is not None


@pytest.mark.parametrize("tag", ("0.1.0", "v01.0.0", "v1.0", "v1.0.0-alpha", "v1.0.0+local"))
def test_release_tag_pattern_rejects_ambiguous_versions(tag: str) -> None:
    assert TAG_PATTERN.fullmatch(tag) is None


@pytest.mark.parametrize(
    "name",
    (
        "dploydb-0.1.0/.claude/settings.local.json",
        "dploydb-0.1.0/AGENTS.md",
        "dploydb-0.1.0/demo/.state/app.db",
    ),
)
def test_distribution_verifier_rejects_private_and_generated_paths(name: str) -> None:
    with pytest.raises(DistributionError, match="distribution"):
        _validate_names({name})
