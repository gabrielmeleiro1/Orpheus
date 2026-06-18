"""Download model snapshots required by Orpheus runtimes."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.tts.runtime_spec import KOKORO_PACKAGE_DEFAULT_MODEL


DEFAULT_MODEL_IDS = (KOKORO_PACKAGE_DEFAULT_MODEL,)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download model assets required for local Orpheus runtimes.",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help=(
            "Hugging Face model id to download. May be supplied more than once. "
            "Defaults to the package Kokoro model."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Optional Hugging Face cache directory. Defaults to the user cache.",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=None,
        help=(
            "Optional folder for a flat copy of the downloaded snapshot. "
            "Use this when building an offline/bundled package."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the model ids that would be downloaded without fetching them.",
    )
    return parser


def download_model_snapshot(
    model_id: str,
    *,
    cache_dir: Path | None = None,
    local_dir: Path | None = None,
) -> Path:
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface-hub is required to download model assets. "
            "Install requirements.txt first."
        ) from exc

    kwargs: dict[str, object] = {"repo_id": model_id}
    if cache_dir is not None:
        kwargs["cache_dir"] = str(cache_dir)
    if local_dir is not None:
        kwargs["local_dir"] = str(local_dir)

    snapshot_path = snapshot_download(**kwargs)
    return Path(snapshot_path)


def download_required_models(
    model_ids: Iterable[str],
    *,
    cache_dir: Path | None = None,
    local_dir: Path | None = None,
    dry_run: bool = False,
) -> list[Path]:
    snapshots: list[Path] = []
    for model_id in model_ids:
        normalized_model_id = model_id.strip()
        if not normalized_model_id:
            continue
        if dry_run:
            print(f"Would download {normalized_model_id}")
            continue
        snapshot_path = download_model_snapshot(
            normalized_model_id,
            cache_dir=cache_dir,
            local_dir=local_dir,
        )
        snapshots.append(snapshot_path)
        print(f"Downloaded {normalized_model_id} -> {snapshot_path}")
    return snapshots


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    model_ids = tuple(args.models or DEFAULT_MODEL_IDS)
    snapshots = download_required_models(
        model_ids,
        cache_dir=args.cache_dir,
        local_dir=args.local_dir,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return 0
    if not snapshots:
        print("No model ids were provided.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
