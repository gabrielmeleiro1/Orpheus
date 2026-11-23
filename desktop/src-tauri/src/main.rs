#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;
mod paths;
mod setup;
#[cfg(windows)]
mod windows_identity;

#[cfg(test)]
mod capability_tests;

use backend::{
    emergency_teardown_backend_slot, launch_dev_backend, launch_sidecar_backend, print_diagnostic,
    read_ready_payload_from_file, replace_running_backend, resolve_sidecar_path,
    stop_running_backend, BackendEndpoint, BackendLifecycle, BackendManager, BackendStatus,
    RunningBackend,
};
use paths::resolve_desktop_runtime_paths;
use setup::SetupRunnerState;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::Manager;

// A `Starting` backend that never emits a ready payload (wedged startup) is
// force-failed after this deadline so the UI can recover it via restart instead
// of requiring a full app relaunch. Generous enough to cover a cold sidecar
// unpack plus model load; the transition is generation-fenced.
const BACKEND_STARTUP_DEADLINE: Duration = Duration::from_secs(120);

#[derive(Default)]
struct AppState {
    backend_manager: Arc<Mutex<BackendManager>>,
    backend_process: Mutex<Option<RunningBackend>>,
    backend_ready_file: Mutex<Option<PathBuf>>,
    // Serializes the stop+start of every lifecycle transition (restart, app-exit
    // shutdown, initial launch). Held across the whole stop-then-start so two
    // concurrent restarts, or a restart racing app exit, can never interleave and
    // orphan a live sidecar or spawn a survivor.
    restart_lock: Mutex<()>,
    // Latched once app exit begins. A restart that loses the race to acquire
    // `restart_lock` observes this and refuses to spawn a survivor backend.
    shutting_down: AtomicBool,
    // Generation whose unreadable ready file was last reported, so a file that
    // stays unreadable is logged once per launch instead of on every poll.
    ready_file_error_generation: AtomicU64,
}

// Phase of the one-shot exit coordinator. Closing the last window prevents the
// first native exit, hides the UI, and runs cleanup off the event-loop thread;
// only the final programmatic exit (issued once cleanup finishes) is allowed
// through.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
enum ExitPhase {
    #[default]
    Running,
    CleaningUp,
    Finished,
}

// What the event-loop thread should do with an `ExitRequested` in a given phase.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum ExitAction {
    // First request: prevent the exit, hide windows, start background cleanup.
    StartCleanup,
    // Cleanup already running: prevent the exit and keep waiting.
    Prevent,
    // Cleanup finished: let the (final, programmatic) exit proceed.
    Allow,
}

// Pure state decision for the exit coordinator: map the current phase and an
// incoming exit request to an action plus the next phase.
fn next_exit_action(phase: ExitPhase) -> (ExitAction, ExitPhase) {
    match phase {
        ExitPhase::Running => (ExitAction::StartCleanup, ExitPhase::CleaningUp),
        ExitPhase::CleaningUp => (ExitAction::Prevent, ExitPhase::CleaningUp),
        ExitPhase::Finished => (ExitAction::Allow, ExitPhase::Finished),
    }
}

// Serializes exit handling so repeated close requests (double close, OS + user)
// never start cleanup twice or race the final exit.
#[derive(Default)]
struct ExitCoordinator {
    phase: Mutex<ExitPhase>,
    relaunch_requested: AtomicBool,
}

impl ExitCoordinator {
    // Advance the phase for one exit request and report the action to take. On
    // exit the lock is taken even if poisoned; the decision still has to run.
    fn on_exit_requested(&self) -> ExitAction {
        let mut phase = self
            .phase
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let (action, next) = next_exit_action(*phase);
        *phase = next;
        action
    }

    // A relaunch once exit has begun cannot be served by this instance: its
    // window is hidden or destroyed and its backend is shutting down. Record it
    // so the app restarts after cleanup. Returns false while the app is running.
    fn request_relaunch(&self) -> bool {
        let phase = self
            .phase
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if *phase == ExitPhase::Running {
            return false;
        }
        self.relaunch_requested.store(true, Ordering::SeqCst);
        true
    }

