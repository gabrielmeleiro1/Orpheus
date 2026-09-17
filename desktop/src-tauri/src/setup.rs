use crate::paths::{resolve_desktop_runtime_paths, DesktopRuntimePaths};
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, LazyLock, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Emitter, Manager, State};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

pub const SETUP_PROGRESS_EVENT: &str = "runtime-setup-progress";
const SETUP_OVERALL_TIMEOUT: Duration = Duration::from_secs(80 * 60);
const SETUP_POLL_INTERVAL: Duration = Duration::from_millis(100);
const MAX_MESSAGE_CHARS: usize = 512;
const MAX_MODEL_GROUPS: usize = 4;
const MAX_PROGRESS_LINE_BYTES: usize = 16 * 1024;
const MAX_PROGRESS_EVENTS: usize = 256;
const MAX_PROGRESS_EVENTS_PER_SECOND: usize = 32;
const RESERVED_STAGE_EVENTS: usize = 8;
const READER_JOIN_TIMEOUT: Duration = Duration::from_millis(250);
#[cfg(any(test, windows))]
const WINDOWS_CREATE_SUSPENDED: u32 = 0x0000_0004;
#[cfg(any(test, windows))]
const WINDOWS_CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
#[cfg(any(test, windows))]
const WINDOWS_CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[cfg(any(test, windows))]
pub(crate) fn windows_setup_creation_flags() -> u32 {
    WINDOWS_CREATE_SUSPENDED | WINDOWS_CREATE_NEW_PROCESS_GROUP | WINDOWS_CREATE_NO_WINDOW
}

#[cfg(any(test, windows))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WindowsLaunchPhase {
    Built,
    SpawnedSuspended,
    JobAssigned,
    Resumed,
}

#[cfg(any(test, windows))]
impl WindowsLaunchPhase {
    pub(crate) fn mark_spawned_suspended(&mut self) -> Result<(), String> {
        if *self != Self::Built {
            return Err("invalid Windows setup spawn order".to_string());
        }
        *self = Self::SpawnedSuspended;
        Ok(())
    }

    pub(crate) fn mark_job_assigned(&mut self) -> Result<(), String> {
        if *self != Self::SpawnedSuspended {
            return Err("runtime setup job must follow suspended spawn".to_string());
        }
        *self = Self::JobAssigned;
        Ok(())
    }

    pub(crate) fn mark_resumed(&mut self) -> Result<(), String> {
        if *self != Self::JobAssigned {
            return Err("runtime setup cannot resume before job assignment".to_string());
        }
        *self = Self::Resumed;
        Ok(())
    }
}

pub(crate) type EventSink = Arc<dyn Fn(SetupProgressEvent) + Send + Sync + 'static>;

static BEARER_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+").expect("valid bearer regex"));
static SK_TOKEN_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\bsk-[a-z0-9_-]+").expect("valid sk token regex"));
static URL_USERINFO_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@").expect("valid URL userinfo regex")
});
static QUERY_SECRET_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)([?&](?:token|api[_-]?key|key|password|secret|authorization)=)[^&\s#]+")
        .expect("valid query secret regex")
});
static KEY_VALUE_SECRET_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)\b(token|api[_-]?key|key|password|secret|authorization)\s*[:=]\s*[^\s,;]+")
        .expect("valid key value secret regex")
});

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct SetupRequest {
    pub profile: String,
    #[serde(default)]
    pub model_groups: Vec<String>,
    #[serde(default)]
    pub accept_pytorch_risk: bool,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SetupLifecycle {
    Idle,
    Running,
    Cancelling,
    Complete,
    Failed,
    Cancelled,
    TimedOut,
}

impl Default for SetupLifecycle {
    fn default() -> Self {
        Self::Idle
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Default)]
#[serde(rename_all = "camelCase")]
pub struct SetupStatus {
    pub lifecycle: SetupLifecycle,
    pub profile: Option<String>,
    pub model_groups: Vec<String>,
    pub stage: Option<String>,
    pub message: Option<String>,
    pub percent: Option<f64>,
    pub restart_required: bool,
    pub error: Option<String>,
    #[serde(skip)]
    pub generation: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeSetupEnvironment {
    pub available: bool,
    pub mode: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SetupLaunchConfig {
    pub program: PathBuf,
    pub script: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub env: BTreeMap<String, String>,
    // Kept explicit so the security property is visible and unit-testable.
    pub uses_shell: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SetupProgressEvent {
    pub event: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stage: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub profile: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub model_groups: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub percent: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub terminal: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub restart_required: Option<bool>,
}

impl SetupProgressEvent {
    fn terminal(event: &str, message: Option<String>, restart_required: bool) -> Self {
        Self {
            event: event.to_string(),
            stage: None,
            profile: None,
            model_groups: Vec::new(),
            message,
            percent: (event == "complete").then_some(100.0),
            terminal: Some(true),
            restart_required: Some(restart_required),
        }
    }
}

#[derive(Debug, Default)]
struct SetupRunnerInner {
    status: SetupStatus,
    active_process: Option<ActiveProcess>,
    terminal_emitted: bool,
    restart_in_progress: bool,
}

#[derive(Debug, Clone, Default)]
pub struct SetupRunnerState {
    inner: Arc<Mutex<SetupRunnerInner>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct RestartAuthorization {
    generation: u64,
}

#[derive(Debug, Clone)]
pub(crate) struct ActiveProcess {
    child: Arc<Mutex<Option<Child>>>,
    #[cfg(unix)]
    process_group_id: i32,
    #[cfg(windows)]
    job: Arc<WindowsJob>,
}

#[cfg(windows)]
#[derive(Debug)]
pub(crate) struct WindowsJob {
    handle: WindowsHandle,
}

#[cfg(windows)]
#[derive(Debug)]
struct WindowsHandle(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
unsafe impl Send for WindowsHandle {}
#[cfg(windows)]
unsafe impl Sync for WindowsHandle {}

#[cfg(windows)]
impl Drop for WindowsHandle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                windows_sys::Win32::Foundation::CloseHandle(self.0);
            }
        }
    }
}

#[cfg(windows)]
impl WindowsJob {
    pub(crate) fn assign(child: &Child) -> Result<Arc<Self>, String> {
        use std::mem::{size_of, zeroed};
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::System::JobObjects::{
            AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
            SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        };

        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err("failed to create runtime setup job object".to_string());
        }
        let job = Arc::new(Self {
            handle: WindowsHandle(handle),
        });
        let mut information: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { zeroed() };
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                job.handle.0,
                JobObjectExtendedLimitInformation,
                (&information as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if configured == 0 {
            return Err("failed to configure runtime setup job object".to_string());
        }
        let assigned = unsafe {
            AssignProcessToJobObject(
                job.handle.0,
                child.as_raw_handle() as windows_sys::Win32::Foundation::HANDLE,
            )
        };
        if assigned == 0 {
            return Err("failed to contain runtime setup process".to_string());
        }
        Ok(job)
    }

    pub(crate) fn terminate(&self) {
        use windows_sys::Win32::System::JobObjects::TerminateJobObject;
        unsafe {
            TerminateJobObject(self.handle.0, 1);
        }
    }
}

#[cfg(windows)]
pub(crate) fn resume_suspended_child_threads(process_id: u32) -> Result<(), String> {
    use std::mem::size_of;
    use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
    };
    use windows_sys::Win32::System::Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME};

    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err("failed to enumerate suspended runtime setup threads".to_string());
    }
    let snapshot = WindowsHandle(snapshot);
    let mut entry = THREADENTRY32::default();
    entry.dwSize = size_of::<THREADENTRY32>() as u32;
    if unsafe { Thread32First(snapshot.0, &mut entry) } == 0 {
        return Err("failed to read suspended runtime setup threads".to_string());
    }
    let mut resumed = 0_u32;
    loop {
        if entry.th32OwnerProcessID == process_id {
            let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID) };
            if thread.is_null() {
                return Err("failed to open suspended runtime setup thread".to_string());
            }
            let thread = WindowsHandle(thread);
            let mut previous = unsafe { ResumeThread(thread.0) };
            if previous == u32::MAX || previous == 0 {
                return Err("failed to resume contained runtime setup thread".to_string());
            }
            while previous > 1 {
                previous = unsafe { ResumeThread(thread.0) };
                if previous == u32::MAX || previous == 0 {
                    return Err("failed to fully resume contained runtime setup thread".to_string());
                }
            }
            resumed += 1;
        }
        if unsafe { Thread32Next(snapshot.0, &mut entry) } == 0 {
            break;
        }
    }
    if resumed == 0 {
        return Err("suspended runtime setup process has no owned thread".to_string());
    }
    Ok(())
}

