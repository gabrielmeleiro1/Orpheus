use crate::paths::DesktopRuntimePaths;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendLifecycle {
    Stopped,
    Starting,
    Ready,
    Failed,
}

impl Default for BackendLifecycle {
    fn default() -> Self {
        Self::Stopped
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BackendEndpoint {
    pub host: String,
    pub port: u16,
    pub token: String,
    pub base_url: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BackendStatusEndpoint {
    pub host: String,
    pub port: u16,
    pub base_url: String,
}

impl From<&BackendEndpoint> for BackendStatusEndpoint {
    fn from(endpoint: &BackendEndpoint) -> Self {
        Self {
            host: endpoint.host.clone(),
            port: endpoint.port,
            base_url: endpoint.base_url.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct BackendReadyPayload {
    pub event: String,
    pub host: String,
    pub port: u16,
    pub token: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct BackendStatus {
    pub lifecycle: BackendLifecycle,
    pub endpoint: Option<BackendStatusEndpoint>,
    pub error: Option<String>,
}

#[derive(Debug, Default)]
pub struct BackendManager {
    lifecycle: BackendLifecycle,
    endpoint: Option<BackendEndpoint>,
    error: Option<String>,
}

impl BackendManager {
    pub fn mark_starting(&mut self) {
        self.lifecycle = BackendLifecycle::Starting;
        self.endpoint = None;
        self.error = None;
    }

    pub fn mark_ready(&mut self, payload: BackendReadyPayload) {
        if self.lifecycle != BackendLifecycle::Starting {
            return;
        }
        self.lifecycle = BackendLifecycle::Ready;
        self.error = None;
        self.endpoint = Some(BackendEndpoint {
            base_url: format!("http://{}:{}", payload.host, payload.port),
            host: payload.host,
            port: payload.port,
            token: payload.token,
        });
    }

    pub fn mark_stopped(&mut self) {
        self.lifecycle = BackendLifecycle::Stopped;
        self.endpoint = None;
        self.error = None;
    }

    pub fn mark_failed(&mut self, error: impl Into<String>) {
        self.lifecycle = BackendLifecycle::Failed;
        self.endpoint = None;
        self.error = Some(error.into());
    }

    pub fn status(&self) -> BackendStatus {
        BackendStatus {
            lifecycle: self.lifecycle,
            endpoint: self.endpoint.as_ref().map(BackendStatusEndpoint::from),
            error: self.error.clone(),
        }
    }

    pub fn endpoint(&self) -> Option<BackendEndpoint> {
        self.endpoint.clone()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessLaunchConfig {
    pub program: String,
    pub args: Vec<String>,
    pub cwd: Option<PathBuf>,
    pub env: Vec<(String, String)>,
}

#[derive(Debug)]
pub struct RunningBackend {
    child: Child,
    stdout_thread: JoinHandle<()>,
    stderr_thread: JoinHandle<()>,
    #[cfg(unix)]
    process_group_id: Option<i32>,
}

const BACKEND_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(1);
const BACKEND_SHUTDOWN_POLL_INTERVAL: Duration = Duration::from_millis(50);
const BACKEND_READER_JOIN_TIMEOUT: Duration = Duration::from_millis(500);
const BACKEND_READER_JOIN_POLL_INTERVAL: Duration = Duration::from_millis(25);
const BACKEND_SIDECAR_NAME: &str = "orpheus-desktop-backend";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ShutdownOutcome {
    AlreadyExited,
    GracefulTerminated,
    ForcedKill,
}

fn terminate_backend_process(
    child: &mut Child,
    timeout: Duration,
    #[cfg(unix)] process_group_id: Option<i32>,
) -> ShutdownOutcome {
    match child.try_wait() {
        Ok(Some(_)) => return ShutdownOutcome::AlreadyExited,
        Ok(None) => {}
        Err(_) => {}
    }

    #[cfg(unix)]
    {
        let _ = send_sigterm(child, process_group_id);
        if wait_for_child_exit(child, timeout) {
            return ShutdownOutcome::GracefulTerminated;
        }
    }

    #[cfg(not(unix))]
    {
        let _ = timeout;
    }

    #[cfg(unix)]
    if let Some(process_group_id) = process_group_id {
        let _ = send_signal_to_process_group(process_group_id, libc::SIGKILL);
    }

    let _ = child.kill();
    ShutdownOutcome::ForcedKill
}

#[cfg(unix)]
fn send_sigterm(child: &Child, process_group_id: Option<i32>) -> std::io::Result<()> {
    if let Some(process_group_id) = process_group_id {
        return match send_signal_to_process_group(process_group_id, libc::SIGTERM) {
            Ok(()) => Ok(()),
            Err(group_error) => send_signal_to_child(child, libc::SIGTERM).or(Err(group_error)),
        };
    }

    send_signal_to_child(child, libc::SIGTERM)
}

#[cfg(unix)]
fn send_signal_to_child(child: &Child, signal: i32) -> std::io::Result<()> {
    let pid = i32::try_from(child.id())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "invalid child pid"))?;
    let result = unsafe { libc::kill(pid, signal) };
    if result == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(unix)]
fn send_signal_to_process_group(process_group_id: i32, signal: i32) -> std::io::Result<()> {
    if process_group_id <= 0 {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "invalid process group id",
        ));
    }

    let result = unsafe { libc::kill(-process_group_id, signal) };
    if result == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(unix)]
fn wait_for_child_exit(child: &mut Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) => {}
            Err(_) => return false,
        }

        let now = Instant::now();
        if now >= deadline {
            return false;
        }

        let remaining = deadline.saturating_duration_since(now);
        thread::sleep(remaining.min(BACKEND_SHUTDOWN_POLL_INTERVAL));
    }
}

fn join_reader_thread(handle: JoinHandle<()>, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if handle.is_finished() {
            let _ = handle.join();
            return true;
        }

        let now = Instant::now();
        if now >= deadline {
            return false;
        }

        let remaining = deadline.saturating_duration_since(now);
        thread::sleep(remaining.min(BACKEND_READER_JOIN_POLL_INTERVAL));
    }
}

impl RunningBackend {
    pub fn refresh_exit_status(
        &mut self,
        manager: &Arc<Mutex<BackendManager>>,
    ) -> Result<(), String> {
        let exit_status = self
            .child
            .try_wait()
            .map_err(|error| format!("failed to inspect desktop backend process: {error}"))?;
        let Some(exit_status) = exit_status else {
            return Ok(());
        };

        let mut guard = manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        match guard.status().lifecycle {
            BackendLifecycle::Starting => {
                guard.mark_failed(format!(
                    "desktop backend exited before ready payload ({exit_status})"
                ));
            }
            BackendLifecycle::Ready => {
                guard.mark_failed(format!(
                    "desktop backend exited unexpectedly ({exit_status})"
                ));
            }
            BackendLifecycle::Stopped | BackendLifecycle::Failed => {}
        }
        Ok(())
    }