    // Mark cleanup complete so the following programmatic exit is allowed.
    // Returns whether a relaunch was requested while cleanup ran.
    fn mark_finished(&self) -> bool {
        let mut phase = self
            .phase
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *phase = ExitPhase::Finished;
        self.relaunch_requested.load(Ordering::SeqCst)
    }
}

fn env_flag(name: &str) -> bool {
    std::env::var(name)
        .map(|value| value.trim().to_ascii_lowercase())
        .map(|value| matches!(value.as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(false)
}

impl AppState {
    fn start_backend(&self, app_handle: &tauri::AppHandle) -> Result<(), String> {
        let _restart = self
            .restart_lock
            .lock()
            .map_err(|_| "backend restart lock poisoned".to_string())?;
        self.start_backend_inner(app_handle)
    }

    // Launch a new backend and install it, stopping any child it displaces.
    // Every launch failure is recorded on the manager so the UI can show why the
    // backend is down, whichever lifecycle transition triggered the launch.
    // Caller must hold `restart_lock`.
    fn start_backend_inner(&self, app_handle: &tauri::AppHandle) -> Result<(), String> {
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("desktop backend is shutting down".to_string());
        }
        self.spawn_and_install_backend(app_handle)
            .inspect_err(|error| {
                if let Ok(mut manager) = self.backend_manager.lock() {
                    manager.mark_failed(error.clone());
                }
            })
    }

    fn spawn_and_install_backend(&self, app_handle: &tauri::AppHandle) -> Result<(), String> {
        let app_data_dir = app_handle
            .path()
            .app_data_dir()
            .map_err(|error| format!("failed to resolve app data directory: {error}"))?;
        let runtime_paths = resolve_desktop_runtime_paths(app_data_dir)?;
        let ready_file_path = runtime_paths.ready_file_path.clone();
        let _ = std::fs::remove_file(&ready_file_path);
        let manager = Arc::clone(&self.backend_manager);

        let use_dev_python = cfg!(debug_assertions) && !env_flag("AUDIOBOOK_DESKTOP_USE_SIDECAR");

        let process = if use_dev_python {
            launch_dev_backend(manager, &runtime_paths)?
        } else {
            let resource_dir = app_handle.path().resource_dir().ok();
            // Windows app virtualization can redirect only the executable into
            // a package cache. Keep the loader's logical path so adjacent
            // sidecar resources remain reachable in the original install.
            #[cfg(target_os = "windows")]
            let current_binary = std::env::current_exe().ok();
            #[cfg(not(target_os = "windows"))]
            let current_binary = tauri::process::current_binary(&app_handle.env()).ok();
            let sidecar_path =
                resolve_sidecar_path(resource_dir.as_deref(), current_binary.as_deref())?;
            launch_sidecar_backend(
                Arc::clone(&self.backend_manager),
                &runtime_paths,
                sidecar_path,
            )?
        };

        // Never overwrite a live child: `replace_running_backend` stops whatever
        // it displaces instead of dropping it. Under `restart_lock` the slot is
        // normally already empty, but this is the last-line orphan guard.
        replace_running_backend(&self.backend_process, &self.backend_manager, process)?;

        let mut ready_file = self
            .backend_ready_file
            .lock()
            .map_err(|_| "backend ready file lock poisoned".to_string())?;
        *ready_file = Some(ready_file_path);
        Ok(())
    }

    // Latch shutdown then stop the backend, so no restart can spawn a survivor.
    fn shutdown(&self) {
        // Latch before waiting for the lock: a restart that is mid-stop when
        // exit begins then refuses to spawn a replacement, instead of exit
        // waiting for a fresh backend to launch only to stop it again.
        self.shutting_down.store(true, Ordering::SeqCst);
        // On exit take the lock even if poisoned; cleanup still has to run.
        let _restart = match self.restart_lock.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        if let Err(error) = self.stop_backend_inner() {
            print_diagnostic(format_args!("failed to stop desktop backend: {error}"));
        }
    }

    // Stop and clear the current child (outside the slot lock, so status
    // polling isn't blocked for the multi-second graceful shutdown). Fails, with
    // the child kept in the slot and its generation failed, when the child did
    // not exit after a forced kill: callers must not start a replacement then.
    // Caller must hold `restart_lock`.
    fn stop_backend_inner(&self) -> Result<(), String> {
        let ready_file_path = self
            .backend_ready_file
            .lock()
            .map_err(|_| "backend ready file lock poisoned".to_string())?
            .take();

        let stopped = stop_running_backend(&self.backend_process, &self.backend_manager);

        if let Some(path) = ready_file_path {
            let _ = std::fs::remove_file(path);
        }
        stopped
    }

    fn refresh_backend_ready_from_file(&self) -> Result<(), String> {
        let generation = {
            let manager = self
                .backend_manager
                .lock()
                .map_err(|_| "backend manager lock poisoned".to_string())?;
            if manager.status().lifecycle != BackendLifecycle::Starting {
                return Ok(());
            }
            manager.generation()
        };

        // Hold the ready-file lock across the existence check, read, and delete:
        // two concurrent status commands could otherwise both pass `is_file()`
        // and the loser's read/delete would fail with NotFound, surfacing as a
        // bootstrap error to the frontend.
        let ready_file = self
            .backend_ready_file
            .lock()
            .map_err(|_| "backend ready file lock poisoned".to_string())?;

        let Some(path) = ready_file.as_ref() else {
            return Ok(());
        };
        if !path.is_file() {
            return Ok(());
        }

        let running = self
            .backend_process
            .lock()
            .map_err(|_| "backend process lock poisoned".to_string())?;
        let Some(process) = running
            .as_ref()
            .filter(|process| process.generation() == generation)
        else {
            return Ok(());
        };
        // stdout is the primary handshake. A file that cannot be read or parsed
        // yet is retried on the next poll (the startup deadline bounds the
        // wait) rather than failing the status command, which the UI treats as
        // a fatal bootstrap error.
        let payload = match read_ready_payload_from_file(path) {
            Ok(payload) => payload,
            Err(error) => {
                if self
                    .ready_file_error_generation
                    .swap(generation, Ordering::SeqCst)
                    != generation
                {
                    print_diagnostic(format_args!(
                        "desktop ready file is not usable yet: {error}"
                    ));
                }
                return Ok(());
            }
        };
        process.adopt_ready(&self.backend_manager, payload)
    }

    fn refresh_backend_generation(&self, generation: u64) -> Result<BackendStatus, String> {
        let _restart = self
            .restart_lock
            .lock()
            .map_err(|_| "backend restart lock poisoned".to_string())?;
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("desktop backend is shutting down".to_string());
        }
        if self
            .backend_manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?
            .generation()
            != generation
        {
            return Err("the restarted backend was replaced during startup".to_string());
        }
        self.refresh_backend_state()?;
        let manager = self
            .backend_manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        Ok(manager.status())
    }

    fn fail_backend_generation(&self, generation: u64, error: &str) {
        if let Ok(_restart) = self.restart_lock.lock() {
            if self.shutting_down.load(Ordering::SeqCst) {
                return;
            }
            let is_current = self
                .backend_manager
                .lock()
                .map(|manager| manager.generation() == generation)
                .unwrap_or(false);
            if is_current {
                let _ = self.stop_backend_inner();
                if let Ok(mut manager) = self.backend_manager.lock() {
                    manager.mark_failed_for_generation(generation, error);
                }
            }
        }
    }

    fn refresh_backend_process_exit(&self) -> Result<(), String> {
        let mut running = self
            .backend_process
            .lock()
            .map_err(|_| "backend process lock poisoned".to_string())?;

        if let Some(process) = running.as_mut() {
            process.refresh_exit_status(&self.backend_manager)?;
        }
        Ok(())
    }

    // Force-fail a wedged `Starting` backend once it exceeds the startup
    // deadline, so a hung startup becomes recoverable via restart.
    fn refresh_backend_startup_deadline(&self) -> Result<(), String> {
        self.enforce_startup_deadline(BACKEND_STARTUP_DEADLINE)
    }

    fn enforce_startup_deadline(&self, deadline: Duration) -> Result<(), String> {
        let timed_out_generation = {
            let mut manager = self
                .backend_manager
                .lock()
                .map_err(|_| "backend manager lock poisoned".to_string())?;
            manager
                .fail_if_startup_timed_out(deadline)
                .then(|| manager.generation())
        };
        let Some(generation) = timed_out_generation else {
            return Ok(());
        };
        // A timed-out generation is never adopted later, so end its process now
        // rather than leave it holding the port, workers and GPU memory until
        // the user restarts. It stays in the slot, like an exited backend, so
        // restart and shutdown still own its teardown.
        let mut running = self
            .backend_process
            .lock()
            .map_err(|_| "backend process lock poisoned".to_string())?;
        if let Some(process) = running
            .as_mut()
            .filter(|process| process.generation() == generation)
        {
            process.kill_after_startup_timeout();
        }
        Ok(())
    }

    /// Bring the backend lifecycle up to date before reading it: adopt a ready
    /// file the child wrote, reap an exited process, and fail a startup that
    /// blew its deadline. Call sites that deliberately skip a step (see
    /// `restart_backend`) spell the individual refreshes out instead.
    fn refresh_backend_state(&self) -> Result<(), String> {
        self.refresh_backend_ready_from_file()?;
        self.refresh_backend_process_exit()?;
        self.refresh_backend_startup_deadline()
    }

    fn status(&self) -> Result<BackendStatus, String> {
        self.refresh_backend_state()?;
        let manager = self
            .backend_manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        Ok(manager.status())
    }

    fn endpoint(&self) -> Result<Option<BackendEndpoint>, String> {
        self.refresh_backend_state()?;
        let manager = self
            .backend_manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        Ok(manager.endpoint())
    }

    // Restart a failed or stopped backend. Holds the restart lock across the
    // entire stop+start so a second restart (double-click) or the app-exit
    // shutdown cannot interleave with this one.
    fn restart(&self, app_handle: &tauri::AppHandle) -> Result<(), String> {
        let _restart = self
            .restart_lock
            .lock()
            .map_err(|_| "backend restart lock poisoned".to_string())?;

        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("desktop backend is shutting down".to_string());
        }

        // Bring the lifecycle up to date (process exit, startup timeout) and
        // re-check it while still holding `restart_lock`, so the decision to
        // restart cannot be raced by the stop/start we are about to perform.
        // Ready-file adoption is skipped on purpose: it can only move a
        // `Starting` backend to `Ready`, and neither state may be restarted.
        self.refresh_backend_process_exit()?;
        self.refresh_backend_startup_deadline()?;
        {
            let manager = self
                .backend_manager
                .lock()
                .map_err(|_| "backend manager lock poisoned".to_string())?;
            if !matches!(
                manager.status().lifecycle,
                BackendLifecycle::Failed | BackendLifecycle::Stopped
            ) {
                return Err("backend is not in a failed or stopped state".to_string());
            }
        }
        self.stop_backend_inner()?;
        self.start_backend_inner(app_handle)
    }
}

