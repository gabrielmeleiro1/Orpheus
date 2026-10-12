from modules.app.config import AppConfig


def make_app_config(tmp_path, **overrides):
    values = {
        "data_dir": tmp_path / "data",
        "output_dir": tmp_path / "output",
        "temp_dir": tmp_path / "temp",
        "db_path": tmp_path / "data" / "app.db",
    }
    values.update(overrides)
    return AppConfig(**values)
