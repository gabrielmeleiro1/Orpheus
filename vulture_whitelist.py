# ruff: noqa: F821
"""Reviewed vulture false positives for dynamic/framework/test entrypoints.

This file is passed to vulture as an allowlist. It is not imported at runtime.
"""

# Public callback/config/dataclass contracts.
on_chapter_complete
updated_at
chapters_completed
chapters_total
supported_languages
supports_voice_cloning
supports_engine_options
is_local

# Reserved error code and public error API surface.
E006
E102
E200
E201
E302
E400
E401
M4BPackagingError
InvalidFilenameError
check_disk_space
estimate_audio_size

# Dynamic runtime attributes and public compatibility hooks.
_._doc
_.row_factory
_.patch_load_weights_compat
_.patch_generation_result_compat
_.runtime_error_message
_.ensure_runtime
_.current_model
_.on_pressure_change
_.on_unload
_.on_warning
_.probe_mlx_cleaner_runtime
_.get_book_by_source_path
_.create_chapter
_.estimate_duration
_.healthcheck
_.is_available
_.get_voice
_.capture_signals
_.chunk_generator
_.model_context
_.unregister
_.get_cover
_.from_dict
_.get_usage_trend

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
TestTextChunkerStatefulTests
_.created_chapters
_.shutdown_called
_.raise_for_status
_.change_config
_.chunks_are_valid
_.mock_parser_deps
