#[cfg(unix)]
use super::setup::resolve_setup_python;
#[cfg(windows)]
use super::setup::windows_taskkill_path;
use super::setup::{
    build_setup_launch_config_for_platform,
    build_setup_launch_config_for_platform_with_runtime_paths, cancel_with_sink_for_test,
    read_progress_events, runtime_setup_environment_for_test, sanitize_progress_line,
    setup_command_env_for_test, start_supervised_for_test, start_supervised_with_hook_for_test,
    windows_setup_creation_flags, EventSink, SetupLaunchConfig, SetupLifecycle, SetupProgressEvent,
    SetupRequest, SetupRunnerState, WindowsLaunchPhase, SETUP_PROGRESS_EVENT,
};
use super::{
    restart_after_setup_transaction, restart_after_setup_with_readiness, wait_for_backend_ready,
    BackendLifecycle,
};
use crate::paths::DesktopRuntimePaths;
use std::fs;
use std::io::Cursor;
use std::path::{Path, PathBuf};
#[cfg(unix)]
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn request(profile: &str, groups: &[&str], accept_pytorch_risk: bool) -> SetupRequest {
    SetupRequest {
        profile: profile.to_string(),
        model_groups: groups.iter().map(|value| (*value).to_string()).collect(),
        accept_pytorch_risk,
    }
}

#[test]
fn windows_setup_is_constructed_suspended_and_in_its_own_process_group() {
    let flags = windows_setup_creation_flags();
    assert_ne!(flags & 0x0000_0004, 0, "CREATE_SUSPENDED is required");
    assert_ne!(
        flags & 0x0000_0200,
        0,
        "CREATE_NEW_PROCESS_GROUP is required"
    );
}

#[test]
fn windows_launch_protocol_requires_job_assignment_before_resume() {
    let mut phase = WindowsLaunchPhase::Built;
    assert!(phase.mark_resumed().is_err());
    phase.mark_spawned_suspended().expect("suspended spawn");
    assert!(phase.mark_resumed().is_err());
    phase.mark_job_assigned().expect("job assignment");
    phase.mark_resumed().expect("resume after containment");
    assert_eq!(phase, WindowsLaunchPhase::Resumed);
}

#[test]
fn setup_launch_is_a_fixed_shell_free_command() {
    let repo = Path::new("/trusted/orpheus");
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir("/trusted/app-data");
    let launch = build_setup_launch_config_for_platform_with_runtime_paths(
        repo,
        "/trusted/python",
        request("mlx", &["tts-mlx", "cleaner-mlx"], false),
        "macos",
        "aarch64",
        &runtime_paths,
    )
    .expect("valid MLX setup");

    assert_eq!(launch.program, PathBuf::from("/trusted/python"));
    assert_eq!(launch.cwd, repo);
    assert_eq!(launch.script, repo.join("scripts").join("setup_runtime.py"));
    assert_eq!(
        launch.args,
        vec![
            repo.join("scripts")
                .join("setup_runtime.py")
                .to_string_lossy(),
            "--profile".into(),
            "mlx".into(),
            "--models".into(),
            "tts-mlx".into(),
            "--models".into(),
            "cleaner-mlx".into(),
            "--progress-json".into(),
        ]
    );
    assert!(!launch.uses_shell);
    assert_eq!(launch.env, runtime_paths.as_env());
}

#[test]
fn setup_launch_overrides_inherited_desktop_paths_with_backend_contract() {
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir("/authoritative/app-data");
    let launch = build_setup_launch_config_for_platform_with_runtime_paths(
        Path::new("/trusted/orpheus"),
        "/trusted/python",
        request("base", &[], false),
        "macos",
        "aarch64",
        &runtime_paths,
    )
    .expect("valid base setup");
    let configured = setup_command_env_for_test(
        &launch,
        &[
            ("AUDIOBOOK_DATA_DIR", "/untrusted/data"),
            ("AUDIOBOOK_OUTPUT_DIR", "/untrusted/output"),
            ("AUDIOBOOK_TEMP_DIR", "/untrusted/temp"),
            ("AUDIOBOOK_DB_PATH", "/untrusted/database"),
            ("AUDIOBOOK_DESKTOP_READY_FILE", "/untrusted/ready"),
        ],
    );

    for (key, expected) in runtime_paths.as_env() {
        assert_eq!(configured.get(&key), Some(&expected), "wrong {key}");
    }
    assert_eq!(
        configured
            .get("ORPHEUS_TAURI_SETUP_SUPERVISED")
            .map(String::as_str),
        Some("1")
    );
}

#[test]
fn packaged_runtime_setup_never_uses_a_source_build_path() {
    let environment = runtime_setup_environment_for_test(true, None);
    assert!(!environment.available);
    assert_eq!(environment.mode, "packaged");
    assert!(environment.reason.contains("profile-baked"));
}

