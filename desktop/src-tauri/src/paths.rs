use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

pub const QUALIFICATION_RUNTIME_ROOT_ENV: &str = "ORPHEUS_QUALIFICATION_RUNTIME_ROOT";
pub const QUALIFICATION_RELEASE_ID_ENV: &str = "ORPHEUS_QUALIFICATION_RELEASE_ID";
pub const QUALIFICATION_SENTINEL_NAME: &str = ".orpheus-release-qualification";

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

/// Resolve desktop runtime paths for backend/setup launch.
///
/// With neither qualification env set, production behavior is unchanged: paths
/// are derived from the live app-data directory and inherited `AUDIOBOOK_*`
/// values are ignored (they are outputs of [`DesktopRuntimePaths::as_env`] only).
///
/// When either qualification key is present, both are required and the override
/// is fail-closed: invalid/incomplete input returns an actionable error and never
/// falls back to live app data.
pub fn resolve_desktop_runtime_paths(
    app_data_dir: impl AsRef<Path>,
) -> Result<DesktopRuntimePaths, String> {
    resolve_desktop_runtime_paths_with_env(
        app_data_dir,
        std::env::var_os(QUALIFICATION_RUNTIME_ROOT_ENV),
        std::env::var_os(QUALIFICATION_RELEASE_ID_ENV),
    )
}

pub fn resolve_desktop_runtime_paths_with_env(
    app_data_dir: impl AsRef<Path>,
    runtime_root: Option<OsString>,
    release_id: Option<OsString>,
) -> Result<DesktopRuntimePaths, String> {
    match (runtime_root, release_id) {
        (None, None) => Ok(DesktopRuntimePaths::from_app_data_dir(app_data_dir)),
        (Some(_), None) => Err(format!(
            "qualification override incomplete: {QUALIFICATION_RELEASE_ID_ENV} must be set with {QUALIFICATION_RUNTIME_ROOT_ENV}; refusing to fall back to live app data"
        )),
        (None, Some(_)) => Err(format!(
            "qualification override incomplete: {QUALIFICATION_RUNTIME_ROOT_ENV} must be set with {QUALIFICATION_RELEASE_ID_ENV}; refusing to fall back to live app data"
        )),
        (Some(root), Some(id)) => {
            let validated = validate_qualification_runtime_root(
                PathBuf::from(root),
                &id,
                app_data_dir.as_ref(),
            )?;
            Ok(DesktopRuntimePaths::from_app_data_dir(validated))
        }
    }
}

fn validate_qualification_runtime_root(
    root: PathBuf,
    release_id: &std::ffi::OsStr,
    live_app_data_dir: &Path,
) -> Result<PathBuf, String> {
    let release_id = release_id
        .to_str()
        .ok_or_else(|| {
            format!(
                "qualification override invalid: {QUALIFICATION_RELEASE_ID_ENV} must be valid UTF-8; refusing to fall back to live app data"
            )
        })?
        .trim();
    if release_id.is_empty() {
        return Err(format!(
            "qualification override invalid: {QUALIFICATION_RELEASE_ID_ENV} must be nonempty; refusing to fall back to live app data"
        ));
    }

    if !root.is_absolute() {
        return Err(format!(
            "qualification override invalid: {QUALIFICATION_RUNTIME_ROOT_ENV} must be an absolute path (got '{}'); refusing to fall back to live app data",
            root.display()
        ));
    }

    if root
        .symlink_metadata()
        .map(|meta| meta.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(format!(
            "qualification override unsafe: {QUALIFICATION_RUNTIME_ROOT_ENV} must not be a symlink ('{}'); refusing to fall back to live app data",
            root.display()
        ));
    }

    if !root.is_dir() {
        return Err(format!(
            "qualification override invalid: {QUALIFICATION_RUNTIME_ROOT_ENV} must be an existing directory ('{}'); refusing to fall back to live app data",
            root.display()
        ));
    }

    let canonical = root.canonicalize().map_err(|error| {
        format!(
            "qualification override unsafe: failed to canonicalize {QUALIFICATION_RUNTIME_ROOT_ENV} '{}': {error}; refusing to fall back to live app data",
            root.display()
        )
    })?;

    reject_broad_qualification_targets(&canonical, live_app_data_dir)?;

    let sentinel = root.join(QUALIFICATION_SENTINEL_NAME);
    if sentinel
        .symlink_metadata()
        .map(|meta| meta.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(format!(
            "qualification override unsafe: sentinel '{QUALIFICATION_SENTINEL_NAME}' must not be a symlink; refusing to fall back to live app data"
        ));
    }
    if !sentinel.is_file() {
        return Err(format!(
            "qualification override invalid: sentinel '{QUALIFICATION_SENTINEL_NAME}' missing in '{}'; refusing to fall back to live app data",
            root.display()
        ));
    }
    let contents = fs::read_to_string(&sentinel).map_err(|error| {
        format!(
            "qualification override invalid: failed to read sentinel '{}': {error}; refusing to fall back to live app data",
            sentinel.display()
        )
    })?;
    if contents.trim() != release_id {
        return Err(format!(
            "qualification override invalid: sentinel mismatch in '{}' (expected release id '{release_id}'); refusing to fall back to live app data",
            root.display()
        ));
    }

    // Fail-closed isolation: existing runtime children must be real paths
    // contained by the canonical root. Symlink escapes under data/output/temp
    // (or the DB/ready files) would otherwise bypass root/sentinel checks.
    validate_qualification_runtime_tree(&canonical)?;

    Ok(canonical)
}

