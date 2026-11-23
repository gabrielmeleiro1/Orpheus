use crate::paths::DesktopRuntimePaths;
use crate::setup::{remove_inherited_python_overrides, resolve_setup_python};
#[cfg(windows)]
use crate::setup::{resume_suspended_child_threads, WindowsJob};
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum BackendLifecycle {
    #[default]
    Stopped,
    Starting,
    Ready,
    Failed,
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
    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn mark_starting(&mut self) -> u64 {
        self.generation = self.generation.wrapping_add(1);
        self.lifecycle = BackendLifecycle::Starting;
        self.endpoint = None;
        self.error = None;
        self.started_at = Some(Instant::now());
        self.generation
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

const BACKEND_LOG_MAX_BYTES: u64 = 5 * 1024 * 1024;

struct BackendLog {
    path: PathBuf,
    writer: Option<BufWriter<fs::File>>,
    size: u64,
}

impl BackendLog {
    fn open(data_dir: &Path) -> io::Result<Self> {
        let log_dir = data_dir.join("logs");
        fs::create_dir_all(&log_dir)?;
        let path = log_dir.join("backend.log");
        let file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)?;
        let size = file.metadata()?.len();
        Ok(Self {
            path,
            writer: Some(BufWriter::new(file)),
            size,
        })
    }

    fn write_line(&mut self, line: &str) -> io::Result<()> {
        // Keep even a single oversized diagnostic within the rotation budget.
        let mut start = line
            .len()
            .saturating_sub(BACKEND_LOG_MAX_BYTES as usize - 1);
        while !line.is_char_boundary(start) {
            start += 1;
        }
        let line = &line[start..];
        let bytes = line.len() as u64 + 1;
        if self.size + bytes > BACKEND_LOG_MAX_BYTES {
            if let Some(mut writer) = self.writer.take() {
                writer.flush()?;
            }
            let backup = self.path.with_file_name("backend.log.1");
            match fs::remove_file(&backup) {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
            fs::rename(&self.path, backup)?;
            self.writer = Some(BufWriter::new(
                fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&self.path)?,
            ));
            self.size = 0;
        }
        if let Some(writer) = self.writer.as_mut() {
            writeln!(writer, "{line}")?;
            writer.flush()?;
            self.size += bytes;
        }
        Ok(())
    }
}

/// Best-effort diagnostic on the shell's own stderr. `eprintln!` panics when
/// stderr is a closed pipe; in a reader thread that would stop draining the
/// backend's pipe until the child blocks on a full buffer (and abort a release
/// build, which uses `panic = "abort"`), so write errors are ignored.
pub(crate) fn print_diagnostic(message: std::fmt::Arguments<'_>) {
    let _ = writeln!(io::stderr().lock(), "{message}");
}

fn log_backend_line(log: &Mutex<Option<BackendLog>>, line: &str) {
    if let Ok(mut guard) = log.lock() {
        if let Some(writer) = guard.as_mut() {
            if writer.write_line(line).is_err() {
                *guard = None;
            }
        }
    }
}

// The launcher ends its stderr with this prefix and the cause when startup
// fails for a reason it can name (for example a library from a newer version).
const BACKEND_STARTUP_FAILURE_PREFIX: &str = "failed to start desktop backend: ";

fn backend_exit_detail(message: impl AsRef<str>, stderr: &Mutex<Vec<String>>) -> String {
    let lines = stderr.lock().map(|lines| lines.clone()).unwrap_or_default();
    // A named cause is the whole message: the traceback above it and the exit
    // code add nothing a user can act on.
    if let Some(cause) = lines
        .iter()
        .rev()
        .map(|line| line.trim())
        .find(|line| !line.is_empty())
        .filter(|line| line.starts_with(BACKEND_STARTUP_FAILURE_PREFIX))
    {
        return cause.to_string();
    }
    let tail = lines.join("\n");
    if tail.trim().is_empty() {
        message.as_ref().to_string()
    } else {
        format!("{}: {}", message.as_ref(), tail.trim())
    }
}

#[derive(Debug)]
pub struct RunningBackend {
    child: Child,
    stdout_thread: Option<JoinHandle<()>>,
    stderr_thread: Option<JoinHandle<()>>,
    stderr_lines: Arc<Mutex<Vec<String>>>,
    // Set by the stdout reader when the backend prints its shutdown
    // acknowledgement after finishing cleanup; exit is imminent at that point.
    shutdown_ack: Arc<AtomicBool>,
    #[cfg(windows)]
    shutdown_endpoint: Arc<Mutex<Option<BackendReadyPayload>>>,
    generation: u64,
    #[cfg(unix)]
    process_group_id: Option<i32>,
    // True after this Child has been reaped via wait/try_wait. Used with the
    // process-group ownership guard: on macOS getpgid(zombie) fails even while
    // unreaped, but an unreaped leader PID cannot be reused and still names the
    // process_group(0) group for remaining members.
    #[cfg(unix)]
    child_reaped: bool,
    // When true, Drop/Exit fallback force-kills the owned process group. Ordinary
    // `stop()` disarms this after teardown so Drop cannot signal a reused pgid.
    emergency_teardown_armed: bool,
    #[cfg(windows)]
    job: Arc<WindowsJob>,
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
const BACKEND_EMERGENCY_TEARDOWN_WAIT: Duration = Duration::from_millis(500);
// How long an ordinary stop waits for the process to be gone after a forced
// kill. A killed process can take a moment to release GPU memory, but one stuck
// in an uninterruptible wait must not hold `restart_lock` (and app exit) forever.
const BACKEND_FORCED_KILL_REAP_WAIT: Duration = Duration::from_secs(5);
const BACKEND_DID_NOT_EXIT: &str =
    "desktop backend did not exit after it was stopped; it may still be using its port and library";
const BACKEND_READER_JOIN_TIMEOUT: Duration = Duration::from_millis(500);
const BACKEND_READER_JOIN_POLL_INTERVAL: Duration = Duration::from_millis(25);
// Number of trailing stderr lines quoted in the error when the backend exits.
const BACKEND_STDERR_TAIL_LINES: usize = 20;
const BACKEND_SIDECAR_NAME: &str = "orpheus-desktop-backend";
// The only address the backend is asked to bind. A ready payload naming any
// other host is not the child the shell launched, so it is never adopted: the
// token would otherwise be handed to that host, and the CSP only admits this one.
const BACKEND_LOOPBACK_HOST: &str = "127.0.0.1";

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
    #[cfg(unix)] child_reaped: &mut bool,
    #[cfg(windows)] job: Option<&WindowsJob>,
    #[cfg(windows)] endpoint: Option<&BackendReadyPayload>,
) -> ShutdownOutcome {
    #[cfg(unix)]
    {
        if *child_reaped {
            return ShutdownOutcome::AlreadyExited;
        }
        // Observe an exited child without reaping it (waitid WNOWAIT). That
        // leaves the leader PID reserved while the owned process group is
        // verified and killed, closing the leader-exit race.
        if child_exited_without_reap(child).unwrap_or(false) {
            kill_owned_group_and_reap(child, process_group_id, child_reaped);
            return ShutdownOutcome::AlreadyExited;
        }
    }
    #[cfg(windows)]
    if let Ok(Some(_)) = child.try_wait() {
        return ShutdownOutcome::AlreadyExited;
    }

    // Deliver a best-effort graceful stop, then honor the cleanup budget before
    // force-killing. A ready Windows child gets the budget even if its HTTP
    // response is lost after the shutdown request reached the server.
    let graceful_signal_sent;
    #[cfg(unix)]
    {
        let _ = send_sigterm(child, process_group_id);
        graceful_signal_sent = true;
    }
    #[cfg(windows)]
    {
        graceful_signal_sent = endpoint.is_some();
        if let Some(endpoint) = endpoint {
            let _ = request_backend_shutdown(endpoint);
        }
    }

    // When the deadline passes but the backend has already acknowledged that
    // cleanup finished, its exit is imminent; grant a short extension instead
    // of force-killing a clean shutdown at the deadline.
    if graceful_signal_sent
        && (wait_for_graceful_exit(child, timeout)
            || (shutdown_ack.is_some_and(|ack| ack.load(Ordering::SeqCst))
                && wait_for_graceful_exit(child, BACKEND_SHUTDOWN_ACK_EXTENSION)))
    {
        // The leader is still unreaped on Unix. Kill the verified group before
        // reaping it so inherited descendants do not survive a clean exit.
        #[cfg(unix)]
        kill_owned_group_and_reap(child, process_group_id, child_reaped);
        return ShutdownOutcome::GracefulTerminated;
    }

    #[cfg(unix)]
    if let Some(process_group_id) = process_group_id {
        let _ = send_signal_to_process_group(process_group_id, libc::SIGKILL);
    }

    #[cfg(windows)]
    if let Some(job) = job {
        job.terminate();
    }

    let _ = child.kill();
    ShutdownOutcome::ForcedKill
}

#[cfg(unix)]
fn kill_owned_group_and_reap(
    child: &mut Child,
    process_group_id: Option<i32>,
    child_reaped: &mut bool,
) {
    if let Some(pgid) = verified_owned_process_group_id(child, process_group_id, *child_reaped) {
        let _ = send_signal_to_process_group(pgid, libc::SIGKILL);
    }
    if let Ok(Some(_)) = child.try_wait() {
        *child_reaped = true;
    }
}

// Unix leaves the exited leader unreaped; Windows reaps it.
#[cfg(unix)]
fn wait_for_graceful_exit(child: &mut Child, timeout: Duration) -> bool {
    wait_for_child_exit_without_reap(child, timeout).unwrap_or(false)
}

#[cfg(windows)]
fn wait_for_graceful_exit(child: &mut Child, timeout: Duration) -> bool {
    wait_for_child_exit(child, timeout)
}

// Hidden Windows children have no console, so use the authenticated local API.
#[cfg(windows)]
fn request_backend_shutdown(endpoint: &BackendReadyPayload) -> std::io::Result<()> {
    use std::io::{Read, Write};
    use std::net::{IpAddr, SocketAddr, TcpStream};

    let invalid = || {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "invalid shutdown endpoint",
        )
    };
    let host: IpAddr = endpoint.host.parse().map_err(|_| invalid())?;
    if !host.is_loopback() || endpoint.token.contains(['\r', '\n']) {
        return Err(invalid());
    }
    let address = SocketAddr::new(host, endpoint.port);
    let timeout = Duration::from_millis(500);
    let mut stream = TcpStream::connect_timeout(&address, timeout)?;
    stream.set_write_timeout(Some(timeout))?;
    stream.set_read_timeout(Some(timeout))?;
    write!(stream, "POST /api/desktop/shutdown HTTP/1.1\r\nHost: {address}\r\nAuthorization: Bearer {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n", endpoint.token)?;
    let mut status = [0_u8; 12];
    stream.read_exact(&mut status)?;
    if &status != b"HTTP/1.1 202" && &status != b"HTTP/1.0 202" {
        return Err(std::io::Error::other("backend rejected shutdown request"));
    }
    Ok(())
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

/// Strongest portable ownership guard for an owned backend process group.
///
/// 1. `getpgid(leader_pid) == stored_pgid` while the leader is live (or on
///    platforms where zombie getpgid still works), or
/// 2. the leader is still unreaped and `stored_pgid == leader_pid` — true for
///    `process_group(0)` spawns. On macOS, getpgid/kill(0) on an unreaped zombie
///    return ESRCH, but the PID remains reserved until reap, so signaling
///    `-pgid` still targets only that owned group (or ESRCH if empty).
///
/// After reap, PID reuse makes pgid-only signaling unsafe — return None.
#[cfg(unix)]
pub(crate) fn verified_owned_process_group_id(
    child: &Child,
    process_group_id: Option<i32>,
    child_reaped: bool,
) -> Option<i32> {
    // Once Child::wait/try_wait has reaped the leader, its PID may be reused;
    // a matching PGID is no longer proof that the group belongs to us.
    if child_reaped {
        return None;
    }
    let process_group_id = process_group_id.filter(|pgid| *pgid > 0)?;
    let pid = i32::try_from(child.id()).ok().filter(|pid| *pid > 0)?;
    let current_pgid = unsafe { libc::getpgid(pid) };
    if current_pgid == process_group_id {
        return Some(process_group_id);
    }
    if !child_reaped && pid == process_group_id {
        return Some(process_group_id);
    }
    None
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
pub(crate) fn child_exited_without_reap(child: &Child) -> std::io::Result<bool> {
    let pid = i32::try_from(child.id())
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidInput, "invalid child pid"))?;
    let mut info = unsafe { std::mem::zeroed::<libc::siginfo_t>() };
    let result = unsafe {
        libc::waitid(
            libc::P_PID,
            pid as libc::id_t,
            &mut info,
            libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
        )
    };
    if result != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(unsafe { info.si_pid() } == pid)
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn wait_for_child_exit_without_reap(child: &Child, timeout: Duration) -> std::io::Result<bool> {
    let deadline = Instant::now() + timeout;
    loop {
        if child_exited_without_reap(child)? {
            return Ok(true);
        }

        let now = Instant::now();
        if now >= deadline {
            return Ok(false);
        }
        let remaining = deadline.saturating_duration_since(now);
        thread::sleep(remaining.min(BACKEND_SHUTDOWN_POLL_INTERVAL));
    }
}

pub(crate) fn wait_for_child_exit(child: &mut Child, timeout: Duration) -> bool {
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

fn adopt_backend_ready(
    manager: &Mutex<BackendManager>,
    generation: u64,
    payload: BackendReadyPayload,
    ready_file: Option<&Path>,
    #[cfg(windows)] shutdown_endpoint: &Mutex<Option<BackendReadyPayload>>,
) -> Result<(), String> {
    let mut manager = manager
        .lock()
        .map_err(|_| "backend manager lock poisoned".to_string())?;
    if manager.generation() != generation
        || manager.status().lifecycle != BackendLifecycle::Starting
    {
        return Ok(());
    }
    #[cfg(windows)]
    {
        let mut endpoint = shutdown_endpoint
            .lock()
            .map_err(|_| "backend shutdown endpoint lock poisoned".to_string())?;
        *endpoint = Some(payload.clone());
    }
    manager.mark_ready_for_generation(generation, payload);
    // The file repeats the token from stdout; it has served its purpose once
    // this generation is adopted. Removing it under the manager lock means a
    // newer generation (which must mark itself starting first) cannot have
    // written it yet.
    if let Some(path) = ready_file {
        let _ = fs::remove_file(path);
    }
    Ok(())
}

impl RunningBackend {
    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn adopt_ready(
        &self,
        manager: &Mutex<BackendManager>,
        payload: BackendReadyPayload,
    ) -> Result<(), String> {
        adopt_backend_ready(
            manager,
            self.generation,
            payload,
            None,
            #[cfg(windows)]
            &self.shutdown_endpoint,
        )
    }

    /// End a child whose startup deadline passed. Its generation is already
    /// failed, so there is nothing to shut down gracefully.
    pub fn kill_after_startup_timeout(&mut self) {
        self.force_kill_owned_process_group();
    }

    pub fn disarm_emergency_teardown(&mut self) {
        self.emergency_teardown_armed = false;
    }

    /// Last-chance, bounded force-kill of the owned backend process group.
    /// Idempotent: safe to call repeatedly and after the child has already exited.
    pub fn emergency_teardown(&mut self) {
        if !self.emergency_teardown_armed {
            return;
        }
        self.force_kill_owned_process_group();
        self.emergency_teardown_armed = false;
    }

    fn force_kill_owned_process_group(&mut self) {
        // Do not return early when the leader has already exited: a same-group
        // grandchild can outlive the leader. Signal the group only under the
        // ownership guard (live getpgid match, or unreaped process_group(0)
        // leader). After reap, skip pgid signaling rather than risk reuse.
        #[cfg(unix)]
        {
            if let Some(process_group_id) = verified_owned_process_group_id(
                &self.child,
                self.process_group_id,
                self.child_reaped,
            ) {
                let _ = send_signal_to_process_group(process_group_id, libc::SIGKILL);
            }
        }

        #[cfg(windows)]
        self.job.terminate();

        let _ = self.child.kill();
        // Bounded: a child stuck in an uninterruptible wait must not hang exit
        // or the caller's lock. An unreaped leader keeps its PID reserved, so
        // later group signals and the exit refresh stay safe.
        #[cfg(unix)]
        {
            self.child_reaped =
                wait_for_child_exit(&mut self.child, BACKEND_EMERGENCY_TEARDOWN_WAIT);
        }
        #[cfg(not(unix))]
        let _ = wait_for_child_exit(&mut self.child, BACKEND_EMERGENCY_TEARDOWN_WAIT);
    }

    pub fn refresh_exit_status(
        &mut self,
        manager: &Arc<Mutex<BackendManager>>,
    ) -> Result<(), String> {
        #[cfg(unix)]
        if self.child_reaped {
            // Polling can continue after the first exit observation (for
            // example while the supervisor prepares a restart). Child::waitid
            // reports ECHILD once the leader has been reaped, so make refresh
            // idempotent instead of turning an already handled exit into an
            // error.
            return Ok(());
        }

        #[cfg(any(target_os = "linux", target_os = "macos"))]
        let exit_observed_without_reap = child_exited_without_reap(&self.child)
            .map_err(|error| format!("failed to inspect desktop backend process: {error}"))?;
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        if !exit_observed_without_reap {
            return Ok(());
        }

        #[cfg(any(target_os = "linux", target_os = "macos"))]
        if let Some(pgid) =
            verified_owned_process_group_id(&self.child, self.process_group_id, self.child_reaped)
        {
            // Keep the leader unreaped until the owned group has been killed.
            let _ = send_signal_to_process_group(pgid, libc::SIGKILL);
        }

        let exit_status = self
            .child
            .try_wait()
            .map_err(|error| format!("failed to inspect desktop backend process: {error}"))?;
        let Some(exit_status) = exit_status else {
            return Ok(());
        };
        // The failed backend remains in the application slot until restart.
        // Close its workers and inherited pipes now, while the job is still owned.
        #[cfg(windows)]
        self.job.terminate();
        #[cfg(unix)]
        {
            self.child_reaped = true;
        }

        // Drain diagnostics before the supervisor races the stdout EOF handler.
        if let Some(handle) = self.stderr_thread.take() {
            join_reader_thread(handle, BACKEND_READER_JOIN_TIMEOUT);
        }
        if let Some(handle) = self.stdout_thread.take() {
            join_reader_thread(handle, BACKEND_READER_JOIN_TIMEOUT);
        }

        let mut guard = manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        match guard.status().lifecycle {
            BackendLifecycle::Starting => {
                guard.mark_failed_for_generation(
                    self.generation,
                    backend_exit_detail(
                        format!("desktop backend exited before ready payload ({exit_status})"),
                        &self.stderr_lines,
                    ),
                );
            }
            BackendLifecycle::Ready => {
                guard.mark_failed_for_generation(
                    self.generation,
                    backend_exit_detail(
                        format!("desktop backend exited unexpectedly ({exit_status})"),
                        &self.stderr_lines,
                    ),
                );
            }
            BackendLifecycle::Stopped | BackendLifecycle::Failed => {}
        }
        Ok(())
    }

    /// Stop the backend. Returns it back when its process could not be seen to
    /// exit within a bound after the forced kill: that generation is then
    /// marked failed, emergency teardown stays armed, and the caller must keep
    /// owning it rather than start a replacement while it may still hold the
    /// port and library. Stopping it again later retries the teardown.
    #[must_use = "a backend returned by stop() has not exited and must stay owned"]
    pub fn stop(self, manager: &Arc<Mutex<BackendManager>>) -> Option<RunningBackend> {
        self.stop_with_reap(manager, |child| {
            wait_for_child_exit(child, BACKEND_FORCED_KILL_REAP_WAIT)
        })
    }

    fn stop_with_reap(
        mut self,
        manager: &Arc<Mutex<BackendManager>>,
        reap: impl FnOnce(&mut Child) -> bool,
    ) -> Option<RunningBackend> {
        // Tear down first while emergency Drop fallback remains armed. Disarm
        // only after ownership is released so a panic/error mid-stop cannot
        // leave same-group grandchildren behind; getpgid ownership checks keep
        // a late Drop from signaling a reused process group.
        let generation = self.generation;
        #[cfg(windows)]
        let endpoint = self
            .shutdown_endpoint
            .lock()
            .ok()
            .and_then(|guard| guard.clone());
        let _ = terminate_backend_process(
            &mut self.child,
            BACKEND_SHUTDOWN_GRACEFUL_TIMEOUT,
            Some(self.shutdown_ack.as_ref()),
            #[cfg(unix)]
            self.process_group_id,
            #[cfg(unix)]
            &mut self.child_reaped,
            #[cfg(windows)]
            Some(self.job.as_ref()),
            #[cfg(windows)]
            endpoint.as_ref(),
        );

        // Bounded. An unreaped leader keeps its PID reserved, so the group
        // ownership checks of a later stop, refresh or Drop stay reuse-safe.
        if !reap(&mut self.child) {
            if let Ok(mut guard) = manager.lock() {
                guard.mark_failed_for_generation(generation, BACKEND_DID_NOT_EXIT);
            }
            return Some(self);
        }
        #[cfg(unix)]
        {
            self.child_reaped = true;
        }
        let _stdout_joined = self
            .stdout_thread
            .take()
            .map(|handle| join_reader_thread(handle, BACKEND_READER_JOIN_TIMEOUT))
            .unwrap_or(true);
        let _stderr_joined = self
            .stderr_thread
            .take()
            .map(|handle| join_reader_thread(handle, BACKEND_READER_JOIN_TIMEOUT))
            .unwrap_or(true);

        self.disarm_emergency_teardown();

        if let Ok(mut guard) = manager.lock() {
            guard.mark_stopped_for_generation(generation);
        }
        None
    }
}

impl Drop for RunningBackend {
    fn drop(&mut self) {
        // Last-chance teardown when ordinary stop()/ExitRequested cleanup did not
        // run (e.g. abrupt RunEvent::Exit). Bounded and idempotent.
        self.emergency_teardown();
    }
}

// Drain a supervised backend slot for last-chance exit teardown. Taking the
// value drops it, which force-kills when still armed. Empty slots are no-ops.
pub fn emergency_teardown_backend_slot(slot: &Mutex<Option<RunningBackend>>) {
    let _taken = match slot.lock() {
        Ok(mut guard) => guard.take(),
        Err(poisoned) => poisoned.into_inner().take(),
    };
}

// Install `next` as the supervised backend, fully stopping any child it
// displaces. `Child` has no kill-on-drop, so silently overwriting a live entry
// would leak a running sidecar (and its bound port). Swapping under a short lock
// and stopping the displaced child outside the lock guarantees the previous
// process is always torn down. `RunningBackend`'s Drop is a last-chance backup
// only; ordinary displacement still uses graceful `stop()`.
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
        if let Some(survivor) = displaced.stop(manager) {
            // The replacement is already installed, so nothing can own the
            // survivor any longer; dropping it force-kills it once more.
            print_diagnostic(format_args!("{BACKEND_DID_NOT_EXIT}"));
            drop(survivor);
        }
    }
    Ok(())
}

