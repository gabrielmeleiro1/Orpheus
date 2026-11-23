"""Smoke the final DMG's embedded backend and record the qualified bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .artifact import (
    ArtifactInspectionError,
    _detach_with_retry,
    default_detach_dmg,
    default_mount_dmg,
    sha256_file,
)

SIDECAR_RELATIVE_PATH = Path("Contents/Resources/binaries/orpheus-desktop-backend")
SIDECAR_EXECUTABLE = "orpheus-desktop-backend"


def _tree_record(path: Path, relative: str, file_digest: str | None) -> bytes:
    if path.is_symlink():
        record = [relative, "symlink", str(path.readlink())]
    elif path.is_file():
        record = [relative, "file", path.stat().st_mode & 0o777, file_digest or sha256_file(path)]
    else:
        record = [relative, "directory"]
    return json.dumps(record, separators=(",", ":")).encode() + b"\n"


def sidecar_tree_digest(root: Path) -> str:
    """Include resource paths, contents, executable modes, and symlink targets."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if not path.resolve().is_relative_to(root.resolve()):
            raise ArtifactInspectionError(f"sidecar resource escapes its bundle: {relative}")
        digest.update(_tree_record(path, relative, None))
    return digest.hexdigest()


def app_tree_digest(app: Path) -> str:
    if not app.is_dir():
        raise ArtifactInspectionError(f"app bundle missing: {app}")
    return sidecar_tree_digest(app)


def _reached_without_symlinks(root: Path, nested: Path) -> bool:
    current = root
    for part in nested.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return False
    return True


def app_and_sidecar_digests(app: Path) -> tuple[str, str, str | None]:
    """Return app_tree_digest, the sidecar's tree digest and its executable's hash.

    One walk of the app yields all three, identical to what app_tree_digest,
    sidecar_tree_digest and sha256_file compute; the sidecar subtree keeps its
    own containment check. The executable hash is None when the walk did not
    hash it as a regular file (missing, or a symlink).
    """
    if not app.is_dir():
        raise ArtifactInspectionError(f"app bundle missing: {app}")
    sidecar_root = app / SIDECAR_RELATIVE_PATH
    executable = sidecar_root / SIDECAR_EXECUTABLE
    if not _reached_without_symlinks(app, sidecar_root):
        # rglob from the app root would not descend into this sidecar the way
        # a walk rooted at the sidecar does, so hash it separately.
        return app_tree_digest(app), sidecar_tree_digest(sidecar_root), None
    app_resolved = app.resolve()
    sidecar_resolved = sidecar_root.resolve()
    app_digest = hashlib.sha256()
    sidecar_digest = hashlib.sha256()
    executable_digest: str | None = None
    sidecar_escape: str | None = None
    for path in sorted(app.rglob("*")):
        relative = path.relative_to(app).as_posix()
        resolved = path.resolve()
        if not resolved.is_relative_to(app_resolved):
            raise ArtifactInspectionError(f"sidecar resource escapes its bundle: {relative}")
        file_digest = (
            sha256_file(path) if not path.is_symlink() and path.is_file() else None
        )
        app_digest.update(_tree_record(path, relative, file_digest))
        if not path.is_relative_to(sidecar_root) or path == sidecar_root:
            continue
        sidecar_relative = path.relative_to(sidecar_root).as_posix()
        if sidecar_escape is None and not resolved.is_relative_to(sidecar_resolved):
            # The separate walks check the whole app before the sidecar.
            sidecar_escape = sidecar_relative
        sidecar_digest.update(_tree_record(path, sidecar_relative, file_digest))
        if path == executable and file_digest is not None:
            executable_digest = file_digest
    if sidecar_escape is not None:
        raise ArtifactInspectionError(f"sidecar resource escapes its bundle: {sidecar_escape}")
    return app_digest.hexdigest(), sidecar_digest.hexdigest(), executable_digest


def artifact_fingerprint(dmg_digest: str, app_digest: str) -> str:
    return hashlib.sha256(json.dumps([dmg_digest, app_digest]).encode()).hexdigest()


