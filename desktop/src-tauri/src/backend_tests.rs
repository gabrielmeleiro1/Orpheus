use super::{
    backend_exit_detail, build_dev_launch_config_for_root, parse_ready_line, parse_shutdown_line,
    read_ready_payload_from_file, resolve_sidecar_path, BackendLifecycle, BackendManager,
    BACKEND_SHUTDOWN_ACK_EXTENSION, BACKEND_SHUTDOWN_FORCE_KILL_MAX,
    BACKEND_SHUTDOWN_GRACEFUL_TIMEOUT, BACKEND_SIDECAR_NAME,
};

#[cfg(windows)]
#[test]
fn forced_backend_shutdown_terminates_root_and_descendant_in_job() {
    use std::fs;
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("expected system time after unix epoch")
        .as_nanos();
    let marker = std::env::temp_dir().join(format!("orpheus-backend-descendant-{unique}.txt"));
    let script = r#"import ctypes, http.server, json, os, pathlib, subprocess, sys, threading, time
assert ctypes.windll.kernel32.GetConsoleWindow() == 0, "backend opened a console"
marker = pathlib.Path(sys.argv[1])
# Accept shutdown but never exit, exercising the timeout fallback.
class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(202)
        self.end_headers()
server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
)
marker.write_text(f"{os.getpid()} {descendant.pid}", encoding="ascii")
print(json.dumps({"event":"ready","host":"127.0.0.1","port":server.server_port,"token":"test"}), flush=True)
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
    let processes = [WatchedProcess::open(pids[0]), WatchedProcess::open(pids[1])];
    assert!(processes.iter().all(WatchedProcess::is_running));

    let endpoint = loop {
        if let Some(endpoint) = running.shutdown_endpoint.lock().unwrap().clone() {
            break endpoint;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "missing ready endpoint"
        );
        thread::sleep(Duration::from_millis(25));
    };
    let outcome = super::terminate_backend_process(
        &mut running.child,
        Duration::from_millis(100),
        None,
        Some(running.job.as_ref()),
        Some(&endpoint),
    );
    assert_eq!(outcome, super::ShutdownOutcome::ForcedKill);
    let _ = running.child.wait();

    // Verify Job Object termination itself killed both processes. Do this
    // before dropping `running`; otherwise KILL_ON_JOB_CLOSE would mask a
    // broken TerminateJobObject call.
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while processes.iter().any(WatchedProcess::is_running) {
        assert!(
            std::time::Instant::now() < deadline,
            "forced backend shutdown left a process alive"
        );
        thread::sleep(Duration::from_millis(25));
    }

    drop(running);
    let _ = fs::remove_file(marker);
}

/// Keeps a process handle open so its PID cannot be recycled while a test
/// polls it; parallel tests spawn many short-lived Python processes.
#[cfg(windows)]
struct WatchedProcess {
    pid: u32,
    handle: windows_sys::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
impl WatchedProcess {
    fn open(pid: u32) -> Self {
        use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_SYNCHRONIZE};

        let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
        assert!(!handle.is_null(), "failed to open process {pid}");
        Self { pid, handle }
    }

    /// Uses the signaled state, like `Child::try_wait`. An exit code can be
    /// published slightly before the handle is signaled, so polling it would
    /// let a test call `refresh_exit_status` before the exit is observable.
    fn is_running(&self) -> bool {
        use windows_sys::Win32::Foundation::WAIT_TIMEOUT;
        use windows_sys::Win32::System::Threading::WaitForSingleObject;

        unsafe { WaitForSingleObject(self.handle, 0) == WAIT_TIMEOUT }
    }
}

#[cfg(windows)]
impl Drop for WatchedProcess {
    fn drop(&mut self) {
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.handle) };
    }
}

#[cfg(windows)]
fn launch_windows_backend_with_descendant() -> (
    super::RunningBackend,
    std::sync::Arc<std::sync::Mutex<BackendManager>>,
    WatchedProcess,
    PathBuf,
) {
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    let root = unique_temp_path("windows-backend-exit");
    std::fs::create_dir_all(&root).unwrap();
    let script = r#"import os, pathlib, subprocess, sys, time
root = pathlib.Path(sys.argv[1])
descendant = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(60)'],
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
)
(root / 'worker.pid').write_text(str(descendant.pid), encoding='ascii')
while not (root / 'exit').exists():
    time.sleep(0.01)
os._exit(23)
"#;
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let running = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".into(),
                script.into(),
                root.to_string_lossy().into_owned(),
            ],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("contained Windows backend fixture");
    let deadline = Instant::now() + Duration::from_secs(5);
    let worker = loop {
        if let Ok(contents) = std::fs::read_to_string(root.join("worker.pid")) {
            if let Ok(pid) = contents.parse::<u32>() {
                break pid;
            }
        }
        assert!(Instant::now() < deadline, "backend worker did not start");
        std::thread::sleep(Duration::from_millis(10));
    };
    let worker = WatchedProcess::open(worker);
    assert!(worker.is_running());
    (running, manager, worker, root)
}