// Stop the supervised backend and clear its slot. The stop runs outside the
// slot lock so status polling is not blocked for the graceful shutdown. A
// backend that did not exit goes back into the slot, still owned (exit
// teardown, exit refresh and a later stop all reach it), and the error keeps
// the caller from starting a replacement beside it. An empty slot marks the
// manager stopped.
pub fn stop_running_backend(
    slot: &Mutex<Option<RunningBackend>>,
    manager: &Arc<Mutex<BackendManager>>,
) -> Result<(), String> {
    stop_running_backend_with(slot, manager, RunningBackend::stop)
}

fn stop_running_backend_with(
    slot: &Mutex<Option<RunningBackend>>,
    manager: &Arc<Mutex<BackendManager>>,
    stop: impl FnOnce(RunningBackend, &Arc<Mutex<BackendManager>>) -> Option<RunningBackend>,
) -> Result<(), String> {
    let process = slot
        .lock()
        .map_err(|_| "backend process lock poisoned".to_string())?
        .take();
    let Some(process) = process else {
        manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?
            .mark_stopped();
        return Ok(());
    };
    let Some(survivor) = stop(process, manager) else {
        return Ok(());
    };
    let mut guard = slot.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    // Callers hold `restart_lock`, so nothing refills the slot meanwhile; if
    // something did, the survivor is dropped (force-killed) instead.
    if guard.is_none() {
        *guard = Some(survivor);
    }
    Err(BACKEND_DID_NOT_EXIT.to_string())
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
        .or_else(|| {
            resolve_setup_python(&project_root_dir())
                .ok()
                .map(|path| path.to_string_lossy().into_owned())
        })
        .unwrap_or_else(|| "python".to_string())
}