    pub fn stop(mut self, manager: &Arc<Mutex<BackendManager>>) {
        let _ = terminate_backend_process(
            &mut self.child,
            BACKEND_SHUTDOWN_TIMEOUT,
            #[cfg(unix)]
            self.process_group_id,
        );

        let _ = self.child.wait();
        let stdout_joined = join_reader_thread(self.stdout_thread, BACKEND_READER_JOIN_TIMEOUT);
        let stderr_joined = join_reader_thread(self.stderr_thread, BACKEND_READER_JOIN_TIMEOUT);

        #[cfg(unix)]
        if (!stdout_joined || !stderr_joined) && self.process_group_id.is_some() {
            let _ = send_signal_to_process_group(self.process_group_id.unwrap(), libc::SIGKILL);
        }

        if let Ok(mut guard) = manager.lock() {
            guard.mark_stopped();
        }
    }
}

fn project_root_dir() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .unwrap_or(manifest_dir)
}

fn python_program() -> String {
    std::env::var("AUDIOBOOK_DESKTOP_PYTHON")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "python".to_string())
}

#[cfg(test)]
pub fn sidecar_binary_stem() -> &'static str {
    BACKEND_SIDECAR_NAME
}

pub fn sidecar_runtime_filename() -> String {
    if cfg!(target_os = "windows") {
        format!("{BACKEND_SIDECAR_NAME}.exe")
    } else {
        BACKEND_SIDECAR_NAME.to_string()
    }
}