#[test]
fn source_runtime_setup_requires_fixed_script_and_repository_venv() {
    let repo = unique_temp_dir("environment-missing");
    fs::create_dir_all(repo.join("scripts")).expect("scripts");
    let environment = runtime_setup_environment_for_test(false, Some(&repo));
    assert!(!environment.available);
    assert_eq!(environment.mode, "source_checkout");
    assert!(environment.reason.contains("setup_runtime.py"));
    let _ = fs::remove_dir_all(repo);
}

#[cfg(unix)]
#[test]
fn source_runtime_setup_reports_available_only_for_fixed_repo_assets() {
    use std::os::unix::fs::symlink;
    let repo = unique_temp_dir("environment-ready");
    fs::create_dir_all(repo.join("scripts")).expect("scripts");
    fs::write(repo.join("scripts/setup_runtime.py"), "# fixed setup").expect("script");
    fs::create_dir_all(repo.join(".venv/bin")).expect("venv");
    symlink("/bin/sh", repo.join(".venv/bin/python")).expect("python");
    let environment = runtime_setup_environment_for_test(false, Some(&repo));
    assert!(environment.available);
    assert_eq!(environment.mode, "source_checkout");
    assert!(environment.reason.is_empty());
    let _ = fs::remove_dir_all(repo);
}

#[test]
fn setup_validation_rejects_unknown_mismatched_and_injection_values() {
    let repo = Path::new("/trusted/orpheus");
    for invalid in [
        request("unknown", &[], false),
        request("mlx", &["cleaner-cuda"], false),
        request("mlx; rm -rf /", &[], false),
        request("mlx", &["tts-mlx; open /tmp"], false),
        request("mlx", &["../../model"], false),
    ] {
        assert!(build_setup_launch_config_for_platform(
            repo,
            "/trusted/python",
            invalid,
            "macos",
            "aarch64"
        )
        .is_err());
    }

    assert!(build_setup_launch_config_for_platform(
        repo,
        "/trusted/python",
        request("cuda", &["tts-cpu-cuda"], true),
        "macos",
        "aarch64"
    )
    .is_err());
    assert!(build_setup_launch_config_for_platform(
        repo,
        "/trusted/python",
        request("mlx", &["tts-mlx"], false),
        "linux",
        "x86_64"
    )
    .is_err());
}

#[test]
fn cuda_ignores_obsolete_risk_acknowledgement_without_forwarding_a_flag() {
    let repo = Path::new("/trusted/orpheus");
    let launch_without_ack = build_setup_launch_config_for_platform(
        repo,
        "/trusted/python",
        request("cuda", &["cleaner-cuda"], false),
        "linux",
        "x86_64",
    )
    .expect("CUDA no longer requires a resolved advisory acknowledgement");
    assert!(!launch_without_ack
        .args
        .iter()
        .any(|arg| arg == "--accept-pytorch-risk"));

    let launch = build_setup_launch_config_for_platform(
        repo,
        "/trusted/python",
        request("cuda", &["cleaner-cuda"], true),
        "linux",
        "x86_64",
    )
    .expect("CUDA setup");
    assert!(!launch.args.iter().any(|arg| arg == "--accept-pytorch-risk"));
}

#[test]
fn setup_guard_rejects_concurrent_runs_and_clears_on_terminal_state() {
    let state = SetupRunnerState::default();
    let generation = state
        .begin(request("base", &[], false))
        .expect("first setup should start");
    assert!(state.begin(request("base", &[], false)).is_err());

    state.finish_success(generation, true);
    assert_eq!(state.status().lifecycle, SetupLifecycle::Complete);
    assert!(state.begin(request("base", &[], false)).is_ok());
}

#[test]
fn running_setup_status_retains_latest_sanitized_progress_snapshot() {
    let state = SetupRunnerState::default();
    let generation = state
        .begin(request("mlx", &["tts-mlx"], false))
        .expect("run");

    assert!(state.record_progress(
        generation,
        SetupProgressEvent {
            event: "stage_started".to_string(),
            stage: Some("model_download".to_string()),
            profile: None,
            model_groups: Vec::new(),
            message: Some("Downloading models".to_string()),
            percent: Some(42.0),
            terminal: None,
            restart_required: None,
        },
    ));

    let status = state.status();
    assert_eq!(status.stage.as_deref(), Some("model_download"));
    assert_eq!(status.message.as_deref(), Some("Downloading models"));
    assert_eq!(status.percent, Some(42.0));
    let serialized = serde_json::to_value(status).expect("status json");
    assert_eq!(serialized["stage"], "model_download");
    assert_eq!(serialized["message"], "Downloading models");
    assert_eq!(serialized["percent"], 42.0);
}

#[test]
fn new_setup_generation_clears_the_previous_progress_snapshot() {
    let state = SetupRunnerState::default();
    let first = state.begin(request("base", &[], false)).expect("first run");
    state.record_progress(
        first,
        SetupProgressEvent {
            event: "stage_started".to_string(),
            stage: Some("verify".to_string()),
            profile: None,
            model_groups: Vec::new(),
            message: Some("Verifying setup".to_string()),
            percent: Some(90.0),
            terminal: None,
            restart_required: None,
        },
    );
    state.finish_failure(first, SetupLifecycle::Failed, "failed");

    let second = state
        .begin(request("base", &[], false))
        .expect("second run");
    let status = state.status();
    assert_ne!(first, second);
    assert_eq!(status.stage, None);
    assert_eq!(status.message, None);
    assert_eq!(status.percent, None);
}