pub fn sidecar_runtime_filename() -> String {
    if cfg!(target_os = "windows") {
        format!("{BACKEND_SIDECAR_NAME}.exe")
    } else {
        BACKEND_SIDECAR_NAME.to_string()
    }
}

// The bundle ships the onedir sidecar as `binaries/<name>/<name>[.exe]` under
// the resource directory. The executable's directory is the fallback for
// Windows app virtualization, which can redirect only the executable.
pub fn resolve_sidecar_path(
    resource_dir: Option<&Path>,
    current_binary_path: Option<&Path>,
) -> Result<PathBuf, String> {
    let filename = sidecar_runtime_filename();
    let mut candidates: Vec<PathBuf> = Vec::new();
    for dir in [resource_dir, current_binary_path.and_then(Path::parent)]
        .into_iter()
        .flatten()
    {
        let candidate = dir
            .join("binaries")
            .join(BACKEND_SIDECAR_NAME)
            .join(&filename);
        if candidate.is_file() {
            return Ok(candidate);
        }
        if !candidates.contains(&candidate) {
            candidates.push(candidate);
        }
    }

    Err(format!(
        "desktop backend sidecar not found: expected '{}' in one of [{}]",
        filename,
        candidates
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

/// Base backend environment, parent process identity, and debug-only dev-CORS opt-in.
///
/// Debug builds (`tauri dev`) serve the frontend from the Vite dev server, so
/// the backend must allow `http://localhost:5173` in CORS. Release builds
/// (`tauri build`) compile with `debug_assertions` off and never set this flag,
/// so packaged backends only accept the `tauri.localhost` origin.
fn backend_launch_env(runtime_paths: &DesktopRuntimePaths) -> Vec<(String, String)> {
    let mut env: Vec<(String, String)> = runtime_paths.as_env().into_iter().collect();
    env.push((
        "ORPHEUS_DESKTOP_PARENT_PID".to_string(),
        std::process::id().to_string(),
    ));
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
            BACKEND_LOOPBACK_HOST.to_string(),
            "--port".to_string(),
            "0".to_string(),
        ],
        cwd: Some(project_root.to_path_buf()),
        env: backend_launch_env(runtime_paths),
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
            BACKEND_LOOPBACK_HOST.to_string(),
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

    remove_inherited_python_overrides(&mut command);
    for (key, value) in &launch.env {
        command.env(key, value);
    }

    #[cfg(unix)]
    command.process_group(0);

    // Keep the hidden backend suspended until it has been assigned to a
    // kill-on-close Job Object.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(crate::setup::windows_setup_creation_flags());
    }

    let mut child = command.spawn().map_err(|error| {
        let message = format!("failed to launch desktop backend process: {error}");
        if let Ok(mut guard) = manager.lock() {
            guard.mark_failed_for_generation(generation, message.clone());
        }
        message
    })?;
    #[cfg(unix)]
    let process_group_id = i32::try_from(child.id()).ok();

    // Kill a child whose launch cannot be completed and record why. The job
    // (once assigned) is terminated first so no descendant can survive.
    let abort =
        |child: &mut Child, #[cfg(windows)] job: Option<&WindowsJob>, error: String| -> String {
            #[cfg(windows)]
            if let Some(job) = job {
                job.terminate();
            }
            let _ = child.kill();
            // Bounded: this runs under `restart_lock`.
            let _ = wait_for_child_exit(child, BACKEND_EMERGENCY_TEARDOWN_WAIT);
            if let Ok(mut guard) = manager.lock() {
                guard.mark_failed_for_generation(generation, error.clone());
            }
            error
        };

    #[cfg(windows)]
    let job = match WindowsJob::assign(&child) {
        Ok(job) => job,
        Err(error) => return Err(abort(&mut child, None, error)),
    };

    let (Some(stdout), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
        return Err(abort(
            &mut child,
            #[cfg(windows)]
            Some(job.as_ref()),
            "failed to capture desktop backend output".to_string(),
        ));
    };

    #[cfg(windows)]
    if let Err(error) = resume_suspended_child_threads(child.id()) {
        return Err(abort(&mut child, Some(job.as_ref()), error));
    }

    let log = Arc::new(Mutex::new(
        launch
            .env
            .iter()
            .find(|(key, _)| key == "AUDIOBOOK_DATA_DIR")
            .and_then(|(_, value)| BackendLog::open(Path::new(value)).ok()),
    ));
    let stderr_lines = Arc::new(Mutex::new(Vec::new()));
    let shutdown_ack = Arc::new(AtomicBool::new(false));
    #[cfg(windows)]
    let shutdown_endpoint = Arc::new(Mutex::new(None));
    let (stderr_done_tx, stderr_done_rx) = mpsc::channel();

    let stdout_thread = spawn_stdout_reader(
        stdout,
        StdoutReaderContext {
            manager: Arc::clone(&manager),
            generation,
            log: Arc::clone(&log),
            ready_file: launch
                .env
                .iter()
                .find(|(key, _)| key == "AUDIOBOOK_DESKTOP_READY_FILE")
                .map(|(_, value)| PathBuf::from(value)),
            stderr_lines: Arc::clone(&stderr_lines),
            stderr_done: stderr_done_rx,
            shutdown_ack: Arc::clone(&shutdown_ack),
            #[cfg(windows)]
            shutdown_endpoint: Arc::clone(&shutdown_endpoint),
        },
    );
    let stderr_thread = spawn_stderr_reader(stderr, log, Arc::clone(&stderr_lines), stderr_done_tx);

    Ok(RunningBackend {
        child,
        stdout_thread: Some(stdout_thread),
        stderr_thread: Some(stderr_thread),
        stderr_lines,
        shutdown_ack,
        #[cfg(windows)]
        shutdown_endpoint,
        generation,
        #[cfg(unix)]
        process_group_id,
        #[cfg(unix)]
        child_reaped: false,
        emergency_teardown_armed: true,
        #[cfg(windows)]
        job,
    })
}

