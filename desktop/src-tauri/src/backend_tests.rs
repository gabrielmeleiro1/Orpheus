#[cfg(unix)]
use super::build_dev_launch_config_for_test;
use super::{
    backend_shutdown_force_kill_max, backend_shutdown_graceful_timeout,
    build_dev_launch_config_for_root, parse_ready_line, parse_shutdown_line,
    read_ready_payload_from_file, resolve_sidecar_path, sidecar_binary_stem,
    sidecar_source_filename, BackendLifecycle, BackendManager,
};

#[cfg(windows)]
#[test]
fn forced_backend_shutdown_terminates_root_and_descendant_in_job() {
    use std::fs;
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    fn process_is_running(pid: u32) -> bool {
        use windows_sys::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
        use windows_sys::Win32::System::Threading::{
            GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };

        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return false;
        }
        let mut exit_code = 0_u32;
        let running = unsafe { GetExitCodeProcess(handle, &mut exit_code) != 0 }
            && exit_code == STILL_ACTIVE as u32;
        unsafe { CloseHandle(handle) };
        running
    }

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("expected system time after unix epoch")
        .as_nanos();
    let marker = std::env::temp_dir().join(format!("orpheus-backend-descendant-{unique}.txt"));
    let script = r#"import ctypes, json, os, pathlib, signal, subprocess, sys, time
assert ctypes.windll.kernel32.GetConsoleWindow() == 0, "backend opened a console"
marker = pathlib.Path(sys.argv[1])
# Keep the root alive when terminate_backend_process sends CTRL_BREAK, so this
# test must exercise the forced Job Object termination path. Put the descendant
# in its own console process group so it cannot receive that graceful signal.
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, lambda _signum, _frame: None)
descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
)
marker.write_text(f"{os.getpid()} {descendant.pid}", encoding="ascii")
print(json.dumps({"event":"ready","host":"127.0.0.1","port":1,"token":"test"}), flush=True)
while True:
    time.sleep(1)
"#;
    let launch = super::ProcessLaunchConfig {
        program: super::python_program(),
        args: vec![
            "-c".to_string(),
            script.to_string(),
            marker.to_string_lossy().into_owned(),
        ],
        cwd: None,
        env: Vec::new(),
    };
    let manager = std::sync::Arc::new(std::sync::Mutex::new(super::BackendManager::default()));
    let mut running = super::launch_backend(std::sync::Arc::clone(&manager), launch)
        .expect("expected backend launch to assign a Job Object");

    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    let pids = loop {
        if let Ok(contents) = fs::read_to_string(&marker) {
            let pids = contents
                .split_whitespace()
                .map(|pid| pid.parse::<u32>().expect("valid process id"))
                .collect::<Vec<_>>();
            if pids.len() == 2 {
                break pids;
            }
        }
        assert!(
            std::time::Instant::now() < deadline,
            "backend fixture did not start"
        );
        thread::sleep(Duration::from_millis(25));
    };
    assert!(process_is_running(pids[0]));
    assert!(process_is_running(pids[1]));

    let outcome = super::terminate_backend_process(
        &mut running.child,
        Duration::from_millis(100),
        None,
        Some(running.job.as_ref()),
    );
    assert_eq!(outcome, super::ShutdownOutcome::ForcedKill);
    let _ = running.child.wait();

    // Verify Job Object termination itself killed both processes. Do this
    // before dropping `running`; otherwise KILL_ON_JOB_CLOSE would mask a
    // broken TerminateJobObject call.
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while process_is_running(pids[0]) || process_is_running(pids[1]) {
        assert!(
            std::time::Instant::now() < deadline,
            "forced backend shutdown left a process alive"
        );
        thread::sleep(Duration::from_millis(25));
    }
    assert!(!process_is_running(pids[0]));
    assert!(!process_is_running(pids[1]));

    drop(running);
    let _ = fs::remove_file(marker);
}
use crate::paths::DesktopRuntimePaths;
use std::path::{Path, PathBuf};

#[test]
fn parse_ready_line_accepts_valid_json_payload() {
    let payload = parse_ready_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":54321,\"token\":\"abc123\"}",
    )
    .expect("expected ready payload to parse");

    assert_eq!(payload.host, "127.0.0.1");
    assert_eq!(payload.port, 54321);
    assert_eq!(payload.token, "abc123");
}

#[test]
fn parse_ready_line_rejects_non_ready_event() {
    let error = parse_ready_line(
        "{\"event\":\"starting\",\"host\":\"127.0.0.1\",\"port\":54321,\"token\":\"abc123\"}",
    )
    .expect_err("expected invalid event to fail");

    assert!(error.contains("unexpected desktop event"));
}

#[test]
fn backend_state_machine_transitions_start_ready_stop() {
    let mut manager = BackendManager::default();

    assert_eq!(manager.status().lifecycle, BackendLifecycle::Stopped);
    assert!(manager.status().endpoint.is_none());

    manager.mark_starting();
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Starting);
    assert!(manager.status().endpoint.is_none());

    let payload = parse_ready_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"token-1\"}",
    )
    .expect("expected valid ready payload");
    manager.mark_ready(payload);

    let ready = manager.status();
    assert_eq!(ready.lifecycle, BackendLifecycle::Ready);
    let status_endpoint = ready.endpoint.expect("endpoint should exist when ready");
    assert_eq!(status_endpoint.base_url, "http://127.0.0.1:9000");
    let endpoint = manager
        .endpoint()
        .expect("credentialed endpoint should remain available through endpoint command");
    assert_eq!(endpoint.token, "token-1");

    manager.mark_stopped();
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Stopped);
    assert!(manager.endpoint().is_none());
}

#[test]
fn backend_manager_ignores_late_ready_payload_after_failure() {
    let mut manager = BackendManager::default();
    manager.mark_starting();
    manager.mark_failed("backend exited");

    let payload = parse_ready_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"token-1\"}",
    )
    .expect("expected valid ready payload");
    manager.mark_ready(payload);

    let status = manager.status();
    assert_eq!(status.lifecycle, BackendLifecycle::Failed);
    assert!(status.endpoint.is_none());
    assert_eq!(status.error.as_deref(), Some("backend exited"));
}