#[test]
fn successful_setup_status_owns_terminal_percent_100() {
    let state = SetupRunnerState::default();
    let generation = state.begin(request("base", &[], false)).expect("run");
    state.record_progress(
        generation,
        SetupProgressEvent {
            event: "stage_started".to_string(),
            stage: Some("verify".to_string()),
            profile: None,
            model_groups: Vec::new(),
            message: Some("Verifying setup".to_string()),
            percent: Some(99.0),
            terminal: None,
            restart_required: None,
        },
    );

    assert!(state.finish_success(generation, true));
    let status = state.status();
    assert_eq!(status.lifecycle, SetupLifecycle::Complete);
    assert_eq!(status.percent, Some(100.0));
}

#[test]
fn restart_authorization_requires_successful_restart_required_setup() {
    let idle = SetupRunnerState::default();
    assert!(idle.acquire_restart_authorization().is_err());

    let running = SetupRunnerState::default();
    let generation = running.begin(request("base", &[], false)).expect("run");
    assert!(running.acquire_restart_authorization().is_err());
    assert!(running.finish_failure(generation, SetupLifecycle::Failed, "failed"));
    assert!(running.acquire_restart_authorization().is_err());

    let no_restart = SetupRunnerState::default();
    let generation = no_restart.begin(request("base", &[], false)).expect("run");
    assert!(no_restart.finish_success(generation, false));
    assert!(no_restart.acquire_restart_authorization().is_err());

    let authorized = SetupRunnerState::default();
    let generation = authorized.begin(request("base", &[], false)).expect("run");
    assert!(authorized.finish_success(generation, true));
    assert!(authorized.acquire_restart_authorization().is_ok());
}

#[test]
fn restart_authorization_rejects_every_non_complete_lifecycle() {
    let cancelling = SetupRunnerState::default();
    let generation = cancelling.begin(request("base", &[], false)).expect("run");
    cancelling.cancel().expect("cancel request");
    assert!(cancelling.acquire_restart_authorization().is_err());
    assert!(cancelling.finish_failure(generation, SetupLifecycle::Cancelled, "cancelled"));
    assert!(cancelling.acquire_restart_authorization().is_err());

    let timed_out = SetupRunnerState::default();
    let generation = timed_out.begin(request("base", &[], false)).expect("run");
    assert!(timed_out.finish_failure(generation, SetupLifecycle::TimedOut, "timed out"));
    assert!(timed_out.acquire_restart_authorization().is_err());
}

#[test]
fn restart_authorization_is_atomic_and_one_shot() {
    let state = Arc::new(SetupRunnerState::default());
    let generation = state.begin(request("base", &[], false)).expect("run");
    assert!(state.finish_success(generation, true));

    let handles: Vec<_> = (0..8)
        .map(|_| {
            let state = Arc::clone(&state);
            thread::spawn(move || state.acquire_restart_authorization().ok())
        })
        .collect();
    let authorizations: Vec<_> = handles
        .into_iter()
        .filter_map(|handle| handle.join().expect("restart contender"))
        .collect();
    assert_eq!(authorizations.len(), 1);
    assert!(state
        .complete_restart_authorization(authorizations[0])
        .is_ok());
    assert!(!state.status().restart_required);
    assert!(state.acquire_restart_authorization().is_err());
}

#[test]
fn failed_restart_restores_authorization_for_retry() {
    let state = SetupRunnerState::default();
    let generation = state.begin(request("base", &[], false)).expect("run");
    assert!(state.finish_success(generation, true));
    let authorization = state.acquire_restart_authorization().expect("authorized");
    state
        .restore_restart_authorization(authorization)
        .expect("restored");
    assert!(state.acquire_restart_authorization().is_ok());
}

#[test]
fn setup_restart_transaction_restarts_ready_backend_and_consumes_authorization() {
    let state = SetupRunnerState::default();
    let generation = state.begin(request("base", &[], false)).expect("run");
    assert!(state.finish_success(generation, true));
    let calls = Arc::new(Mutex::new(Vec::new()));

    restart_after_setup_transaction(
        &state,
        BackendLifecycle::Ready,
        {
            let calls = Arc::clone(&calls);
            move || {
                calls.lock().expect("calls").push("stop");
                Ok(())
            }
        },
        {
            let calls = Arc::clone(&calls);
            move || {
                calls.lock().expect("calls").push("start");
                Ok(())
            }
        },
    )
    .expect("restart succeeds");

    assert_eq!(*calls.lock().expect("calls"), vec!["stop", "start"]);
    assert!(!state.status().restart_required);
    assert!(state.acquire_restart_authorization().is_err());
}