def run_bundled_smoke(argv, *, cwd, check=True, timeout=180):
    from scripts.desktop_runtime_smoke import (
        _own_windows_process_tree, _resume_windows_process, stop_backend,
    )

    # The backend inherits this group so a timeout also reaches its descendants.
    env = dict(os.environ, ORPHEUS_SMOKE_PROCESS_GROUP="1")
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, start_new_session=os.name != "nt",
        creationflags=0x4 if os.name == "nt" else 0,
    )
    try:
        if os.name == "nt":
            try:
                _own_windows_process_tree(process)
                _resume_windows_process(process)
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                raise
        returncode = process.wait(timeout=timeout)
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, argv)
        return subprocess.CompletedProcess(argv, returncode)
    finally:
        if os.name == "nt":
            stop_backend(process)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Allow descendants to handle termination before forcing the group down.
            time.sleep(0.2)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:
                    raise
            process.wait(timeout=5)


def qualify_bundled_runtime(
    *, app: Path, dmg: Path, repo_root: Path,
    run=run_bundled_smoke, mount_dmg=default_mount_dmg, detach_dmg=default_detach_dmg,
) -> dict[str, str]:
    # Each checkpoint walks each tree once; the before/after pairs stay separate
    # because the smoke runs between them.
    image_digest = sha256_file(dmg)
    app_digest, source_digest, _ = app_and_sidecar_digests(app)
    with tempfile.TemporaryDirectory(prefix="orpheus-qualified-dmg-") as temp:
        mount = mount_dmg(dmg, Path(temp))
        try:
            shipped_app = mount / app.name
            sidecar = shipped_app / SIDECAR_RELATIVE_PATH / SIDECAR_EXECUTABLE
            shipped_app_digest, embedded_digest, executable_digest = (
                app_and_sidecar_digests(shipped_app)
            )
            if executable_digest is None:
                executable_digest = sha256_file(sidecar)
            if embedded_digest != source_digest:
                raise ArtifactInspectionError("DMG and app bundle sidecar resources differ")
            if shipped_app_digest != app_digest:
                raise ArtifactInspectionError("DMG and app bundle trees differ")
            run(
                [
                    sys.executable, str(repo_root / "scripts/desktop_runtime_smoke.py"),
                    "--backend", "sidecar", "--app-bundle", str(shipped_app),
                    "--reduced-path", "--check-ffmpeg-runtime", "--require-local-tts",
                    "--probe-wait-seconds", "30", "--timeout-seconds", "60",
                ],
                cwd=repo_root, check=True, timeout=180,
            )
            if (
                app_and_sidecar_digests(shipped_app)[:2] != (shipped_app_digest, embedded_digest)
                or sha256_file(dmg) != image_digest
                or app_and_sidecar_digests(app)[:2] != (app_digest, source_digest)
            ):
                raise ArtifactInspectionError("packaged runtime changed during qualification")
        finally:
            _detach_with_retry(detach_dmg, mount)
    return {
        "app": str(app), "dmg": str(dmg), "dmg_sha256": image_digest,
        "app_tree_sha256": app_digest,
        "sidecar_sha256": executable_digest, "sidecar_tree_sha256": embedded_digest,
    }


def _raise_system_exit(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args()
    # The command runner's timeout sends SIGTERM first; unwinding through the
    # finally blocks stops the smoke and detaches the mounted DMG.
    signal.signal(signal.SIGTERM, _raise_system_exit)
    tauri = args.repo_root / "desktop/src-tauri"
    config = json.loads((tauri / "tauri.conf.json").read_text(encoding="utf-8"))
    product, version = config["productName"], config["version"]
    bundle = tauri / "target/release/bundle"
    evidence = qualify_bundled_runtime(
        app=bundle / "macos" / f"{product}.app",
        dmg=bundle / "dmg" / f"{product}_{version}_aarch64.dmg",
        repo_root=args.repo_root,
    )
    for key in ("app", "dmg"):
        evidence[key] = Path(evidence[key]).resolve().relative_to(args.repo_root.resolve()).as_posix()
    # The qualification command runner retains stdout in its evidence log.
    print(json.dumps(evidence, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
