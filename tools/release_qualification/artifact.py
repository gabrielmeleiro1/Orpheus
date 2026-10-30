"""Inspect packaged macOS app bundles and DMGs for release qualification."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .commands import CommandSpec
from .model import QualificationConfig, parse_version_tuple

FORBIDDEN_DYLIB_PREFIXES: tuple[str, ...] = (
    "/Users/",
    "/home/",
    "/opt/homebrew/",
    "/usr/local/",
    "/private/var/folders/",
)
ALLOWED_DMG_PRESENTATION: frozenset[str] = frozenset(
    {
        ".DS_Store",
        "Applications",
        ".background",
        ".VolumeIcon.icns",
        ".fseventsd",
        ".Trashes",
        ".Spotlight-V100",
    }
)
# Tauri 2 embeds frontendDist into the main Mach-O; require both the asset
# catalog key and a Vite hashed /assets/index-* path inside that binary.
EMBEDDED_FRONTEND_MARKERS: tuple[bytes, ...] = (
    b"index.html",
    b"/assets/index-",
)
DMG_DETACH_ATTEMPTS = 3
DMG_DETACH_RETRY_DELAY_S = 0.2


class ArtifactInspectionError(ValueError):
    """Raised when a packaged artifact fails a release inspection gate."""


class SigningState(str, Enum):
    UNSIGNED = "unsigned"
    ADHOC = "adhoc"
    DEVELOPER_ID = "developer_id"
    OTHER = "other"


@dataclass(frozen=True)
class CodesignInfo:
    state: SigningState
    team_identifier: str | None
    flags: str
    authorities: tuple[str, ...]
    raw_text: str


@dataclass(frozen=True)
class ArtifactInspectionResult:
    ok: bool
    path: str
    signing_state: SigningState | None = None
    sha256: str | None = None
    detail: str = ""


def build_sidecar_commands(*, repo_root: Path) -> tuple[CommandSpec, ...]:
    root = Path(repo_root)
    python = str(root / ".venv" / "bin" / "python")
    # Pin to the resource tree that build-sidecar just wrote so a stale
    # Orpheus.app bundle binary cannot satisfy (or fail) the gate.
    sidecar_path = (
        root
        / "desktop"
        / "src-tauri"
        / "binaries"
        / "orpheus-desktop-backend"
        / "orpheus-desktop-backend"
    )
    return (
        CommandSpec(
            label="build-sidecar",
            argv=(python, "scripts/build_desktop_backend.py", "--runtime-profile", "mlx"),
            cwd=str(root),
        ),
        CommandSpec(
            label="sidecar-runtime-smoke",
            argv=(
                python,
                "scripts/desktop_runtime_smoke.py",
                "--backend",
                "sidecar",
                "--sidecar-path",
                str(sidecar_path),
                "--reduced-path",
                "--check-ffmpeg-runtime",
                "--require-local-tts",
                "--probe-wait-seconds",
                "30",
                "--timeout-seconds",
                "60",
            ),
            cwd=str(root),
        ),
    )


def build_bundle_commands(*, repo_root: Path) -> tuple[CommandSpec, ...]:
    root = Path(repo_root)
    return (
        CommandSpec(
            label="tauri-desktop-build",
            argv=("npm", "run", "desktop:build"),
            cwd=str(root / "desktop"),
        ),
        CommandSpec(
            label="bundled-runtime-smoke",
            argv=(str(root / ".venv/bin/python"), "-m", "tools.release_qualification.runtime",
                  "--repo-root", str(root)),
            cwd=str(root),
            timeout_seconds=240,
        ),
    )


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_version_tuple(text: str) -> tuple[int, ...]:
    return parse_version_tuple(text, error_cls=ArtifactInspectionError)


def default_read_architectures(path: Path) -> tuple[str, ...]:
    lipo = subprocess.run(
        ["lipo", "-archs", str(path)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if lipo.returncode == 0 and lipo.stdout.strip():
        return tuple(lipo.stdout.split())
    file_probe = subprocess.run(
        ["file", "-b", str(path)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if lipo.returncode != 0 and file_probe.returncode != 0:
        raise ArtifactInspectionError(
            f"architecture tools failed for {path}: lipo and file"
        )
    if file_probe.returncode != 0:
        raise ArtifactInspectionError(f"architecture file probe failed for {path}")
    text = file_probe.stdout.lower()
    found: list[str] = []
    if "arm64" in text:
        found.append("arm64")
    if "x86_64" in text:
        found.append("x86_64")
    if not found:
        raise ArtifactInspectionError(
            f"unable to determine architecture for {path}"
        )
    return tuple(found)


def default_read_dylibs(path: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["otool", "-L", str(path)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise ArtifactInspectionError(f"otool -L failed for {path}")
    lines = []
    for raw in completed.stdout.splitlines()[1:]:
        stripped = raw.strip()
        if not stripped:
            continue
        dep = stripped.split(" (", 1)[0].strip()
        if dep:
            lines.append(dep)
    return tuple(lines)


def parse_codesign_info(text: str, *, returncode: int) -> CodesignInfo:
    lowered = text.lower()
    compact = lowered.replace(" ", "")
    authorities = tuple(
        line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line.lower().startswith("authority=")
    )
    team_identifier: str | None = None
    for line in text.splitlines():
        if line.lower().startswith("teamidentifier="):
            value = line.split("=", 1)[1].strip()
            if value and value.lower() != "not set":
                team_identifier = value
            break
    flags = ""
    flags_match = re.search(r"flags\s*=\s*0x[0-9a-f]+\(([^)]+)\)", text, re.IGNORECASE)
    if flags_match:
        flags = flags_match.group(1)

    if "code object is not signed at all" in lowered or (
        returncode != 0 and "not signed" in lowered
    ):
        state = SigningState.UNSIGNED
    elif "developer id application" in lowered:
        state = SigningState.DEVELOPER_ID
    elif (
        "signature=adhoc" in compact
        or "flags=0x2(adhoc)" in compact
        or "adhoc" in flags.lower()
    ):
        state = SigningState.ADHOC
    elif "not signed" in lowered:
        state = SigningState.UNSIGNED
    else:
        state = SigningState.OTHER

    return CodesignInfo(
        state=state,
        team_identifier=team_identifier,
        flags=flags,
        authorities=authorities,
        raw_text=text,
    )


def default_read_codesign_info(path: Path) -> CodesignInfo:
    completed = subprocess.run(
        ["codesign", "-dv", "--verbose=4", str(path)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    text = f"{completed.stdout}\n{completed.stderr}"
    return parse_codesign_info(text, returncode=completed.returncode)


def _is_expected_unsigned_codesign(info: CodesignInfo) -> bool:
    """Accept truly unsigned or no-team ad-hoc/linker-signed seals only."""
    if info.team_identifier:
        return False
    authority_blob = " ".join(info.authorities).lower()
    if "developer id" in authority_blob or "distribution" in authority_blob:
        return False
    if info.state is SigningState.UNSIGNED:
        return True
    if info.state is SigningState.ADHOC:
        return True
    return False


def _codesign_rejection_reason(info: CodesignInfo) -> str:
    if info.state is SigningState.DEVELOPER_ID:
        return "unexpected Developer ID identity; expected unsigned/ad-hoc without team"
    authority_blob = " ".join(info.authorities).lower()
    if "distribution" in authority_blob:
        return (
            "unexpected Apple Distribution identity; "
            "expected unsigned/ad-hoc without team"
        )
    if info.team_identifier:
        return (
            f"unexpected TeamIdentifier={info.team_identifier!r}; "
            "expected unsigned/ad-hoc with TeamIdentifier not set"
        )
    if info.state is SigningState.OTHER:
        return "unexpected signing identity; expected unsigned/ad-hoc without team"
    return (
        f"unexpected signing state {info.state.value}; "
        "expected unsigned/ad-hoc without team"
    )


def default_verify_dmg(path: Path) -> bool:
    completed = subprocess.run(
        ["hdiutil", "verify", str(path)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.returncode == 0


def default_verify_bundle_signature(path: Path) -> bool:
    """Return True when the outer bundle seal verifies deep+strict."""
    completed = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(path)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.returncode == 0


def default_mount_dmg(path: Path, mount_root: Path) -> Path:
    mount_root.mkdir(parents=True, exist_ok=True)
    mount_point = mount_root / "volume"
    mount_point.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            "hdiutil",
            "attach",
            str(path),
            "-readonly",
            "-nobrowse",
            "-mountpoint",
            str(mount_point),
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise ArtifactInspectionError(
            f"hdiutil attach failed for {path}: {completed.stderr.strip()}"
        )
    return mount_point


def default_detach_dmg(mount_point: Path) -> None:
    completed = subprocess.run(
        ["hdiutil", "detach", str(mount_point), "-force"],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ArtifactInspectionError(
            f"hdiutil detach failed for {mount_point}: {detail}"
        )


def default_classify_macho(path: Path) -> bool:
    completed = subprocess.run(
        ["file", "-b", str(path)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise ArtifactInspectionError(f"Mach-O classification failed for {path}")
    return "mach-o" in completed.stdout.lower()


def _is_within_bundle(path: Path, app: Path) -> bool:
    try:
        path.resolve().relative_to(app.resolve())
        return True
    except ValueError:
        return False


def _resolve_contained_file(path: Path, app: Path) -> Path | None:
    """Resolve path if it is a file contained in app; reject escaped symlinks."""
    if path.is_symlink():
        target = path.resolve()
        if not _is_within_bundle(target, app):
            raise ArtifactInspectionError(
                f"symlink escapes app bundle: {path} -> {target}"
            )
        return target if target.is_file() else None
    if path.is_file():
        return path
    return None


def _iter_bundle_files(app: Path):
    """Yield files under app without following directory symlinks."""
    for dirpath, dirnames, filenames in os.walk(app, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
        for name in filenames:
            yield base / name


def _iter_macho_executables(
    app: Path,
    *,
    known_paths: tuple[Path, ...],
    classify_macho: Callable[[Path], bool],
) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key in seen:
            return
        seen.add(key)
        candidates.append(path)

    for known in known_paths:
        resolved = _resolve_contained_file(known, app)
        if resolved is None:
            continue
        if classify_macho(resolved):
            add(known)

    for path in _iter_bundle_files(app):
        resolved = _resolve_contained_file(path, app)
        if resolved is None:
            continue
        if not classify_macho(resolved):
            continue
        add(path)
    return candidates


def _resolve_bundle_icon(resources: Path, plist: dict[str, object]) -> Path | None:
    icon_file = str(plist.get("CFBundleIconFile") or "").strip()
    if not icon_file:
        return None
    candidate = resources / icon_file
    if candidate.suffix.lower() != ".icns":
        candidate = resources / f"{icon_file}.icns"
    if candidate.is_file() and candidate.suffix.lower() == ".icns":
        return candidate
    return None


def _has_embedded_frontend_assets(executable: Path) -> bool:
    data = Path(executable).read_bytes()
    return all(marker in data for marker in EMBEDDED_FRONTEND_MARKERS)


def inspect_app_bundle(
    app_path: Path,
    *,
    config: QualificationConfig,
    release_version: str,
    read_architectures: Callable[[Path], tuple[str, ...]] = default_read_architectures,
    read_dylibs: Callable[[Path], tuple[str, ...]] = default_read_dylibs,
    read_codesign_info: Callable[[Path], CodesignInfo] = default_read_codesign_info,
    verify_bundle_signature: Callable[
        [Path], bool
    ] = default_verify_bundle_signature,
    classify_macho: Callable[[Path], bool] = default_classify_macho,
) -> ArtifactInspectionResult:
    app = Path(app_path)
    errors: list[str] = []
    if not app.is_dir():
        raise ArtifactInspectionError(f"app bundle missing: {app}")

    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        raise ArtifactInspectionError("Info.plist missing")
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)

    product = str(plist.get("CFBundleName") or plist.get("CFBundleDisplayName") or "")
    identifier = str(plist.get("CFBundleIdentifier") or "")
    version = str(plist.get("CFBundleShortVersionString") or "")
    min_os = str(plist.get("LSMinimumSystemVersion") or "")
    executable_name = str(plist.get("CFBundleExecutable") or config.product_name)

    if product != config.product_name:
        errors.append(f"product name mismatch: {product!r}")
    if identifier != config.bundle_id:
        errors.append(f"bundle identifier mismatch: {identifier!r}")
    if version != release_version:
        errors.append(f"version mismatch: {version!r} != {release_version!r}")
    required_min = _parse_version_tuple(config.minimum_macos)
    if not min_os or _parse_version_tuple(min_os) < required_min:
        errors.append(f"minimum OS {min_os!r} below required {config.minimum_macos}")

    executable = app / "Contents" / "MacOS" / executable_name
    if not executable.is_file():
        errors.append("missing executable")

    resources = app / "Contents" / "Resources"
    sidecar = (
        resources / "binaries" / "orpheus-desktop-backend" / "orpheus-desktop-backend"
    )
    if not sidecar.is_file():
        errors.append("missing sidecar")
    ffmpeg = resources / "binaries" / "orpheus-desktop-backend" / "bin" / "ffmpeg"
    if not ffmpeg.is_file():
        errors.append("missing ffmpeg")
    ffprobe = resources / "binaries" / "orpheus-desktop-backend" / "bin" / "ffprobe"
    if not ffprobe.is_file():
        errors.append("missing ffprobe")

    icon = _resolve_bundle_icon(resources, plist)
    if icon is None:
        errors.append("missing icon")

    if executable.is_file():
        try:
            resolved_exe = _resolve_contained_file(executable, app)
        except ArtifactInspectionError as exc:
            errors.append(str(exc))
            resolved_exe = None
        if resolved_exe is not None and not _has_embedded_frontend_assets(resolved_exe):
            errors.append("missing embedded frontend assets in main executable")
    elif "missing executable" not in errors:
        errors.append("missing frontend")

    known_binaries = (executable, sidecar, ffmpeg, ffprobe)
    try:
        macho_paths = _iter_macho_executables(
            app,
            known_paths=known_binaries,
            classify_macho=classify_macho,
        )
    except ArtifactInspectionError as exc:
        errors.append(str(exc))
        macho_paths = []

    for binary in macho_paths:
        archs = read_architectures(binary)
        if "arm64" not in archs:
            errors.append(f"missing arm64 in {binary.relative_to(app)}")
            continue
        for dep in read_dylibs(binary):
            if any(dep.startswith(prefix) for prefix in FORBIDDEN_DYLIB_PREFIXES):
                errors.append(f"forbidden absolute dylib path {dep} in {binary.name}")

    codesign_info = read_codesign_info(app)
    signing_state = codesign_info.state
    signing_detail = "app bundle inspection passed"
    if not _is_expected_unsigned_codesign(codesign_info):
        errors.append(_codesign_rejection_reason(codesign_info))
    elif signing_state is SigningState.ADHOC:
        if not verify_bundle_signature(app):
            errors.append(
                "ad-hoc bundle seal failed codesign --verify --deep --strict "
                "(missing or incomplete CodeResources / sealed resources)"
            )
        else:
            signing_detail = (
                "app bundle inspection passed; expected unsigned state recorded as "
                "a valid complete ad-hoc outer seal with TeamIdentifier not set "
                "(no distribution identity)"
            )
    elif signing_state is SigningState.UNSIGNED:
        signing_detail = "app bundle inspection passed; codesign reports unsigned"

    if errors:
        raise ArtifactInspectionError("; ".join(errors))

    return ArtifactInspectionResult(
        ok=True,
        path=str(app),
        signing_state=signing_state,
        sha256=sha256_file(executable) if executable.is_file() else None,
        detail=signing_detail,
    )


def _detach_with_retry(
    detach_dmg: Callable[[Path], None],
    mount_point: Path,
    *,
    attempts: int = DMG_DETACH_ATTEMPTS,
    delay_s: float = DMG_DETACH_RETRY_DELAY_S,
) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            detach_dmg(mount_point)
            return
        except Exception as exc:  # noqa: BLE001 - surface any detach failure
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(delay_s)
    raise ArtifactInspectionError(
        f"hdiutil detach failed for {mount_point} after {attempts} attempts: {last_error}"
    ) from last_error


def inspect_dmg(
    dmg_path: Path,
    *,
    product_name: str,
    release_version: str,
    verify_dmg: Callable[[Path], bool] = default_verify_dmg,
    mount_dmg: Callable[[Path, Path], Path] = default_mount_dmg,
    detach_dmg: Callable[[Path], None] = default_detach_dmg,
    list_mounted_entries: Callable[[Path], tuple[str, ...]] | None = None,
) -> ArtifactInspectionResult:
    dmg = Path(dmg_path)
    expected_name = f"{product_name}_{release_version}_aarch64.dmg"
    if dmg.name != expected_name:
        raise ArtifactInspectionError(
            f"DMG name {dmg.name!r} must exactly equal {expected_name!r}"
        )

    if not verify_dmg(dmg):
        raise ArtifactInspectionError("hdiutil verify failed")

    mount_point: Path | None = None
    temp_root = Path(tempfile.mkdtemp(prefix="orpheus-dmg-"))
    pending_errors: list[Exception] = []
    result: ArtifactInspectionResult | None = None
    try:
        mount_point = mount_dmg(dmg, temp_root)
        if list_mounted_entries is None:
            entries = tuple(
                sorted(
                    path.name
                    for path in mount_point.iterdir()
                    if path.name not in {".", ".."}
                )
            )
        else:
            entries = list_mounted_entries(mount_point)

        apps = [name for name in entries if name.endswith(".app")]
        if len(apps) != 1 or apps[0] != f"{product_name}.app":
            raise ArtifactInspectionError(
                f"mounted DMG must contain exactly one {product_name}.app; found {apps!r}"
            )
        extras = [
            name
            for name in entries
            if name != f"{product_name}.app" and name not in ALLOWED_DMG_PRESENTATION
        ]
        if extras:
            raise ArtifactInspectionError(
                f"unexpected DMG presentation files: {extras}"
            )
        result = ArtifactInspectionResult(
            ok=True,
            path=str(dmg),
            signing_state=None,
            sha256=sha256_file(dmg),
            detail="dmg inspection passed",
        )
    except Exception as exc:  # noqa: BLE001 - preserve and still detach
        pending_errors.append(exc)
    finally:
        if mount_point is not None:
            try:
                _detach_with_retry(detach_dmg, mount_point)
            except Exception as exc:  # noqa: BLE001 - detach is release-blocking
                pending_errors.append(exc)
        shutil.rmtree(temp_root, ignore_errors=True)

    if pending_errors:
        if len(pending_errors) == 1:
            raise pending_errors[0]
        raise ArtifactInspectionError(
            "; ".join(str(exc) for exc in pending_errors)
        )

    assert result is not None
    return result
