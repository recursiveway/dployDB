"""Durable checks for Milestone 8's real-user documentation contract."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "CHANGELOG.md",
    ROOT / "CODE_OF_CONDUCT.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "RELEASING.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "first-run.md",
    ROOT / "docs" / "security.md",
    ROOT / "docs" / "limitations.md",
    ROOT / "docs" / "uninstall.md",
    ROOT / "examples" / "nginx" / "README.md",
)
LOCAL_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def test_all_documented_local_links_resolve() -> None:
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for raw_target in LOCAL_LINK.findall(text):
            target = raw_target.split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            assert (document.parent / target).resolve().exists(), (
                f"broken local link {raw_target!r} in {document.relative_to(ROOT)}"
            )


def test_readme_quick_start_uses_the_installed_cli_and_parses_real_json() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for required in (
        "pipx install dploydb==0.1.0",
        "git clone https://github.com/recursiveway/dployDB.git",
        "python3 -m demo.prepare",
        "dploydb --no-color doctor --deep",
        "--json --non-interactive",
        'p["outcome"] == "active"',
        "python3 demo/controller.py --instance quickstart --port 4510 stop",
    ):
        assert required in readme


def test_security_limitations_and_uninstall_state_the_safety_boundaries() -> None:
    security = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")
    limitations = (ROOT / "docs" / "limitations.md").read_text(encoding="utf-8")
    uninstall = (ROOT / "docs" / "uninstall.md").read_text(encoding="utf-8")

    assert "Docker daemon membership as root-equivalent" in security
    assert "Automatic database rollback is allowed only before" in limitations
    assert "could erase" in limitations
    assert "backs up the current database first" in limitations
    assert "pipx uninstall dploydb" in uninstall
    assert "Do not delete the configured state directory" in uninstall
    assert "rm -rf" not in uninstall


def test_nginx_docs_never_route_normal_traffic_to_the_candidate() -> None:
    guide = (ROOT / "examples" / "nginx" / "README.md").read_text(encoding="utf-8")
    site = (ROOT / "examples" / "nginx" / "site.conf").read_text(encoding="utf-8")

    assert "candidate remains isolated on `4511`" in guide
    assert "proxy_pass http://127.0.0.1:4510;" in site
    assert "127.0.0.1:4511" not in site
    assert "/run/dploydb/example-app.maintenance" in site
