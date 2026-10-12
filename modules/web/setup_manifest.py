"""Requirements-manifest parsing and installed-package verification.

Pure functions over the fixed ``requirements/*.txt`` manifests shipped with the
app; no FastAPI or setup-state involvement.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

VersionResolver = Callable[[str], str | None]


def _manifest_requirements(
    manifest_path: Path,
    *,
    requirements_root: Path | None = None,
    visited: set[Path] | None = None,
) -> list[Requirement]:
    """Resolve applicable PEP 508 requirements from fixed recursive manifests."""
    path = manifest_path.resolve()
    root = (requirements_root or path.parent).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Requirements include escapes the manifest directory.") from exc

    seen = visited if visited is not None else set()
    if path in seen:
        return []
    seen.add(path)

    requirements: list[Requirement] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()
        include_match = re.fullmatch(r"(?:-r|--requirement)\s+(.+)", line)
        if include_match:
            include_path = (path.parent / include_match.group(1).strip()).resolve()
            requirements.extend(
                _manifest_requirements(
                    include_path,
                    requirements_root=root,
                    visited=seen,
                )
            )
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise ValueError("Requirements manifest contains an invalid entry.") from exc
        if requirement.marker is None or requirement.marker.evaluate():
            requirements.append(requirement)
    return requirements


def _expected_requirement(requirement: Requirement) -> str:
    if requirement.specifier:
        return str(requirement.specifier)
    if requirement.url:
        filename = unquote(Path(urlsplit(requirement.url).path).name)
        try:
            _, version, _, _ = parse_wheel_filename(filename)
        except ValueError:
            return "direct artifact"
        return f"=={version}"
    return "installed"


def _safe_installed_version(value: str) -> str:
    normalized = str(value).strip()
    if len(normalized) <= 80 and re.fullmatch(r"[A-Za-z0-9.!+_-]+", normalized):
        return normalized
    return "unknown"


def _version_matches(requirement: Requirement, installed: str) -> bool:
    if requirement.specifier:
        return requirement.specifier.contains(installed, prereleases=True)
    if requirement.url:
        expected = _expected_requirement(requirement)
        if not expected.startswith("=="):
            return True
        try:
            return Version(installed) == Version(expected[2:])
        except InvalidVersion:
            return False
    return True


def _check_manifest_packages(
    manifest_path: Path,
    *,
    version_resolver: VersionResolver = metadata.version,
) -> dict[str, Any]:
    missing: list[dict[str, str | None]] = []
    incompatible: list[dict[str, str | None]] = []
    for requirement in _manifest_requirements(manifest_path):
        name = canonicalize_name(requirement.name)
        expected = _expected_requirement(requirement)
        try:
            installed_raw = version_resolver(name)
        except (metadata.PackageNotFoundError, KeyError):
            installed_raw = None
        # A dist-info left without a Version field (interrupted install) is
        # as unusable as an absent package.
        if installed_raw is None:
            missing.append({"name": name, "expected": expected, "installed": None})
            continue
        installed = _safe_installed_version(installed_raw)
        if not _version_matches(requirement, installed_raw):
            incompatible.append(
                {"name": name, "expected": expected, "installed": installed}
            )
    return {
        "ready": not missing and not incompatible,
        "missing": missing,
        "incompatible": incompatible,
    }