#[test]
fn ready_backend_stdout_eof_marks_unexpected_exit_failed() {
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let launch = super::ProcessLaunchConfig {
        program: super::python_program(),
        args: vec![
            "-c".to_string(),
            "print('{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"token-1\"}', flush=True)".to_string(),
        ],
        cwd: None,
        env: Vec::new(),
    };
    let running = super::launch_backend(Arc::clone(&manager), launch)
        .expect("expected backend fixture to launch");

    let deadline = Instant::now() + Duration::from_secs(3);
    let terminal_status = loop {
        let status = manager.lock().expect("expected manager lock").status();
        if matches!(
            status.lifecycle,
            BackendLifecycle::Failed | BackendLifecycle::Stopped
        ) {
            break status;
        }
        assert!(
            Instant::now() < deadline,
            "expected backend exit to update lifecycle before timeout, got {:?}",
            status
        );
        thread::sleep(Duration::from_millis(25));
    };

    running.stop(&manager);

    assert_eq!(terminal_status.lifecycle, BackendLifecycle::Failed);
    assert!(terminal_status.endpoint.is_none());
    assert!(terminal_status
        .error
        .as_deref()
        .unwrap_or_default()
        .contains("desktop backend exited unexpectedly"));
}

#[test]
fn ready_backend_shutdown_ack_before_eof_marks_stopped() {
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let launch = super::ProcessLaunchConfig {
        program: super::python_program(),
        args: vec![
            "-c".to_string(),
            concat!(
                "print('{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"token-1\"}', flush=True); ",
                "print('{\"event\":\"shutdown\"}', flush=True)"
            )
            .to_string(),
        ],
        cwd: None,
        env: Vec::new(),
    };
    let running = super::launch_backend(Arc::clone(&manager), launch)
        .expect("expected backend fixture to launch");

    let deadline = Instant::now() + Duration::from_secs(3);
    let terminal_status = loop {
        let status = manager.lock().expect("expected manager lock").status();
        if matches!(
            status.lifecycle,
            BackendLifecycle::Failed | BackendLifecycle::Stopped
        ) {
            break status;
        }
        assert!(
            Instant::now() < deadline,
            "expected acknowledged shutdown to update lifecycle before timeout, got {:?}",
            status
        );
        thread::sleep(Duration::from_millis(25));
    };

    running.stop(&manager);

    assert_eq!(terminal_status.lifecycle, BackendLifecycle::Stopped);
    assert!(terminal_status.error.is_none());
}