// Backend lifecycle commands do blocking work: lock waits, a graceful stop of
// several seconds, and (after runtime setup) a readiness wait of up to
// `BACKEND_STARTUP_DEADLINE`. A sync command would freeze the UI thread, and
// blocking inside an `async` command would stall a shared async-runtime worker,
// so each one runs its synchronous body on the blocking thread pool.
async fn run_blocking<T, F>(app_handle: tauri::AppHandle, operation: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce(&tauri::AppHandle) -> Result<T, String> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(move || operation(&app_handle))
        .await
        .map_err(|error| format!("desktop backend task failed: {error}"))?
}

#[tauri::command]
async fn backend_status(app_handle: tauri::AppHandle) -> Result<BackendStatus, String> {
    run_blocking(app_handle, |app| app.state::<AppState>().status()).await
}

#[tauri::command]
async fn backend_endpoint(app_handle: tauri::AppHandle) -> Result<Option<BackendEndpoint>, String> {
    run_blocking(app_handle, |app| app.state::<AppState>().endpoint()).await
}

#[tauri::command]
async fn restart_backend(app_handle: tauri::AppHandle) -> Result<(), String> {
    run_blocking(app_handle, |app| app.state::<AppState>().restart(app)).await
}

fn restart_after_setup_transaction<Stop, Start>(
    setup_state: &SetupRunnerState,
    backend_lifecycle: BackendLifecycle,
    stop_backend: Stop,
    start_backend: Start,
) -> Result<(), String>
where
    Stop: FnOnce() -> Result<(), String>,
    Start: FnOnce() -> Result<(), String>,
{
    // A failed or stopped backend must stay restartable: a start that failed
    // after the stop leaves it `Failed` with the authorization restored for a
    // retry. Only a launch still in progress is refused.
    if !matches!(
        backend_lifecycle,
        BackendLifecycle::Ready | BackendLifecycle::Failed | BackendLifecycle::Stopped
    ) {
        return Err("backend is not ready for the setup restart".to_string());
    }

    let authorization = setup_state.acquire_restart_authorization()?;
    let result = stop_backend().and_then(|_| start_backend());
    match result {
        Ok(()) => setup_state.complete_restart_authorization(authorization),
        Err(error) => {
            setup_state.restore_restart_authorization(authorization)?;
            Err(error)
        }
    }
}

