"""Tests for macOS app bundle / DMG artifact inspection."""

from __future__ import annotations

import plistlib
import struct
import sys
from pathlib import Path

import pytest

from tools.release_qualification.artifact import (
    ArtifactInspectionError,
    ArtifactInspectionResult,
    CodesignInfo,
    SigningState,
    build_bundle_commands,
    build_packaging_commands,
    build_sidecar_commands,
    expected_app_bundle_path,
    expected_dmg_path,
    inspect_app_bundle,
    inspect_dmg,
    parse_codesign_info,
    sha256_file,
)
from tools.release_qualification.commands import CommandSpec
from tools.release_qualification.model import load_qualification_config

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "release_qualification.toml"

# Markers Tauri 2 embeds from frontendDist into the main executable.
EMBEDDED_FRONTEND_BYTES = b"index.html\x00/assets/index-DjJ3LFsN.js\x00Orpheus"


def _write_plist(path: Path, **overrides: object) -> None:
    payload = {
        "CFBundleName": "Orpheus",
        "CFBundleDisplayName": "Orpheus",
        "CFBundleIdentifier": "com.orpheus.desktop",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleExecutable": "orpheus_desktop",
        "CFBundleIconFile": "Orpheus.icns",
        "LSMinimumSystemVersion": "14.0",
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


def _macho_arm64_header() -> bytes:
    # Minimal Mach-O 64-bit header with CPU_TYPE_ARM64 (0x0100000c).
    magic = 0xFEEDFACF
    cputype = 0x0100000C
    return struct.pack("<IIIIIIII", magic, cputype, 0, 0, 0, 0, 0, 0)


def _make_app(
    root: Path,
    *,
    include_executable: bool = True,
    include_sidecar: bool = True,
    include_ffmpeg: bool = True,
    include_ffprobe: bool = True,
    include_icon: bool = True,
    include_embedded_frontend: bool = True,
    macho_bytes: bytes | None = None,
    executable_payload: bytes | None = None,
    plist_overrides: dict[str, object] | None = None,
) -> Path:
    app = root / "Orpheus.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    binaries = resources / "binaries" / "orpheus-desktop-backend"
    bin_dir = binaries / "bin"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    _write_plist(contents / "Info.plist", **(plist_overrides or {}))

    payload = macho_bytes if macho_bytes is not None else _macho_arm64_header()
    if include_executable:
        exe = macos / "orpheus_desktop"
        body = executable_payload if executable_payload is not None else payload
        if include_embedded_frontend and executable_payload is None:
            body = payload + EMBEDDED_FRONTEND_BYTES
        exe.write_bytes(body)
        exe.chmod(0o755)
    if include_sidecar:
        sidecar = binaries / "orpheus-desktop-backend"
        sidecar.write_bytes(payload)
        sidecar.chmod(0o755)
    if include_ffmpeg:
        (bin_dir / "ffmpeg").write_bytes(payload)
        (bin_dir / "ffmpeg").chmod(0o755)
    if include_ffprobe:
        (bin_dir / "ffprobe").write_bytes(payload)
        (bin_dir / "ffprobe").chmod(0o755)
    if include_icon:
        (resources / "Orpheus.icns").write_bytes(b"icns")
    return app


def test_deterministic_bundle_paths():
    config = load_qualification_config(DEFAULT_CONFIG)
    app = expected_app_bundle_path(REPO_ROOT, config)
    dmg = expected_dmg_path(REPO_ROOT, version="1.0.0")
    assert app == REPO_ROOT / "desktop/src-tauri/target/release/bundle/macos/Orpheus.app"
    assert dmg == (
        REPO_ROOT
        / "desktop/src-tauri/target/release/bundle/dmg/Orpheus_1.0.0_aarch64.dmg"
    )
    assert "aarch64" in dmg.name
    assert "1.0.0" in dmg.name


def test_build_packaging_commands_are_argv_only():
    specs = build_packaging_commands(repo_root=REPO_ROOT)
    assert all(isinstance(spec, CommandSpec) for spec in specs)
    assert all(spec.shell is False for spec in specs)
    by_label = {spec.label: spec for spec in specs}
    python = str(REPO_ROOT / ".venv" / "bin" / "python")
    assert by_label["build-sidecar"].argv == (
        python,
        "scripts/build_desktop_backend.py",
        "--runtime-profile",
        "mlx",
    )
    assert by_label["build-sidecar"].cwd == str(REPO_ROOT)
    assert by_label["tauri-desktop-build"].argv == ("npm", "run", "desktop:build")
    assert by_label["tauri-desktop-build"].cwd == str(REPO_ROOT / "desktop")


def test_sidecar_and_bundle_command_helpers_are_disjoint():
    sidecar = build_sidecar_commands(repo_root=REPO_ROOT)
    bundle = build_bundle_commands(repo_root=REPO_ROOT)
    assert [spec.label for spec in sidecar] == [
        "build-sidecar",
        "sidecar-runtime-smoke",
    ]
    assert [spec.label for spec in bundle] == ["tauri-desktop-build"]
    assert build_packaging_commands(repo_root=REPO_ROOT) == sidecar + bundle


def test_sidecar_runtime_smoke_requires_local_tts_without_downloads():
    specs = build_sidecar_commands(repo_root=REPO_ROOT)
    by_label = {spec.label: spec for spec in specs}
    python = str(REPO_ROOT / ".venv" / "bin" / "python")
    smoke = by_label["sidecar-runtime-smoke"]
    assert smoke.argv[0] == python
    assert smoke.argv[1:3] == ("scripts/desktop_runtime_smoke.py", "--backend")
    assert "sidecar" in smoke.argv
    assert "--require-local-tts" in smoke.argv
    assert "--reduced-path" in smoke.argv
    assert "--check-ffmpeg-runtime" in smoke.argv
    sidecar_path = (
        REPO_ROOT
        / "desktop"
        / "src-tauri"
        / "binaries"
        / "orpheus-desktop-backend"
        / "orpheus-desktop-backend"
    )
    assert "--sidecar-path" in smoke.argv
    assert str(sidecar_path) in smoke.argv
    assert smoke.cwd == str(REPO_ROOT)
    # Guardrail: packaged smoke must stay offline / non-network.
    assert "--conversion-journey" not in smoke.argv
    joined = " ".join(smoke.argv)
    assert "download" not in joined.lower()
    assert "http" not in joined.lower()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_info_plist_product_identifier_version_and_min_os(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path)
    result = inspect_app_bundle(app, config=config, release_version="1.0.0")
    assert isinstance(result, ArtifactInspectionResult)
    assert result.ok is True

    bad = _make_app(
        tmp_path / "bad",
        plist_overrides={
            "CFBundleIdentifier": "com.wrong.id",
            "LSMinimumSystemVersion": "13.0",
            "CFBundleShortVersionString": "0.9.0",
        },
    )
    with pytest.raises(ArtifactInspectionError, match="identifier|version|14|minimum"):
        inspect_app_bundle(bad, config=config, release_version="1.0.0")


@pytest.mark.parametrize(
    "missing",
    ["executable", "sidecar", "ffmpeg", "ffprobe", "icon", "frontend"],
)
@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_missing_required_bundle_contents_fail(tmp_path: Path, missing: str):
    config = load_qualification_config(DEFAULT_CONFIG)
    kwargs = {
        "include_executable": missing != "executable",
        "include_sidecar": missing != "sidecar",
        "include_ffmpeg": missing != "ffmpeg",
        "include_ffprobe": missing != "ffprobe",
        "include_icon": missing != "icon",
        "include_embedded_frontend": missing != "frontend",
    }
    app = _make_app(tmp_path, **kwargs)
    with pytest.raises(ArtifactInspectionError, match=missing):
        inspect_app_bundle(app, config=config, release_version="1.0.0")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_ffmpeg_ffprobe_must_live_under_sidecar_bin(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path, include_ffmpeg=False, include_ffprobe=False)
    binaries = app / "Contents" / "Resources" / "binaries" / "orpheus-desktop-backend"
    # Legacy layout (incorrect for current Tauri resource packaging).
    (binaries / "ffmpeg").write_bytes(_macho_arm64_header())
    (binaries / "ffprobe").write_bytes(_macho_arm64_header())
    with pytest.raises(ArtifactInspectionError, match="ffmpeg|ffprobe"):
        inspect_app_bundle(app, config=config, release_version="1.0.0")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_icon_uses_configured_icns_from_plist(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path, include_icon=False)
    resources = app / "Contents" / "Resources"
    (resources / "Orpheus.icns").write_bytes(b"icns")
    result = inspect_app_bundle(app, config=config, release_version="1.0.0")
    assert result.ok is True

    stray_only = _make_app(
        tmp_path / "stray",
        include_icon=False,
        plist_overrides={"CFBundleIconFile": "Missing.icns"},
    )
    (stray_only / "Contents" / "Resources" / "stray.icns").write_bytes(b"icns")
    with pytest.raises(ArtifactInspectionError, match="icon"):
        inspect_app_bundle(stray_only, config=config, release_version="1.0.0")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_frontend_requires_embedded_tauri_assets_not_loose_html(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path, include_embedded_frontend=False)
    resources = app / "Contents" / "Resources"
    (resources / "_up_" / "index.html").parent.mkdir(parents=True)
    (resources / "_up_" / "index.html").write_text("<html></html>", encoding="utf-8")
    # Source dist beside the fake app must not satisfy the gate either.
    dist = tmp_path / "desktop" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html><title>Orpheus Desktop</title></html>")
    with pytest.raises(ArtifactInspectionError, match="frontend|embedded"):
        inspect_app_bundle(app, config=config, release_version="1.0.0")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_macho_executables_must_contain_arm64(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    # CPU_TYPE_X86_64
    x86 = struct.pack("<IIIIIIII", 0xFEEDFACF, 0x01000007, 0, 0, 0, 0, 0, 0)
    app = _make_app(tmp_path, macho_bytes=x86, include_embedded_frontend=False)
    # Keep frontend markers while forcing non-arm64 arch reporting.
    exe = app / "Contents" / "MacOS" / "orpheus_desktop"
    exe.write_bytes(x86 + EMBEDDED_FRONTEND_BYTES)

    def architectures(_path: Path) -> tuple[str, ...]:
        return ("x86_64",)

    with pytest.raises(ArtifactInspectionError, match="arm64"):
        inspect_app_bundle(
            app,
            config=config,
            release_version="1.0.0",
            read_architectures=architectures,
        )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_forbidden_absolute_build_machine_dylib_paths_fail(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path)

    def dylibs(path: Path) -> tuple[str, ...]:
        if path.name == "orpheus_desktop":
            return ("/Users/builder/lib/libfoo.dylib", "@rpath/libbar.dylib")
        return ("/usr/lib/libSystem.B.dylib",)

    with pytest.raises(ArtifactInspectionError, match="absolute|/Users/"):
        inspect_app_bundle(
            app,
            config=config,
            release_version="1.0.0",
            read_dylibs=dylibs,
        )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_codesign_unsigned_and_no_team_adhoc_pass_distribution_identities_fail(
    tmp_path: Path,
):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path)

    def unsigned(_path: Path) -> CodesignInfo:
        return CodesignInfo(
            state=SigningState.UNSIGNED,
            team_identifier=None,
            flags="",
            authorities=(),
            raw_text="code object is not signed at all",
        )

    result = inspect_app_bundle(
        app,
        config=config,
        release_version="1.0.0",
        read_codesign_info=unsigned,
    )
    assert result.signing_state is SigningState.UNSIGNED
    assert result.ok is True

    def adhoc_no_team(_path: Path) -> CodesignInfo:
        return CodesignInfo(
            state=SigningState.ADHOC,
            team_identifier=None,
            flags="adhoc",
            authorities=(),
            raw_text=(
                "flags=0x2(adhoc)\n"
                "Signature=adhoc\n"
                "TeamIdentifier=not set\n"
            ),
        )

    def seal_ok(_path: Path) -> bool:
        return True

    adhoc_result = inspect_app_bundle(
        app,
        config=config,
        release_version="1.0.0",
        read_codesign_info=adhoc_no_team,
        verify_bundle_signature=seal_ok,
    )
    assert adhoc_result.ok is True
    assert adhoc_result.signing_state is SigningState.ADHOC
    assert "ad-hoc" in adhoc_result.detail.lower() or "adhoc" in adhoc_result.detail.lower()
    assert "team" in adhoc_result.detail.lower() or "unsigned" in adhoc_result.detail.lower()

    def developer_id(_path: Path) -> CodesignInfo:
        return CodesignInfo(
            state=SigningState.DEVELOPER_ID,
            team_identifier="TEAMID",
            flags="",
            authorities=("Developer ID Application: Example (TEAMID)",),
            raw_text="Authority=Developer ID Application: Example (TEAMID)\n",
        )

    with pytest.raises(ArtifactInspectionError, match="Developer ID|identity"):
        inspect_app_bundle(
            app,
            config=config,
            release_version="1.0.0",
            read_codesign_info=developer_id,
        )

    def adhoc_with_team(_path: Path) -> CodesignInfo:
        return CodesignInfo(
            state=SigningState.ADHOC,
            team_identifier="ABCDE12345",
            flags="adhoc",
            authorities=(),
            raw_text="Signature=adhoc\nTeamIdentifier=ABCDE12345\n",
        )

    with pytest.raises(ArtifactInspectionError, match="TeamIdentifier|identity|team"):
        inspect_app_bundle(
            app,
            config=config,
            release_version="1.0.0",
            read_codesign_info=adhoc_with_team,
        )

    def apple_distribution(_path: Path) -> CodesignInfo:
        return CodesignInfo(
            state=SigningState.OTHER,
            team_identifier="TEAMID",
            flags="",
            authorities=("Apple Distribution: Example (TEAMID)",),
            raw_text="Authority=Apple Distribution: Example (TEAMID)\n",
        )

    with pytest.raises(ArtifactInspectionError, match="Distribution|identity|signing"):
        inspect_app_bundle(
            app,
            config=config,
            release_version="1.0.0",
            read_codesign_info=apple_distribution,
        )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_adhoc_invalid_seal_fails_valid_no_team_seal_passes(tmp_path: Path):
    """ADHOC with TeamIdentifier not set still requires a valid outer bundle seal."""
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path)

    def adhoc_no_team(_path: Path) -> CodesignInfo:
        return CodesignInfo(
            state=SigningState.ADHOC,
            team_identifier=None,
            flags="adhoc",
            authorities=(),
            raw_text=(
                "Identifier=com.orpheus.desktop\n"
                "Signature=adhoc\n"
                "TeamIdentifier=not set\n"
            ),
        )

    def seal_invalid(_path: Path) -> bool:
        return False

    with pytest.raises(
        ArtifactInspectionError,
        match=r"seal|verify|CodeResources|resources",
    ):
        inspect_app_bundle(
            app,
            config=config,
            release_version="1.0.0",
            read_codesign_info=adhoc_no_team,
            verify_bundle_signature=seal_invalid,
        )

    def seal_valid(_path: Path) -> bool:
        return True

    ok_result = inspect_app_bundle(
        app,
        config=config,
        release_version="1.0.0",
        read_codesign_info=adhoc_no_team,
        verify_bundle_signature=seal_valid,
    )
    assert ok_result.ok is True
    assert ok_result.signing_state is SigningState.ADHOC

    def unsigned(_path: Path) -> CodesignInfo:
        return CodesignInfo(
            state=SigningState.UNSIGNED,
            team_identifier=None,
            flags="",
            authorities=(),
            raw_text="code object is not signed at all",
        )

    # Truly unsigned remains allowed even if a seal probe would fail.
    unsigned_result = inspect_app_bundle(
        app,
        config=config,
        release_version="1.0.0",
        read_codesign_info=unsigned,
        verify_bundle_signature=seal_invalid,
    )
    assert unsigned_result.ok is True
    assert unsigned_result.signing_state is SigningState.UNSIGNED