// POSIX-shaped fixture paths below, so compile this out on Windows rather than
// filtering it out of every gate at the command line.
#[cfg(unix)]
#[test]
fn build_dev_launch_config_matches_desktop_runtime_contract() {
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir("/tmp/audiobook-app");
    let repo = Path::new("/trusted/orpheus");
    let python = repo.join(".venv").join(if cfg!(windows) {
        "Scripts/python.exe"
    } else {
        "bin/python"
    });
    let config = build_dev_launch_config_for_test(&runtime_paths, repo, &python);

    assert!(!config.program.trim().is_empty());
    assert_eq!(
        config.args,
        vec![
            "-m",
            "apps.launchers.desktop_backend",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ]
    );
    assert_eq!(config.program, python.to_string_lossy());
    assert_eq!(config.cwd, Some(repo.to_path_buf()));

    let env = config
        .env
        .into_iter()
        .collect::<std::collections::BTreeMap<_, _>>();
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

#[cfg(unix)]
#[test]
fn dev_backend_ignores_path_and_python_override_for_fixed_checkout_venv() {
    const CHILD_FLAG: &str = "ORPHEUS_BACKEND_RESOLVER_TEST_CHILD";
    if std::env::var_os(CHILD_FLAG).is_some() {
        use std::os::unix::fs::symlink;

        let repo = unique_temp_path("backend-python-env-isolation");
        std::fs::create_dir_all(repo.join(".venv/bin")).expect("venv bin");
        symlink("/bin/sh", repo.join(".venv/bin/python")).expect("python symlink");
        let runtime_paths = DesktopRuntimePaths::from_app_data_dir(repo.join("app-data"));
        let launch = build_dev_launch_config_for_root(&runtime_paths, &repo)
            .expect("fixed dev backend launch");
        let canonical_repo = repo.canonicalize().expect("canonical repo");
        assert_eq!(
            launch.program,
            canonical_repo.join(".venv/bin/python").to_string_lossy()
        );
        assert_ne!(launch.program, "/tmp/attacker-python");
        assert_eq!(launch.cwd, Some(repo.clone()));
        let _ = std::fs::remove_dir_all(repo);
        return;
    }

    let output = std::process::Command::new(std::env::current_exe().expect("test executable"))
        .args([
            "--exact",
            "backend::tests::dev_backend_ignores_path_and_python_override_for_fixed_checkout_venv",
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
fn dev_backend_missing_venv_reports_actionable_startup_error() {
    let repo = unique_temp_path("backend-python-missing");
    std::fs::create_dir_all(&repo).expect("repo");
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir(repo.join("app-data"));
    let error = build_dev_launch_config_for_root(&runtime_paths, &repo)
        .expect_err("missing fixed venv must fail");
    assert!(error.contains("create the repository .venv"), "{error}");
    let _ = std::fs::remove_dir_all(repo);
}

#[test]
fn read_ready_payload_from_file_accepts_packaged_handoff_json() {
    let temp_root = unique_temp_path("ready-file");
    std::fs::create_dir_all(&temp_root).expect("expected test temp dir");
    let ready_file = temp_root.join("backend-ready.json");
    std::fs::write(
        &ready_file,
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":54321,\"token\":\"abc123\"}",
    )
    .expect("expected ready file fixture");

    let payload = read_ready_payload_from_file(&ready_file).expect("expected ready payload");

    assert_eq!(payload.host, "127.0.0.1");
    assert_eq!(payload.port, 54321);
    assert_eq!(payload.token, "abc123");
    assert!(
        !ready_file.exists(),
        "ready file should be unlinked immediately after successful consumption"
    );

    let _ = std::fs::remove_file(&ready_file);
    let _ = std::fs::remove_dir_all(&temp_root);
}

#[test]
fn sidecar_source_filename_matches_tauri_target_convention() {
    assert_eq!(
        sidecar_source_filename("aarch64-apple-darwin"),
        "orpheus-desktop-backend-aarch64-apple-darwin"
    );
    assert_eq!(
        sidecar_source_filename("x86_64-unknown-linux-gnu"),
        "orpheus-desktop-backend-x86_64-unknown-linux-gnu"
    );
    assert_eq!(
        sidecar_source_filename("x86_64-pc-windows-msvc"),
        "orpheus-desktop-backend-x86_64-pc-windows-msvc.exe"
    );
}

#[test]
fn resolve_sidecar_path_finds_resource_root_binary() {
    let temp_root = unique_temp_path("resource-root");
    std::fs::create_dir_all(&temp_root).expect("expected test temp dir");
    let sidecar_path = temp_root.join(runtime_sidecar_file_name_for_tests());
    std::fs::write(&sidecar_path, b"test-sidecar").expect("expected sidecar fixture");

    let resolved = resolve_sidecar_path(Some(&temp_root), None).expect("expected sidecar path");
    assert_eq!(resolved, sidecar_path);

    let _ = std::fs::remove_file(&sidecar_path);
    let _ = std::fs::remove_dir_all(&temp_root);
}

#[test]
fn resolve_sidecar_path_finds_resource_binaries_subdir() {
    let temp_root = unique_temp_path("resource-binaries");
    let binaries_dir = temp_root.join("binaries");
    std::fs::create_dir_all(&binaries_dir).expect("expected test binaries dir");
    let sidecar_path = binaries_dir.join(runtime_sidecar_file_name_for_tests());
    std::fs::write(&sidecar_path, b"test-sidecar").expect("expected sidecar fixture");

    let resolved = resolve_sidecar_path(Some(&temp_root), None).expect("expected sidecar path");
    assert_eq!(resolved, sidecar_path);

    let _ = std::fs::remove_file(&sidecar_path);
    let _ = std::fs::remove_dir_all(&temp_root);
}

#[test]
fn resolve_sidecar_path_finds_resource_onedir_binary() {
    let temp_root = unique_temp_path("resource-onedir");
    let sidecar_dir = temp_root.join(sidecar_binary_stem());
    std::fs::create_dir_all(&sidecar_dir).expect("expected test sidecar dir");
    let sidecar_path = sidecar_dir.join(runtime_sidecar_file_name_for_tests());
    std::fs::write(&sidecar_path, b"test-sidecar").expect("expected sidecar fixture");

    let resolved = resolve_sidecar_path(Some(&temp_root), None).expect("expected sidecar path");
    assert_eq!(resolved, sidecar_path);

    let _ = std::fs::remove_file(&sidecar_path);
    let _ = std::fs::remove_dir_all(&temp_root);
}

#[test]
fn resolve_sidecar_path_reports_missing_binary() {
    let temp_root = unique_temp_path("resource-missing");
    std::fs::create_dir_all(&temp_root).expect("expected test temp dir");

    let error = resolve_sidecar_path(Some(&temp_root), None)
        .expect_err("expected missing sidecar resolution to fail");
    assert!(error.contains(sidecar_binary_stem()));

    let _ = std::fs::remove_dir_all(&temp_root);
}

#[test]
fn resolve_sidecar_path_uses_logical_install_when_resource_directory_is_virtualized() {
    let temp_root = unique_temp_path("virtualized-resource");
    let install = temp_root.join("install");
    let resources = temp_root.join("package-cache");
    let sidecar_dir = install.join("binaries").join(sidecar_binary_stem());
    std::fs::create_dir_all(&sidecar_dir).unwrap();
    let sidecar = sidecar_dir.join(runtime_sidecar_file_name_for_tests());
    std::fs::write(&sidecar, b"test-sidecar").unwrap();
    let executable = install.join("Orpheus.exe");
    assert_eq!(
        resolve_sidecar_path(Some(&resources), Some(&executable)).unwrap(),
        sidecar
    );
    std::fs::remove_dir_all(&temp_root).unwrap();
}

fn runtime_sidecar_file_name_for_tests() -> String {
    if cfg!(target_os = "windows") {
        format!("{}.exe", sidecar_binary_stem())
    } else {
        sidecar_binary_stem().to_string()
    }
}

fn unique_temp_path(label: &str) -> PathBuf {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("expected unix epoch")
        .as_nanos();
    std::env::temp_dir().join(Path::new(&format!("audiobook-tauri-{label}-{nanos}")))
}

#[cfg(unix)]
#[test]
fn running_backend_stop_sends_sigterm_before_force_kill() {
    use std::fs;
    use std::io::{BufRead, BufReader};
    use std::process::{Command, Stdio};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{SystemTime, UNIX_EPOCH};

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("expected system time after unix epoch")
        .as_nanos();
    let marker_path = std::env::temp_dir().join(format!("audiobook-tauri-sigterm-{unique}.txt"));
    let _ = fs::remove_file(&marker_path);

    let script = r#"import pathlib, signal, sys, time
marker = pathlib.Path(sys.argv[1])
def on_term(sig, frame):
    marker.write_text("sigterm")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
while True:
    time.sleep(0.1)
"#;

    let mut child = Command::new(super::python_program())
        .arg("-c")
        .arg(script)
        .arg(marker_path.to_string_lossy().into_owned())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("expected test process to launch");
    #[cfg(windows)]
    let job = super::setup::WindowsJob::assign(&child).expect("expected job assignment");
    let stdout = child.stdout.take().expect("expected stdout pipe");
    let stderr = child.stderr.take().expect("expected stderr pipe");

    let mut stdout_reader = BufReader::new(stdout);
    let mut ready_line = String::new();
    stdout_reader
        .read_line(&mut ready_line)
        .expect("expected backend child readiness line");
    assert_eq!(ready_line.trim(), "ready");

    let stdout_thread = thread::spawn(
        move || {
            for _ in stdout_reader.lines().map_while(Result::ok) {}
        },
    );
    let stderr_thread = thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for _ in reader.lines().map_while(Result::ok) {}
    });

    let running = super::RunningBackend {
        child,
        stdout_thread: Some(stdout_thread),
        stderr_thread: Some(stderr_thread),
        shutdown_ack: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        generation: 0,
        #[cfg(unix)]
        process_group_id: None,
        child_reaped: false,
        emergency_teardown_armed: true,
        #[cfg(windows)]
        job,
    };
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    running.stop(&manager);

    let marker_contents =
        fs::read_to_string(&marker_path).expect("expected backend stop to trigger SIGTERM");
    assert_eq!(marker_contents, "sigterm");
    let _ = fs::remove_file(&marker_path);
}

#[cfg(unix)]
#[test]
fn terminate_backend_process_falls_back_to_child_sigterm_when_group_signal_fails() {
    use std::fs;
    use std::process::{Command, Stdio};
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("expected system time after unix epoch")
        .as_nanos();
    let marker_path =
        std::env::temp_dir().join(format!("audiobook-tauri-group-fallback-{unique}.txt"));
    let _ = fs::remove_file(&marker_path);

    let script = r#"import pathlib, signal, sys, time
marker = pathlib.Path(sys.argv[1])
def on_term(sig, frame):
    marker.write_text("sigterm")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.1)
"#;

    let mut child = Command::new(super::python_program())
        .arg("-c")
        .arg(script)
        .arg(marker_path.to_string_lossy().into_owned())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("expected test process to launch");

    thread::sleep(Duration::from_millis(150));
    let mut child_reaped = false;
    let outcome = super::terminate_backend_process(
        &mut child,
        Duration::from_millis(250),
        None,
        Some(i32::MAX),
        &mut child_reaped,
    );

    assert_eq!(outcome, super::ShutdownOutcome::GracefulTerminated);
    let marker_contents =
        fs::read_to_string(&marker_path).expect("expected direct SIGTERM fallback marker");
    assert_eq!(marker_contents, "sigterm");
    let _ = child.wait();
    let _ = fs::remove_file(&marker_path);
}

#[test]
fn backend_shutdown_graceful_timeout_matches_documented_cleanup_budget() {
    let graceful = backend_shutdown_graceful_timeout();
    assert!(
        graceful > std::time::Duration::from_millis(5000),
        "graceful shutdown timeout {:?} must exceed the backend's documented 5.0s cleanup budget",
        graceful
    );
    assert!(
        graceful + super::backend_shutdown_ack_extension() <= backend_shutdown_force_kill_max(),
        "force-kill ceiling {:?} must cover the graceful timeout {:?} plus the ack extension {:?}",
        backend_shutdown_force_kill_max(),
        graceful,
        super::backend_shutdown_ack_extension()
    );
}

#[test]
fn parse_shutdown_line_accepts_valid_json_payload() {
    let payload =
        parse_shutdown_line("{\"event\":\"shutdown\"}").expect("expected shutdown payload");
    assert_eq!(payload.event, "shutdown");
}

#[test]
fn parse_shutdown_line_rejects_non_shutdown_event() {
    let error = parse_shutdown_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":1,\"token\":\"x\"}",
    )
    .expect_err("expected non-shutdown event to fail");
    assert!(error.contains("unexpected desktop event"));
}