// Run the stop+start swap under `restart_lock`, then release the lock before
// waiting for the restarted backend to report ready. Holding the lock across the
// (up to `BACKEND_STARTUP_DEADLINE`) readiness wait would make app-exit shutdown
// and any concurrent restart block on the lock for the full deadline when a
// post-setup restart wedges. The swapped-in child is already installed in the
// process slot and orphan-guarded, so shutdown/restart can observe and stop it
// while the readiness wait runs unlocked.
fn restart_after_setup_with_readiness<Swap, Wait, T>(
    restart_lock: &Mutex<()>,
    swap: Swap,
    wait: Wait,
) -> Result<(), String>
where
    Swap: FnOnce() -> Result<T, String>,
    Wait: FnOnce(T) -> Result<(), String>,
{
    let launched = {
        let _restart = restart_lock
            .lock()
            .map_err(|_| "backend restart lock poisoned".to_string())?;
        swap()?
    };
    wait(launched)
}

fn wait_for_backend_ready<Refresh>(
    timeout: Duration,
    poll_interval: Duration,
    mut refresh: Refresh,
) -> Result<(), String>
where
    Refresh: FnMut() -> Result<BackendStatus, String>,
{
    let started = Instant::now();
    loop {
        let status = refresh()?;
        match status.lifecycle {
            BackendLifecycle::Ready => return Ok(()),
            BackendLifecycle::Failed => {
                return Err(status
                    .error
                    .unwrap_or_else(|| "the restarted backend failed during startup".to_string()))
            }
            BackendLifecycle::Stopped => {
                return Err("the restarted backend stopped during startup".to_string())
            }
            BackendLifecycle::Starting => {}
        }
        if started.elapsed() >= timeout {
            return Err(format!(
                "the restarted backend timed out after {} seconds",
                timeout.as_secs()
            ));
        }
        if !poll_interval.is_zero() {
            thread::sleep(poll_interval);
        }
    }
}

