use crate::paths::DesktopRuntimePaths;
use crate::setup::resolve_setup_python;
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
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
    // Monotonic id for the currently supervised process generation. Reader
    // threads capture the id at spawn and must not mutate a newer generation
    // after a restart (stop only joins readers for ~500ms).
    generation: u64,
    // When the current generation entered `Starting`. Reset on every
    // `mark_starting`, so the startup deadline always tracks the newest
    // generation and can never time out a generation that has been superseded.
    started_at: Option<Instant>,
}

impl BackendManager {
    pub fn mark_starting(&mut self) -> u64 {
        self.generation = self.generation.wrapping_add(1);
        self.lifecycle = BackendLifecycle::Starting;
        self.endpoint = None;
        self.error = None;
        self.started_at = Some(Instant::now());
        self.generation
    }

    pub fn mark_ready(&mut self, payload: BackendReadyPayload) {
        self.mark_ready_for_generation(self.generation, payload);
    }

    pub fn mark_ready_for_generation(&mut self, generation: u64, payload: BackendReadyPayload) {
        if generation != self.generation {
            return;
        }
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
        self.mark_stopped_for_generation(self.generation);
    }

    pub fn mark_stopped_for_generation(&mut self, generation: u64) {
        if generation != self.generation {
            return;
        }
        self.lifecycle = BackendLifecycle::Stopped;
        self.endpoint = None;
        self.error = None;
    }

    pub fn mark_failed(&mut self, error: impl Into<String>) {
        self.mark_failed_for_generation(self.generation, error);
    }

    pub fn mark_failed_for_generation(&mut self, generation: u64, error: impl Into<String>) {
        if generation != self.generation {
            return;
        }
        self.lifecycle = BackendLifecycle::Failed;
        self.endpoint = None;
        self.error = Some(error.into());
    }

    // Fail a startup that has been stuck in `Starting` for at least `deadline`.
    // Returns whether the transition happened. This only ever acts on the
    // manager's current state, and `started_at` is reset by every
    // `mark_starting`, so it is generation-safe: a superseded generation's
    // elapsed time is discarded on restart and can never fail a newer one.
    pub fn fail_if_startup_timed_out(&mut self, deadline: Duration) -> bool {
        if self.lifecycle != BackendLifecycle::Starting {
            return false;
        }
        let Some(started_at) = self.started_at else {
            return false;
        };
        if started_at.elapsed() < deadline {
            return false;
        }
        self.lifecycle = BackendLifecycle::Failed;
        self.endpoint = None;
        self.error = Some(format!(
            "desktop backend did not become ready within {} seconds",
            deadline.as_secs()
        ));
        true
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
    // Set by the stdout reader when the backend prints its shutdown
    // acknowledgement after finishing cleanup; exit is imminent at that point.
    shutdown_ack: Arc<AtomicBool>,
    generation: u64,
    #[cfg(unix)]
    process_group_id: Option<i32>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct BackendShutdownPayload {
    pub event: String,
}

// The backend documents a 5.0s cleanup budget after SIGTERM
// (AppController.DESKTOP_SHUTDOWN_CLEANUP_BUDGET_SECONDS); leave real margin
// above it so a clean shutdown is not force-killed at the deadline.
const BACKEND_SHUTDOWN_GRACEFUL_TIMEOUT: Duration = Duration::from_millis(6500);
// Extra wait granted when the backend has already acknowledged shutdown
// cleanup but has not finished exiting when the graceful deadline passes.
const BACKEND_SHUTDOWN_ACK_EXTENSION: Duration = Duration::from_millis(1500);
// Upper bound on how long stop() waits before SIGKILL: the graceful window
// plus the acknowledgement extension. Asserted by the shutdown-budget tests.
#[cfg(test)]
const BACKEND_SHUTDOWN_FORCE_KILL_MAX: Duration = Duration::from_millis(8000);
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
    shutdown_ack: Option<&AtomicBool>,
    #[cfg(unix)] process_group_id: Option<i32>,
) -> ShutdownOutcome {
    if let Ok(Some(_)) = child.try_wait() {
        return ShutdownOutcome::AlreadyExited;
    }

    // Deliver a best-effort graceful stop that the backend understands, then
    // honor the shutdown budget on every platform before force-killing. Only
    // wait when a graceful signal was actually delivered: if it could not be
    // sent (e.g. no console on Windows) the backend will not self-exit, so
    // waiting out the budget would only delay the inevitable force-kill.
    let graceful_signal_sent;
    #[cfg(unix)]
    {
        let _ = send_sigterm(child, process_group_id);
        graceful_signal_sent = true;
    }
    #[cfg(windows)]
    {
        // uvicorn installs a SIGBREAK handler on Windows, so a CTRL_BREAK to the
        // backend's own process group (created via CREATE_NEW_PROCESS_GROUP)
        // triggers the same graceful shutdown as SIGTERM on unix.
        graceful_signal_sent = send_ctrl_break_to_child(child).is_ok();
    }
    #[cfg(not(any(unix, windows)))]
    {
        graceful_signal_sent = false;
    }

    if graceful_signal_sent {
        if wait_for_child_exit(child, timeout) {
            return ShutdownOutcome::GracefulTerminated;
        }
        // The backend acknowledged that cleanup finished, so its exit is
        // imminent; grant a short extension instead of force-killing a clean
        // shutdown at the deadline.
        if shutdown_ack.is_some_and(|ack| ack.load(Ordering::SeqCst))
            && wait_for_child_exit(child, BACKEND_SHUTDOWN_ACK_EXTENSION)
        {
            return ShutdownOutcome::GracefulTerminated;
        }
    }

    #[cfg(unix)]
    if let Some(process_group_id) = process_group_id {
        let _ = send_signal_to_process_group(process_group_id, libc::SIGKILL);
    }

    let _ = child.kill();
    ShutdownOutcome::ForcedKill
}

// Windows-only graceful stop. The child is spawned with CREATE_NEW_PROCESS_GROUP
// so its process-group id equals its pid; targeting that group leaves this
// process (a different group) untouched. Best-effort: in a console-less GUI
// build GenerateConsoleCtrlEvent fails, and the caller falls back to TerminateProcess.
#[cfg(windows)]
fn send_ctrl_break_to_child(child: &Child) -> std::io::Result<()> {
    const CTRL_BREAK_EVENT: u32 = 1;
    let result = unsafe { GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, child.id()) };
    if result != 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(windows)]