#[cfg(test)]
pub fn sidecar_source_filename(target_triple: &str) -> String {
    let extension = if target_triple.contains("windows") {
        ".exe"
    } else {
        ""
    };
    format!("{BACKEND_SIDECAR_NAME}-{target_triple}{extension}")
}

pub fn resolve_sidecar_path(
    resource_dir: Option<&Path>,
    current_binary_path: Option<&Path>,
) -> Result<PathBuf, String> {
    let filename = sidecar_runtime_filename();
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Some(dir) = resource_dir {
        candidates.push(dir.join(&filename));
        candidates.push(dir.join("binaries").join(&filename));
        candidates.push(dir.join(&filename).join(&filename));
        candidates.push(dir.join("binaries").join(&filename).join(&filename));
    }

    if let Some(current_binary) = current_binary_path {
        if let Some(exe_dir) = current_binary.parent() {
            candidates.push(exe_dir.join(&filename));
            candidates.push(exe_dir.join("binaries").join(&filename));
            candidates.push(exe_dir.join(&filename).join(&filename));
            candidates.push(exe_dir.join("binaries").join(&filename).join(&filename));
            #[cfg(target_os = "macos")]
            {
                candidates.push(exe_dir.join("../Resources").join(&filename));
                candidates.push(
                    exe_dir
                        .join("../Resources")
                        .join("binaries")
                        .join(&filename),
                );
                candidates.push(exe_dir.join("../Resources").join(&filename).join(&filename));
                candidates.push(
                    exe_dir
                        .join("../Resources")
                        .join("binaries")
                        .join(&filename)
                        .join(&filename),
                );
            }
        }
    }

    let mut deduped: Vec<PathBuf> = Vec::new();
    for candidate in candidates {
        if deduped.iter().any(|seen| seen == &candidate) {
            continue;
        }
        if candidate.is_file() {
            return Ok(candidate);
        }
        deduped.push(candidate);
    }

    Err(format!(
        "desktop backend sidecar not found: expected '{}' in one of [{}]",
        filename,
        deduped
            .iter()
            .map(|path| path.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    ))
}

pub fn build_dev_launch_config(runtime_paths: &DesktopRuntimePaths) -> ProcessLaunchConfig {
    ProcessLaunchConfig {
        program: python_program(),
        args: vec![
            "-m".to_string(),
            "apps.launchers.desktop_backend".to_string(),
            "--host".to_string(),
            "127.0.0.1".to_string(),
            "--port".to_string(),
            "0".to_string(),
        ],
        cwd: Some(project_root_dir()),
        env: runtime_paths.as_env().into_iter().collect(),
    }
}

pub fn build_sidecar_launch_config(
    runtime_paths: &DesktopRuntimePaths,
    sidecar_path: impl AsRef<Path>,
) -> ProcessLaunchConfig {
    ProcessLaunchConfig {
        program: sidecar_path.as_ref().to_string_lossy().into_owned(),
        args: vec![
            "--host".to_string(),
            "127.0.0.1".to_string(),
            "--port".to_string(),
            "0".to_string(),
        ],
        cwd: None,
        env: runtime_paths.as_env().into_iter().collect(),
    }
}

fn launch_backend(
    manager: Arc<Mutex<BackendManager>>,
    launch: ProcessLaunchConfig,
) -> Result<RunningBackend, String> {
    if let Ok(mut guard) = manager.lock() {
        guard.mark_starting();
    }

    let mut command = Command::new(&launch.program);
    command
        .args(&launch.args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    if let Some(cwd) = &launch.cwd {
        command.current_dir(cwd);
    }

    for (key, value) in &launch.env {
        command.env(key, value);
    }

    #[cfg(unix)]
    command.process_group(0);

    let mut child = command.spawn().map_err(|error| {
        let message = format!("failed to launch desktop backend process: {error}");
        if let Ok(mut guard) = manager.lock() {
            guard.mark_failed(message.clone());
        }
        message
    })?;
    #[cfg(unix)]
    let process_group_id = i32::try_from(child.id()).ok();

    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            if let Ok(mut guard) = manager.lock() {
                guard.mark_failed("failed to capture desktop backend stdout");
            }
            return Err("failed to capture desktop backend stdout".to_string());
        }
    };
    let stderr = match child.stderr.take() {
        Some(stderr) => stderr,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            if let Ok(mut guard) = manager.lock() {
                guard.mark_failed("failed to capture desktop backend stderr");
            }
            return Err("failed to capture desktop backend stderr".to_string());
        }
    };

    let stdout_manager = Arc::clone(&manager);
    let stderr_lines = Arc::new(Mutex::new(Vec::<String>::new()));
    let stdout_stderr_lines = Arc::clone(&stderr_lines);
    let stdout_thread = thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            if let Ok(payload) = parse_ready_line(&line) {
                if let Ok(mut guard) = stdout_manager.lock() {
                    guard.mark_ready(payload);
                }
            }
        }

        if let Ok(mut guard) = stdout_manager.lock() {
            match guard.status().lifecycle {
                BackendLifecycle::Starting => {
                    let stderr = stdout_stderr_lines
                        .lock()
                        .ok()
                        .map(|lines| lines.join("\n"))
                        .unwrap_or_default();
                    let error = if stderr.trim().is_empty() {
                        "desktop backend exited before ready payload".to_string()
                    } else {
                        format!(
                            "desktop backend exited before ready payload: {}",
                            stderr.trim()
                        )
                    };
                    guard.mark_failed(error);
                }
                BackendLifecycle::Ready => {
                    guard.mark_failed("desktop backend exited unexpectedly");
                }
                BackendLifecycle::Stopped | BackendLifecycle::Failed => {}
            }
        }
    });

    let stderr_stderr_lines = Arc::clone(&stderr_lines);
    let stderr_thread = thread::spawn(move || {
        let reader = BufReader::new(stderr);
        for line in reader.lines().map_while(Result::ok) {
            if let Ok(mut lines) = stderr_stderr_lines.lock() {
                lines.push(line.clone());
                if lines.len() > 20 {
                    lines.remove(0);
                }
            }
            eprintln!("[desktop-backend] {line}");
        }
    });

    Ok(RunningBackend {
        child,
        stdout_thread,
        stderr_thread,
        #[cfg(unix)]
        process_group_id,
    })
}