#[tauri::command]
async fn restart_backend_after_setup(app_handle: tauri::AppHandle) -> Result<(), String> {
    run_blocking(app_handle, restart_backend_after_setup_blocking).await
}

fn restart_backend_after_setup_blocking(app_handle: &tauri::AppHandle) -> Result<(), String> {
    let app_state = app_handle.state::<AppState>();
    let setup_state = app_handle.state::<SetupRunnerState>();
    restart_after_setup_with_readiness(
        &app_state.restart_lock,
        // The stop+start swap runs under `restart_lock`, mirroring the ordinary
        // `restart_backend`. Its transactional authorization semantics are
        // preserved; only the readiness wait moves outside the lock.
        || {
            if app_state.shutting_down.load(Ordering::SeqCst) {
                return Err("desktop backend is shutting down".to_string());
            }

            app_state.refresh_backend_state()?;
            let lifecycle = app_state
                .backend_manager
                .lock()
                .map_err(|_| "backend manager lock poisoned".to_string())?
                .status()
                .lifecycle;

            restart_after_setup_transaction(
                setup_state.inner(),
                lifecycle,
                || app_state.stop_backend_inner(),
                || app_state.start_backend_inner(app_handle),
            )?;
            let running = app_state
                .backend_process
                .lock()
                .map_err(|_| "backend process lock poisoned".to_string())?;
            running
                .as_ref()
                .map(RunningBackend::generation)
                .ok_or_else(|| "the restarted backend is unavailable".to_string())
        },
        // Each poll briefly takes `restart_lock` to check the captured generation.
        // Release it between polls so shutdown and another restart can interrupt
        // a wedged startup without waiting for the full deadline.
        |generation| {
            let readiness = wait_for_backend_ready(
                BACKEND_STARTUP_DEADLINE,
                Duration::from_millis(100),
                || app_state.refresh_backend_generation(generation),
            );
            if let Err(error) = readiness {
                app_state.fail_backend_generation(generation, &error);
                return Err(error);
            }
            Ok(())
        },
    )
}

