"""Download model snapshots required by Orpheus runtimes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.hf_cache import configure_hf_cache  # noqa: E402

configure_hf_cache()

from modules.runtime_setup.catalog import (  # noqa: E402
    CLEANER_CUDA_DEFAULT_MODEL as CLEANER_CUDA_DEFAULT_MODEL,
    CLEANER_MLX_DEFAULT_MODEL as CLEANER_MLX_DEFAULT_MODEL,
    KOKORO_MLX_DEFAULT_MODEL_REPO as KOKORO_MLX_DEFAULT_MODEL_REPO,
    KOKORO_PACKAGE_DEFAULT_MODEL as KOKORO_PACKAGE_DEFAULT_MODEL,
    DEFAULT_MODEL_IDS as DEFAULT_MODEL_IDS,
    _REVISION_RE as _REVISION_RE,
    MODEL_GROUPS as MODEL_GROUPS,
    MODEL_REVISIONS as MODEL_REVISIONS,
    _normalize_model_id as _normalize_model_id,
    pinned_revision_for as pinned_revision_for,
    _deduplicate_model_ids as _deduplicate_model_ids,
    resolve_model_groups as resolve_model_groups,
)
from modules.runtime_setup.receipts import (  # noqa: E402
    DOWNLOAD_RECEIPT_FILENAME as DOWNLOAD_RECEIPT_FILENAME,
    DOWNLOAD_RECEIPT_SCHEMA_VERSION as DOWNLOAD_RECEIPT_SCHEMA_VERSION,
    _SHA256_RE as _SHA256_RE,
    _MAX_RECEIPT_BYTES as _MAX_RECEIPT_BYTES,
    _HASH_CHUNK_BYTES as _HASH_CHUNK_BYTES,
    _ReceiptFile as _ReceiptFile,
    _ParsedReceipt as _ParsedReceipt,
    _is_transient_path as _is_transient_path,
    _resolved_allowed_file_target as _resolved_allowed_file_target,
    _allowed_file_target as _allowed_file_target,
    _file_size_and_sha256 as _file_size_and_sha256,
    _snapshot_manifest as _snapshot_manifest,
    _revision_identity as _revision_identity,
    write_download_receipt as write_download_receipt,
    _valid_receipt_relative_path as _valid_receipt_relative_path,
    _stat_identity as _stat_identity,
    _read_receipt_json as _read_receipt_json,
    _parse_download_receipt as _parse_download_receipt,
    _snapshot_metadata_fingerprint as _snapshot_metadata_fingerprint,
    _validate_parsed_receipt as _validate_parsed_receipt,
    ModelReceiptVerifier as ModelReceiptVerifier,
    validate_download_receipt as validate_download_receipt,
)
from modules.runtime_setup.downloads import (  # noqa: E402
    download_model_snapshot as download_model_snapshot,
    _local_dir_for_model as _local_dir_for_model,
    download_required_models as download_required_models,
)


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
        "--group",
        action="append",
        dest="groups",
        help=(
            "Curated model group to download. May be supplied more than once. "
            "Use --list-groups to see the allowlist."
        ),
    )
    parser.add_argument(
        "--list-groups",
        action="store_true",
        help="List curated model groups and exit.",
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
            "Optional folder for downloaded snapshots. A single model uses the "
            "folder directly; multiple models use deterministic subfolders."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the model ids that would be downloaded without fetching them.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON Lines output.",
    )
    return parser


def _print_groups(*, json_output: bool) -> None:
    groups = [
        {"id": group_id, "models": list(model_ids)}
        for group_id, model_ids in MODEL_GROUPS.items()
    ]
    if json_output:
        print(json.dumps({"groups": groups}, sort_keys=True))
        return
    for group in groups:
        print(f"{group['id']}: {', '.join(group['models'])}")


def _print_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps({"error": message, "event": "error"}, sort_keys=True),
            file=sys.stderr,
        )
        return
    print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_groups:
        _print_groups(json_output=args.json)
        return 0

    try:
        grouped_model_ids = resolve_model_groups(args.groups or ())
        if grouped_model_ids or args.models:
            model_ids = _deduplicate_model_ids((*grouped_model_ids, *(args.models or ())))
        else:
            model_ids = DEFAULT_MODEL_IDS
    except ValueError as exc:
        _print_error(str(exc), json_output=args.json)
        return 2

    if args.json:
        print(
            json.dumps(
                {
                    "dry_run": args.dry_run,
                    "event": "plan",
                    "models": list(model_ids),
                },
                sort_keys=True,
            ),
            flush=True,
        )
    else:
        print(f"Models to download: {', '.join(model_ids)}", flush=True)

    try:
        snapshots = download_required_models(
            model_ids,
            cache_dir=args.cache_dir,
            local_dir=args.local_dir,
            dry_run=args.dry_run,
            json_output=args.json,
        )
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        _print_error(f"Model download failed: {detail}", json_output=args.json)
        return 1
    if args.dry_run:
        return 0
    if not snapshots:
        print("No model ids were provided.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