#[test]
fn setup_restart_transaction_restores_authorization_after_stop_or_start_failure() {
    for fail_start in [false, true] {
        let state = SetupRunnerState::default();
        let generation = state.begin(request("base", &[], false)).expect("run");
        assert!(state.finish_success(generation, true));
        let result = restart_after_setup_transaction(
            &state,
            BackendLifecycle::Ready,
            move || {
                if fail_start {
                    Ok(())
                } else {
                    Err("stop failed".to_string())
                }
            },
            move || {
                if fail_start {
                    Err("start failed".to_string())
                } else {
                    Ok(())
                }
            },
        );
        assert!(result.is_err());
        assert!(state.status().restart_required);
        assert!(state.acquire_restart_authorization().is_ok());
    }
}

#[test]
fn setup_restart_transaction_rejects_non_ready_backend_without_consuming_authorization() {
    let state = SetupRunnerState::default();
    let generation = state.begin(request("base", &[], false)).expect("run");
    assert!(state.finish_success(generation, true));
    let result = restart_after_setup_transaction(
        &state,
        BackendLifecycle::Failed,
        || panic!("must not stop"),
        || panic!("must not start"),
    );
    assert!(result.is_err());
    assert!(state.acquire_restart_authorization().is_ok());
}

#[test]
fn backend_restart_waits_for_ready_before_success() {
    let mut states = vec![
        BackendLifecycle::Starting,
        BackendLifecycle::Starting,
        BackendLifecycle::Ready,
    ]
    .into_iter();
    let result = wait_for_backend_ready(Duration::from_secs(1), Duration::ZERO, || {
        Ok(states.next().unwrap_or(BackendLifecycle::Ready))
    });
    assert!(result.is_ok());
}

#[test]
fn backend_restart_readiness_timeout_is_bounded() {
    let result = wait_for_backend_ready(Duration::ZERO, Duration::ZERO, || {
        Ok(BackendLifecycle::Starting)
    });
    assert!(result.expect_err("must time out").contains("timed out"));
}

#[test]
fn setup_restart_releases_lock_before_readiness_wait() {
    // Regression: the post-setup restart must not hold `restart_lock` across the
    // readiness wait, or app-exit shutdown (which blocks on the lock) would hang
    // for the full startup deadline when a restarted backend wedges.
    let restart_lock = Mutex::new(());
    let swap_ran = Arc::new(Mutex::new(false));
    let lock_free_during_wait = Arc::new(Mutex::new(false));

    let result = restart_after_setup_with_readiness(
        &restart_lock,
        {
            let swap_ran = Arc::clone(&swap_ran);
            let restart_lock = &restart_lock;
            move || {
                // The swap runs under the lock, so it must not be re-acquirable here.
                assert!(restart_lock.try_lock().is_err(), "swap must hold the lock");
                *swap_ran.lock().expect("swap flag") = true;
                Ok(())
            }
        },
        {
            let lock_free_during_wait = Arc::clone(&lock_free_during_wait);
            let restart_lock = &restart_lock;
            move || {
                // The lock must be released before the readiness wait so a
                // concurrent shutdown/restart can acquire it.
                *lock_free_during_wait.lock().expect("wait flag") = restart_lock.try_lock().is_ok();
                Ok(())
            }
        },
    );

    assert!(result.is_ok());
    assert!(*swap_ran.lock().expect("swap flag"), "swap must run");
    assert!(
        *lock_free_during_wait.lock().expect("wait flag"),
        "restart lock must be free during the readiness wait"
    );
}

#[test]
fn progress_payload_is_allowlisted_bounded_and_redacted() {
    let line = r#"{"event":"stage_started","stage":"Dependency installation","command":["/Users/alice/secret/python","--token","sk-secret123456"],"profile":"mlx","model_groups":["tts-mlx","bogus"],"message":"token=verysecret\u0007 /Users/alice/private","percent":150,"unknown":"drop"}"#;
    let event = sanitize_progress_line(line).expect("valid event");
    let value = serde_json::to_value(event).expect("serializable event");

    assert_eq!(SETUP_PROGRESS_EVENT, "runtime-setup-progress");
    assert_eq!(value["event"], "stage_started");
    assert_eq!(value["stage"], "dependency_installation");
    assert_eq!(value["profile"], "mlx");
    assert_eq!(value["model_groups"], serde_json::json!(["tts-mlx"]));
    assert_eq!(value["percent"].as_f64(), Some(100.0));
    assert!(!value.to_string().contains("command"));
    assert!(!value.to_string().contains("secret"));
    assert!(!value.to_string().contains("/Users"));
    assert!(!value.to_string().contains("unknown"));
}

#[test]
fn verification_stage_is_allowlisted_and_normalized() {
    let line = r#"{"event":"stage_started","stage":"Verification","message":"Verifying setup","percent":90}"#;
    let event = sanitize_progress_line(line).expect("valid event");
    let value = serde_json::to_value(event).expect("serializable event");

    assert_eq!(value["event"], "stage_started");
    assert_eq!(value["stage"], "verify");
    assert_eq!(value["percent"].as_f64(), Some(90.0));
}