#[tauri::command]
fn choose_source_files() -> Vec<String> {
    rfd::FileDialog::new()
        .add_filter("Orpheus Sources", &["pdf", "epub", "txt", "md"])
        .pick_files()
        .unwrap_or_default()
        .into_iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect()
}

/// Drop the Windows verbatim prefix that `canonicalize` adds (`\\?\C:\...`);
/// Explorer and `start` reject such paths.
fn displayable_path(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    match text.strip_prefix(r"\\?\") {
        Some(rest) if rest.as_bytes().get(1) == Some(&b':') => PathBuf::from(rest),
        _ => path,
    }
}

#[tauri::command]
fn open_app_data_folder(app_handle: tauri::AppHandle) -> Result<(), String> {
    let app_data_dir = app_handle
        .path()
        .app_data_dir()
        .map_err(|error| format!("failed to resolve app data directory: {error}"))?;
    let runtime_paths = resolve_desktop_runtime_paths(app_data_dir)?;
    std::fs::create_dir_all(&runtime_paths.app_data_dir)
        .map_err(|error| format!("failed to create app data directory: {error}"))?;
    let app_data_dir = displayable_path(
        runtime_paths
            .app_data_dir
            .canonicalize()
            .map_err(|error| format!("failed to resolve app data directory: {error}"))?,
    );
    open::that(app_data_dir).map_err(|error| format!("failed to open app data folder: {error}"))
}

fn should_register_single_instance(
    runtime_root: Option<&std::ffi::OsStr>,
    release_id: Option<&std::ffi::OsStr>,
) -> bool {
    runtime_root.is_none() && release_id.is_none()
}