impl SetupRunnerState {
    pub fn status(&self) -> SetupStatus {
        self.inner
            .lock()
            .map(|inner| inner.status.clone())
            .unwrap_or_else(|_| SetupStatus {
                lifecycle: SetupLifecycle::Failed,
                error: Some("runtime setup state is unavailable".to_string()),
                ..SetupStatus::default()
            })
    }

    pub fn begin(&self, request: SetupRequest) -> Result<u64, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "runtime setup state lock poisoned".to_string())?;
        if inner.restart_in_progress {
            return Err("runtime setup restart is in progress".to_string());
        }
        if matches!(
            inner.status.lifecycle,
            SetupLifecycle::Running | SetupLifecycle::Cancelling
        ) {
            return Err("a runtime setup operation is already running".to_string());
        }
        let generation = inner.status.generation.wrapping_add(1);
        inner.status = SetupStatus {
            lifecycle: SetupLifecycle::Running,
            profile: Some(request.profile),
            model_groups: request.model_groups,
            stage: None,
            message: None,
            percent: None,
            restart_required: false,
            error: None,
            generation,
        };
        inner.active_process = None;
        inner.terminal_emitted = false;
        inner.restart_in_progress = false;
        Ok(generation)
    }

    pub(crate) fn acquire_restart_authorization(&self) -> Result<RestartAuthorization, String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "runtime setup state lock poisoned".to_string())?;
        if inner.status.lifecycle != SetupLifecycle::Complete || !inner.status.restart_required {
            return Err("runtime setup has no completed restart authorization".to_string());
        }
        if inner.restart_in_progress {
            return Err("runtime setup restart is already in progress".to_string());
        }
        inner.restart_in_progress = true;
        Ok(RestartAuthorization {
            generation: inner.status.generation,
        })
    }

    pub(crate) fn restore_restart_authorization(
        &self,
        authorization: RestartAuthorization,
    ) -> Result<(), String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "runtime setup state lock poisoned".to_string())?;
        if inner.status.generation != authorization.generation
            || inner.status.lifecycle != SetupLifecycle::Complete
            || !inner.status.restart_required
            || !inner.restart_in_progress
        {
            return Err("runtime setup restart authorization is stale".to_string());
        }
        inner.restart_in_progress = false;
        Ok(())
    }

    pub(crate) fn complete_restart_authorization(
        &self,
        authorization: RestartAuthorization,
    ) -> Result<(), String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "runtime setup state lock poisoned".to_string())?;
        if inner.status.generation != authorization.generation
            || inner.status.lifecycle != SetupLifecycle::Complete
            || !inner.status.restart_required
            || !inner.restart_in_progress
        {
            return Err("runtime setup restart authorization is stale".to_string());
        }
        inner.status.restart_required = false;
        inner.restart_in_progress = false;
        Ok(())
    }

    fn attach_process(&self, generation: u64, process: ActiveProcess) -> Result<(), String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "runtime setup state lock poisoned".to_string())?;
        if inner.status.generation != generation
            || inner.status.lifecycle != SetupLifecycle::Running
        {
            return Err("runtime setup operation was cancelled before launch".to_string());
        }
        inner.active_process = Some(process);
        Ok(())
    }

    pub(crate) fn record_progress(&self, generation: u64, event: SetupProgressEvent) -> bool {
        let Ok(mut inner) = self.inner.lock() else {
            return false;
        };
        if inner.status.generation != generation
            || inner.status.lifecycle != SetupLifecycle::Running
        {
            return false;
        }
        if let Some(stage) = event.stage {
            inner.status.stage = Some(stage);
        }
        if let Some(message) = event.message {
            inner.status.message = Some(message);
        }
        if let Some(percent) = event.percent.filter(|value| value.is_finite()) {
            inner.status.percent = Some(percent.clamp(0.0, 99.0));
        }
        true
    }

    pub fn finish_success(&self, generation: u64, restart_required: bool) -> bool {
        self.transition_terminal(generation, SetupLifecycle::Complete, None, restart_required)
    }

    pub fn finish_failure(
        &self,
        generation: u64,
        lifecycle: SetupLifecycle,
        error: impl AsRef<str>,
    ) -> bool {
        debug_assert!(matches!(
            lifecycle,
            SetupLifecycle::Failed | SetupLifecycle::Cancelled | SetupLifecycle::TimedOut
        ));
        self.transition_terminal(
            generation,
            lifecycle,
            Some(sanitize_text(error.as_ref())),
            false,
        )
    }

    fn transition_terminal(
        &self,
        generation: u64,
        lifecycle: SetupLifecycle,
        error: Option<String>,
        restart_required: bool,
    ) -> bool {
        let Ok(mut inner) = self.inner.lock() else {
            return false;
        };
        let valid_source = match lifecycle {
            SetupLifecycle::Cancelled => matches!(
                inner.status.lifecycle,
                SetupLifecycle::Running | SetupLifecycle::Cancelling
            ),
            _ => inner.status.lifecycle == SetupLifecycle::Running,
        };
        if inner.status.generation != generation || !valid_source || inner.terminal_emitted {
            return false;
        }
        inner.status.lifecycle = lifecycle;
        inner.status.error = error;
        inner.status.restart_required = restart_required;
        if lifecycle == SetupLifecycle::Complete {
            inner.status.percent = Some(100.0);
        }
        inner.active_process = None;
        inner.terminal_emitted = true;
        inner.restart_in_progress = false;
        true
    }

    pub(crate) fn cancel(&self) -> Result<(u64, Option<ActiveProcess>), String> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| "runtime setup state lock poisoned".to_string())?;
        if inner.status.lifecycle != SetupLifecycle::Running {
            return Err("no runtime setup operation is running".to_string());
        }
        let generation = inner.status.generation;
        inner.status.lifecycle = SetupLifecycle::Cancelling;
        let process = inner.active_process.take();
        Ok((generation, process))
    }

    pub fn shutdown(&self) {
        let cancelled = self.cancel().ok();
        if let Some((generation, process)) = cancelled {
            if let Some(process) = process {
                terminate_process(&process);
            }
            self.transition_terminal(
                generation,
                SetupLifecycle::Cancelled,
                Some("runtime setup was cancelled".to_string()),
                false,
            );
        }
    }

    #[cfg(all(test, unix))]
    pub(crate) fn active_pid(&self) -> Option<u32> {
        self.inner
            .lock()
            .ok()
            .and_then(|inner| inner.active_process.clone())
            .and_then(|process| {
                process
                    .child
                    .lock()
                    .ok()
                    .and_then(|child| child.as_ref().map(Child::id))
            })
    }
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

