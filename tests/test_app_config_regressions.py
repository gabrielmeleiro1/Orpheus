"""Regression coverage for app config."""

from pathlib import Path

from modules.app.config import AppConfig

from tests._factories_group2_paths import isolated_paths  # noqa: F401  (autouse fixture)


def test_application_defaults_use_working_directory_without_desktop_overrides(tmp_path, monkeypatch):
    for key in ("AUDIOBOOK_DATA_DIR", "AUDIOBOOK_OUTPUT_DIR", "AUDIOBOOK_TEMP_DIR", "AUDIOBOOK_DB_PATH"):
        monkeypatch.delenv(key)
    config = AppConfig()
    assert (config.data_dir, config.output_dir, config.temp_dir, config.db_path) == (
        Path("data"), Path("output"), Path("temp"), Path("data/audiobooks.db"),
    )
    assert all((tmp_path / name).is_dir() for name in ("data", "output", "temp"))
    assert not config.db_path.exists()


def test_application_paths_honor_explicit_values_before_environment(tmp_path):
    inherited = AppConfig()
    assert inherited.data_dir == tmp_path / "data"
    assert inherited.output_dir == tmp_path / "output"
    assert inherited.temp_dir == tmp_path / "temp"
    assert inherited.db_path == tmp_path / "data/audiobooks.db"
    explicit = AppConfig(
        data_dir=tmp_path / "custom/data", output_dir=tmp_path / "custom/output",
        temp_dir=tmp_path / "custom/temp", db_path=tmp_path / "separate/library.sqlite",
    )
    assert explicit.data_dir == tmp_path / "custom/data"
    assert explicit.output_dir == tmp_path / "custom/output"
    assert explicit.temp_dir == tmp_path / "custom/temp"
    assert explicit.db_path == tmp_path / "separate/library.sqlite"
    assert explicit.db_path.parent.is_dir()