#[cfg(windows)]
fn assert_windows_process_exits(process: &WatchedProcess) {
    use std::time::{Duration, Instant};

    let deadline = Instant::now() + Duration::from_secs(5);
    while process.is_running() {
        assert!(
            Instant::now() < deadline,
            "owned process {} survived cleanup",
            process.pid
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(windows)]
#[test]
fn windows_exit_refresh_terminates_surviving_worker_before_drop() {
    let (mut running, manager, worker, root) = launch_windows_backend_with_descendant();
    std::fs::write(root.join("exit"), b"exit").unwrap();
    assert_windows_process_exits(&WatchedProcess::open(running.child.id()));
    running.refresh_exit_status(&manager).unwrap();
    running.refresh_exit_status(&manager).unwrap();

    // A failed backend remains in the application slot until the user restarts.
    // Keeping it alive here ensures kill-on-job-close cannot mask missing cleanup.
    assert_windows_process_exits(&worker);
    assert_eq!(
        manager.lock().unwrap().status().lifecycle,
        BackendLifecycle::Failed
    );
    drop(running);
    std::fs::remove_dir_all(root).unwrap();
}

#[cfg(windows)]
#[test]
fn windows_emergency_teardown_terminates_surviving_worker_before_drop() {
    let (mut running, _manager, worker, root) = launch_windows_backend_with_descendant();
    running.emergency_teardown();
    running.emergency_teardown();
    assert_windows_process_exits(&WatchedProcess::open(running.child.id()));
    assert_windows_process_exits(&worker);
    drop(running);
    std::fs::remove_dir_all(root).unwrap();
}

use crate::paths::DesktopRuntimePaths;
use std::path::{Path, PathBuf};

#[cfg(windows)]
#[test]
fn hidden_backend_shutdown_runs_cleanup_and_acknowledges_before_exit() {
    use std::sync::{atomic::Ordering, Arc, Mutex};
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let marker = std::env::temp_dir().join(format!("orpheus-graceful-shutdown-{unique}.txt"));
    let script = r#"import ctypes, json, pathlib, sys
assert ctypes.windll.kernel32.GetConsoleWindow() == 0, "backend opened a console"
from apps.launchers.desktop_backend import _build_desktop_server
from modules.web import server
server._acknowledge_runtime_setup_on_startup = lambda _: None
class Controller:
    def cleanup(self):
        pathlib.Path(sys.argv[1]).write_text('cleanup completed', encoding='ascii')
        print(json.dumps({'event': 'shutdown'}), flush=True)
app = server.create_app(controller_factory=Controller, desktop_mode=True, desktop_api_token='shutdown-test')
backend = _build_desktop_server(app, host='127.0.0.1', port=0, token='shutdown-test', runtime_profile='base')
backend.run()
"#;
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let running = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".into(),
                script.into(),
                marker.to_string_lossy().into_owned(),
            ],
            cwd: Some(super::project_root_dir()),
            env: Vec::new(),
        },
    )
    .expect("hidden backend should start");
    let deadline = Instant::now() + Duration::from_secs(15);
    while manager.lock().unwrap().endpoint().is_none() {
        assert!(Instant::now() < deadline, "backend did not become ready");
        std::thread::sleep(Duration::from_millis(25));
    }
    let acknowledgement = Arc::clone(&running.shutdown_ack);
    // Even if a new generation replaces the manager, stop must target this child.
    manager.lock().unwrap().mark_starting();
    let _ = running.stop(&manager);
    assert!(
        acknowledgement.load(Ordering::SeqCst),
        "cleanup was not acknowledged"
    );
    assert_eq!(
        std::fs::read_to_string(&marker).unwrap(),
        "cleanup completed"
    );
    let _ = std::fs::remove_file(marker);
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
fn read_lossy_line_trims_line_endings_and_survives_invalid_utf8() {
    let mut reader = std::io::Cursor::new(b"first\r\nJos\xe9\nlast".to_vec());
    let mut buffer = Vec::new();
    let lines: Vec<String> =
        std::iter::from_fn(|| super::read_lossy_line(&mut reader, &mut buffer)).collect();
    assert_eq!(lines, ["first", "Jos\u{fffd}", "last"]);
}

#[test]
fn backend_state_machine_transitions_start_ready_stop() {
    let mut manager = BackendManager::default();

    assert_eq!(manager.status().lifecycle, BackendLifecycle::Stopped);
    assert!(manager.status().endpoint.is_none());

    let generation = manager.mark_starting();
    assert_eq!(manager.status().lifecycle, BackendLifecycle::Starting);
    assert!(manager.status().endpoint.is_none());

    let payload = parse_ready_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"token-1\"}",
    )
    .expect("expected valid ready payload");
    manager.mark_ready_for_generation(generation, payload);

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
    let generation = manager.mark_starting();
    manager.mark_failed("backend exited");

    let payload = parse_ready_line(
        "{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"token-1\"}",
    )
    .expect("expected valid ready payload");
    manager.mark_ready_for_generation(generation, payload);

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

    let _ = running.stop(&manager);

    assert_eq!(terminal_status.lifecycle, BackendLifecycle::Failed);
    assert!(terminal_status.endpoint.is_none());
    assert!(terminal_status
        .error
        .as_deref()
        .unwrap_or_default()
        .contains("desktop backend exited unexpectedly"));
}