fn path_is_within(parent: &Path, candidate: &Path) -> bool {
    candidate.starts_with(parent)
}

fn is_strict_ancestor(ancestor: &Path, descendant: &Path) -> bool {
    descendant.starts_with(ancestor) && descendant != ancestor
}

fn validate_existing_contained_dir(
    canonical_root: &Path,
    path: &Path,
    label: &str,
) -> Result<(), String> {
    let meta = match path.symlink_metadata() {
        Ok(meta) => meta,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(format!(
                "qualification override unsafe: failed to inspect '{label}' ('{}'): {error}; refusing to fall back to live app data",
                path.display()
            ))
        }
    };
    if meta.file_type().is_symlink() {
        return Err(format!(
            "qualification override unsafe: '{label}' must not be a symlink ('{}'); refusing to fall back to live app data",
            path.display()
        ));
    }
    if !meta.is_dir() {
        return Err(format!(
            "qualification override unsafe: '{label}' must be a directory ('{}'); refusing to fall back to live app data",
            path.display()
        ));
    }
    let resolved = path.canonicalize().map_err(|error| {
        format!(
            "qualification override unsafe: failed to canonicalize '{label}' ('{}'): {error}; refusing to fall back to live app data",
            path.display()
        )
    })?;
    if !path_is_within(canonical_root, &resolved) {
        return Err(format!(
            "qualification override unsafe: '{label}' escapes runtime root ('{}' -> '{}'); refusing to fall back to live app data",
            path.display(),
            resolved.display()
        ));
    }
    Ok(())
}

fn validate_existing_contained_file(
    canonical_root: &Path,
    path: &Path,
    label: &str,
) -> Result<(), String> {
    let meta = match path.symlink_metadata() {
        Ok(meta) => meta,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => {
            return Err(format!(
                "qualification override unsafe: failed to inspect '{label}' ('{}'): {error}; refusing to fall back to live app data",
                path.display()
            ))
        }
    };
    if meta.file_type().is_symlink() {
        return Err(format!(
            "qualification override unsafe: '{label}' must not be a symlink ('{}'); refusing to fall back to live app data",
            path.display()
        ));
    }
    if !meta.is_file() {
        return Err(format!(
            "qualification override unsafe: '{label}' must be a regular file ('{}'); refusing to fall back to live app data",
            path.display()
        ));
    }
    let resolved = path.canonicalize().map_err(|error| {
        format!(
            "qualification override unsafe: failed to canonicalize '{label}' ('{}'): {error}; refusing to fall back to live app data",
            path.display()
        )
    })?;
    if !path_is_within(canonical_root, &resolved) {
        return Err(format!(
            "qualification override unsafe: '{label}' escapes runtime root ('{}' -> '{}'); refusing to fall back to live app data",
            path.display(),
            resolved.display()
        ));
    }
    Ok(())
}

fn validate_qualification_runtime_tree(canonical_root: &Path) -> Result<(), String> {
    validate_existing_contained_dir(canonical_root, &canonical_root.join("data"), "data")?;
    validate_existing_contained_dir(canonical_root, &canonical_root.join("output"), "output")?;
    validate_existing_contained_dir(canonical_root, &canonical_root.join("temp"), "temp")?;
    validate_existing_contained_file(
        canonical_root,
        &canonical_root.join("data").join("audiobooks.db"),
        "audiobooks.db",
    )?;
    validate_existing_contained_file(
        canonical_root,
        &canonical_root.join("backend-ready.json"),
        "backend-ready.json",
    )?;
    Ok(())
}