#[test]
fn malformed_output_becomes_a_safe_bounded_diagnostic() {
    let source = format!(
        "bad output token=supersecret /Users/alice/private {}",
        "x".repeat(2000)
    );
    let event = sanitize_progress_line(&source).expect("diagnostic event");
    let value = serde_json::to_value(event).expect("serializable event");

    assert_eq!(value["event"], "diagnostic");
    let message = value["message"].as_str().expect("diagnostic message");
    assert!(message.len() <= 512);
    assert!(!message.contains("supersecret"));
    assert!(!message.contains("/Users"));
}

#[test]
fn stale_generation_cannot_clear_or_replace_active_run() {
    let state = SetupRunnerState::default();
    let first = state.begin(request("base", &[], false)).expect("first run");
    state.finish_failure(first, SetupLifecycle::Failed, "first failed");
    let second = state
        .begin(request("base", &[], false))
        .expect("second run");

    state.finish_success(first, true);
    let status = state.status();
    assert_eq!(status.lifecycle, SetupLifecycle::Running);
    assert_eq!(status.generation, second);
}

#[test]
fn request_dto_rejects_arbitrary_execution_fields() {
    for field in ["executable", "script", "path", "args", "modelIds", "env"] {
        let mut value = serde_json::json!({
            "profile": "base",
            "modelGroups": [],
            "acceptPytorchRisk": false
        });
        value
            .as_object_mut()
            .expect("request object")
            .insert(field.to_string(), serde_json::json!("untrusted"));
        assert!(serde_json::from_value::<SetupRequest>(value).is_err());
    }
}

#[test]
fn cancellation_and_timeout_are_terminal_and_release_guard() {
    let state = SetupRunnerState::default();
    let first = state.begin(request("base", &[], false)).expect("first run");
    let (cancelled, process) = state.cancel().expect("active run cancels");
    assert_eq!(cancelled, first);
    assert!(process.is_none());
    assert_eq!(state.status().lifecycle, SetupLifecycle::Cancelling);
    assert!(state.finish_failure(
        first,
        SetupLifecycle::Cancelled,
        "runtime setup was cancelled"
    ));
    assert_eq!(state.status().lifecycle, SetupLifecycle::Cancelled);

    let second = state
        .begin(request("base", &[], false))
        .expect("second run");
    assert!(state.finish_failure(second, SetupLifecycle::TimedOut, "timed out"));
    assert_eq!(state.status().lifecycle, SetupLifecycle::TimedOut);
    assert!(state.begin(request("base", &[], false)).is_ok());
}

#[test]
fn cancelling_holds_run_guard_until_atomic_terminal_cleanup() {
    let state = SetupRunnerState::default();
    let generation = state.begin(request("base", &[], false)).expect("run");
    let (_, _) = state.cancel().expect("cancel requested");
    assert_eq!(state.status().lifecycle, SetupLifecycle::Cancelling);
    assert!(state.begin(request("base", &[], false)).is_err());
    assert!(state.finish_failure(
        generation,
        SetupLifecycle::Cancelled,
        "runtime setup was cancelled"
    ));
    assert!(state.begin(request("base", &[], false)).is_ok());
}

#[test]
fn terminal_ownership_remains_unique_if_new_run_begins_before_emit() {
    let state = SetupRunnerState::default();
    let first = state.begin(request("base", &[], false)).expect("first run");
    assert!(state.finish_success(first, true));
    let second = state
        .begin(request("base", &[], false))
        .expect("second run");
    assert_ne!(first, second);
    assert!(!state.finish_success(first, true));
    assert_eq!(state.status().generation, second);
    assert_eq!(state.status().lifecycle, SetupLifecycle::Running);
}

fn unique_temp_dir(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    std::env::temp_dir().join(format!(
        "orpheus-setup-{label}-{}-{nonce}",
        std::process::id()
    ))
}

#[cfg(unix)]
#[test]
fn setup_python_uses_only_fixed_venv_entry_and_accepts_normal_python_symlink() {
    use std::os::unix::fs::symlink;

    let repo = unique_temp_dir("python-fixed");
    fs::create_dir_all(repo.join(".venv/bin")).expect("venv bin");
    symlink("/bin/sh", repo.join(".venv/bin/python")).expect("python symlink");

    let resolved = resolve_setup_python(&repo).expect("fixed venv python");
    assert_eq!(
        resolved,
        repo.canonicalize()
            .expect("canonical repo")
            .join(".venv/bin/python")
    );
    assert_ne!(resolved, PathBuf::from("/tmp/attacker-python"));
    let _ = fs::remove_dir_all(repo);
}

#[cfg(unix)]
#[test]
fn setup_python_rejects_venv_directory_symlink_escape() {
    use std::os::unix::fs::symlink;

    let repo = unique_temp_dir("python-escape");
    let outside = unique_temp_dir("python-outside");
    fs::create_dir_all(outside.join("bin")).expect("outside bin");
    symlink("/bin/sh", outside.join("bin/python")).expect("outside python");
    fs::create_dir_all(&repo).expect("repo");
    symlink(&outside, repo.join(".venv")).expect("escaped venv");

    assert!(resolve_setup_python(&repo).is_err());
    let _ = fs::remove_dir_all(repo);
    let _ = fs::remove_dir_all(outside);
}