pub fn launch_dev_backend(
    manager: Arc<Mutex<BackendManager>>,
    runtime_paths: &DesktopRuntimePaths,
) -> Result<RunningBackend, String> {
    let launch = build_dev_launch_config(runtime_paths);
    launch_backend(manager, launch)
}

pub fn launch_sidecar_backend(
    manager: Arc<Mutex<BackendManager>>,
    runtime_paths: &DesktopRuntimePaths,
    sidecar_path: impl AsRef<Path>,
) -> Result<RunningBackend, String> {
    let launch = build_sidecar_launch_config(runtime_paths, sidecar_path);
    launch_backend(manager, launch)
}

pub fn parse_ready_line(line: &str) -> Result<BackendReadyPayload, String> {
    let payload = serde_json::from_str::<BackendReadyPayload>(line.trim())
        .map_err(|error| format!("invalid desktop ready payload: {error}"))?;

    if payload.event != "ready" {
        return Err(format!(
            "unexpected desktop event '{}'; expected 'ready'",
            payload.event
        ));
    }
    if payload.host.trim().is_empty() {
        return Err("desktop ready payload missing host".to_string());
    }
    if payload.port == 0 {
        return Err("desktop ready payload missing port".to_string());
    }
    if payload.token.trim().is_empty() {
        return Err("desktop ready payload missing token".to_string());
    }

    Ok(payload)
}

pub fn read_ready_payload_from_file(path: impl AsRef<Path>) -> Result<BackendReadyPayload, String> {
    let path = path.as_ref();
    let payload = fs::read_to_string(path).map_err(|error| {
        format!(
            "failed to read desktop ready file '{}': {error}",
            path.display()
        )
    })?;
    let parsed = parse_ready_line(&payload)?;
    fs::remove_file(path).map_err(|error| {
        format!(
            "failed to remove desktop ready file '{}': {error}",
            path.display()
        )
    })?;
    Ok(parsed)
}

#[cfg(test)]
#[path = "backend_tests.rs"]
mod tests;
