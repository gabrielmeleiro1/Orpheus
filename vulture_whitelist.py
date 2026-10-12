# ruff: noqa: F821
"""Reviewed vulture false positives for dynamic/framework/test entrypoints.

This file is passed to vulture as an allowlist. It is not imported at runtime.
"""

# Public callback/config/dataclass contracts.
updated_at
chapters_completed
chapters_total
supported_languages
supports_voice_cloning
supports_engine_options
is_local

# Dynamic runtime attributes and public compatibility hooks.
_.row_factory
_.get_book_by_source_path
_.create_chapter
_.is_available
_.capture_signals
_.from_dict

# FastAPI route handlers and lazy package exports are discovered dynamically.
get_maintenance_diagnostics
post_maintenance_backup
post_maintenance_cleanup
post_source_preflight
get_model_options
get_voice_options
list_openai_models
get_active_conversion
get_library_book
get_library_chapter_cleaning_preview
update_library_book
update_library_book_playback_position
open_library_book_folder
delete_library_book
get_library_audio
stream_events
post_voice_preview
post_voice_suggestion
upload_text_source
upload_source
enforce_desktop_api_token
list_library
__getattr__

# Test fixtures, pytest markers, and protocol fakes.
pytestmark
EPUB_MIMETYPE
abbreviation_text
registry_snapshot
_.created_chapters
_.shutdown_called
_.raise_for_status
_.mock_parser_deps