#[cfg(unix)]
#[test]
fn setup_python_rejects_non_executable_fixed_entry() {
    use std::os::unix::fs::PermissionsExt;

    let repo = unique_temp_dir("python-nonexec");
    let entry = repo.join(".venv/bin/python");
    fs::create_dir_all(entry.parent().expect("entry parent")).expect("venv bin");
    fs::write(&entry, b"not executable").expect("entry");
    fs::set_permissions(&entry, fs::Permissions::from_mode(0o600)).expect("permissions");

    assert!(resolve_setup_python(&repo).is_err());
    let _ = fs::remove_dir_all(repo);
}

#[cfg(unix)]
#[test]
fn setup_python_resolution_ignores_backend_override_and_path() {
    const CHILD_FLAG: &str = "ORPHEUS_SETUP_RESOLVER_TEST_CHILD";
    if std::env::var_os(CHILD_FLAG).is_some() {
        use std::os::unix::fs::symlink;

        let repo = unique_temp_dir("python-env-isolation");
        fs::create_dir_all(repo.join(".venv/bin")).expect("venv bin");
        symlink("/bin/sh", repo.join(".venv/bin/python")).expect("python symlink");
        let resolved = resolve_setup_python(&repo).expect("fixed venv entry");
        assert!(resolved.ends_with(".venv/bin/python"));
        assert_ne!(resolved, PathBuf::from("/tmp/attacker-python"));
        let _ = fs::remove_dir_all(repo);
        return;
    }

    let output = Command::new(std::env::current_exe().expect("test executable"))
        .args([
            "--exact",
            "setup_tests::setup_python_resolution_ignores_backend_override_and_path",
        ])
        .env(CHILD_FLAG, "1")
        .env("AUDIOBOOK_DESKTOP_PYTHON", "/tmp/attacker-python")
        .env("PATH", "/tmp/attacker-bin")
        .output()
        .expect("isolated resolver test");
    assert!(
        output.status.success(),
        "isolated resolver failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn bounded_reader_discards_megabyte_line_without_forwarding_contents() {
    let secret = format!("Bearer super-secret {}", "x".repeat(1_100_000));
    let events = Arc::new(Mutex::new(Vec::new()));
    let captured = Arc::clone(&events);
    read_progress_events(Cursor::new(secret.into_bytes()), move |event| {
        captured.lock().expect("events").push(event);
    });

    let events = events.lock().expect("events");
    assert_eq!(events.len(), 1);
    let message = events[0].message.as_deref().expect("bounded diagnostic");
    assert!(message.len() <= 512);
    assert!(!message.contains("super-secret"));
    assert!(message.contains("exceeded"));
}

#[test]
fn diagnostic_redaction_covers_embedded_credentials_and_urls() {
    let line = r#"{"event":"error","message":"Authorization: Bearer ey.secret-token https://alice:hunter2@example.test/file?token=qwerty&API_KEY=abc123&key=k&password=p&secret=s sk-live-123 api-key=punctuated!,"}"#;
    let event = sanitize_progress_line(line).expect("sanitized diagnostic");
    let text = serde_json::to_string(&event).expect("serialized diagnostic");
    for secret in [
        "ey.secret-token",
        "alice",
        "hunter2",
        "qwerty",
        "abc123",
        "sk-live-123",
        "punctuated",
    ] {
        assert!(!text.contains(secret), "leaked {secret}: {text}");
    }
    assert!(!text.chars().any(|character| character.is_control()));
    assert!(text.len() < 1024);
}

fn fixture_launch(program: &Path, args: &[String]) -> SetupLaunchConfig {
    SetupLaunchConfig {
        program: program.to_path_buf(),
        script: program.to_path_buf(),
        args: args.to_vec(),
        cwd: std::env::temp_dir(),
        env: Default::default(),
        uses_shell: false,
    }
}

fn collecting_sink() -> (EventSink, Arc<Mutex<Vec<SetupProgressEvent>>>) {
    let events = Arc::new(Mutex::new(Vec::new()));
    let captured = Arc::clone(&events);
    let sink: EventSink = Arc::new(move |event| {
        captured.lock().expect("events").push(event);
    });
    (sink, events)
}

fn wait_for_terminal(state: &SetupRunnerState, timeout: Duration) -> SetupLifecycle {
    let deadline = Instant::now() + timeout;
    loop {
        let lifecycle = state.status().lifecycle;
        if lifecycle != SetupLifecycle::Running {
            return lifecycle;
        }
        assert!(Instant::now() < deadline, "setup fixture did not terminate");
        thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn spawn_failure_clears_guard_and_emits_one_terminal_error() {
    let state = SetupRunnerState::default();
    let (sink, events) = collecting_sink();
    let result = start_supervised_for_test(
        state.clone(),
        request("base", &[], false),
        fixture_launch(Path::new("/definitely/missing/orpheus-python"), &[]),
        Duration::from_secs(1),
        sink,
    );
    assert!(result.is_err());
    assert_eq!(state.status().lifecycle, SetupLifecycle::Failed);
    assert!(state.begin(request("base", &[], false)).is_ok());
    let terminal = events
        .lock()
        .expect("events")
        .iter()
        .filter(|event| event.terminal == Some(true))
        .count();
    assert_eq!(terminal, 1);
}

#[test]
fn cancel_between_spawn_and_attach_emits_exactly_one_terminal_event() {
    let program = if cfg!(windows) {
        PathBuf::from("C:\\Windows\\System32\\cmd.exe")
    } else {
        PathBuf::from("/bin/sleep")
    };
    if !program.is_file() {
        return;
    }
    let args = if cfg!(windows) {
        vec!["/C".to_string(), "ping -n 30 127.0.0.1 >NUL".to_string()]
    } else {
        vec!["30".to_string()]
    };
    let state = SetupRunnerState::default();
    let (sink, events) = collecting_sink();
    let hook_state = state.clone();
    let hook_sink = Arc::clone(&sink);
    let result = start_supervised_with_hook_for_test(
        state.clone(),
        request("base", &[], false),
        fixture_launch(&program, &args),
        Duration::from_secs(30),
        sink,
        Arc::new(move || {
            cancel_with_sink_for_test(&hook_state, Arc::clone(&hook_sink))
                .expect("cancel during launch");
        }),
    );
    assert!(result.is_err());
    assert_eq!(state.status().lifecycle, SetupLifecycle::Cancelled);
    assert_eq!(
        events
            .lock()
            .expect("events")
            .iter()
            .filter(|event| event.terminal == Some(true))
            .count(),
        1
    );
}

#[test]
fn successful_real_child_emits_exactly_one_authoritative_terminal_event() {
    let program = if cfg!(windows) {
        PathBuf::from("C:\\Windows\\System32\\cmd.exe")
    } else {
        PathBuf::from("/usr/bin/true")
    };
    if !program.is_file() {
        return;
    }
    let args = if cfg!(windows) {
        vec!["/C".to_string(), "exit 0".to_string()]
    } else {
        Vec::new()
    };
    let state = SetupRunnerState::default();
    let (sink, events) = collecting_sink();
    start_supervised_for_test(
        state.clone(),
        request("base", &[], false),
        fixture_launch(&program, &args),
        Duration::from_secs(2),
        sink,
    )
    .expect("fixture starts");
    assert_eq!(
        wait_for_terminal(&state, Duration::from_secs(3)),
        SetupLifecycle::Complete
    );
    let events = events.lock().expect("events");
    let terminal = events
        .iter()
        .filter(|event| event.terminal == Some(true))
        .collect::<Vec<_>>();
    assert_eq!(terminal.len(), 1);
    assert_eq!(terminal[0].event, "complete");
    assert_eq!(terminal[0].percent, Some(100.0));
    assert_eq!(terminal[0].restart_required, Some(true));
}

#[test]
fn supervised_child_receives_fixed_internal_containment_flag() {
    let python = if cfg!(windows) {
        PathBuf::from("C:\\Windows\\py.exe")
    } else {
        PathBuf::from("/usr/bin/python3")
    };
    if !python.is_file() {
        return;
    }
    let code = "import json,os; print(json.dumps({'event':'plan','message':os.environ.get('ORPHEUS_TAURI_SETUP_SUPERVISED')}), flush=True)";
    let state = SetupRunnerState::default();
    let (sink, events) = collecting_sink();
    start_supervised_for_test(
        state.clone(),
        request("base", &[], false),
        fixture_launch(&python, &["-c".into(), code.into()]),
        Duration::from_secs(3),
        sink,
    )
    .expect("fixture starts");
    assert_eq!(
        wait_for_terminal(&state, Duration::from_secs(3)),
        SetupLifecycle::Complete
    );
    assert!(events
        .lock()
        .expect("events")
        .iter()
        .any(|event| event.message.as_deref() == Some("1")));
}

#[cfg(unix)]
#[test]
fn root_exit_with_pipe_inheriting_detached_child_completes_and_kills_child() {
    let python = PathBuf::from("/usr/bin/python3");
    if !python.is_file() {
        return;
    }
    let temp = unique_temp_dir("root-exit-tree");
    fs::create_dir_all(&temp).expect("temp");
    let pid_file = temp.join("child.pid");
    let code = format!(
        "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); open({:?},'w').write(str(p.pid))",
        pid_file
    );
    let state = SetupRunnerState::default();
    let (sink, events) = collecting_sink();
    let started = Instant::now();
    start_supervised_for_test(
        state.clone(),
        request("base", &[], false),
        fixture_launch(&python, &["-c".into(), code]),
        Duration::from_secs(10),
        sink,
    )
    .expect("fixture starts");
    assert_eq!(
        wait_for_terminal(&state, Duration::from_secs(4)),
        SetupLifecycle::Complete
    );
    assert!(started.elapsed() < Duration::from_secs(4));
    let child_pid: i32 = fs::read_to_string(&pid_file)
        .expect("child pid")
        .parse()
        .expect("pid");
    thread::sleep(Duration::from_millis(100));
    assert_ne!(
        unsafe { libc::kill(child_pid, 0) },
        0,
        "child survived root exit"
    );
    assert_eq!(
        events
            .lock()
            .expect("events")
            .iter()
            .filter(|event| event.terminal == Some(true))
            .count(),
        1
    );
    let _ = fs::remove_dir_all(temp);
}

#[test]
fn noisy_child_is_rate_limited_but_terminal_is_preserved() {
    let python = if cfg!(windows) {
        PathBuf::from("C:\\Windows\\py.exe")
    } else {
        PathBuf::from("/usr/bin/python3")
    };
    if !python.is_file() {
        return;
    }
    let code = "import json; [print(json.dumps({'event':'plan','message':'noise'}), flush=True) for _ in range(5000)]; print(json.dumps({'event':'stage_started','stage':'Dependency installation'}), flush=True)";
    let state = SetupRunnerState::default();
    let (sink, events) = collecting_sink();
    start_supervised_for_test(
        state.clone(),
        request("base", &[], false),
        fixture_launch(&python, &["-c".into(), code.into()]),
        Duration::from_secs(10),
        sink,
    )
    .expect("noisy child starts");
    assert_eq!(
        wait_for_terminal(&state, Duration::from_secs(5)),
        SetupLifecycle::Complete
    );
    let events = events.lock().expect("events");
    assert!(events.len() <= 257, "forwarded {} events", events.len());
    assert!(events.iter().any(|event| event.event == "stage_started"));
    assert_eq!(
        events
            .iter()
            .filter(|event| event.terminal == Some(true))
            .count(),
        1
    );
}

#[cfg(windows)]
#[test]
fn windows_taskkill_path_is_absolute_system32_path() {
    let path = windows_taskkill_path().expect("system taskkill path");
    assert!(path.is_absolute());
    assert_eq!(
        path.file_name().and_then(|name| name.to_str()),
        Some("taskkill.exe")
    );
    assert!(path
        .to_string_lossy()
        .to_ascii_lowercase()
        .contains("system32"));
}

#[cfg(unix)]
#[test]
fn supervisor_timeout_kills_detached_descendant_and_emits_one_terminal_event() {
    let python = PathBuf::from("/usr/bin/python3");
    if !python.is_file() {
        return;
    }
    let temp = unique_temp_dir("timeout-tree");
    fs::create_dir_all(&temp).expect("temp");
    let pid_file = temp.join("child.pid");
    let code = format!(
        "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True); open({:?},'w').write(str(p.pid)); time.sleep(60)",
        pid_file
    );
    let state = SetupRunnerState::default();
    let (sink, events) = collecting_sink();
    start_supervised_for_test(
        state.clone(),
        request("base", &[], false),
        fixture_launch(&python, &["-c".into(), code]),
        Duration::from_millis(1500),
        sink,
    )
    .expect("fixture starts");
    assert_eq!(
        wait_for_terminal(&state, Duration::from_secs(6)),
        SetupLifecycle::TimedOut
    );
    let child_pid: i32 = fs::read_to_string(&pid_file)
        .expect("child pid")
        .parse()
        .expect("pid");
    thread::sleep(Duration::from_millis(100));
    assert_ne!(
        unsafe { libc::kill(child_pid, 0) },
        0,
        "detached child survived"
    );
    assert_eq!(
        events
            .lock()
            .expect("events")
            .iter()
            .filter(|event| event.terminal == Some(true))
            .count(),
        1
    );
    let _ = fs::remove_dir_all(temp);
}

#[cfg(unix)]
#[test]
fn cancel_and_shutdown_kill_real_children_and_release_guard() {
    let sleep = PathBuf::from("/bin/sleep");
    if !sleep.is_file() {
        return;
    }
    for shutdown in [false, true] {
        let state = SetupRunnerState::default();
        let (sink, events) = collecting_sink();
        start_supervised_for_test(
            state.clone(),
            request("base", &[], false),
            fixture_launch(&sleep, &["60".into()]),
            Duration::from_secs(30),
            Arc::clone(&sink),
        )
        .expect("sleep starts");
        let pid = state.active_pid().expect("supervised child pid");
        if shutdown {
            state.shutdown();
        } else {
            cancel_with_sink_for_test(&state, sink).expect("cancel");
        }
        assert_eq!(state.status().lifecycle, SetupLifecycle::Cancelled);
        thread::sleep(Duration::from_millis(50));
        assert_ne!(
            unsafe { libc::kill(pid as i32, 0) },
            0,
            "supervised child survived cleanup"
        );
        assert!(state.begin(request("base", &[], false)).is_ok());
        if !shutdown {
            assert_eq!(
                events
                    .lock()
                    .expect("events")
                    .iter()
                    .filter(|event| event.terminal == Some(true))
                    .count(),
                1
            );
        }
    }
}