fn allowed_groups(profile: &str) -> &'static [&'static str] {
    match profile {
        "base" => &[],
        "mlx" => &["tts-mlx", "cleaner-mlx"],
        "cuda" => &["tts-cpu-cuda", "cleaner-cuda"],
        _ => &[],
    }
}

fn validate_platform(profile: &str, os: &str, arch: &str) -> Result<(), String> {
    match profile {
        "base" => Ok(()),
        "mlx" if os == "macos" && arch == "aarch64" => Ok(()),
        "mlx" => Err("MLX setup requires an Apple Silicon Mac".to_string()),
        "cuda" if matches!(os, "linux" | "windows") => Ok(()),
        "cuda" => Err("CUDA setup requires Linux or Windows".to_string()),
        _ => Err("unknown runtime profile".to_string()),
    }
}

fn build_setup_launch_config(
    repo_root: &Path,
    python_program: impl AsRef<Path>,
    request: SetupRequest,
    os: &str,
    arch: &str,
    runtime_paths: &DesktopRuntimePaths,
) -> Result<SetupLaunchConfig, String> {
    if !valid_identifier(&request.profile)
        || !matches!(request.profile.as_str(), "base" | "mlx" | "cuda")
    {
        return Err("unknown or invalid runtime profile".to_string());
    }
    validate_platform(&request.profile, os, arch)?;
    if request.model_groups.len() > MAX_MODEL_GROUPS {
        return Err("too many model groups requested".to_string());
    }

    let allowed = allowed_groups(&request.profile);
    let mut groups = Vec::new();
    for group in request.model_groups {
        if !valid_identifier(&group) || !allowed.contains(&group.as_str()) {
            return Err(format!(
                "model group is not allowed for profile '{}'",
                request.profile
            ));
        }
        if !groups.contains(&group) {
            groups.push(group);
        }
    }
    let script = repo_root.join("scripts").join("setup_runtime.py");
    let mut args = vec![
        script.to_string_lossy().into_owned(),
        "--profile".to_string(),
        request.profile,
    ];
    for group in groups {
        args.push("--models".to_string());
        args.push(group);
    }
    args.push("--progress-json".to_string());
    Ok(SetupLaunchConfig {
        program: python_program.as_ref().to_path_buf(),
        script,
        args,
        cwd: repo_root.to_path_buf(),
        env: runtime_paths.as_env(),
        uses_shell: false,
    })
}

#[cfg(test)]
pub(crate) fn build_setup_launch_config_for_platform(
    repo_root: &Path,
    python_program: impl AsRef<Path>,
    request: SetupRequest,
    os: &str,
    arch: &str,
) -> Result<SetupLaunchConfig, String> {
    let runtime_paths = DesktopRuntimePaths::from_app_data_dir("/test-app-data");
    build_setup_launch_config(repo_root, python_program, request, os, arch, &runtime_paths)
}

#[cfg(test)]
pub(crate) fn build_setup_launch_config_for_platform_with_runtime_paths(
    repo_root: &Path,
    python_program: impl AsRef<Path>,
    request: SetupRequest,
    os: &str,
    arch: &str,
    runtime_paths: &DesktopRuntimePaths,
) -> Result<SetupLaunchConfig, String> {
    build_setup_launch_config(repo_root, python_program, request, os, arch, runtime_paths)
}