#[cfg(unix)]
#[test]
fn running_backend_stop_allows_slow_cleanup_within_budget() {
    use std::fs;
    use std::io::{BufRead, BufReader};
    use std::process::{Command, Stdio};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("expected system time after unix epoch")
        .as_nanos();
    let marker_path =
        std::env::temp_dir().join(format!("audiobook-tauri-slow-cleanup-{unique}.txt"));
    let _ = fs::remove_file(&marker_path);

    let script = r#"import pathlib, signal, sys, time
marker = pathlib.Path(sys.argv[1])
def on_term(sig, frame):
    time.sleep(2.5)
    marker.write_text("cleanup_done")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
while True:
    time.sleep(0.1)
"#;

    let mut child = Command::new(super::python_program())
        .arg("-c")
        .arg(script)
        .arg(marker_path.to_string_lossy().into_owned())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("expected test process to launch");
    #[cfg(windows)]
    let job = super::setup::WindowsJob::assign(&child).expect("expected job assignment");
    let stdout = child.stdout.take().expect("expected stdout pipe");
    let stderr = child.stderr.take().expect("expected stderr pipe");

    let mut stdout_reader = BufReader::new(stdout);
    let mut ready_line = String::new();
    stdout_reader
        .read_line(&mut ready_line)
        .expect("expected backend child readiness line");
    assert_eq!(ready_line.trim(), "ready");

    let stdout_thread = thread::spawn(
        move || {
            for _ in stdout_reader.lines().map_while(Result::ok) {}
        },
    );
    let stderr_thread = thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for _ in reader.lines().map_while(Result::ok) {}
    });

    let running = super::RunningBackend {
        child,
        stdout_thread: Some(stdout_thread),
        stderr_thread: Some(stderr_thread),
        shutdown_ack: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        generation: 0,
        #[cfg(unix)]
        process_group_id: None,
        child_reaped: false,
        emergency_teardown_armed: true,
        #[cfg(windows)]
        job,
    };
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let started_at = Instant::now();

    running.stop(&manager);

    assert!(
        started_at.elapsed() >= Duration::from_millis(2400),
        "stop should wait for simulated cleanup to finish"
    );
    assert!(
        started_at.elapsed() < backend_shutdown_force_kill_max(),
        "slow cleanup should exit gracefully before the force-kill ceiling"
    );

    let marker_contents =
        fs::read_to_string(&marker_path).expect("expected slow cleanup to complete before exit");
    assert_eq!(marker_contents, "cleanup_done");
    let _ = fs::remove_file(&marker_path);
}