def test_hdiutil_verify_failure_blocks_release(tmp_path: Path):
    dmg = tmp_path / "Orpheus_1.0.0_aarch64.dmg"
    dmg.write_bytes(b"fake-dmg")

    def verify(_path: Path) -> bool:
        return False

    with pytest.raises(ArtifactInspectionError, match="hdiutil verify|verify"):
        inspect_dmg(
            dmg,
            product_name="Orpheus",
            release_version="1.0.0",
            verify_dmg=verify,
            list_mounted_entries=lambda _mount: ("Orpheus.app", ".DS_Store"),
        )


def test_mounted_dmg_contents_and_safe_detach(tmp_path: Path):
    dmg = tmp_path / "Orpheus_1.0.0_aarch64.dmg"
    dmg.write_bytes(b"fake-dmg")
    mount_calls: list[Path] = []
    detach_calls: list[Path] = []

    def mount(path: Path, mount_root: Path) -> Path:
        mount_calls.append(path)
        mounted = mount_root / "volume"
        mounted.mkdir(parents=True)
        (mounted / "Orpheus.app").mkdir()
        (mounted / "Applications").symlink_to("/Applications")
        return mounted

    def detach(mount_point: Path) -> None:
        detach_calls.append(mount_point)

    def verify(_path: Path) -> bool:
        return True

    result = inspect_dmg(
        dmg,
        product_name="Orpheus",
        release_version="1.0.0",
        verify_dmg=verify,
        mount_dmg=mount,
        detach_dmg=detach,
        list_mounted_entries=None,
    )
    assert result.ok is True
    assert mount_calls == [dmg]
    assert len(detach_calls) == 1

    def bad_entries(_mount: Path) -> tuple[str, ...]:
        return ("Orpheus.app", "Extra.app", "README.txt")

    with pytest.raises(ArtifactInspectionError, match="exactly one|presentation|Extra"):
        inspect_dmg(
            dmg,
            product_name="Orpheus",
            release_version="1.0.0",
            verify_dmg=verify,
            mount_dmg=mount,
            detach_dmg=detach,
            list_mounted_entries=bad_entries,
        )
    assert len(detach_calls) >= 2