fn run_backend_to_failure(data_dir: &Path, output: &str) -> super::BackendStatus {
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let script = format!(
        "import sys\nprint('{{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"secret-token\"}}', flush=True)\n{output}"
    );
    let launch = super::ProcessLaunchConfig {
        program: super::python_program(),
        args: vec!["-c".to_string(), script],
        cwd: None,
        env: vec![
            (
                "AUDIOBOOK_DATA_DIR".to_string(),
                data_dir.to_string_lossy().into_owned(),
            ),
            ("PYTHONIOENCODING".to_string(), "utf-8".to_string()),
        ],
    };
    let mut running = super::launch_backend(Arc::clone(&manager), launch).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    let status = loop {
        running.refresh_exit_status(&manager).unwrap();
        let status = manager.lock().unwrap().status();
        if status.lifecycle == BackendLifecycle::Failed {
            break status;
        }
        assert!(Instant::now() < deadline, "backend did not report its exit");
        std::thread::sleep(Duration::from_millis(10));
    };
    let _ = running.stop(&manager);
    status
}

#[test]
fn ready_backend_persists_diagnostics_and_reports_stderr_on_exit() {
    let data_dir = unique_temp_path("backend-diagnostics");
    let status = run_backend_to_failure(
        &data_dir,
        "print('conversion progress', flush=True)\nprint('speech initialization failed', file=sys.stderr, flush=True)",
    );
    let log = std::fs::read_to_string(data_dir.join("logs/backend.log")).unwrap();
    assert!(log.contains("speech initialization failed"));
    assert!(log.contains("conversion progress"));
    assert!(!log.contains("secret-token"));
    assert!(status
        .error
        .unwrap()
        .contains("speech initialization failed"));
    std::fs::remove_dir_all(data_dir).unwrap();
}

#[test]
fn backend_log_keeps_reading_after_non_utf8_stdout_and_stderr() {
    let data_dir = unique_temp_path("backend-log-invalid-bytes");
    let status = run_backend_to_failure(
        &data_dir,
        "sys.stdout.buffer.write(b'Jos\\xe9 stdout\\ncontinued stdout\\n'); sys.stdout.flush()\nsys.stderr.buffer.write(b'Jos\\xe9 stderr\\ncontinued stderr\\n'); sys.stderr.flush()",
    );
    let log = std::fs::read_to_string(data_dir.join("logs/backend.log")).unwrap();
    for line in [
        "Jos� stdout",
        "continued stdout",
        "Jos� stderr",
        "continued stderr",
    ] {
        assert!(log.contains(line), "missing diagnostic: {line}");
    }
    let error = status.error.unwrap();
    assert!(error.contains("Jos� stderr"));
    assert!(error.contains("continued stderr"));
    std::fs::remove_dir_all(data_dir).unwrap();
}

#[test]
fn backend_log_rotates_at_size_limit() {
    let data_dir = unique_temp_path("backend-log-rotation");
    let logs = data_dir.join("logs");
    std::fs::create_dir_all(&logs).unwrap();
    let limit = 5 * 1024 * 1024;
    std::fs::write(logs.join("backend.log"), vec![b'x'; limit]).unwrap();
    std::fs::write(logs.join("backend.log.1"), "older log").unwrap();
    run_backend_to_failure(&data_dir, "print('new diagnostic', flush=True)");
    assert_eq!(
        std::fs::metadata(logs.join("backend.log.1")).unwrap().len(),
        limit as u64
    );
    assert!(std::fs::metadata(logs.join("backend.log")).unwrap().len() <= limit as u64);
    assert!(std::fs::read_to_string(logs.join("backend.log"))
        .unwrap()
        .contains("new diagnostic"));
    std::fs::remove_dir_all(data_dir).unwrap();
}

#[test]
fn backend_log_bounds_oversized_lines() {
    let data_dir = unique_temp_path("backend-log-large-line");
    run_backend_to_failure(
        &data_dir,
        "print('é' * (3 * 1024 * 1024), flush=True)\nprint('last diagnostic', flush=True)",
    );
    for name in ["backend.log", "backend.log.1"] {
        let contents = std::fs::read_to_string(data_dir.join("logs").join(name)).unwrap();
        assert!(contents.len() <= 5 * 1024 * 1024);
    }
    assert!(std::fs::read_to_string(data_dir.join("logs/backend.log"))
        .unwrap()
        .contains("last diagnostic"));
    std::fs::remove_dir_all(data_dir).unwrap();
}

#[test]
fn backend_log_io_failures_preserve_exit_diagnostics() {
    for block_rotation in [false, true] {
        let data_dir = unique_temp_path("backend-log-blocked");
        let logs = data_dir.join("logs");
        std::fs::create_dir_all(&data_dir).unwrap();
        if block_rotation {
            std::fs::create_dir_all(logs.join("backend.log.1")).unwrap();
            std::fs::write(logs.join("backend.log"), vec![b'x'; 5 * 1024 * 1024]).unwrap();
        } else {
            std::fs::write(&logs, "blocked").unwrap();
        }
        let status = run_backend_to_failure(
            &data_dir,
            "print('speech initialization failed', file=sys.stderr, flush=True)",
        );
        assert!(status
            .error
            .unwrap()
            .contains("speech initialization failed"));
        std::fs::remove_dir_all(data_dir).unwrap();
    }
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

    let _ = running.stop(&manager);

    assert_eq!(terminal_status.lifecycle, BackendLifecycle::Stopped);
    assert!(terminal_status.error.is_none());
}