#[cfg(unix)]
#[test]
fn running_backend_stop_forces_unresponsive_backend_within_bounded_limit() {
    use std::io::{BufRead, BufReader};
    use std::process::{Command, Stdio};
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    let script = r#"import signal, time
def on_term(sig, frame):
    pass
signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.1)
"#;

    let mut child = Command::new(super::python_program())
        .arg("-c")
        .arg(script)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("expected test process to launch");
    #[cfg(windows)]
    let job = super::setup::WindowsJob::assign(&child).expect("expected job assignment");
    let stdout = child.stdout.take().expect("expected stdout pipe");
    let stderr = child.stderr.take().expect("expected stderr pipe");

    let stdout_thread = thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for _ in reader.lines().map_while(Result::ok) {}
    });
    let stderr_thread = thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for _ in reader.lines().map_while(Result::ok) {}
    });

    thread::sleep(Duration::from_millis(150));

    let running = super::RunningBackend {
        child,
        stdout_thread: Some(stdout_thread),
        stderr_thread: Some(stderr_thread),
        shutdown_ack: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        generation: 0,
        #[cfg(unix)]
        process_group_id: None,
        child_reaped: false,
        emergency_teardown_armed: true,
        #[cfg(windows)]
        job,
    };
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let started_at = Instant::now();

    running.stop(&manager);

    assert!(
        started_at.elapsed() >= backend_shutdown_graceful_timeout(),
        "unresponsive backend should receive the full graceful shutdown window"
    );
    assert!(
        started_at.elapsed() < backend_shutdown_force_kill_max() + Duration::from_millis(500),
        "backend stop must force-kill hung backends within the bounded ceiling"
    );
}

#[cfg(unix)]
#[test]
fn terminate_backend_process_falls_back_to_kill_after_timeout() {
    use std::fs;
    use std::process::{Command, Stdio};
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("expected system time after unix epoch")
        .as_nanos();
    let marker_path = std::env::temp_dir().join(format!("audiobook-tauri-timeout-{unique}.txt"));
    let _ = fs::remove_file(&marker_path);

    let script = r#"import pathlib, signal, sys, time
marker = pathlib.Path(sys.argv[1])
def on_term(sig, frame):
    marker.write_text("sigterm")
signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.1)
"#;

    let mut child = Command::new(super::python_program())
        .arg("-c")
        .arg(script)
        .arg(marker_path.to_string_lossy().into_owned())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("expected test process to launch");

    thread::sleep(Duration::from_millis(150));
    assert!(
        child
            .try_wait()
            .expect("expected try_wait to succeed")
            .is_none(),
        "expected timeout test process to still be running before stop"
    );

    let mut child_reaped = false;
    let outcome = super::terminate_backend_process(
        &mut child,
        Duration::from_millis(250),
        None,
        None,
        &mut child_reaped,
    );
    assert_eq!(outcome, super::ShutdownOutcome::ForcedKill);

    let marker_contents =
        fs::read_to_string(&marker_path).expect("expected SIGTERM to be delivered before kill");
    assert_eq!(marker_contents, "sigterm");

    let _ = child.wait();
    let _ = fs::remove_file(&marker_path);
}

#[cfg(unix)]
#[test]
fn running_backend_stop_does_not_block_on_stdout_inherited_by_grandchild() {
    use std::fs;
    use std::sync::mpsc;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("expected system time after unix epoch")
        .as_nanos();
    let temp_root = std::env::temp_dir().join(format!("audiobook-tauri-grandchild-{unique}"));
    fs::create_dir_all(&temp_root).expect("expected temp root");
    let marker_path = temp_root.join("grandchild-sigterm.txt");
    let pid_path = temp_root.join("grandchild.pid");
    let ready_path = temp_root.join("grandchild-ready.txt");

    let script = r#"import pathlib, signal, subprocess, sys, time
marker = pathlib.Path(sys.argv[1])
pid_path = pathlib.Path(sys.argv[2])
ready_path = pathlib.Path(sys.argv[3])
grandchild_code = r'''
import pathlib, signal, sys, time
marker = pathlib.Path(sys.argv[1])
ready_path = pathlib.Path(sys.argv[2])
def on_term(sig, frame):
    marker.write_text("sigterm")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
ready_path.write_text("ready")
while True:
    time.sleep(0.1)
'''
grandchild = subprocess.Popen(
    [sys.executable, "-c", grandchild_code, str(marker), str(ready_path)],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
pid_path.write_text(str(grandchild.pid))
def on_term(sig, frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.1)
"#;

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let launch = super::ProcessLaunchConfig {
        program: super::python_program(),
        args: vec![
            "-c".to_string(),
            script.to_string(),
            marker_path.to_string_lossy().into_owned(),
            pid_path.to_string_lossy().into_owned(),
            ready_path.to_string_lossy().into_owned(),
        ],
        cwd: None,
        env: Vec::new(),
    };
    let running = super::launch_backend(Arc::clone(&manager), launch)
        .expect("expected backend fixture to launch");

    let deadline = std::time::Instant::now() + Duration::from_secs(3);
    while (!pid_path.is_file() || !ready_path.is_file()) && std::time::Instant::now() < deadline {
        thread::sleep(Duration::from_millis(25));
    }
    assert!(pid_path.is_file(), "expected grandchild pid fixture");
    assert!(ready_path.is_file(), "expected grandchild ready fixture");

    let grandchild_pid: i32 = fs::read_to_string(&pid_path)
        .expect("expected grandchild pid")
        .trim()
        .parse()
        .expect("expected integer grandchild pid");

    let (done_tx, done_rx) = mpsc::channel();
    let stop_manager = Arc::clone(&manager);
    thread::spawn(move || {
        running.stop(&stop_manager);
        let _ = done_tx.send(());
    });

    if done_rx.recv_timeout(Duration::from_secs(1)).is_err() {
        let _ = unsafe { libc::kill(grandchild_pid, libc::SIGTERM) };
        let _ = done_rx.recv_timeout(Duration::from_secs(3));
        panic!("backend stop blocked on stdout inherited by a grandchild process");
    }

    let marker_deadline = std::time::Instant::now() + Duration::from_secs(2);
    while !marker_path.is_file() && std::time::Instant::now() < marker_deadline {
        thread::sleep(Duration::from_millis(25));
    }
    assert!(
        marker_path.is_file(),
        "expected process-group SIGTERM to reach the grandchild"
    );

    let _ = fs::remove_dir_all(&temp_root);
}

#[test]
fn stale_process_generation_cannot_mutate_newer_lifecycle() {
    let mut manager = BackendManager::default();

    let generation_one = manager.mark_starting();
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Starting);

    // Simulate a restart: a new child claims the shared manager.
    let generation_two = manager.mark_starting();
    assert_ne!(generation_one, generation_two);
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Starting);

    // Late EOF / shutdown from the previous reader must not clobber the new start.
    manager.mark_stopped_for_generation(generation_one);
    assert_eq!(
        manager.status().lifecycle,
        BackendLifecycle::Starting,
        "stale stop must not move a newer Starting generation"
    );

    manager.mark_failed_for_generation(generation_one, "stale reader exit");
    assert_eq!(
        manager.status().lifecycle,
        BackendLifecycle::Starting,
        "stale failure must not move a newer Starting generation"
    );

    let payload = parse_ready_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"token-2\"}",
    )
    .expect("expected valid ready payload");
    manager.mark_ready_for_generation(generation_two, payload.clone());
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Ready);
    assert_eq!(manager.endpoint().expect("endpoint").token, "token-2");

    // A stale ready from the old generation must not overwrite the new endpoint.
    let stale_payload = parse_ready_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":1111,\"token\":\"stale\"}",
    )
    .expect("expected valid ready payload");
    // Force lifecycle back to Starting under generation_two would be wrong; instead
    // verify stale ready is ignored while Ready under generation_two.
    manager.mark_ready_for_generation(generation_one, stale_payload);
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Ready);
    assert_eq!(
        manager.endpoint().expect("endpoint").token,
        "token-2",
        "stale ready must not replace the current generation endpoint"
    );

    manager.mark_failed_for_generation(generation_one, "stale after ready");
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Ready);
    assert!(manager.endpoint().is_some());
}