#[link(name = "kernel32")]
extern "system" {
    fn GenerateConsoleCtrlEvent(ctrl_event: u32, process_group_id: u32) -> i32;
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
                guard.mark_failed_for_generation(
                    self.generation,
                    format!("desktop backend exited before ready payload ({exit_status})"),
                );
            }
            BackendLifecycle::Ready => {
                guard.mark_failed_for_generation(
                    self.generation,
                    format!("desktop backend exited unexpectedly ({exit_status})"),
                );
            }
            BackendLifecycle::Stopped | BackendLifecycle::Failed => {}
        }
        Ok(())
    }

    pub fn stop(mut self, manager: &Arc<Mutex<BackendManager>>) {
        let generation = self.generation;
        let _ = terminate_backend_process(
            &mut self.child,
            BACKEND_SHUTDOWN_GRACEFUL_TIMEOUT,
            Some(self.shutdown_ack.as_ref()),
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
            guard.mark_stopped_for_generation(generation);
        }
    }
}

// Install `next` as the supervised backend, fully stopping any child it
// displaces. `Child` has no kill-on-drop and `RunningBackend` has no `Drop`, so
// silently overwriting a live entry would leak a running sidecar (and its bound
// port). Swapping under a short lock and stopping the displaced child outside
// the lock guarantees the previous process is always torn down.
pub fn replace_running_backend(
    slot: &Mutex<Option<RunningBackend>>,
    manager: &Arc<Mutex<BackendManager>>,
    next: RunningBackend,
) -> Result<(), String> {
    let displaced = {
        let mut guard = slot
            .lock()
            .map_err(|_| "backend process lock poisoned".to_string())?;
        guard.replace(next)
    };
    if let Some(displaced) = displaced {
        displaced.stop(manager);
    }
    Ok(())
}

pub(crate) fn project_root_dir() -> PathBuf {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .unwrap_or(manifest_dir)
}