fn main() {
    let context = tauri::generate_context!();
    // Set the same identity as the installed shortcuts before any window exists.
    // Otherwise Windows can treat a running Orpheus window as a separate app.
    #[cfg(windows)]
    if let Err(error) = windows_identity::set_app_id(&context.config().identifier) {
        print_diagnostic(format_args!(
            "failed to register taskbar identity: HRESULT {error:#010x}"
        ));
    }

    let builder = tauri::Builder::default();
    let runtime_root = std::env::var_os(paths::QUALIFICATION_RUNTIME_ROOT_ENV);
    let release_id = std::env::var_os(paths::QUALIFICATION_RELEASE_ID_ENV);
    let builder = if should_register_single_instance(runtime_root.as_deref(), release_id.as_deref())
    {
        // Qualification launches must reach runtime-root validation without
        // activating the production window. Python enforces library ownership.
        builder.plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if app
                .try_state::<ExitCoordinator>()
                .is_some_and(|coordinator| coordinator.request_relaunch())
            {
                return;
            }
            if let Some(window) = app.webview_windows().values().next() {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
    } else {
        builder
    };
    let app = builder
        .manage(AppState::default())
        .manage(SetupRunnerState::default())
        .manage(ExitCoordinator::default())
        .setup(|app| {
            // A failed launch is recorded on the backend manager, so the UI
            // reports it and can offer a restart; the app itself still starts.
            if let Err(error) = app.state::<AppState>().start_backend(app.handle()) {
                print_diagnostic(format_args!("failed to launch desktop backend: {error}"));
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_status,
            backend_endpoint,
            restart_backend,
            restart_backend_after_setup,
            choose_source_files,
            open_app_data_folder,
            setup::start_runtime_setup,
            setup::runtime_setup_status,
            setup::runtime_setup_environment,
            setup::cancel_runtime_setup
        ])
        .build(context)
        .expect("error while building tauri application");

    app.run(|app_handle, event| match event {
        // Make close feel immediate: prevent the first native exit, hide the UI
        // now, and run the (multi-second) backend + setup-runner cleanup off the
        // event-loop thread. When cleanup finishes we issue a final programmatic
        // exit, which comes back through here and is allowed once `Finished`.
        tauri::RunEvent::ExitRequested { api, code, .. } => {
            let action = app_handle
                .try_state::<ExitCoordinator>()
                .map(|coordinator| coordinator.on_exit_requested())
                // Without the coordinator we cannot coordinate a bounded exit;
                // fall back to letting the exit proceed.
                .unwrap_or(ExitAction::Allow);

            match action {
                ExitAction::StartCleanup => {
                    api.prevent_exit();
                    let exit_code = code.unwrap_or(0);
                    // Hide every window immediately so the app disappears while
                    // cleanup runs. Hiding an already-hidden window is a no-op.
                    for (_, window) in app_handle.webview_windows() {
                        let _ = window.hide();
                    }

                    let cleanup_handle = app_handle.clone();
                    thread::spawn(move || {
                        if let Some(state) = cleanup_handle.try_state::<AppState>() {
                            state.shutdown();
                        }
                        if let Some(state) = cleanup_handle.try_state::<SetupRunnerState>() {
                            state.shutdown();
                        }
                        let relaunch = cleanup_handle
                            .try_state::<ExitCoordinator>()
                            .is_some_and(|coordinator| coordinator.mark_finished());
                        // Allowed through by the coordinator's `Finished` phase.
                        if relaunch {
                            cleanup_handle.request_restart();
                        } else {
                            cleanup_handle.exit(exit_code);
                        }
                    });
                }
                ExitAction::Prevent => {
                    api.prevent_exit();
                }
                ExitAction::Allow => {}
            }
        }
        // A Dock click while cleanup runs reaches this instance, not a new one.
        #[cfg(target_os = "macos")]
        tauri::RunEvent::Reopen { .. } => {
            if let Some(coordinator) = app_handle.try_state::<ExitCoordinator>() {
                coordinator.request_relaunch();
            }
        }
        // Last-chance backend teardown if ExitRequested cleanup did not drain the
        // slot (Cmd+Q / abrupt exit). Idempotent when ordinary stop already ran.
        tauri::RunEvent::Exit => {
            if let Some(state) = app_handle.try_state::<AppState>() {
                emergency_teardown_backend_slot(&state.backend_process);
                let ready_file = match state.backend_ready_file.lock() {
                    Ok(mut guard) => guard.take(),
                    Err(poisoned) => poisoned.into_inner().take(),
                };
                if let Some(path) = ready_file {
                    let _ = std::fs::remove_file(path);
                }
            }
            // Bounded, and a no-op once ExitRequested cleanup already ran.
            if let Some(state) = app_handle.try_state::<SetupRunnerState>() {
                state.shutdown();
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod exit_coordinator_tests {
    use super::{ExitAction, ExitCoordinator};

    #[test]
    fn coordinator_starts_cleanup_once_then_prevents() {
        let coordinator = ExitCoordinator::default();
        assert_eq!(coordinator.on_exit_requested(), ExitAction::StartCleanup);
        // Repeated close requests during cleanup never restart it.
        assert_eq!(coordinator.on_exit_requested(), ExitAction::Prevent);
        assert_eq!(coordinator.on_exit_requested(), ExitAction::Prevent);
    }

    #[test]
    fn coordinator_allows_exit_after_cleanup_finishes() {
        let coordinator = ExitCoordinator::default();
        assert_eq!(coordinator.on_exit_requested(), ExitAction::StartCleanup);
        coordinator.mark_finished();
        // The final programmatic exit is allowed through.
        assert_eq!(coordinator.on_exit_requested(), ExitAction::Allow);
    }

    #[test]
    fn relaunch_while_running_is_left_to_the_live_window() {
        let coordinator = ExitCoordinator::default();
        assert!(!coordinator.request_relaunch());
        assert_eq!(coordinator.on_exit_requested(), ExitAction::StartCleanup);
        assert!(!coordinator.mark_finished(), "no relaunch was requested");
    }

    #[test]
    fn relaunch_during_cleanup_restarts_after_cleanup() {
        let coordinator = ExitCoordinator::default();
        assert_eq!(coordinator.on_exit_requested(), ExitAction::StartCleanup);
        assert!(coordinator.request_relaunch());
        assert!(coordinator.mark_finished());
        assert_eq!(coordinator.on_exit_requested(), ExitAction::Allow);
    }
}

#[cfg(test)]
mod shutdown_tests {
    use super::AppState;
    use std::sync::atomic::Ordering;
    use std::sync::Arc;
    use std::thread;
    use std::time::{Duration, Instant};

    #[test]
    fn shutdown_latches_before_waiting_for_an_in_flight_restart() {
        let state = Arc::new(AppState::default());
        // Stands in for a restart that is between stopping the old backend
        // and spawning its replacement.
        let in_flight_restart = state.restart_lock.lock().unwrap();
        let exiting = Arc::clone(&state);
        let shutdown = thread::spawn(move || exiting.shutdown());

        let deadline = Instant::now() + Duration::from_secs(5);
        let latched = loop {
            if state.shutting_down.load(Ordering::SeqCst) {
                break true;
            }
            if Instant::now() >= deadline {
                break false;
            }
            thread::sleep(Duration::from_millis(10));
        };
        drop(in_flight_restart);
        shutdown.join().unwrap();

        assert!(
            latched,
            "the restart holding the lock must see shutdown before it spawns a replacement"
        );
    }
}

#[cfg(test)]
mod single_instance_tests {
    use super::should_register_single_instance;
    use std::ffi::OsStr;

    #[test]
    fn qualification_overrides_skip_single_instance_routing() {
        let root = Some(OsStr::new("/tmp/qualification"));
        let release = Some(OsStr::new("release"));
        assert!(should_register_single_instance(None, None));
        for (root, release) in [
            (root, release),
            (root, None),
            (None, release),
            (Some(OsStr::new("")), None),
        ] {
            assert!(!should_register_single_instance(root, release));
        }
    }
}

#[cfg(test)]
mod displayable_path_tests {
    use super::displayable_path;
    use std::path::PathBuf;

    #[test]
    fn strips_verbatim_drive_prefix() {
        assert_eq!(
            displayable_path(PathBuf::from(
                r"\\?\C:\Users\me\AppData\Roaming\com.orpheus.desktop"
            )),
            PathBuf::from(r"C:\Users\me\AppData\Roaming\com.orpheus.desktop")
        );
    }

    #[test]
    fn turns_verbatim_unc_into_plain_unc() {
        assert_eq!(
            displayable_path(PathBuf::from(r"\\?\UNC\server\share\orpheus")),
            PathBuf::from(r"\\server\share\orpheus")
        );
    }

    #[test]
    fn leaves_ordinary_paths_unchanged() {
        let path = PathBuf::from("/Users/me/Library/Application Support/com.orpheus.desktop");
        assert_eq!(displayable_path(path.clone()), path);
    }
}