// Read one newline-terminated line, decoding invalid UTF-8 lossily so a stray
// byte never stops diagnostics. Returns `None` at EOF or on a read error.
fn read_lossy_line(reader: &mut impl BufRead, buffer: &mut Vec<u8>) -> Option<String> {
    buffer.clear();
    match reader.read_until(b'\n', buffer) {
        Ok(0) | Err(_) => None,
        Ok(_) => Some(
            String::from_utf8_lossy(buffer)
                .trim_end_matches(['\r', '\n'])
                .to_string(),
        ),
    }
}

struct StdoutReaderContext {
    manager: Arc<Mutex<BackendManager>>,
    generation: u64,
    log: Arc<Mutex<Option<BackendLog>>>,
    ready_file: Option<PathBuf>,
    stderr_lines: Arc<Mutex<Vec<String>>>,
    stderr_done: mpsc::Receiver<()>,
    shutdown_ack: Arc<AtomicBool>,
    #[cfg(windows)]
    shutdown_endpoint: Arc<Mutex<Option<BackendReadyPayload>>>,
}

// Watch stdout for the ready handshake and the shutdown acknowledgement, log
// everything else, and turn EOF into this generation's terminal lifecycle.
// The ready line carries the session token, so it is never logged.
fn spawn_stdout_reader(stdout: ChildStdout, context: StdoutReaderContext) -> JoinHandle<()> {
    thread::spawn(move || {
        let StdoutReaderContext {
            manager,
            generation,
            log,
            ready_file,
            stderr_lines,
            stderr_done,
            shutdown_ack,
            #[cfg(windows)]
            shutdown_endpoint,
        } = context;
        let mut reader = BufReader::new(stdout);
        let mut buffer = Vec::new();
        while let Some(line) = read_lossy_line(&mut reader, &mut buffer) {
            if let Ok(payload) = parse_ready_line(&line) {
                let _ = adopt_backend_ready(
                    &manager,
                    generation,
                    payload,
                    ready_file.as_deref(),
                    #[cfg(windows)]
                    &shutdown_endpoint,
                );
            } else if is_ready_event(&line) {
                // A rejected handshake still carries a token; record only that
                // it was ignored.
                log_backend_line(&log, "ignored an invalid desktop ready payload");
            } else {
                log_backend_line(&log, &line);
                if parse_shutdown_line(&line).is_ok() {
                    shutdown_ack.store(true, Ordering::SeqCst);
                }
            }
        }
        // stderr can reach EOF after stdout even when the child has exited.
        let _ = stderr_done.recv_timeout(BACKEND_READER_JOIN_TIMEOUT);

        let Ok(mut guard) = manager.lock() else {
            return;
        };
        // An acknowledged shutdown means the backend finished cleanup and
        // exited on purpose; report it as stopped rather than failed.
        if shutdown_ack.load(Ordering::SeqCst) {
            guard.mark_stopped_for_generation(generation);
            return;
        }
        let message = match guard.status().lifecycle {
            BackendLifecycle::Starting => "desktop backend exited before ready payload",
            BackendLifecycle::Ready => "desktop backend exited unexpectedly",
            BackendLifecycle::Stopped | BackendLifecycle::Failed => return,
        };
        guard.mark_failed_for_generation(generation, backend_exit_detail(message, &stderr_lines));
    })
}