/// Resolve the setup interpreter from one fixed, user-created repository venv.
///
/// Trust boundary: the repository owner is responsible for creating `.venv`.
/// A normal Unix venv may make its fixed `bin/python` entry a symlink to the
/// system interpreter, so the target may live outside the repository. We reject
/// an escaped `.venv` directory itself, never consult PATH or environment
/// interpreter overrides, and never accept a path from the frontend.
pub(crate) fn resolve_setup_python(repo_root: &Path) -> Result<PathBuf, String> {
    let canonical_repo = repo_root
        .canonicalize()
        .map_err(|_| "the Orpheus project directory is unavailable".to_string())?;
    let venv = canonical_repo.join(".venv");
    let canonical_venv = venv
        .canonicalize()
        .map_err(|_| "create the repository .venv before installing a runtime".to_string())?;
    if !canonical_venv.starts_with(&canonical_repo) || !canonical_venv.is_dir() {
        return Err("the repository .venv directory is not trusted".to_string());
    }

    #[cfg(windows)]
    let entry = canonical_venv.join("Scripts").join("python.exe");
    #[cfg(not(windows))]
    let entry = canonical_venv.join("bin").join("python");

    let entry_metadata = std::fs::symlink_metadata(&entry)
        .map_err(|_| "the repository .venv Python interpreter is missing".to_string())?;
    if !(entry_metadata.file_type().is_file() || entry_metadata.file_type().is_symlink()) {
        return Err("the repository .venv Python entry is not a file".to_string());
    }
    let target = entry
        .canonicalize()
        .map_err(|_| "the repository .venv Python interpreter is invalid".to_string())?;
    let target_metadata = target
        .metadata()
        .map_err(|_| "the repository .venv Python interpreter is unavailable".to_string())?;
    if !target_metadata.is_file() {
        return Err("the repository .venv Python interpreter is not a regular file".to_string());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if target_metadata.permissions().mode() & 0o111 == 0 {
            return Err("the repository .venv Python interpreter is not executable".to_string());
        }
    }
    Ok(entry)
}

fn production_launch_config(
    request: SetupRequest,
    runtime_paths: &DesktopRuntimePaths,
) -> Result<SetupLaunchConfig, String> {
    let environment = current_runtime_setup_environment();
    if !environment.available {
        return Err(environment.reason);
    }
    let repo_root = source_checkout_root()
        .ok_or_else(|| "runtime setup is unavailable in this packaged build".to_string())?;
    let python = resolve_setup_python(&repo_root)?;
    let launch = build_setup_launch_config(
        &repo_root,
        python,
        request,
        std::env::consts::OS,
        std::env::consts::ARCH,
        runtime_paths,
    )?;
    if !launch.script.is_file() {
        return Err("the fixed runtime setup script is unavailable".to_string());
    }
    Ok(launch)
}

fn runtime_setup_environment_for(
    packaged: bool,
    repo_root: Option<&Path>,
) -> RuntimeSetupEnvironment {
    if packaged {
        return RuntimeSetupEnvironment {
            available: false,
            mode: "packaged".to_string(),
            reason: "Packaged Orpheus builds use profile-baked runtimes; dynamic installation is available only from a source checkout.".to_string(),
        };
    }
    let Some(repo_root) = repo_root else {
        return RuntimeSetupEnvironment {
            available: false,
            mode: "source_checkout".to_string(),
            reason: "The Orpheus source checkout is unavailable.".to_string(),
        };
    };
    if !repo_root.join("scripts/setup_runtime.py").is_file() {
        return RuntimeSetupEnvironment {
            available: false,
            mode: "source_checkout".to_string(),
            reason:
                "The fixed scripts/setup_runtime.py installer is missing from this source checkout."
                    .to_string(),
        };
    }
    if let Err(reason) = resolve_setup_python(repo_root) {
        return RuntimeSetupEnvironment {
            available: false,
            mode: "source_checkout".to_string(),
            reason,
        };
    }
    RuntimeSetupEnvironment {
        available: true,
        mode: "source_checkout".to_string(),
        reason: String::new(),
    }
}

#[cfg(debug_assertions)]
fn source_checkout_root() -> Option<PathBuf> {
    Some(crate::backend::project_root_dir())
}

#[cfg(not(debug_assertions))]
fn source_checkout_root() -> Option<PathBuf> {
    None
}

fn current_runtime_setup_environment() -> RuntimeSetupEnvironment {
    let repo_root = source_checkout_root();
    runtime_setup_environment_for(!cfg!(debug_assertions), repo_root.as_deref())
}

#[cfg(test)]
pub(crate) fn runtime_setup_environment_for_test(
    packaged: bool,
    repo_root: Option<&Path>,
) -> RuntimeSetupEnvironment {
    runtime_setup_environment_for(packaged, repo_root)
}

fn sanitize_stage(value: &str) -> Option<String> {
    match value {
        "Dependency installation" | "dependency_installation" => {
            Some("dependency_installation".to_string())
        }
        "Model download" | "model_download" => Some("model_download".to_string()),
        "Verification" | "verification" => Some("verify".to_string()),
        _ => None,
    }
}

fn scrub_word(word: &str) -> String {
    let lower = word.to_ascii_lowercase();
    let sensitive = [
        "token=",
        "token:",
        "api_key=",
        "api-key=",
        "apikey=",
        "authorization=",
        "password=",
        "sk-",
    ];
    if sensitive.iter().any(|prefix| lower.starts_with(prefix)) {
        return "[redacted]".to_string();
    }
    if word.starts_with('/')
        || word.starts_with("~/")
        || (word.len() > 2
            && word.as_bytes()[1] == b':'
            && matches!(word.as_bytes()[2], b'\\' | b'/'))
    {
        return "<path>".to_string();
    }
    word.to_string()
}

fn truncate_bytes(value: &str, max_bytes: usize) -> String {
    if value.len() <= max_bytes {
        return value.to_string();
    }
    let mut start = value
        .len()
        .saturating_sub(max_bytes.saturating_sub('…'.len_utf8()));
    while start < value.len() && !value.is_char_boundary(start) {
        start += 1;
    }
    format!("…{}", &value[start..])
}