#[test]
fn starting_backend_fails_after_startup_deadline() {
    use std::time::Duration;

    let mut manager = BackendManager::default();
    manager.mark_starting();
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Starting);

    // A far-future deadline leaves a fresh startup untouched.
    assert!(!manager.fail_if_startup_timed_out(Duration::from_secs(3600)));
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Starting);

    // A zero deadline force-fails a wedged startup so it can be restarted.
    assert!(manager.fail_if_startup_timed_out(Duration::from_millis(0)));
    let status = manager.status();
    assert_eq!(status.lifecycle, BackendLifecycle::Failed);
    assert!(status.endpoint.is_none());
    assert!(status
        .error
        .as_deref()
        .unwrap_or_default()
        .contains("did not become ready"));

    // Idempotent once the backend is no longer `Starting`.
    assert!(!manager.fail_if_startup_timed_out(Duration::from_millis(0)));
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Failed);
}

#[test]
fn startup_deadline_tracks_latest_generation_only() {
    use std::time::Duration;

    let mut manager = BackendManager::default();
    let first = manager.mark_starting();

    // A restart supersedes the first generation and resets the startup clock.
    let second = manager.mark_starting();
    assert_ne!(first, second);

    // The fresh generation must not be failed by a long deadline: the timeout
    // tracks the newest `started_at`, so a superseded generation can never fail
    // the one that replaced it.
    assert!(!manager.fail_if_startup_timed_out(Duration::from_secs(3600)));
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Starting);

    let payload = parse_ready_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9100,\"token\":\"gen2\"}",
    )
    .expect("expected valid ready payload");
    manager.mark_ready_for_generation(second, payload);
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Ready);
    assert_eq!(manager.endpoint().expect("endpoint").token, "gen2");
}

#[cfg(unix)]
#[test]
fn replace_running_backend_stops_displaced_child() {
    use std::fs;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("expected system time after unix epoch")
        .as_nanos();
    let marker_path = std::env::temp_dir().join(format!("audiobook-tauri-replace-{unique}.txt"));
    let _ = fs::remove_file(&marker_path);

    // The displaced child writes a marker on SIGTERM, proving it was stopped
    // rather than silently dropped and orphaned when a new backend replaces it.
    let displaced_script = r#"import pathlib, signal, sys, time
marker = pathlib.Path(sys.argv[1])
def on_term(sig, frame):
    marker.write_text("stopped")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
print('{"event":"ready","host":"127.0.0.1","port":9000,"token":"first"}', flush=True)
while True:
    time.sleep(0.1)
"#;

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let slot: Mutex<Option<super::RunningBackend>> = Mutex::new(None);

    let first = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".to_string(),
                displaced_script.to_string(),
                marker_path.to_string_lossy().into_owned(),
            ],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("expected first backend fixture to launch");
    super::replace_running_backend(&slot, &manager, first).expect("expected first install");

    // The SIGTERM handler is installed before the ready line is printed, so
    // waiting for `Ready` guarantees the marker path will be written on stop.
    let ready_deadline = Instant::now() + Duration::from_secs(3);
    while manager.lock().expect("manager lock").status().lifecycle != BackendLifecycle::Ready {
        assert!(
            Instant::now() < ready_deadline,
            "expected first backend to become ready before replacement"
        );
        thread::sleep(Duration::from_millis(25));
    }

    let second_script = concat!(
        "import time\n",
        "print('{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9001,\"token\":\"second\"}', flush=True)\n",
        "while True:\n",
        "    time.sleep(0.1)\n",
    );
    let second = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec!["-c".to_string(), second_script.to_string()],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("expected second backend fixture to launch");

    // Installing the second backend must synchronously stop the first.
    super::replace_running_backend(&slot, &manager, second)
        .expect("expected second install to stop the displaced child");

    let marker =
        fs::read_to_string(&marker_path).expect("displaced backend must be stopped, not orphaned");
    assert_eq!(marker, "stopped");

    // The displaced child's generation-fenced stop must not clobber the new one.
    let ready_deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let status = manager.lock().expect("manager lock").status();
        if status.lifecycle == BackendLifecycle::Ready {
            assert_eq!(
                manager
                    .lock()
                    .expect("manager lock")
                    .endpoint()
                    .expect("endpoint")
                    .token,
                "second"
            );
            break;
        }
        assert!(
            Instant::now() < ready_deadline,
            "expected replacement backend to become ready, got {:?}",
            status
        );
        thread::sleep(Duration::from_millis(25));
    }

    if let Some(second) = slot.lock().expect("slot lock").take() {
        second.stop(&manager);
    }
    let _ = fs::remove_file(&marker_path);
}