#[cfg(test)]
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
    let sidecar_dir_name = BACKEND_SIDECAR_NAME;
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Some(dir) = resource_dir {
        candidates.push(dir.join(&filename));
        candidates.push(dir.join("binaries").join(&filename));
        candidates.push(dir.join(sidecar_dir_name).join(&filename));
        candidates.push(dir.join("binaries").join(sidecar_dir_name).join(&filename));
        candidates.push(dir.join(&filename).join(&filename));
        candidates.push(dir.join("binaries").join(&filename).join(&filename));
    }

    if let Some(current_binary) = current_binary_path {
        if let Some(exe_dir) = current_binary.parent() {
            candidates.push(exe_dir.join(&filename));
            candidates.push(exe_dir.join("binaries").join(&filename));
            candidates.push(exe_dir.join(sidecar_dir_name).join(&filename));
            candidates.push(
                exe_dir
                    .join("binaries")
                    .join(sidecar_dir_name)
                    .join(&filename),
            );
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

pub fn build_dev_launch_config(
    runtime_paths: &DesktopRuntimePaths,
) -> Result<ProcessLaunchConfig, String> {
    build_dev_launch_config_for_root(runtime_paths, &project_root_dir())
}

pub(crate) fn build_dev_launch_config_for_root(
    runtime_paths: &DesktopRuntimePaths,
    project_root: &Path,
) -> Result<ProcessLaunchConfig, String> {
    let python = resolve_setup_python(project_root)?;
    Ok(build_dev_launch_config_with_python(
        runtime_paths,
        project_root,
        &python,
    ))
}

/// Base backend environment plus the debug-only dev-CORS opt-in.
///
/// Debug builds (`tauri dev`) serve the frontend from the Vite dev server, so
/// the backend must allow `http://localhost:5173` in CORS. Release builds
/// (`tauri build`) compile with `debug_assertions` off and never set this flag,
/// so packaged backends only accept the `tauri.localhost` origin.
fn backend_launch_env(runtime_paths: &DesktopRuntimePaths) -> Vec<(String, String)> {
    // `mut` is only used under debug_assertions; release builds never push.
    #[cfg_attr(not(debug_assertions), allow(unused_mut))]
    let mut env: Vec<(String, String)> = runtime_paths.as_env().into_iter().collect();
    #[cfg(debug_assertions)]
    env.push(("ORPHEUS_DESKTOP_DEV".to_string(), "1".to_string()));
    env
}

fn build_dev_launch_config_with_python(
    runtime_paths: &DesktopRuntimePaths,
    project_root: &Path,
    python: &Path,
) -> ProcessLaunchConfig {
    ProcessLaunchConfig {
        program: python.to_string_lossy().into_owned(),
        args: vec![
            "-m".to_string(),
            "apps.launchers.desktop_backend".to_string(),
            "--host".to_string(),
            "127.0.0.1".to_string(),
            "--port".to_string(),
            "0".to_string(),
        ],
        cwd: Some(project_root.to_path_buf()),
        env: backend_launch_env(runtime_paths),
    }
}

#[cfg(test)]
pub(crate) fn build_dev_launch_config_for_test(
    runtime_paths: &DesktopRuntimePaths,
    project_root: &Path,
    python: &Path,
) -> ProcessLaunchConfig {
    build_dev_launch_config_with_python(runtime_paths, project_root, python)
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
        env: backend_launch_env(runtime_paths),
    }
}

fn launch_backend(
    manager: Arc<Mutex<BackendManager>>,
    launch: ProcessLaunchConfig,
) -> Result<RunningBackend, String> {
    let generation = {
        let mut guard = manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        guard.mark_starting()
    };

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

    // Put the backend in its own process group so a graceful CTRL_BREAK can
    // target it without signalling this process. 0x0200 = CREATE_NEW_PROCESS_GROUP.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0000_0200);
    }

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
    let shutdown_ack = Arc::new(AtomicBool::new(false));
    let stdout_shutdown_ack = Arc::clone(&shutdown_ack);
    let stdout_thread = thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            if let Ok(payload) = parse_ready_line(&line) {
                if let Ok(mut guard) = stdout_manager.lock() {
                    guard.mark_ready_for_generation(generation, payload);
                }
            } else if parse_shutdown_line(&line).is_ok() {
                stdout_shutdown_ack.store(true, Ordering::SeqCst);
            }
        }

        // An acknowledged shutdown means the backend finished cleanup and
        // exited on purpose; report it as stopped rather than failed.
        if stdout_shutdown_ack.load(Ordering::SeqCst) {
            if let Ok(mut guard) = stdout_manager.lock() {
                guard.mark_stopped_for_generation(generation);
            }
            return;
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
                    guard.mark_failed_for_generation(generation, error);
                }
                BackendLifecycle::Ready => {
                    guard.mark_failed_for_generation(
                        generation,
                        "desktop backend exited unexpectedly",
                    );
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
        shutdown_ack,
        generation,
        #[cfg(unix)]
        process_group_id,
    })
}

pub fn launch_dev_backend(
    manager: Arc<Mutex<BackendManager>>,
    runtime_paths: &DesktopRuntimePaths,
) -> Result<RunningBackend, String> {
    let launch = build_dev_launch_config(runtime_paths)?;
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

pub fn parse_shutdown_line(line: &str) -> Result<BackendShutdownPayload, String> {
    let payload = serde_json::from_str::<BackendShutdownPayload>(line.trim())
        .map_err(|error| format!("invalid desktop shutdown payload: {error}"))?;

    if payload.event != "shutdown" {
        return Err(format!(
            "unexpected desktop event '{}'; expected 'shutdown'",
            payload.event
        ));
    }

    Ok(payload)
}

#[cfg(test)]
pub fn backend_shutdown_graceful_timeout() -> Duration {
    BACKEND_SHUTDOWN_GRACEFUL_TIMEOUT
}

#[cfg(test)]
pub fn backend_shutdown_force_kill_max() -> Duration {
    BACKEND_SHUTDOWN_FORCE_KILL_MAX
}

#[cfg(test)]
pub fn backend_shutdown_ack_extension() -> Duration {
    BACKEND_SHUTDOWN_ACK_EXTENSION
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