def test_dmg_name_requires_version_and_aarch64(tmp_path: Path):
    bad = tmp_path / "Orpheus_1.0.0.dmg"
    bad.write_bytes(b"x")
    with pytest.raises(ArtifactInspectionError, match="aarch64|name"):
        inspect_dmg(
            bad,
            product_name="Orpheus",
            release_version="1.0.0",
            verify_dmg=lambda _p: True,
            list_mounted_entries=lambda _m: ("Orpheus.app",),
            mount_dmg=lambda _p, root: root,
            detach_dmg=lambda _m: None,
        )


def test_dmg_name_requires_exact_basename(tmp_path: Path):
    """Substring soft-matches must not pass; basename must be exact."""
    for name in (
        "WrongApp_1.0.0_aarch64.dmg",
        "Orpheus_11.0.0_aarch64.dmg",
        "Orpheus_1.0.0_aarch64_extra.dmg",
        "prefix_Orpheus_1.0.0_aarch64.dmg",
    ):
        bad = tmp_path / name
        bad.write_bytes(b"x")
        with pytest.raises(ArtifactInspectionError, match="name|exact|expected"):
            inspect_dmg(
                bad,
                product_name="Orpheus",
                release_version="1.0.0",
                verify_dmg=lambda _p: True,
                list_mounted_entries=lambda _m: ("Orpheus.app",),
                mount_dmg=lambda _p, root: root,
                detach_dmg=lambda _m: None,
            )


