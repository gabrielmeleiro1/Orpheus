use super::{
    build_dev_launch_config, parse_ready_line, read_ready_payload_from_file, resolve_sidecar_path,
    sidecar_binary_stem, sidecar_source_filename, BackendLifecycle, BackendManager,
};
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
fn build_dev_launch_config_matches_desktop_runtime_contract() {
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir("/tmp/audiobook-app");
    let config = build_dev_launch_config(&runtime_paths);

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
    let cwd = config.cwd.expect("dev launcher should have repository cwd");
    assert!(
        cwd.ends_with("Orpheus"),
        "expected cwd to point at repository root, got {}",
        cwd.display()
    );

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
        Some(PathBuf::from("/tmp/audiobook-app").join("data").join("audiobooks.db"))
    );
    assert_eq!(
        env.get("AUDIOBOOK_DESKTOP_READY_FILE").map(PathBuf::from),
        Some(PathBuf::from("/tmp/audiobook-app").join("backend-ready.json"))
    );
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
        stdout_thread,
        stderr_thread,
        process_group_id: None,
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
    let outcome =
        super::terminate_backend_process(&mut child, Duration::from_millis(250), Some(i32::MAX));

    assert_eq!(outcome, super::ShutdownOutcome::GracefulTerminated);
    let marker_contents =
        fs::read_to_string(&marker_path).expect("expected direct SIGTERM fallback marker");
    assert_eq!(marker_contents, "sigterm");
    let _ = child.wait();
    let _ = fs::remove_file(&marker_path);
}

#[cfg(unix)]
#[test]
fn running_backend_stop_forces_unresponsive_backend_without_long_quit_delay() {
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
        stdout_thread,
        stderr_thread,
        process_group_id: None,
    };
    let manager = Arc::new(Mutex::new(BackendManager::default()));
    let started_at = Instant::now();

    running.stop(&manager);

    assert!(
        started_at.elapsed() < Duration::from_secs(2),
        "backend stop should not hold desktop quit for the old five-second fallback budget"
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

    let outcome = super::terminate_backend_process(&mut child, Duration::from_millis(250), None);
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