#[cfg(unix)]
fn process_is_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    let result = unsafe { libc::kill(pid, 0) };
    result == 0
}

#[cfg(unix)]
fn wait_until_process_exits(pid: i32, timeout: std::time::Duration) -> bool {
    use std::thread;
    use std::time::{Duration, Instant};

    let deadline = Instant::now() + timeout;
    while process_is_alive(pid) {
        if Instant::now() >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(25));
    }
    true
}

#[cfg(unix)]
fn launch_backend_with_grandchild() -> (
    super::RunningBackend,
    std::sync::Arc<std::sync::Mutex<BackendManager>>,
    i32,
    i32,
    PathBuf,
) {
    use std::fs;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    let temp_root = unique_temp_path("drop-teardown");
    fs::create_dir_all(&temp_root).expect("temp root");
    let pid_path = temp_root.join("grandchild.pid");
    let ready_path = temp_root.join("grandchild.ready");

    let script = r#"import pathlib, signal, subprocess, sys, time
pid_path = pathlib.Path(sys.argv[1])
ready_path = pathlib.Path(sys.argv[2])
grandchild_code = r'''
import pathlib, signal, sys, time
ready_path = pathlib.Path(sys.argv[1])
def on_term(sig, frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
ready_path.write_text("ready")
while True:
    time.sleep(0.1)
'''
grandchild = subprocess.Popen(
    [sys.executable, "-c", grandchild_code, str(ready_path)],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
pid_path.write_text(str(grandchild.pid))
def on_term(sig, frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
print('{"event":"ready","host":"127.0.0.1","port":9555,"token":"drop-test"}', flush=True)
while True:
    time.sleep(0.1)
"#;

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let running = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".to_string(),
                script.to_string(),
                pid_path.to_string_lossy().into_owned(),
                ready_path.to_string_lossy().into_owned(),
            ],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("backend fixture launch");

    let child_pid = i32::try_from(running.child.id()).expect("child pid");
    let deadline = Instant::now() + Duration::from_secs(3);
    while (!pid_path.is_file() || !ready_path.is_file()) && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(25));
    }
    assert!(pid_path.is_file(), "expected grandchild pid fixture");
    assert!(ready_path.is_file(), "expected grandchild ready fixture");
    let grandchild_pid: i32 = fs::read_to_string(&pid_path)
        .expect("grandchild pid")
        .trim()
        .parse()
        .expect("integer grandchild pid");

    (running, manager, child_pid, grandchild_pid, temp_root)
}

#[cfg(unix)]
#[test]
fn dropping_running_backend_without_stop_kills_process_group() {
    use std::fs;
    use std::time::Duration;

    let (running, _manager, child_pid, grandchild_pid, temp_root) =
        launch_backend_with_grandchild();

    // Last-chance teardown path: drop without calling ordinary stop().
    drop(running);

    let child_gone = wait_until_process_exits(child_pid, Duration::from_secs(2));
    let grandchild_gone = wait_until_process_exits(grandchild_pid, Duration::from_secs(2));
    if !child_gone || !grandchild_gone {
        let _ = unsafe { libc::kill(grandchild_pid, libc::SIGKILL) };
        let _ = unsafe { libc::kill(child_pid, libc::SIGKILL) };
    }
    assert!(
        child_gone,
        "Drop fallback must kill backend child pid {child_pid}"
    );
    assert!(
        grandchild_gone,
        "Drop fallback must kill backend process-group grandchild pid {grandchild_pid}"
    );

    let _ = fs::remove_dir_all(temp_root);
}

#[cfg(unix)]
fn launch_backend_leader_exits_leaving_same_group_grandchild() -> (
    super::RunningBackend,
    std::sync::Arc<std::sync::Mutex<BackendManager>>,
    i32,
    i32,
    PathBuf,
) {
    use std::fs;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    let temp_root = unique_temp_path("leader-exit-grandchild");
    fs::create_dir_all(&temp_root).expect("temp root");
    let pid_path = temp_root.join("grandchild.pid");
    let ready_path = temp_root.join("grandchild.ready");
    let leader_exited_path = temp_root.join("leader.exited");

    // Leader spawns a same-process-group grandchild, then exits. The grandchild
    // remains in the owned group — the Drop/emergency path must still kill it
    // even though child.try_wait() reports the leader has already exited.
    let script = r#"import os, pathlib, signal, subprocess, sys, time
pid_path = pathlib.Path(sys.argv[1])
ready_path = pathlib.Path(sys.argv[2])
leader_exited_path = pathlib.Path(sys.argv[3])
grandchild_code = r'''
import pathlib, signal, sys, time
ready_path = pathlib.Path(sys.argv[1])
def on_term(sig, frame):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, on_term)
ready_path.write_text("ready")
while True:
    time.sleep(0.1)
'''
grandchild = subprocess.Popen(
    [sys.executable, "-c", grandchild_code, str(ready_path)],
    stdout=sys.stdout,
    stderr=sys.stderr,
)
pid_path.write_text(str(grandchild.pid))
# Wait until grandchild is ready, then exit the leader while leaving it alive.
deadline = time.time() + 3
while not ready_path.is_file() and time.time() < deadline:
    time.sleep(0.025)
leader_exited_path.write_text(str(os.getpid()))
raise SystemExit(0)
"#;

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let running = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".to_string(),
                script.to_string(),
                pid_path.to_string_lossy().into_owned(),
                ready_path.to_string_lossy().into_owned(),
                leader_exited_path.to_string_lossy().into_owned(),
            ],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("backend fixture launch");

    let child_pid = i32::try_from(running.child.id()).expect("child pid");
    let deadline = Instant::now() + Duration::from_secs(3);
    while (!pid_path.is_file() || !ready_path.is_file() || !leader_exited_path.is_file())
        && Instant::now() < deadline
    {
        thread::sleep(Duration::from_millis(25));
    }
    assert!(pid_path.is_file(), "expected grandchild pid fixture");
    assert!(ready_path.is_file(), "expected grandchild ready fixture");
    assert!(
        leader_exited_path.is_file(),
        "expected leader exit marker fixture"
    );
    let grandchild_pid: i32 = fs::read_to_string(&pid_path)
        .expect("grandchild pid")
        .trim()
        .parse()
        .expect("integer grandchild pid");

    // Leader wrote its exit marker and then exited; do not reap via waitpid —
    // RunningBackend must retain the unreaped child so Drop can still prove
    // process-group ownership. Brief settle so the leader is a zombie.
    thread::sleep(Duration::from_millis(100));
    assert!(
        process_is_alive(grandchild_pid),
        "grandchild must still be alive after leader exit"
    );
    let grandchild_pgid = unsafe { libc::getpgid(grandchild_pid) };
    assert_eq!(
        grandchild_pgid, child_pid,
        "grandchild must remain in the leader's process group"
    );

    (running, manager, child_pid, grandchild_pid, temp_root)
}

