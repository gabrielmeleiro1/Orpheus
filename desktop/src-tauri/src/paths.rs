use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DesktopRuntimePaths {
    pub app_data_dir: PathBuf,
    pub data_dir: PathBuf,
    pub output_dir: PathBuf,
    pub temp_dir: PathBuf,
    pub db_path: PathBuf,
    pub ready_file_path: PathBuf,
}

impl DesktopRuntimePaths {
    pub fn from_app_data_dir(app_data_dir: impl AsRef<Path>) -> Self {
        let app_data_dir = app_data_dir.as_ref().to_path_buf();
        let data_dir = app_data_dir.join("data");
        let output_dir = app_data_dir.join("output");
        let temp_dir = app_data_dir.join("temp");
        let db_path = data_dir.join("audiobooks.db");
        let ready_file_path = app_data_dir.join("backend-ready.json");

        Self {
            app_data_dir,
            data_dir,
            output_dir,
            temp_dir,
            db_path,
            ready_file_path,
        }
    }

    pub fn as_env(&self) -> BTreeMap<String, String> {
        let mut env = BTreeMap::new();
        env.insert(
            "AUDIOBOOK_DATA_DIR".to_string(),
            self.data_dir.to_string_lossy().into_owned(),
        );
        env.insert(
            "AUDIOBOOK_OUTPUT_DIR".to_string(),
            self.output_dir.to_string_lossy().into_owned(),
        );
        env.insert(
            "AUDIOBOOK_TEMP_DIR".to_string(),
            self.temp_dir.to_string_lossy().into_owned(),
        );
        env.insert(
            "AUDIOBOOK_DB_PATH".to_string(),
            self.db_path.to_string_lossy().into_owned(),
        );
        env.insert(
            "AUDIOBOOK_DESKTOP_READY_FILE".to_string(),
            self.ready_file_path.to_string_lossy().into_owned(),
        );
        env
    }
}

#[cfg(test)]
mod tests {
    use super::DesktopRuntimePaths;
    use std::path::PathBuf;

    #[test]
    fn app_data_path_generation_matches_desktop_contract() {
        let app_data_root = PathBuf::from("/tmp/audiobook-app");
        let runtime_paths = DesktopRuntimePaths::from_app_data_dir(&app_data_root);

        assert_eq!(runtime_paths.data_dir, app_data_root.join("data"));
        assert_eq!(runtime_paths.output_dir, app_data_root.join("output"));
        assert_eq!(runtime_paths.temp_dir, app_data_root.join("temp"));
        assert_eq!(
            runtime_paths.db_path,
            app_data_root.join("data").join("audiobooks.db")
        );
        assert_eq!(
            runtime_paths.ready_file_path,
            app_data_root.join("backend-ready.json")
        );
    }

    #[test]
    fn app_data_env_generation_uses_expected_variable_names() {
        let runtime_paths = DesktopRuntimePaths::from_app_data_dir("/tmp/audiobook-app");
        let env = runtime_paths.as_env();

        assert_eq!(
            env.get("AUDIOBOOK_DATA_DIR").map(PathBuf::from),
            Some(PathBuf::from("/tmp/audiobook-app").join("data"))
        );
        assert_eq!(
            env.get("AUDIOBOOK_OUTPUT_DIR").map(PathBuf::from),
            Some(PathBuf::from("/tmp/audiobook-app").join("output"))
        );
        assert_eq!(
            env.get("AUDIOBOOK_TEMP_DIR").map(PathBuf::from),
            Some(PathBuf::from("/tmp/audiobook-app").join("temp"))
        );
        assert_eq!(
            env.get("AUDIOBOOK_DB_PATH").map(PathBuf::from),
            Some(PathBuf::from("/tmp/audiobook-app").join("data").join("audiobooks.db"))
        );
        assert_eq!(
            env.get("AUDIOBOOK_DESKTOP_READY_FILE").map(PathBuf::from),
            Some(PathBuf::from("/tmp/audiobook-app").join("backend-ready.json"))
        );
    }
}