def test_sha256_streaming_changes_when_bytes_change(tmp_path: Path):
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"abc" * 100_000)
    first = sha256_file(path)
    assert len(first) == 64
    path.write_bytes(b"abd" * 100_000)
    second = sha256_file(path)
    assert first != second


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_stray_icns_or_html_does_not_satisfy_required_layout(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(
        tmp_path,
        include_icon=False,
        include_embedded_frontend=False,
        plist_overrides={"CFBundleIconFile": "Orpheus.icns"},
    )
    resources = app / "Contents" / "Resources"
    (resources / "stray.icns").write_bytes(b"icns")
    nested = resources / "nested" / "dist"
    nested.mkdir(parents=True)
    (nested / "index.html").write_text("<html></html>", encoding="utf-8")
    with pytest.raises(ArtifactInspectionError, match="icon|frontend"):
        inspect_app_bundle(app, config=config, release_version="1.0.0")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_symlink_executable_inside_bundle_is_inspected(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path)
    exe = app / "Contents" / "MacOS" / "orpheus_desktop"
    real = app / "Contents" / "MacOS" / "orpheus_desktop.bin"
    real.write_bytes(exe.read_bytes())
    real.chmod(0o644)  # not +x: old executable-bit heuristics must still find it
    exe.unlink()
    exe.symlink_to(real.name)

    inspected: list[Path] = []

    def architectures(path: Path) -> tuple[str, ...]:
        inspected.append(path)
        return ("arm64",)

    def dylibs(_path: Path) -> tuple[str, ...]:
        return ("/usr/lib/libSystem.B.dylib",)

    result = inspect_app_bundle(
        app,
        config=config,
        release_version="1.0.0",
        read_architectures=architectures,
        read_dylibs=dylibs,
    )
    assert result.ok is True
    assert any(path.resolve() == real.resolve() for path in inspected)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_escaped_symlink_executable_is_rejected(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path)
    outside = tmp_path / "outside-bin"
    outside.write_bytes(_macho_arm64_header() + EMBEDDED_FRONTEND_BYTES)
    outside.chmod(0o755)
    exe = app / "Contents" / "MacOS" / "orpheus_desktop"
    exe.unlink()
    exe.symlink_to(outside)
    with pytest.raises(ArtifactInspectionError, match="symlink|escape|outside"):
        inspect_app_bundle(app, config=config, release_version="1.0.0")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_plus_x_script_is_skipped_not_reported_as_missing_arm64(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path)
    script = app / "Contents" / "MacOS" / "helper.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)

    def architectures(path: Path) -> tuple[str, ...]:
        if path.suffix == ".sh":
            raise AssertionError("scripts must not be treated as Mach-O")
        return ("arm64",)

    def dylibs(_path: Path) -> tuple[str, ...]:
        return ("/usr/lib/libSystem.B.dylib",)

    result = inspect_app_bundle(
        app,
        config=config,
        release_version="1.0.0",
        read_architectures=architectures,
        read_dylibs=dylibs,
        classify_macho=lambda path: path.suffix != ".sh" and path.name != "helper.sh",
    )
    assert result.ok is True


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_bundle_directory_symlink_escape_is_not_followed(tmp_path: Path):
    config = load_qualification_config(DEFAULT_CONFIG)
    app = _make_app(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = outside / "evil-macho"
    evil.write_bytes(_macho_arm64_header())
    evil.chmod(0o755)
    link = app / "Contents" / "Resources" / "escape"
    link.symlink_to(outside)

    inspected: list[str] = []

    def architectures(path: Path) -> tuple[str, ...]:
        inspected.append(str(path))
        return ("arm64",)

    def dylibs(_path: Path) -> tuple[str, ...]:
        return ("/usr/lib/libSystem.B.dylib",)

    result = inspect_app_bundle(
        app,
        config=config,
        release_version="1.0.0",
        read_architectures=architectures,
        read_dylibs=dylibs,
    )
    assert result.ok is True
    assert not any("evil-macho" in item for item in inspected)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS bundle inspection tools and executable semantics",
)
def test_architecture_tool_failure_raises_clear_error(tmp_path: Path):
    from tools.release_qualification.artifact import default_read_architectures

    missing = tmp_path / "no-such-binary"
    missing.write_bytes(b"not-macho")
    with pytest.raises(ArtifactInspectionError, match="architecture|lipo|file"):
        default_read_architectures(missing)


def test_parse_codesign_output_classifies_unsigned_adhoc_developer_id_and_other():
    unsigned_text = "code object is not signed at all"
    unsigned_info = parse_codesign_info(unsigned_text, returncode=1)
    assert unsigned_info.state is SigningState.UNSIGNED
    assert unsigned_info.team_identifier is None

    adhoc_text = (
        "Authority=Unknown\nSignature=adhoc\nflags=0x20002(adhoc,linker-signed)\n"
        "TeamIdentifier=not set\n"
    )
    adhoc_info = parse_codesign_info(adhoc_text, returncode=0)
    assert adhoc_info.state is SigningState.ADHOC
    assert adhoc_info.team_identifier is None
    assert "linker-signed" in adhoc_info.flags

    developer_text = "Authority=Developer ID Application: Example (TEAMID)\n"
    assert (
        parse_codesign_info(developer_text, returncode=0).state
        is SigningState.DEVELOPER_ID
    )

    assert (
        parse_codesign_info(
            "Authority=Apple Development: Example (TEAMID)\n",
            returncode=0,
        ).state
        is SigningState.OTHER
    )
    distribution_info = parse_codesign_info(
        "Authority=Apple Distribution: Example (TEAMID)\nTeamIdentifier=TEAMID\n",
        returncode=0,
    )
    assert distribution_info.state is SigningState.OTHER
    assert distribution_info.team_identifier == "TEAMID"


def test_dmg_detach_failure_is_release_blocking_after_retry(tmp_path: Path):
    dmg = tmp_path / "Orpheus_1.0.0_aarch64.dmg"
    dmg.write_bytes(b"fake-dmg")
    detach_calls: list[Path] = []

    def mount(path: Path, mount_root: Path) -> Path:
        mounted = mount_root / "volume"
        mounted.mkdir(parents=True)
        (mounted / "Orpheus.app").mkdir()
        return mounted

    def detach(mount_point: Path) -> None:
        detach_calls.append(mount_point)
        raise ArtifactInspectionError(f"hdiutil detach failed for {mount_point}")

    with pytest.raises(ArtifactInspectionError, match="detach"):
        inspect_dmg(
            dmg,
            product_name="Orpheus",
            release_version="1.0.0",
            verify_dmg=lambda _p: True,
            mount_dmg=mount,
            detach_dmg=detach,
            list_mounted_entries=None,
        )
    assert len(detach_calls) >= 2