#[cfg(unix)]
#[test]
fn emergency_teardown_kills_same_group_grandchild_after_leader_exits() {
    use std::fs;
    use std::time::Duration;

    let (running, _manager, child_pid, grandchild_pid, temp_root) =
        launch_backend_leader_exits_leaving_same_group_grandchild();

    // Leader has already exited; Drop/emergency teardown must still tear down
    // the owned process group (grandchild) within a bound.
    drop(running);

    let grandchild_gone = wait_until_process_exits(grandchild_pid, Duration::from_secs(2));
    if !grandchild_gone {
        let _ = unsafe { libc::kill(grandchild_pid, libc::SIGKILL) };
        let _ = unsafe { libc::kill(child_pid, libc::SIGKILL) };
    }
    assert!(
        grandchild_gone,
        "emergency teardown must kill same-group grandchild pid {grandchild_pid} after leader exit"
    );

    let _ = fs::remove_dir_all(temp_root);
}

#[cfg(unix)]
#[test]
fn refresh_exit_status_kills_same_group_grandchild_before_reap() {
    use std::fs;
    use std::time::Duration;

    let (mut running, manager, child_pid, grandchild_pid, temp_root) =
        launch_backend_leader_exits_leaving_same_group_grandchild();

    running
        .refresh_exit_status(&manager)
        .expect("polling should observe the crashed backend");
    running
        .refresh_exit_status(&manager)
        .expect("polling an already reaped backend should be idempotent");

    assert!(wait_until_process_exits(
        grandchild_pid,
        Duration::from_secs(2)
    ));
    assert!(running.child_reaped);
    assert!(!process_is_alive(child_pid));
    assert_eq!(
        manager.lock().expect("manager lock").lifecycle,
        super::BackendLifecycle::Failed
    );

    // The subsequent ordinary stop must not signal the stored PGID after the
    // leader was reaped, where PID reuse could target an unrelated process.
    running.stop(&manager);
    let _ = fs::remove_dir_all(temp_root);
}

#[cfg(unix)]
#[test]
fn verified_owned_process_group_rejects_reaped_leader() {
    use std::process::{Command, Stdio};
    use std::thread;
    use std::time::Duration;

    let mut child = Command::new(super::python_program())
        .arg("-c")
        .arg("import time; time.sleep(60)")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("expected ownership fixture to launch");
    let pid = i32::try_from(child.id()).expect("child pid");
    thread::sleep(Duration::from_millis(50));

    // A stale RunningBackend can retain the old numeric PID after reap. Even
    // if that PID now has a matching PGID, it must never authorize a group kill.
    assert_eq!(
        super::verified_owned_process_group_id(&child, Some(pid), true),
        None
    );

    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(unix)]
#[test]
fn ordinary_stop_disarms_drop_fallback() {
    use std::fs;
    use std::time::Duration;

    let (mut running, manager, child_pid, grandchild_pid, temp_root) =
        launch_backend_with_grandchild();

    // Disarm without stopping, then Drop must not kill (proves ordinary stop's
    // disarm path prevents double-kill / unrelated pgid hazards).
    running.disarm_emergency_teardown();
    drop(running);

    assert!(
        process_is_alive(child_pid),
        "disarmed Drop must not kill the owned backend"
    );
    assert!(
        process_is_alive(grandchild_pid),
        "disarmed Drop must not kill the process group"
    );

    // Cleanup the fixture processes now that disarm behavior is verified.
    let _ = unsafe { libc::kill(grandchild_pid, libc::SIGKILL) };
    let _ = unsafe { libc::kill(child_pid, libc::SIGKILL) };
    // Reap the former child so it does not linger as a zombie that still
    // answers kill(pid, 0).
    let mut status = 0;
    let _ = unsafe { libc::waitpid(child_pid, &mut status, 0) };
    assert!(
        wait_until_process_exits(grandchild_pid, Duration::from_secs(2)),
        "cleanup must terminate grandchild"
    );
    let _ = manager;
    let _ = fs::remove_dir_all(temp_root);
}

#[cfg(unix)]
#[test]
fn ordinary_stop_then_drop_is_safe_and_repeated_emergency_teardown_is_idempotent() {
    use std::fs;
    use std::sync::Mutex;
    use std::time::Duration;

    let (running, manager, child_pid, grandchild_pid, temp_root) = launch_backend_with_grandchild();

    running.stop(&manager);
    assert!(wait_until_process_exits(child_pid, Duration::from_secs(3)));
    assert!(wait_until_process_exits(
        grandchild_pid,
        Duration::from_secs(3)
    ));

    // Slot-style exit fallback after ordinary stop must be a no-op.
    let slot: Mutex<Option<super::RunningBackend>> = Mutex::new(None);
    super::emergency_teardown_backend_slot(&slot);
    super::emergency_teardown_backend_slot(&slot);

    let (mut running2, manager2, child_pid2, grandchild_pid2, temp_root2) =
        launch_backend_with_grandchild();
    running2.emergency_teardown();
    running2.emergency_teardown();
    drop(running2);
    assert!(wait_until_process_exits(child_pid2, Duration::from_secs(2)));
    assert!(wait_until_process_exits(
        grandchild_pid2,
        Duration::from_secs(2)
    ));
    let _ = manager2;

    let _ = fs::remove_dir_all(temp_root);
    let _ = fs::remove_dir_all(temp_root2);
}