fn sanitize_text(value: &str) -> String {
    let without_controls: String = value
        .chars()
        .filter(|character| !character.is_control() || matches!(character, '\n' | '\t'))
        .collect();
    let redacted = BEARER_RE.replace_all(&without_controls, "Bearer [redacted]");
    let redacted = SK_TOKEN_RE.replace_all(&redacted, "[redacted]");
    let redacted = URL_USERINFO_RE.replace_all(&redacted, "$1[redacted]@");
    let redacted = QUERY_SECRET_RE.replace_all(&redacted, "$1[redacted]");
    let redacted = KEY_VALUE_SECRET_RE.replace_all(&redacted, "$1=[redacted]");
    let scrubbed = redacted
        .split_whitespace()
        .map(scrub_word)
        .collect::<Vec<_>>()
        .join(" ");
    truncate_bytes(scrubbed.trim(), MAX_MESSAGE_CHARS)
}

pub fn sanitize_progress_line(line: &str) -> Option<SetupProgressEvent> {
    let Ok(value) = serde_json::from_str::<Value>(line.trim()) else {
        let message = sanitize_text(line);
        return (!message.is_empty()).then_some(SetupProgressEvent {
            event: "diagnostic".to_string(),
            stage: None,
            profile: None,
            model_groups: Vec::new(),
            message: Some(message),
            percent: None,
            terminal: None,
            restart_required: None,
        });
    };
    let object = value.as_object()?;
    let source_event = object.get("event").and_then(Value::as_str).unwrap_or("");
    let event = match source_event {
        "plan" | "stage_started" | "stage_complete" => source_event.to_string(),
        // Child terminal messages are diagnostics only. The supervisor emits
        // exactly one authoritative terminal event after observing process exit.
        "error" => "diagnostic".to_string(),
        "complete" | "status" => return None,
        _ => return None,
    };
    let stage = object
        .get("stage")
        .and_then(Value::as_str)
        .and_then(sanitize_stage);
    let profile = object
        .get("profile")
        .and_then(Value::as_str)
        .filter(|profile| matches!(*profile, "base" | "mlx" | "cuda"))
        .map(str::to_string);
    let model_groups = object
        .get("model_groups")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .filter(|group| {
            matches!(
                *group,
                "tts-mlx" | "cleaner-mlx" | "tts-cpu-cuda" | "cleaner-cuda"
            )
        })
        .take(MAX_MODEL_GROUPS)
        .map(str::to_string)
        .collect();
    let message = object
        .get("message")
        .and_then(Value::as_str)
        .map(sanitize_text)
        .filter(|message| !message.is_empty());
    let percent = object
        .get("percent")
        .and_then(Value::as_f64)
        .filter(|value| value.is_finite())
        .map(|value| value.clamp(0.0, 100.0));

    Some(SetupProgressEvent {
        event,
        stage,
        profile,
        model_groups,
        message,
        percent,
        terminal: None,
        restart_required: None,
    })
}

pub(crate) fn read_progress_events<R, F>(mut reader: R, mut emit: F)
where
    R: Read,
    F: FnMut(SetupProgressEvent),
{
    let mut chunk = [0_u8; 4096];
    let mut line = Vec::with_capacity(MAX_PROGRESS_LINE_BYTES.min(4096));
    let mut overflowing = false;
    loop {
        let read = match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(read) => read,
            Err(_) => return,
        };
        for byte in &chunk[..read] {
            if *byte == b'\n' {
                if overflowing {
                    emit(SetupProgressEvent {
                        event: "diagnostic".to_string(),
                        stage: None,
                        profile: None,
                        model_groups: Vec::new(),
                        message: Some(
                            "runtime setup output line exceeded the safe limit and was discarded"
                                .to_string(),
                        ),
                        percent: None,
                        terminal: None,
                        restart_required: None,
                    });
                } else if !line.is_empty() {
                    if let Some(event) = sanitize_progress_line(&String::from_utf8_lossy(&line)) {
                        emit(event);
                    }
                }
                line.clear();
                overflowing = false;
            } else if !overflowing {
                if line.len() < MAX_PROGRESS_LINE_BYTES {
                    line.push(*byte);
                } else {
                    line.clear();
                    overflowing = true;
                }
            }
        }
    }
    if overflowing {
        emit(SetupProgressEvent {
            event: "diagnostic".to_string(),
            stage: None,
            profile: None,
            model_groups: Vec::new(),
            message: Some(
                "runtime setup output line exceeded the safe limit and was discarded".to_string(),
            ),
            percent: None,
            terminal: None,
            restart_required: None,
        });
    } else if !line.is_empty() {
        if let Some(event) = sanitize_progress_line(&String::from_utf8_lossy(&line)) {
            emit(event);
        }
    }
}

#[derive(Debug)]
struct ProgressGateInner {
    window_started: Instant,
    window_events: usize,
    total_events: usize,
    stage_events: usize,
    last_stage: Option<(String, Option<String>)>,
}

struct ProgressGate {
    inner: Mutex<ProgressGateInner>,
    sink: EventSink,
}

impl ProgressGate {
    fn new(sink: EventSink) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(ProgressGateInner {
                window_started: Instant::now(),
                window_events: 0,
                total_events: 0,
                stage_events: 0,
                last_stage: None,
            }),
            sink,
        })
    }

    fn emit(&self, event: SetupProgressEvent) {
        let Ok(mut inner) = self.inner.lock() else {
            return;
        };
        if inner.window_started.elapsed() >= Duration::from_secs(1) {
            inner.window_started = Instant::now();
            inner.window_events = 0;
        }
        let stage_key = event
            .stage
            .as_ref()
            .map(|stage| (event.event.clone(), Some(stage.clone())));
        let stage_changed = stage_key.is_some() && stage_key != inner.last_stage;
        if inner.total_events >= MAX_PROGRESS_EVENTS
            || (stage_changed && inner.stage_events >= RESERVED_STAGE_EVENTS)
            || (!stage_changed && inner.total_events >= MAX_PROGRESS_EVENTS - RESERVED_STAGE_EVENTS)
        {
            return;
        }
        if inner.window_events >= MAX_PROGRESS_EVENTS_PER_SECOND && !stage_changed {
            return;
        }
        if stage_key.is_some() {
            inner.last_stage = stage_key;
        }
        if stage_changed {
            inner.stage_events += 1;
        }
        inner.window_events += 1;
        inner.total_events += 1;
        drop(inner);
        (self.sink)(event);
    }
}