// Mirror stderr into the log and the console, keeping a short tail for the
// failure message shown when the backend exits.
fn spawn_stderr_reader(
    stderr: ChildStderr,
    log: Arc<Mutex<Option<BackendLog>>>,
    stderr_lines: Arc<Mutex<Vec<String>>>,
    done: mpsc::Sender<()>,
) -> JoinHandle<()> {
    thread::spawn(move || {
        let mut reader = BufReader::new(stderr);
        let mut buffer = Vec::new();
        while let Some(line) = read_lossy_line(&mut reader, &mut buffer) {
            if let Ok(mut lines) = stderr_lines.lock() {
                if lines.len() >= BACKEND_STDERR_TAIL_LINES {
                    lines.remove(0);
                }
                lines.push(line.clone());
            }
            log_backend_line(&log, &line);
            print_diagnostic(format_args!("[desktop-backend] {line}"));
        }
        let _ = done.send(());
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
    if payload.host != BACKEND_LOOPBACK_HOST {
        return Err(format!(
            "desktop ready payload host '{}' is not the requested loopback address {BACKEND_LOOPBACK_HOST}",
            payload.host
        ));
    }
    if payload.port == 0 {
        return Err("desktop ready payload missing port".to_string());
    }
    if payload.token.trim().is_empty() {
        return Err("desktop ready payload missing token".to_string());
    }

    Ok(payload)
}

fn is_ready_event(line: &str) -> bool {
    serde_json::from_str::<serde_json::Value>(line.trim())
        .ok()
        .and_then(|value| {
            value
                .get("event")
                .and_then(serde_json::Value::as_str)
                .map(|event| event == "ready")
        })
        .unwrap_or(false)
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

pub fn read_ready_payload_from_file(path: impl AsRef<Path>) -> Result<BackendReadyPayload, String> {
    let path = path.as_ref();
    let payload = fs::read_to_string(path).map_err(|error| {
        format!(
            "failed to read desktop ready file '{}': {error}",
            path.display()
        )
    })?;
    let parsed = parse_ready_line(&payload)?;
    // The payload is already this generation's (the file is cleared before
    // every launch), so a scanner or indexer briefly holding the file open must
    // not discard it. Stopping the backend removes the file again.
    let _ = fs::remove_file(path);
    Ok(parsed)
}

#[cfg(test)]
#[path = "backend_tests.rs"]
mod tests;
