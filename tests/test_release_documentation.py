"""Documentation contracts for unsigned macOS release and install guidance."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RELEASING = ROOT / "docs" / "RELEASING_MACOS.md"
UNSIGNED_INSTALL = ROOT / "docs" / "UNSIGNED_MACOS_INSTALL.md"
RELEASE_TEMPLATE = ROOT / ".github" / "RELEASE_TEMPLATE.md"
README = ROOT / "README.md"
RUN_APPLICATION = ROOT / "docs" / "RUN_APPLICATION.md"
DOCS_README = ROOT / "docs" / "README.md"
SOURCE_WORKFLOW = ROOT / ".github" / "workflows" / "source-validation.yml"

FORBIDDEN_AUTOMATION_PATTERNS = (
    "gh release create",
    "gh release upload",
    "git push --tags",
    "git tag -a",
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing required documentation file: {path}"
    return path.read_text(encoding="utf-8")


def _combined_docs() -> str:
    return "\n".join(
        _read(path)
        for path in (
            RELEASING,
            UNSIGNED_INSTALL,
            RELEASE_TEMPLATE,
            README,
            RUN_APPLICATION,
            DOCS_README,
        )
    )


def test_required_release_documentation_files_exist() -> None:
    for path in (RELEASING, UNSIGNED_INSTALL, RELEASE_TEMPLATE):
        assert path.is_file(), f"missing {path.relative_to(ROOT)}"


def test_docs_index_and_entry_points_link_release_guides() -> None:
    docs_readme = _read(DOCS_README)
    readme = _read(README)
    run_guide = _read(RUN_APPLICATION)

    assert "RELEASING_MACOS.md" in docs_readme
    assert "UNSIGNED_MACOS_INSTALL.md" in docs_readme
    assert "RELEASING_MACOS.md" in readme or "docs/RELEASING_MACOS.md" in readme
    assert (
        "UNSIGNED_MACOS_INSTALL.md" in readme
        or "docs/UNSIGNED_MACOS_INSTALL.md" in readme
    )
    assert "UNSIGNED_MACOS_INSTALL.md" in run_guide or "RELEASING_MACOS.md" in run_guide


def test_target_platform_is_apple_silicon_macos_14_plus() -> None:
    releasing = _read(RELEASING)
    unsigned = _read(UNSIGNED_INSTALL)
    template = _read(RELEASE_TEMPLATE)

    for text in (releasing, unsigned, template):
        assert re.search(r"Apple Silicon", text)
        assert re.search(r"macOS\s*14", text, re.IGNORECASE)


def test_unsigned_and_unnotarized_stated_honestly() -> None:
    releasing = _read(RELEASING)
    unsigned = _read(UNSIGNED_INSTALL)
    template = _read(RELEASE_TEMPLATE)

    for text in (releasing, unsigned, template):
        lowered = text.lower()
        assert "unsigned" in lowered
        assert "unnotarized" in lowered or "not notarized" in lowered


def test_adhoc_sealing_explained_as_no_distribution_identity() -> None:
    """Expected outer ad-hoc seal is complete and carries no distribution identity."""
    releasing = _read(RELEASING)
    unsigned = _read(UNSIGNED_INSTALL)
    combined = (releasing + "\n" + unsigned).lower()
    assert "ad-hoc" in combined or "adhoc" in combined
    assert "distribution identity" in combined or "no distribution" in combined
    assert "developer id" in combined
    assert "seal" in combined
    assert "teamidentifier=notset" in combined.replace(" ", "")
    assert "sealed resources" in combined or "complete" in combined


def test_checksum_verification_uses_portable_shasum_in_report_dir() -> None:
    releasing = _read(RELEASING)
    unsigned = _read(UNSIGNED_INSTALL)

    assert "shasum -a 256" in releasing
    assert "SHA256SUMS" in releasing
    assert "cd " in releasing
    assert "shasum -a 256 -c SHA256SUMS" in releasing
    assert "release-qualification-" in releasing
    assert "staged" in releasing.lower() or "beside" in releasing.lower()

    assert "shasum -a 256" in unsigned
    assert "SHA256SUMS" in unsigned
    assert re.search(r"before first open|before you open", unsigned, re.IGNORECASE)


def test_supported_gatekeeper_open_guidance_only() -> None:
    unsigned = _read(UNSIGNED_INSTALL)
    releasing = _read(RELEASING)
    combined = unsigned + "\n" + releasing

    assert re.search(r"right-?click", combined, re.IGNORECASE)
    assert re.search(r"\bOpen\b", combined)
    assert re.search(r"Open Anyway", combined)
    assert re.search(r"System Settings", combined)


def test_forbidden_gatekeeper_bypasses_are_absent() -> None:
    combined = _combined_docs().lower()

    for pattern in (
        "spctl --master-disable",
        "master-disable",
        "xattr -dr",
        "xattr -rd",
        "xattr -r -d com.apple.quarantine",
        "disable gatekeeper",
        "globally disabling gatekeeper",
        "recursive quarantine",
    ):
        assert pattern not in combined, f"forbidden bypass guidance present: {pattern}"


def test_source_ci_distinguished_from_local_release_gate() -> None:
    releasing = _read(RELEASING)
    readme = _read(README)
    docs_readme = _read(DOCS_README)
    combined = "\n".join((releasing, readme, docs_readme, _read(RUN_APPLICATION)))

    assert "source-validation.yml" in combined
    assert "release_qualification.py" in combined
    assert re.search(r"source validation|source CI", combined, re.IGNORECASE)
    assert re.search(
        r"local release (qualification|gate)|full local",
        combined,
        re.IGNORECASE,
    )


def test_four_manual_release_inputs_are_listed() -> None:
    releasing = _read(RELEASING)
    template = _read(RELEASE_TEMPLATE)
    combined = releasing + "\n" + template

    assert "SHA256SUMS" in combined
    assert "release-qualification-" in combined
    assert re.search(r"\.dmg\b", combined, re.IGNORECASE)
    assert re.search(r"release notes", combined, re.IGNORECASE)
    assert re.search(r"\bfour\b", combined, re.IGNORECASE)


def test_manual_github_release_draft_review_publish_procedure() -> None:
    releasing = _read(RELEASING)
    template = _read(RELEASE_TEMPLATE)
    combined = releasing + "\n" + template

    assert re.search(r"\bdraft\b", combined, re.IGNORECASE)
    assert re.search(r"\breview\b", combined, re.IGNORECASE)
    assert re.search(r"\bpublish\b", combined, re.IGNORECASE)
    assert re.search(r"GitHub Release", combined)
    assert "manual" in combined.lower()


def test_no_automatic_publishing_or_tag_push_commands() -> None:
    combined = _combined_docs()

    for pattern in FORBIDDEN_AUTOMATION_PATTERNS:
        assert pattern not in combined, f"forbidden automation present: {pattern}"

    assert "workflow_dispatch" not in combined.lower()
    assert re.search(
        r"separate (explicit )?request|explicit (user )?request",
        combined,
        re.IGNORECASE,
    )
    assert re.search(r"tag|push|release", combined, re.IGNORECASE)


def test_rollback_uninstall_and_removal_documented() -> None:
    releasing = _read(RELEASING)
    unsigned = _read(UNSIGNED_INSTALL)
    combined = releasing + "\n" + unsigned

    assert re.search(r"uninstall|removal|remove", combined, re.IGNORECASE)
    assert re.search(r"rollback|roll back|previous version", combined, re.IGNORECASE)
    assert "/Applications/Orpheus.app" in combined or "Applications" in combined


def test_qualification_cli_flow_documented() -> None:
    releasing = _read(RELEASING)

    assert "python scripts/release_qualification.py prepare" in releasing
    assert "--final" in releasing
    assert "python scripts/release_qualification.py run --report-dir" in releasing
    assert "python scripts/release_qualification.py finalize --report-dir" in releasing
    assert ".artifacts/release-qualification/" in releasing


def test_release_template_covers_unsigned_warning_and_checksums() -> None:
    template = _read(RELEASE_TEMPLATE)
    lowered = template.lower()

    assert "unsigned" in lowered
    assert "sha256" in lowered or "SHA256SUMS" in template
    assert "Apple Silicon" in template
    assert re.search(r"known limitations?", template, re.IGNORECASE)
    assert re.search(r"rollback|uninstall|removal", template, re.IGNORECASE)


def test_no_new_github_actions_release_automation() -> None:
    workflows = ROOT / ".github" / "workflows"
    assert SOURCE_WORKFLOW.is_file()
    names = sorted(path.name for path in workflows.glob("*.yml"))
    assert names == [
        "release-artifact-validation.yml",
        "source-validation.yml",
    ]

    # No workflow may publish a release. The artifact-validation workflow does
    # retain its build output via actions/upload-artifact, which is CI
    # retention for inspection, not a published release asset -- so that check
    # stays scoped to the source workflow.
    for path in sorted(workflows.glob("*.yml")):
        contents = path.read_text(encoding="utf-8").lower()
        for banned in ("gh release", "softprops/action-gh-release"):
            assert banned not in contents, f"{path.name} publishes a release"

    source_contents = SOURCE_WORKFLOW.read_text(encoding="utf-8").lower()
    assert "upload-artifact" not in source_contents