fn read_progress<R: Read + Send + 'static>(
    reader: R,
    gate: Arc<ProgressGate>,
    state: SetupRunnerState,
    generation: u64,
    stopped: Arc<AtomicBool>,
) -> (thread::JoinHandle<()>, mpsc::Receiver<()>) {
    let (done_tx, done_rx) = mpsc::channel();
    let handle = thread::spawn(move || {
        read_progress_events(reader, |event| {
            if !stopped.load(Ordering::Acquire) && state.record_progress(generation, event.clone())
            {
                gate.emit(event);
            }
        });
        let _ = done_tx.send(());
    });
    (handle, done_rx)
}

#[cfg(unix)]
fn signal_process_group(pid: u32, signal: i32) {
    if let Ok(pid) = i32::try_from(pid) {
        // The child is created as its own process-group leader.
        unsafe {
            libc::kill(-pid, signal);
        }
    }
}

#[cfg(unix)]
fn terminate_owned_process_group(process_group_id: i32) {
    if process_group_id <= 1 {
        return;
    }
    unsafe {
        libc::kill(-process_group_id, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_millis(500);
    while Instant::now() < deadline {
        if unsafe { libc::kill(-process_group_id, 0) } != 0 {
            return;
        }
        thread::sleep(Duration::from_millis(25));
    }
    unsafe {
        libc::kill(-process_group_id, libc::SIGKILL);
    }
}

#[cfg(unix)]
fn pgrep_program() -> &'static str {
    #[cfg(any(target_os = "macos", target_os = "linux"))]
    {
        "/usr/bin/pgrep"
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        "pgrep"
    }
}

#[cfg(unix)]
fn ps_program() -> &'static str {
    #[cfg(target_os = "macos")]
    {
        "/bin/ps"
    }
    #[cfg(target_os = "linux")]
    {
        "/usr/bin/ps"
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        "ps"
    }
}

#[cfg(unix)]
#[derive(Debug, Clone, PartialEq, Eq)]
struct ProcessIdentity {
    pid: u32,
    started: Option<String>,
}

#[cfg(unix)]
fn process_identity(pid: u32) -> ProcessIdentity {
    let started = Command::new(ps_program())
        .args(["-o", "lstart=", "-p", &pid.to_string()])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .filter(|value| !value.is_empty());
    ProcessIdentity { pid, started }
}

#[cfg(unix)]
fn process_matches(identity: &ProcessIdentity) -> bool {
    let Some(started) = &identity.started else {
        if let Ok(pid) = i32::try_from(identity.pid) {
            return unsafe { libc::kill(pid, 0) } == 0;
        }
        return false;
    };
    process_identity(identity.pid).started.as_ref() == Some(started)
}

#[cfg(unix)]
fn descendant_processes(root_pid: u32) -> Vec<ProcessIdentity> {
    // `setup_runtime.py` deliberately gives pip/model downloads their own
    // sessions. Walk the process tree with a fixed, argument-only OS utility so
    // cancellation also reaches those detached child groups. The root group
    // signal below remains the fallback when pgrep is unavailable.
    let mut pending = vec![root_pid];
    let mut descendants = Vec::new();
    while let Some(parent) = pending.pop() {
        if descendants.len() >= 256 {
            break;
        }
        let Ok(output) = Command::new(pgrep_program())
            .args(["-P", &parent.to_string()])
            .stdin(Stdio::null())
            .stderr(Stdio::null())
            .output()
        else {
            break;
        };
        for pid in String::from_utf8_lossy(&output.stdout)
            .lines()
            .filter_map(|value| value.trim().parse::<u32>().ok())
        {
            if pid > 1
                && pid != root_pid
                && !descendants
                    .iter()
                    .any(|identity: &ProcessIdentity| identity.pid == pid)
            {
                descendants.push(process_identity(pid));
                pending.push(pid);
            }
        }
    }
    descendants
}

#[cfg(unix)]
fn merge_identities(target: &mut Vec<ProcessIdentity>, discovered: Vec<ProcessIdentity>) {
    for identity in discovered {
        if !target.iter().any(|known| known.pid == identity.pid) {
            target.push(identity);
        }
    }
}

#[cfg(unix)]
fn terminate_tracked_descendants(descendants: &[ProcessIdentity]) {
    for identity in descendants.iter().rev() {
        if !process_matches(identity) {
            continue;
        }
        signal_process_group(identity.pid, libc::SIGTERM);
        if let Ok(pid) = i32::try_from(identity.pid) {
            unsafe {
                libc::kill(pid, libc::SIGTERM);
            }
        }
    }
    let deadline = Instant::now() + Duration::from_millis(500);
    while Instant::now() < deadline {
        if descendants
            .iter()
            .all(|identity| !process_matches(identity))
        {
            return;
        }
        thread::sleep(Duration::from_millis(25));
    }
    for identity in descendants.iter().rev() {
        if !process_matches(identity) {
            continue;
        }
        signal_process_group(identity.pid, libc::SIGKILL);
        if let Ok(pid) = i32::try_from(identity.pid) {
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
        }
    }
}