fn reject_broad_qualification_targets(
    resolved: &Path,
    live_app_data_dir: &Path,
) -> Result<(), String> {
    if resolved == Path::new("/") {
        return Err(
            "qualification override refuse: runtime root must not be filesystem root '/'; refusing to fall back to live app data"
                .to_string(),
        );
    }

    if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
        let home_path = PathBuf::from(home);
        let home_resolved = home_path.canonicalize().unwrap_or(home_path);
        if resolved == home_resolved || is_strict_ancestor(resolved, &home_resolved) {
            return Err(
                "qualification override refuse: runtime root must not be the user home directory or an ancestor of it (broad target); refusing to fall back to live app data"
                    .to_string(),
            );
        }
    }

    if let Ok(live) = live_app_data_dir.canonicalize() {
        if resolved == live || is_strict_ancestor(resolved, &live) {
            return Err(
                "qualification override refuse: runtime root must not be the live app data directory or an ancestor of it (broad target); refusing to fall back to live app data"
                    .to_string(),
            );
        }
    } else if resolved == live_app_data_dir || is_strict_ancestor(resolved, live_app_data_dir) {
        return Err(
            "qualification override refuse: runtime root must not be the live app data directory or an ancestor of it (broad target); refusing to fall back to live app data"
                .to_string(),
        );
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        resolve_desktop_runtime_paths_with_env, DesktopRuntimePaths, QUALIFICATION_RELEASE_ID_ENV,
        QUALIFICATION_RUNTIME_ROOT_ENV, QUALIFICATION_SENTINEL_NAME,
    };
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    fn unique_temp_dir(label: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("unix epoch")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("orpheus-paths-{label}-{nanos}"));
        fs::create_dir_all(&path).expect("temp dir");
        path
    }

    fn write_qualification_runtime(root: &Path, release_id: &str) {
        fs::create_dir_all(root).expect("runtime root");
        fs::write(
            root.join(QUALIFICATION_SENTINEL_NAME),
            format!("{release_id}\n"),
        )
        .expect("sentinel");
        fs::create_dir_all(root.join("data")).expect("data");
        fs::create_dir_all(root.join("output")).expect("output");
        fs::create_dir_all(root.join("temp")).expect("temp");
    }

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
            Some(
                PathBuf::from("/tmp/audiobook-app")
                    .join("data")
                    .join("audiobooks.db")
            )
        );
        assert_eq!(
            env.get("AUDIOBOOK_DESKTOP_READY_FILE").map(PathBuf::from),
            Some(PathBuf::from("/tmp/audiobook-app").join("backend-ready.json"))
        );
    }

    #[test]
    fn no_qualification_override_uses_app_data_dir_unchanged() {
        let app_data = PathBuf::from("/tmp/live-app-data");
        let paths = resolve_desktop_runtime_paths_with_env(&app_data, None, None)
            .expect("production resolve");
        assert_eq!(paths, DesktopRuntimePaths::from_app_data_dir(&app_data));
    }

    #[test]
    fn missing_qualification_pair_fails_closed_without_fallback() {
        let app_data = unique_temp_dir("live-pair");
        let runtime = unique_temp_dir("qual-pair");
        write_qualification_runtime(&runtime, "1.0.0-final");

        let only_root = resolve_desktop_runtime_paths_with_env(
            &app_data,
            Some(runtime.as_os_str().to_owned()),
            None,
        )
        .expect_err("incomplete override must fail");
        assert!(
            only_root.contains(QUALIFICATION_RELEASE_ID_ENV),
            "error should name missing release id: {only_root}"
        );

        let only_id = resolve_desktop_runtime_paths_with_env(
            &app_data,
            None,
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("incomplete override must fail");
        assert!(
            only_id.contains(QUALIFICATION_RUNTIME_ROOT_ENV),
            "error should name missing runtime root: {only_id}"
        );

        let _ = fs::remove_dir_all(app_data);
        let _ = fs::remove_dir_all(runtime);
    }

    #[test]
    fn relative_qualification_runtime_root_is_rejected() {
        let app_data = unique_temp_dir("live-rel");
        let error = resolve_desktop_runtime_paths_with_env(
            &app_data,
            Some(std::ffi::OsString::from("relative/qualification-root")),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("relative root must fail");
        assert!(
            error.to_ascii_lowercase().contains("absolute"),
            "expected absolute-path error, got {error}"
        );
        let _ = fs::remove_dir_all(app_data);
    }

    #[test]
    fn mismatched_or_missing_sentinel_fails_closed() {
        let app_data = unique_temp_dir("live-sentinel");
        let missing = unique_temp_dir("qual-missing-sentinel");
        fs::create_dir_all(&missing).expect("root");

        let missing_err = resolve_desktop_runtime_paths_with_env(
            &app_data,
            Some(missing.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("missing sentinel must fail");
        assert!(
            missing_err.to_ascii_lowercase().contains("sentinel")
                || missing_err.contains(QUALIFICATION_SENTINEL_NAME),
            "{missing_err}"
        );

        let mismatched = unique_temp_dir("qual-mismatch");
        write_qualification_runtime(&mismatched, "other-release");
        let mismatch_err = resolve_desktop_runtime_paths_with_env(
            &app_data,
            Some(mismatched.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("mismatched sentinel must fail");
        assert!(
            mismatch_err.to_ascii_lowercase().contains("sentinel")
                || mismatch_err.to_ascii_lowercase().contains("mismatch"),
            "{mismatch_err}"
        );

        let _ = fs::remove_dir_all(app_data);
        let _ = fs::remove_dir_all(missing);
        let _ = fs::remove_dir_all(mismatched);
    }

    #[cfg(unix)]
    #[test]
    fn symlink_runtime_root_is_rejected() {
        let app_data = unique_temp_dir("live-symlink");
        let real = unique_temp_dir("qual-real");
        write_qualification_runtime(&real, "1.0.0-final");
        let link_parent = unique_temp_dir("qual-link-parent");
        let link = link_parent.join("runtime-link");
        std::os::unix::fs::symlink(&real, &link).expect("symlink");

        let error = resolve_desktop_runtime_paths_with_env(
            &app_data,
            Some(link.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("symlink root must fail");
        assert!(
            error.to_ascii_lowercase().contains("symlink")
                || error.to_ascii_lowercase().contains("unsafe"),
            "{error}"
        );

        let _ = fs::remove_dir_all(app_data);
        let _ = fs::remove_dir_all(real);
        let _ = fs::remove_dir_all(link_parent);
    }

    #[test]
    fn valid_qualification_runtime_root_builds_paths_from_override() {
        let app_data = unique_temp_dir("live-valid");
        let runtime = unique_temp_dir("qual-valid");
        write_qualification_runtime(&runtime, "1.0.0-final");

        let paths = resolve_desktop_runtime_paths_with_env(
            &app_data,
            Some(runtime.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect("valid override");

        let canonical = runtime.canonicalize().expect("canonical runtime");
        assert_eq!(paths.app_data_dir, canonical);
        assert_eq!(paths.data_dir, canonical.join("data"));
        assert_eq!(paths.db_path, canonical.join("data").join("audiobooks.db"));
        assert_ne!(paths.app_data_dir, app_data);

        let _ = fs::remove_dir_all(app_data);
        let _ = fs::remove_dir_all(runtime);
    }

    #[test]
    fn audiobook_env_injection_cannot_redirect_production_resolve() {
        // Inherited AUDIOBOOK_* must never select the runtime root. Resolution
        // only consults the qualification override pair (or the live app-data
        // directory); AUDIOBOOK_* are outputs via as_env(), never inputs.
        let app_data = PathBuf::from("/tmp/live-app-data-ignore-audiobook");
        let paths = resolve_desktop_runtime_paths_with_env(&app_data, None, None)
            .expect("production resolve");
        assert_eq!(paths, DesktopRuntimePaths::from_app_data_dir(&app_data));
        assert_eq!(
            paths.data_dir,
            PathBuf::from("/tmp/live-app-data-ignore-audiobook/data")
        );
        assert_ne!(
            paths.data_dir,
            PathBuf::from("/untrusted/attacker-library/data")
        );
    }

    #[test]
    fn broad_targets_like_root_home_and_live_app_data_are_rejected() {
        let app_data = unique_temp_dir("live-broad");
        write_qualification_runtime(&app_data, "1.0.0-final");

        let root_err = resolve_desktop_runtime_paths_with_env(
            &app_data,
            Some(std::ffi::OsString::from("/")),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("filesystem root must fail");
        assert!(
            root_err.to_ascii_lowercase().contains("refuse")
                || root_err.to_ascii_lowercase().contains("unsafe")
                || root_err.contains('/'),
            "{root_err}"
        );

        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            let home_err = resolve_desktop_runtime_paths_with_env(
                &app_data,
                Some(home),
                Some(std::ffi::OsString::from("1.0.0-final")),
            )
            .expect_err("home directory itself must fail");
            assert!(
                home_err.to_ascii_lowercase().contains("home")
                    || home_err.to_ascii_lowercase().contains("refuse")
                    || home_err.to_ascii_lowercase().contains("unsafe"),
                "{home_err}"
            );
        }

        let live_err = resolve_desktop_runtime_paths_with_env(
            &app_data,
            Some(app_data.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("live app data root must fail");
        assert!(
            live_err.to_ascii_lowercase().contains("app data")
                || live_err.to_ascii_lowercase().contains("refuse")
                || live_err.to_ascii_lowercase().contains("unsafe"),
            "{live_err}"
        );

        let _ = fs::remove_dir_all(app_data);
    }

    #[test]
    fn qualification_env_key_names_match_contract() {
        assert_eq!(
            QUALIFICATION_RUNTIME_ROOT_ENV,
            "ORPHEUS_QUALIFICATION_RUNTIME_ROOT"
        );
        assert_eq!(
            QUALIFICATION_RELEASE_ID_ENV,
            "ORPHEUS_QUALIFICATION_RELEASE_ID"
        );
        assert_eq!(
            QUALIFICATION_SENTINEL_NAME,
            ".orpheus-release-qualification"
        );
    }

    #[cfg(unix)]
    #[test]
    fn child_directory_symlink_escape_is_rejected() {
        let live = unique_temp_dir("live-child-symlink");
        fs::create_dir_all(live.join("data")).expect("live data");
        let runtime = unique_temp_dir("qual-child-symlink");
        write_qualification_runtime(&runtime, "1.0.0-final");
        // Replace data/ with a symlink that escapes to live app data.
        fs::remove_dir_all(runtime.join("data")).expect("remove data");
        std::os::unix::fs::symlink(live.join("data"), runtime.join("data")).expect("data symlink");

        let error = resolve_desktop_runtime_paths_with_env(
            &live,
            Some(runtime.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("child directory symlink escape must fail");
        assert!(
            error.to_ascii_lowercase().contains("symlink")
                || error.to_ascii_lowercase().contains("unsafe")
                || error.to_ascii_lowercase().contains("escape"),
            "{error}"
        );

        let _ = fs::remove_dir_all(live);
        let _ = fs::remove_dir_all(runtime);
    }

    #[cfg(unix)]
    #[test]
    fn existing_db_or_ready_file_symlink_escape_is_rejected() {
        let live = unique_temp_dir("live-file-symlink");
        let runtime = unique_temp_dir("qual-file-symlink");
        write_qualification_runtime(&runtime, "1.0.0-final");
        let outside = unique_temp_dir("outside-db");
        let outside_db = outside.join("audiobooks.db");
        fs::write(&outside_db, b"live").expect("outside db");
        std::os::unix::fs::symlink(&outside_db, runtime.join("data").join("audiobooks.db"))
            .expect("db symlink");

        let error = resolve_desktop_runtime_paths_with_env(
            &live,
            Some(runtime.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("db symlink escape must fail");
        assert!(
            error.to_ascii_lowercase().contains("symlink")
                || error.to_ascii_lowercase().contains("unsafe")
                || error.to_ascii_lowercase().contains("escape"),
            "{error}"
        );

        let _ = fs::remove_dir_all(live);
        let _ = fs::remove_dir_all(runtime);
        let _ = fs::remove_dir_all(outside);
    }

    #[test]
    fn ancestor_of_live_app_data_or_home_is_rejected_but_descendant_evidence_is_allowed() {
        let home = unique_temp_dir("fake-home-ancestor");
        let live = home
            .join("Library")
            .join("Application Support")
            .join("Orpheus");
        fs::create_dir_all(&live).expect("live app data");
        // Broad target: ancestor of live app data (and of a nested home-like tree).
        let ancestor = home.join("Library");
        write_qualification_runtime(&ancestor, "1.0.0-final");

        let ancestor_err = resolve_desktop_runtime_paths_with_env(
            &live,
            Some(ancestor.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect_err("ancestor of live app data must fail");
        assert!(
            ancestor_err.to_ascii_lowercase().contains("ancestor")
                || ancestor_err.to_ascii_lowercase().contains("refuse")
                || ancestor_err.to_ascii_lowercase().contains("broad")
                || ancestor_err.to_ascii_lowercase().contains("unsafe"),
            "{ancestor_err}"
        );

        // Normal descendant evidence root under the fake home must still be accepted.
        let evidence = home.join("evidence").join("qual-run");
        write_qualification_runtime(&evidence, "1.0.0-final");
        let paths = resolve_desktop_runtime_paths_with_env(
            &live,
            Some(evidence.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect("descendant evidence root must be allowed");
        assert_eq!(
            paths.app_data_dir,
            evidence.canonicalize().expect("canonical evidence")
        );

        let _ = fs::remove_dir_all(home);
    }
}