#[test]
fn desktop_launch_configs_carry_runtime_paths_and_parent_pid() {
    let root = std::env::temp_dir().join("orpheus-parent-env");
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir(&root);
    let configs = [
        super::build_dev_launch_config_with_python(&runtime_paths, &root, &root.join("python")),
        super::build_sidecar_launch_config(&runtime_paths, root.join("backend")),
    ];
    for config in configs {
        let env = config
            .env
            .into_iter()
            .collect::<std::collections::BTreeMap<_, _>>();
        assert_eq!(
            env.get("ORPHEUS_DESKTOP_PARENT_PID"),
            Some(&std::process::id().to_string())
        );
        for (key, value) in runtime_paths.as_env() {
            assert_eq!(env.get(&key), Some(&value), "wrong {key}");
        }
    }
}

#[test]
fn build_dev_launch_config_matches_desktop_runtime_contract() {
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir("/tmp/audiobook-app");
    let repo = Path::new("/trusted/orpheus");
    let python = repo.join(".venv").join("bin").join("python");
    let config = super::build_dev_launch_config_with_python(&runtime_paths, repo, &python);

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
}

#[cfg(unix)]
#[test]
fn dev_backend_ignores_path_and_python_override_for_fixed_checkout_venv() {
    use std::ffi::OsStr;
    use std::os::unix::fs::symlink;

    if let Some(output) = rerun_in_child_test_process(
        "dev_backend_ignores_path_and_python_override_for_fixed_checkout_venv",
        &[
            (
                "AUDIOBOOK_DESKTOP_PYTHON",
                OsStr::new("/tmp/attacker-python"),
            ),
            ("PATH", OsStr::new("/tmp/attacker-bin")),
        ],
        false,
    ) {
        assert_child_test_passed(&output);
        return;
    }

    let repo = unique_temp_path("backend-python-env-isolation");
    std::fs::create_dir_all(repo.join(".venv/bin")).expect("venv bin");
    symlink("/bin/sh", repo.join(".venv/bin/python")).expect("python symlink");
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir(repo.join("app-data"));
    let launch =
        build_dev_launch_config_for_root(&runtime_paths, &repo).expect("fixed dev backend launch");
    let canonical_repo = repo.canonicalize().expect("canonical repo");
    assert_eq!(
        launch.program,
        canonical_repo.join(".venv/bin/python").to_string_lossy()
    );
    assert_eq!(launch.cwd, Some(repo.clone()));
    let _ = std::fs::remove_dir_all(repo);
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
fn resolve_sidecar_path_finds_resource_onedir_binary() {
    let temp_root = unique_temp_path("resource-onedir");
    let sidecar_dir = temp_root.join("binaries").join(BACKEND_SIDECAR_NAME);
    std::fs::create_dir_all(&sidecar_dir).expect("expected test sidecar dir");
    let sidecar_path = sidecar_dir.join(runtime_sidecar_file_name_for_tests());
    std::fs::write(&sidecar_path, b"test-sidecar").expect("expected sidecar fixture");

    let resolved = resolve_sidecar_path(Some(&temp_root), None).expect("expected sidecar path");
    assert_eq!(resolved, sidecar_path);

    let _ = std::fs::remove_dir_all(&temp_root);
}

#[test]
fn resolve_sidecar_path_reports_missing_binary() {
    let temp_root = unique_temp_path("resource-missing");
    std::fs::create_dir_all(&temp_root).expect("expected test temp dir");

    let error = resolve_sidecar_path(Some(&temp_root), None)
        .expect_err("expected missing sidecar resolution to fail");
    assert!(error.contains(BACKEND_SIDECAR_NAME));

    let _ = std::fs::remove_dir_all(&temp_root);
}

#[test]
fn resolve_sidecar_path_uses_logical_install_when_resource_directory_is_virtualized() {
    let temp_root = unique_temp_path("virtualized-resource");
    let install = temp_root.join("install");
    let resources = temp_root.join("package-cache");
    let sidecar_dir = install.join("binaries").join(BACKEND_SIDECAR_NAME);
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
        format!("{}.exe", BACKEND_SIDECAR_NAME)
    } else {
        BACKEND_SIDECAR_NAME.to_string()
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
        stderr_lines: Arc::new(Mutex::new(Vec::new())),
        shutdown_ack: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        generation: 0,
        process_group_id: None,
        child_reaped: false,
        emergency_teardown_armed: true,
    };
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let _ = running.stop(&manager);

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
    let graceful = BACKEND_SHUTDOWN_GRACEFUL_TIMEOUT;
    assert!(
        graceful > std::time::Duration::from_millis(5000),
        "graceful shutdown timeout {:?} must exceed the backend's documented 5.0s cleanup budget",
        graceful
    );
    assert!(
        graceful + BACKEND_SHUTDOWN_ACK_EXTENSION <= BACKEND_SHUTDOWN_FORCE_KILL_MAX,
        "force-kill ceiling {:?} must cover the graceful timeout {:?} plus the ack extension {:?}",
        BACKEND_SHUTDOWN_FORCE_KILL_MAX,
        graceful,
        BACKEND_SHUTDOWN_ACK_EXTENSION
    );
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
        stderr_lines: Arc::new(Mutex::new(Vec::new())),
        shutdown_ack: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        generation: 0,
        process_group_id: None,
        child_reaped: false,
        emergency_teardown_armed: true,
    };
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let started_at = Instant::now();

    let _ = running.stop(&manager);

    assert!(
        started_at.elapsed() >= Duration::from_millis(2400),
        "stop should wait for simulated cleanup to finish"
    );
    assert!(
        started_at.elapsed() < BACKEND_SHUTDOWN_FORCE_KILL_MAX,
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
        stderr_lines: Arc::new(Mutex::new(Vec::new())),
        shutdown_ack: Arc::new(std::sync::atomic::AtomicBool::new(false)),
        generation: 0,
        process_group_id: None,
        child_reaped: false,
        emergency_teardown_armed: true,
    };
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let started_at = Instant::now();

    let _ = running.stop(&manager);

    assert!(
        started_at.elapsed() >= BACKEND_SHUTDOWN_GRACEFUL_TIMEOUT,
        "unresponsive backend should receive the full graceful shutdown window"
    );
    assert!(
        started_at.elapsed() < BACKEND_SHUTDOWN_FORCE_KILL_MAX + Duration::from_millis(500),
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
        let _ = running.stop(&stop_manager);
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
        let _ = second.stop(&manager);
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
    assert!(!process_is_alive(child_pid));
    assert_eq!(
        manager.lock().expect("manager lock").status().lifecycle,
        BackendLifecycle::Failed
    );

    // The subsequent ordinary stop must not signal the stored PGID after the
    // leader was reaped, where PID reuse could target an unrelated process.
    let _ = running.stop(&manager);
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
fn ordinary_stop_then_drop_is_safe_and_repeated_emergency_teardown_is_idempotent() {
    use std::fs;
    use std::sync::Mutex;
    use std::time::Duration;

    let (running, manager, child_pid, grandchild_pid, temp_root) = launch_backend_with_grandchild();

    let _ = running.stop(&manager);
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

#[test]
fn file_readiness_adopts_the_endpoint_without_stdout() {
    let (state, path) = starting_state_with_ready_file(
        r#"{"event":"ready","host":"127.0.0.1","port":54321,"token":"file-token"}"#,
    );

    let result = state.refresh_backend_ready_from_file();
    let endpoint = state.backend_manager.lock().unwrap().endpoint();
    state.shutdown();
    let _ = std::fs::remove_file(path);

    result.expect("file readiness");
    let endpoint = endpoint.expect("adopted endpoint");
    assert_eq!(endpoint.base_url, "http://127.0.0.1:54321");
    assert_eq!(endpoint.token, "file-token");
}

// Only Windows keeps the adopted payload: it authenticates the graceful
// shutdown request, so a stale generation must never supply it.
#[cfg(windows)]
#[test]
fn readiness_adoption_ignores_stale_generations_and_terminal_states() {
    use std::sync::Mutex;

    let payload = parse_ready_line(
        r#"{"event":"ready","host":"127.0.0.1","port":54321,"token":"old-token"}"#,
    )
    .unwrap();
    let manager = Mutex::new(BackendManager::default());
    let endpoint = Mutex::new(None);
    let old = manager.lock().unwrap().mark_starting();
    let current = manager.lock().unwrap().mark_starting();
    super::adopt_backend_ready(&manager, old, payload.clone(), None, &endpoint).unwrap();
    assert!(endpoint.lock().unwrap().is_none());
    assert_eq!(
        manager.lock().unwrap().status().lifecycle,
        BackendLifecycle::Starting
    );

    manager.lock().unwrap().mark_failed("startup timed out");
    super::adopt_backend_ready(&manager, current, payload, None, &endpoint).unwrap();
    assert!(endpoint.lock().unwrap().is_none());
    assert_eq!(
        manager.lock().unwrap().status().lifecycle,
        BackendLifecycle::Failed
    );
}

#[test]
fn setup_timeout_stops_only_the_launched_child() {
    use std::sync::Arc;

    let state = crate::AppState::default();
    let old = state.backend_manager.lock().unwrap().mark_starting();
    let child = super::launch_backend(
        Arc::clone(&state.backend_manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec!["-c".into(), "import time; time.sleep(60)".into()],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("backend launch");
    let current = child.generation();
    *state.backend_process.lock().unwrap() = Some(child);

    state.fail_backend_generation(old, "startup timed out");
    {
        let mut slot = state.backend_process.lock().unwrap();
        let child = slot.as_mut().expect("replacement remains installed");
        assert!(child.child.try_wait().unwrap().is_none());
    }
    state.fail_backend_generation(current, "startup timed out");
    assert!(state.backend_process.lock().unwrap().is_none());
    let status = state.backend_manager.lock().unwrap().status();
    assert_eq!(status.lifecycle, BackendLifecycle::Failed);
    assert_eq!(status.error.as_deref(), Some("startup timed out"));
}

#[cfg(windows)]
#[test]
fn file_readiness_enables_authenticated_graceful_shutdown() {
    use std::sync::Arc;
    use std::time::{Duration, Instant};

    let root = unique_temp_path("file-shutdown");
    std::fs::create_dir_all(&root).unwrap();
    let ready = root.join("ready.json");
    let marker = root.join("cleanup.txt");
    let script = r#"import http.server, json, pathlib, sys
ready, marker = map(pathlib.Path, sys.argv[1:])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        assert self.path == '/api/desktop/shutdown'
        assert self.headers['Authorization'] == 'Bearer file-token'
        marker.write_text('cleanup completed')
        self.send_response(202)
        self.end_headers()
server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
staged = ready.with_suffix('.tmp')
staged.write_text(json.dumps(dict(event='ready', host='127.0.0.1', port=server.server_port, token='file-token')))
staged.replace(ready)
server.handle_request()
print(json.dumps(dict(event='shutdown')), flush=True)
"#;
    let state = crate::AppState::default();
    let child = super::launch_backend(
        Arc::clone(&state.backend_manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".into(),
                script.into(),
                ready.to_string_lossy().into_owned(),
                marker.to_string_lossy().into_owned(),
            ],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("backend launch");
    *state.backend_process.lock().unwrap() = Some(child);
    *state.backend_ready_file.lock().unwrap() = Some(ready);
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        state.refresh_backend_ready_from_file().unwrap();
        if state.backend_manager.lock().unwrap().endpoint().is_some() {
            break;
        }
        assert!(Instant::now() < deadline, "file readiness did not arrive");
        std::thread::sleep(Duration::from_millis(25));
    }
    state.shutdown();
    assert_eq!(
        std::fs::read_to_string(&marker).unwrap(),
        "cleanup completed"
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn parse_ready_line_rejects_hosts_other_than_the_requested_loopback() {
    for host in ["203.0.113.7", "example.com", "localhost", "0.0.0.0"] {
        let line = format!(r#"{{"event":"ready","host":"{host}","port":54321,"token":"abc123"}}"#);
        let error = parse_ready_line(&line).expect_err("foreign host must be rejected");
        assert!(error.contains("loopback"), "{host}: {error}");
        assert!(!error.contains("abc123"), "{host}: {error}");
    }
}

#[test]
fn rejected_ready_line_is_neither_adopted_nor_logged_with_its_token() {
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    let data_dir = unique_temp_path("backend-foreign-ready");
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let mut running = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".into(),
                r#"print('{"event":"ready","host":"203.0.113.7","port":9000,"token":"never-log-this-token"}', flush=True)"#.into(),
            ],
            cwd: None,
            env: vec![(
                "AUDIOBOOK_DATA_DIR".into(),
                data_dir.to_string_lossy().into_owned(),
            )],
        },
    )
    .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        running.refresh_exit_status(&manager).unwrap();
        let status = manager.lock().unwrap().status();
        if status.lifecycle != BackendLifecycle::Starting {
            break status;
        }
        assert!(Instant::now() < deadline, "backend did not report its exit");
        std::thread::sleep(Duration::from_millis(10));
    };
    let _ = running.stop(&manager);

    assert_eq!(status.lifecycle, BackendLifecycle::Failed, "{status:?}");
    assert!(status
        .error
        .as_deref()
        .unwrap_or_default()
        .contains("exited before ready payload"));
    let log = std::fs::read_to_string(data_dir.join("logs/backend.log")).unwrap();
    assert!(
        log.contains("ignored an invalid desktop ready payload"),
        "{log}"
    );
    assert!(!log.contains("never-log-this-token"), "{log}");
    std::fs::remove_dir_all(data_dir).unwrap();
}

/// Installs a sleeping child as the current `Starting` generation of a fresh
/// application state.
fn starting_state() -> crate::AppState {
    use std::sync::Arc;

    let state = crate::AppState::default();
    let child = super::launch_backend(
        Arc::clone(&state.backend_manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec!["-c".into(), "import time; time.sleep(60)".into()],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("backend launch");
    *state.backend_process.lock().unwrap() = Some(child);
    state
}

/// Like [`starting_state`], with `contents` waiting in the child's ready file.
fn starting_state_with_ready_file(contents: &str) -> (crate::AppState, PathBuf) {
    let state = starting_state();
    let path = unique_temp_path("pending-ready.json");
    std::fs::write(&path, contents).unwrap();
    *state.backend_ready_file.lock().unwrap() = Some(path.clone());
    (state, path)
}

#[test]
fn unreadable_ready_file_leaves_startup_pending_instead_of_failing_status() {
    let (state, path) = starting_state_with_ready_file(r#"{"event":"ready","host":"127.0"#);

    let result = state.refresh_backend_ready_from_file();
    let lifecycle = state.backend_manager.lock().unwrap().status().lifecycle;
    state.shutdown();
    let _ = std::fs::remove_file(path);

    result.expect("a ready file that cannot be parsed yet must not fail the status command");
    assert_eq!(lifecycle, BackendLifecycle::Starting);
}

#[cfg(windows)]
#[test]
fn ready_file_held_open_by_a_scanner_is_still_adopted() {
    use std::os::windows::fs::OpenOptionsExt;

    let (state, path) = starting_state_with_ready_file(
        r#"{"event":"ready","host":"127.0.0.1","port":54321,"token":"held-token"}"#,
    );
    // Antivirus and indexers commonly open new files without delete sharing,
    // which makes removal fail with a sharing violation while reads succeed.
    const FILE_SHARE_READ: u32 = 0x1;
    let scanner = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(&path)
        .unwrap();

    let result = state.refresh_backend_ready_from_file();
    let endpoint = state.backend_manager.lock().unwrap().endpoint();
    drop(scanner);
    state.shutdown();
    let _ = std::fs::remove_file(path);

    result.expect("a ready file that cannot be removed must not fail the status command");
    assert_eq!(endpoint.expect("adopted endpoint").token, "held-token");
}

#[test]
fn startup_deadline_kills_the_timed_out_child() {
    let state = starting_state();

    state
        .enforce_startup_deadline(std::time::Duration::ZERO)
        .unwrap();
    let exited = state
        .backend_process
        .lock()
        .unwrap()
        .as_mut()
        .expect("the failed child stays in the slot until restart")
        .child
        .try_wait()
        .unwrap()
        .is_some();
    let status = state.backend_manager.lock().unwrap().status();
    state.shutdown();

    assert!(exited, "a timed-out startup must not keep running");
    assert_eq!(status.lifecycle, BackendLifecycle::Failed);
    assert!(status
        .error
        .as_deref()
        .unwrap_or_default()
        .contains("did not become ready"));
}

const CHILD_TEST_ENV: &str = "ORPHEUS_BACKEND_CHILD_TEST";

/// Runs `backend::tests::<name>` again in a fresh test process so it can see
/// process-wide state (inherited environment, a closed stderr) that must not
/// leak into tests running in parallel. Returns `None` inside that child.
fn rerun_in_child_test_process(
    name: &str,
    env: &[(&str, &std::ffi::OsStr)],
    close_stderr: bool,
) -> Option<std::process::Output> {
    use std::process::{Command, Stdio};

    if std::env::var(CHILD_TEST_ENV).as_deref() == Ok(name) {
        return None;
    }
    let mut command = Command::new(std::env::current_exe().expect("test executable"));
    command
        .args(["--exact", &format!("backend::tests::{name}"), "--nocapture"])
        .env(CHILD_TEST_ENV, name)
        .envs(env.iter().copied())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().expect("child test process");
    if close_stderr {
        drop(child.stderr.take());
    }
    Some(child.wait_with_output().expect("child test output"))
}

fn assert_child_test_passed(output: &std::process::Output) {
    assert!(
        output.status.success(),
        "child test failed ({}): {}{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        String::from_utf8_lossy(&output.stdout).contains("1 passed"),
        "child test did not run: {}",
        String::from_utf8_lossy(&output.stdout)
    );
}

#[test]
fn backend_launch_drops_inherited_python_overrides() {
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    const NAME: &str = "backend_launch_drops_inherited_python_overrides";
    let foreign = unique_temp_path("foreign-python-home");
    if let Some(output) = rerun_in_child_test_process(
        NAME,
        &[
            ("PYTHONHOME", foreign.as_os_str()),
            ("PYTHONPATH", foreign.as_os_str()),
        ],
        false,
    ) {
        assert_child_test_passed(&output);
        return;
    }

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let running = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".into(),
                concat!(
                    "import os, sys, time\n",
                    "leaked = [k for k in ('PYTHONHOME', 'PYTHONPATH') if k in os.environ]\n",
                    "if leaked: sys.exit('inherited ' + ', '.join(leaked))\n",
                    "print('{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":9000,\"token\":\"t\"}', flush=True)\n",
                    "time.sleep(30)\n",
                )
                .into(),
            ],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("backend launch");
    let deadline = Instant::now() + Duration::from_secs(15);
    let status = loop {
        let status = manager.lock().unwrap().status();
        if status.lifecycle != BackendLifecycle::Starting || Instant::now() >= deadline {
            break status;
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    let _ = running.stop(&manager);
    assert_eq!(status.lifecycle, BackendLifecycle::Ready, "{status:?}");
}

#[test]
fn backend_stderr_is_drained_when_the_shell_stderr_is_closed() {
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, Instant};

    const NAME: &str = "backend_stderr_is_drained_when_the_shell_stderr_is_closed";
    if let Some(output) = rerun_in_child_test_process(NAME, &[], true) {
        assert_child_test_passed(&output);
        return;
    }

    // Far more stderr than a pipe buffer holds: if the reader stopped at the
    // first mirrored line, the child would block forever on a full pipe.
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let mut running = super::launch_backend(
        Arc::clone(&manager),
        super::ProcessLaunchConfig {
            program: super::python_program(),
            args: vec![
                "-c".into(),
                concat!(
                    "import sys\n",
                    "for _ in range(4000): sys.stderr.write('x' * 100 + '\\n')\n",
                    "sys.stderr.write('stderr fully drained\\n')\n",
                    "sys.exit(3)\n",
                )
                .into(),
            ],
            cwd: None,
            env: Vec::new(),
        },
    )
    .expect("backend launch");
    let deadline = Instant::now() + Duration::from_secs(20);
    let status = loop {
        running.refresh_exit_status(&manager).unwrap();
        let status = manager.lock().unwrap().status();
        if status.lifecycle != BackendLifecycle::Starting || Instant::now() >= deadline {
            break status;
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    let _ = running.stop(&manager);
    assert_eq!(status.lifecycle, BackendLifecycle::Failed, "{status:?}");
    assert!(status
        .error
        .as_deref()
        .unwrap_or_default()
        .contains("stderr fully drained"));
}

#[test]
fn exit_detail_is_the_named_startup_cause_alone() {
    let stderr = std::sync::Mutex::new(vec![
        "Traceback (most recent call last):".to_string(),
        "  File \"launcher.py\", line 1, in <module>".to_string(),
        "failed to start desktop backend: This library was created by a newer version.".to_string(),
        String::new(),
    ]);

    assert_eq!(
        backend_exit_detail("desktop backend exited before ready payload", &stderr),
        "failed to start desktop backend: This library was created by a newer version."
    );
}

#[test]
fn stdout_readiness_removes_the_ready_file_holding_the_token() {
    use std::sync::{Arc, Mutex};
    use std::thread;
    use std::time::{Duration, Instant};

    let temp_root = unique_temp_path("stdout-ready-file");
    std::fs::create_dir_all(&temp_root).unwrap();
    let ready_file = temp_root.join("backend-ready.json");
    let payload = r#"{"event":"ready","host":"127.0.0.1","port":9000,"token":"token-1"}"#;
    // Like the launcher: write the file, then print the same payload, then stay up.
    let script = format!(
        "import sys,time\nopen(sys.argv[1],'w').write({payload:?})\nprint({payload:?}, flush=True)\ntime.sleep(30)"
    );
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let launch = super::ProcessLaunchConfig {
        program: super::python_program(),
        args: vec![
            "-c".to_string(),
            script,
            ready_file.to_string_lossy().into_owned(),
        ],
        cwd: None,
        env: vec![(
            "AUDIOBOOK_DESKTOP_READY_FILE".to_string(),
            ready_file.to_string_lossy().into_owned(),
        )],
    };
    let running = super::launch_backend(Arc::clone(&manager), launch).unwrap();

    let deadline = Instant::now() + Duration::from_secs(5);
    let lifecycle = loop {
        let lifecycle = manager.lock().unwrap().status().lifecycle;
        if lifecycle != BackendLifecycle::Starting || Instant::now() >= deadline {
            break lifecycle;
        }
        thread::sleep(Duration::from_millis(20));
    };
    let file_left = ready_file.exists();
    let _ = running.stop(&manager);
    let _ = std::fs::remove_dir_all(&temp_root);

    assert_eq!(lifecycle, BackendLifecycle::Ready);
    assert!(
        !file_left,
        "the token file must not outlive stdout adoption"
    );
}

#[test]
fn stale_generation_readiness_leaves_the_ready_file_alone() {
    use std::sync::Mutex;

    let temp_root = unique_temp_path("stale-ready-file");
    std::fs::create_dir_all(&temp_root).unwrap();
    let ready_file = temp_root.join("backend-ready.json");
    std::fs::write(&ready_file, "newer generation").unwrap();
    let payload =
        parse_ready_line(r#"{"event":"ready","host":"127.0.0.1","port":9000,"token":"old"}"#)
            .unwrap();
    let manager = Mutex::new(BackendManager::default());
    let old = manager.lock().unwrap().mark_starting();
    manager.lock().unwrap().mark_starting();
    #[cfg(windows)]
    let endpoint = Mutex::new(None);

    super::adopt_backend_ready(
        &manager,
        old,
        payload,
        Some(ready_file.as_path()),
        #[cfg(windows)]
        &endpoint,
    )
    .unwrap();
    let kept = ready_file.exists();
    let _ = std::fs::remove_dir_all(&temp_root);
    assert!(
        kept,
        "a stale generation must not delete a newer ready file"
    );
}

fn launch_sleeping_backend(
    manager: &std::sync::Arc<std::sync::Mutex<BackendManager>>,
) -> super::RunningBackend {
    let launch = super::ProcessLaunchConfig {
        program: super::python_program(),
        args: vec!["-c".to_string(), "import time; time.sleep(30)".to_string()],
        cwd: None,
        env: Vec::new(),
    };
    super::launch_backend(std::sync::Arc::clone(manager), launch).unwrap()
}

#[test]
fn stop_hands_back_a_backend_that_did_not_exit_and_fails_its_generation() {
    use std::sync::{Arc, Mutex};

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let running = launch_sleeping_backend(&manager);

    // Stands in for a child still not gone when the bounded reap gives up.
    let survivor = running
        .stop_with_reap(&manager, |_| false)
        .expect("a backend that did not exit must be handed back");
    let status = manager.lock().unwrap().status();
    assert_eq!(status.lifecycle, BackendLifecycle::Failed);
    assert_eq!(status.error.as_deref(), Some(super::BACKEND_DID_NOT_EXIT));
    assert!(
        survivor.emergency_teardown_armed,
        "exit teardown must still reach the survivor"
    );

    // A later stop retries the teardown and completes it.
    assert!(survivor.stop(&manager).is_none());
    assert_eq!(
        manager.lock().unwrap().status().lifecycle,
        BackendLifecycle::Stopped
    );
}

#[test]
fn stopping_a_backend_that_did_not_exit_keeps_it_in_its_slot() {
    use std::sync::{Arc, Mutex};

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let slot = Mutex::new(Some(launch_sleeping_backend(&manager)));

    let result = super::stop_running_backend_with(&slot, &manager, |backend, manager| {
        backend.stop_with_reap(manager, |_| false)
    });
    assert_eq!(result, Err(super::BACKEND_DID_NOT_EXIT.to_string()));
    assert!(
        slot.lock().unwrap().is_some(),
        "the survivor must stay owned so no replacement starts beside it"
    );

    assert_eq!(super::stop_running_backend(&slot, &manager), Ok(()));
    assert!(slot.lock().unwrap().is_none());
    assert_eq!(
        manager.lock().unwrap().status().lifecycle,
        BackendLifecycle::Stopped
    );
}

#[cfg(unix)]
#[test]
fn startup_timeout_kill_reaps_an_exiting_child_within_its_bound() {
    use std::sync::{Arc, Mutex};
    use std::time::Instant;

    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let mut running = launch_sleeping_backend(&manager);

    let started = Instant::now();
    running.kill_after_startup_timeout();
    let elapsed = started.elapsed();

    assert!(
        running.child_reaped,
        "a child seen to exit after the force-kill must be recorded as reaped"
    );
    assert!(
        elapsed < super::BACKEND_EMERGENCY_TEARDOWN_WAIT + std::time::Duration::from_secs(1),
        "force-kill took {elapsed:?}"
    );
    let _ = running.stop(&manager);
}