fn terminate_process(process: &ActiveProcess) {
    #[cfg(unix)]
    terminate_owned_process_group(process.process_group_id);
    #[cfg(windows)]
    process.job.terminate();

    let Ok(mut guard) = process.child.lock() else {
        return;
    };
    let Some(child) = guard.as_mut() else {
        return;
    };
    if child.try_wait().ok().flatten().is_some() {
        return;
    }
    #[cfg(unix)]
    {
        let root = process_identity(child.id());
        let mut descendants = descendant_processes(child.id());
        for identity in descendants.iter().rev() {
            if !process_matches(identity) {
                continue;
            }
            signal_process_group(identity.pid, libc::SIGTERM);
            if let Ok(pid) = i32::try_from(identity.pid) {
                unsafe {
                    libc::kill(pid, libc::SIGTERM);
                }
            }
        }
        if process_matches(&root) {
            signal_process_group(root.pid, libc::SIGTERM);
        }

        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            for discovered in descendant_processes(root.pid) {
                if !descendants
                    .iter()
                    .any(|identity| identity.pid == discovered.pid)
                {
                    descendants.push(discovered);
                }
            }
            let root_exited = child.try_wait().ok().flatten().is_some();
            if root_exited
                && descendants
                    .iter()
                    .all(|identity| !process_matches(identity))
            {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }

        for discovered in descendant_processes(root.pid) {
            if !descendants
                .iter()
                .any(|identity| identity.pid == discovered.pid)
            {
                descendants.push(discovered);
            }
        }
        for identity in descendants.iter().rev() {
            if !process_matches(identity) {
                continue;
            }
            signal_process_group(identity.pid, libc::SIGKILL);
            if let Ok(pid) = i32::try_from(identity.pid) {
                unsafe {
                    libc::kill(pid, libc::SIGKILL);
                }
            }
        }
        if process_matches(&root) {
            signal_process_group(root.pid, libc::SIGKILL);
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    #[cfg(windows)]
    {
        if let Ok(taskkill) = windows_taskkill_path() {
            use std::os::windows::process::CommandExt;
            let _ = Command::new(taskkill)
                .creation_flags(WINDOWS_CREATE_NO_WINDOW)
                .args(["/PID", &child.id().to_string(), "/T", "/F"])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
    }
    #[cfg(not(any(unix, windows)))]
    let _ = child.kill();

    #[cfg(not(unix))]
    {
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            if child.try_wait().ok().flatten().is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

#[cfg(windows)]
pub(crate) fn windows_taskkill_path() -> Result<PathBuf, String> {
    use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;

    let mut buffer = [0_u16; 32_768];
    let length = unsafe { GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
    if length == 0 || length >= buffer.len() {
        return Err("Windows system directory is unavailable".to_string());
    }
    let mut path = PathBuf::from(String::from_utf16_lossy(&buffer[..length]));
    path.push("taskkill.exe");
    Ok(path)
}

fn spawn_setup(
    state: SetupRunnerState,
    generation: u64,
    launch: SetupLaunchConfig,
    timeout: Duration,
    sink: EventSink,
    #[cfg(test)] after_spawn: Option<Arc<dyn Fn() + Send + Sync>>,
) -> Result<(), String> {
    let mut command = Command::new(&launch.program);
    apply_setup_command_env(&mut command, &launch);
    command
        .args(&launch.args)
        .current_dir(&launch.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    command.process_group(0);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(windows_setup_creation_flags());
    }

    let mut child = command
        .spawn()
        .map_err(|error| format!("failed to start runtime setup: {error}"))?;
    #[cfg(windows)]
    let mut windows_phase = WindowsLaunchPhase::Built;
    #[cfg(windows)]
    if let Err(error) = windows_phase.mark_spawned_suspended() {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    let stdout = child.stdout.take();
    let stderr = child.stderr.take();
    #[cfg(unix)]
    let process_group_id =
        i32::try_from(child.id()).map_err(|_| "runtime setup process id is invalid".to_string())?;
    #[cfg(windows)]
    let job = match WindowsJob::assign(&child) {
        Ok(job) => job,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    };
    #[cfg(windows)]
    if let Err(error) = windows_phase.mark_job_assigned() {
        job.terminate();
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    #[cfg(windows)]
    if let Err(error) = resume_suspended_child_threads(child.id()) {
        job.terminate();
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    #[cfg(windows)]
    if let Err(error) = windows_phase.mark_resumed() {
        job.terminate();
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    let process = Arc::new(Mutex::new(Some(child)));
    let active = ActiveProcess {
        child: Arc::clone(&process),
        #[cfg(unix)]
        process_group_id,
        #[cfg(windows)]
        job,
    };
    #[cfg(test)]
    if let Some(hook) = after_spawn {
        hook();
    }
    if let Err(error) = state.attach_process(generation, active.clone()) {
        terminate_process(&active);
        return Err(error);
    }

    thread::spawn(move || {
        let gate = ProgressGate::new(Arc::clone(&sink));
        let readers_stopped = Arc::new(AtomicBool::new(false));
        let stdout_thread = stdout.map(|reader| {
            read_progress(
                reader,
                Arc::clone(&gate),
                state.clone(),
                generation,
                Arc::clone(&readers_stopped),
            )
        });
        let stderr_thread = stderr.map(|reader| {
            read_progress(
                reader,
                Arc::clone(&gate),
                state.clone(),
                generation,
                Arc::clone(&readers_stopped),
            )
        });
        let started = Instant::now();
        #[cfg(unix)]
        let mut tracked_descendants = Vec::new();
        let result = loop {
            let status = state.status();
            if status.generation != generation || status.lifecycle != SetupLifecycle::Running {
                terminate_process(&active);
                break Err((SetupLifecycle::Cancelled, "runtime setup was cancelled"));
            }
            if started.elapsed() >= timeout {
                terminate_process(&active);
                break Err((SetupLifecycle::TimedOut, "runtime setup timed out"));
            }
            #[cfg(unix)]
            if let Some(root_pid) = process
                .lock()
                .ok()
                .and_then(|process| process.as_ref().map(Child::id))
            {
                merge_identities(&mut tracked_descendants, descendant_processes(root_pid));
            }
            let exit = process
                .lock()
                .ok()
                .and_then(|mut process| process.as_mut().and_then(|child| child.try_wait().ok()))
                .flatten();
            if let Some(exit) = exit {
                #[cfg(unix)]
                terminate_owned_process_group(active.process_group_id);
                #[cfg(windows)]
                active.job.terminate();
                #[cfg(unix)]
                terminate_tracked_descendants(&tracked_descendants);
                break if exit.success() {
                    Ok(())
                } else {
                    Err((SetupLifecycle::Failed, "runtime setup failed"))
                };
            }
            thread::sleep(SETUP_POLL_INTERVAL);
        };

        #[cfg(unix)]
        terminate_tracked_descendants(&tracked_descendants);
        for reader in [stdout_thread, stderr_thread].into_iter().flatten() {
            let (handle, done) = reader;
            if done.recv_timeout(READER_JOIN_TIMEOUT).is_ok() {
                let _ = handle.join();
            } else {
                readers_stopped.store(true, Ordering::Release);
            }
            // A blocked reader is safely detached. `readers_stopped` prevents
            // it from forwarding anything if the inherited pipe later closes.
        }
        readers_stopped.store(true, Ordering::Release);

        match result {
            Ok(()) if state.finish_success(generation, true) => {
                sink(SetupProgressEvent::terminal("complete", None, true))
            }
            Err((lifecycle, message)) if state.finish_failure(generation, lifecycle, message) => {
                sink(SetupProgressEvent::terminal(
                    "error",
                    Some(message.to_string()),
                    false,
                ));
            }
            _ => {}
        }
    });
    Ok(())
}

fn apply_setup_command_env(command: &mut Command, launch: &SetupLaunchConfig) {
    // Explicit command-level values take precedence over anything inherited by
    // the desktop process. The frontend cannot supply either names or values.
    for key in launch.env.keys() {
        command.env_remove(key);
    }
    command.envs(&launch.env);
    command.env("ORPHEUS_TAURI_SETUP_SUPERVISED", "1");
}

#[cfg(test)]
pub(crate) fn setup_command_env_for_test(
    launch: &SetupLaunchConfig,
    inherited: &[(&str, &str)],
) -> BTreeMap<String, String> {
    let mut command = Command::new(&launch.program);
    command.envs(inherited.iter().copied());
    apply_setup_command_env(&mut command, launch);
    command
        .get_envs()
        .filter_map(|(key, value)| {
            value.map(|value| {
                (
                    key.to_string_lossy().into_owned(),
                    value.to_string_lossy().into_owned(),
                )
            })
        })
        .collect()
}

fn start_supervised(
    state: SetupRunnerState,
    request: SetupRequest,
    launch: SetupLaunchConfig,
    timeout: Duration,
    sink: EventSink,
) -> Result<SetupStatus, String> {
    let generation = state.begin(request)?;
    if let Err(error) = spawn_setup(
        state.clone(),
        generation,
        launch,
        timeout,
        Arc::clone(&sink),
        #[cfg(test)]
        None,
    ) {
        if state.finish_failure(generation, SetupLifecycle::Failed, &error) {
            sink(SetupProgressEvent::terminal(
                "error",
                Some(sanitize_text(&error)),
                false,
            ));
        }
        return Err(sanitize_text(&error));
    }
    Ok(state.status())
}

#[cfg(test)]
pub(crate) fn start_supervised_with_hook_for_test(
    state: SetupRunnerState,
    request: SetupRequest,
    launch: SetupLaunchConfig,
    timeout: Duration,
    sink: EventSink,
    after_spawn: Arc<dyn Fn() + Send + Sync>,
) -> Result<SetupStatus, String> {
    let generation = state.begin(request)?;
    if let Err(error) = spawn_setup(
        state.clone(),
        generation,
        launch,
        timeout,
        Arc::clone(&sink),
        Some(after_spawn),
    ) {
        if state.finish_failure(generation, SetupLifecycle::Failed, &error) {
            sink(SetupProgressEvent::terminal(
                "error",
                Some(sanitize_text(&error)),
                false,
            ));
        }
        return Err(sanitize_text(&error));
    }
    Ok(state.status())
}

#[cfg(test)]
pub(crate) fn start_supervised_for_test(
    state: SetupRunnerState,
    request: SetupRequest,
    launch: SetupLaunchConfig,
    timeout: Duration,
    sink: EventSink,
) -> Result<SetupStatus, String> {
    start_supervised(state, request, launch, timeout, sink)
}

fn cancel_with_sink(state: &SetupRunnerState, sink: EventSink) -> Result<SetupStatus, String> {
    let (generation, process) = state.cancel()?;
    if let Some(process) = process {
        terminate_process(&process);
    }
    if state.transition_terminal(
        generation,
        SetupLifecycle::Cancelled,
        Some("runtime setup was cancelled".to_string()),
        false,
    ) {
        sink(SetupProgressEvent::terminal(
            "error",
            Some("runtime setup was cancelled".to_string()),
            false,
        ));
    }
    Ok(state.status())
}

#[cfg(test)]
pub(crate) fn cancel_with_sink_for_test(
    state: &SetupRunnerState,
    sink: EventSink,
) -> Result<SetupStatus, String> {
    cancel_with_sink(state, sink)
}

fn app_event_sink(app_handle: tauri::AppHandle) -> EventSink {
    Arc::new(move |event| {
        if let Err(error) = app_handle.emit(SETUP_PROGRESS_EVENT, &event) {
            eprintln!("failed to emit runtime setup progress: {error}");
        }
    })
}

#[tauri::command]
pub async fn start_runtime_setup(
    app_handle: tauri::AppHandle,
    state: State<'_, SetupRunnerState>,
    request: SetupRequest,
) -> Result<SetupStatus, String> {
    let app_data_dir = app_handle
        .path()
        .app_data_dir()
        .map_err(|error| format!("failed to resolve app data directory: {error}"))?;
    let runtime_paths = resolve_desktop_runtime_paths(app_data_dir)?;
    let launch = production_launch_config(request.clone(), &runtime_paths)?;
    start_supervised(
        state.inner().clone(),
        request,
        launch,
        SETUP_OVERALL_TIMEOUT,
        app_event_sink(app_handle),
    )
}

#[tauri::command]
pub fn runtime_setup_status(state: State<'_, SetupRunnerState>) -> SetupStatus {
    state.status()
}

#[tauri::command]
pub fn runtime_setup_environment() -> RuntimeSetupEnvironment {
    current_runtime_setup_environment()
}

#[tauri::command]
pub async fn cancel_runtime_setup(
    app_handle: tauri::AppHandle,
    state: State<'_, SetupRunnerState>,
) -> Result<SetupStatus, String> {
    // `cancel_with_sink` calls `terminate_process`, which blocks for up to a few
    // seconds (SIGTERM grace + `ps`/`pgrep` subprocess spawns). Run it on the
    // blocking pool so it never stalls the async runtime worker.
    let state = state.inner().clone();
    let sink = app_event_sink(app_handle);
    tauri::async_runtime::spawn_blocking(move || cancel_with_sink(&state, sink))
        .await
        .map_err(|error| format!("runtime setup cancellation task failed: {error}"))?
}
