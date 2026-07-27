#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;
mod paths;
mod setup;

#[cfg(test)]
mod capability_tests;
#[cfg(test)]
mod setup_tests;

use backend::{
    emergency_teardown_backend_slot, launch_dev_backend, launch_sidecar_backend,
    read_ready_payload_from_file, replace_running_backend, resolve_sidecar_path, BackendEndpoint,
    BackendLifecycle, BackendManager, BackendStatus, RunningBackend,
};
use paths::resolve_desktop_runtime_paths;
use setup::SetupRunnerState;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{Manager, State};

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
// incoming exit request to an action plus the next phase. Kept free of any Tauri
// types so it can be unit-tested directly.
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

    // Mark cleanup complete so the following programmatic exit is allowed.
    fn mark_finished(&self) {
        let mut phase = self
            .phase
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *phase = ExitPhase::Finished;
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
    // Caller must hold `restart_lock`.
    fn start_backend_inner(&self, app_handle: &tauri::AppHandle) -> Result<(), String> {
        if self.shutting_down.load(Ordering::SeqCst) {
            return Err("desktop backend is shutting down".to_string());
        }

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
            let current_binary = tauri::process::current_binary(&app_handle.env()).ok();
            let sidecar_path =
                resolve_sidecar_path(resource_dir.as_deref(), current_binary.as_deref())
                    .inspect_err(|error| {
                        if let Ok(mut guard) = self.backend_manager.lock() {
                            guard.mark_failed(error.clone());
                        }
                    })?;
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
        // On exit take the lock even if poisoned; cleanup still has to run.
        let _restart = match self.restart_lock.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        self.shutting_down.store(true, Ordering::SeqCst);
        if let Err(error) = self.stop_backend_inner() {
            eprintln!("failed to stop desktop backend: {error}");
        }
    }

    // Stop and clear the current child. Takes the child out from under the lock
    // and stops it outside the lock so status polling isn't blocked for the whole
    // (multi-second) graceful shutdown. Caller must hold `restart_lock`.
    fn stop_backend_inner(&self) -> Result<(), String> {
        let ready_file_path = self
            .backend_ready_file
            .lock()
            .map_err(|_| "backend ready file lock poisoned".to_string())?
            .take();

        let process = self
            .backend_process
            .lock()
            .map_err(|_| "backend process lock poisoned".to_string())?
            .take();

        if let Some(process) = process {
            process.stop(&self.backend_manager);
        } else {
            let mut manager = self
                .backend_manager
                .lock()
                .map_err(|_| "backend manager lock poisoned".to_string())?;
            manager.mark_stopped();
        }

        if let Some(path) = ready_file_path {
            let _ = std::fs::remove_file(path);
        }
        Ok(())
    }

    fn refresh_backend_ready_from_file(&self) -> Result<(), String> {
        let should_refresh = {
            let manager = self
                .backend_manager
                .lock()
                .map_err(|_| "backend manager lock poisoned".to_string())?;
            manager.status().lifecycle == backend::BackendLifecycle::Starting
                && manager.endpoint().is_none()
        };

        if !should_refresh {
            return Ok(());
        }

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

        let payload = read_ready_payload_from_file(path)?;
        let mut manager = self
            .backend_manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        if manager.endpoint().is_none() {
            manager.mark_ready(payload);
        }
        Ok(())
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
        let mut manager = self
            .backend_manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        manager.fail_if_startup_timed_out(BACKEND_STARTUP_DEADLINE);
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
}

// Backend lifecycle commands are `async` so Tauri runs them off the main
// thread: `restart_backend` in particular blocks for the whole graceful stop
// (several seconds against a wedged backend) and would freeze the UI as a
// sync command. The bodies stay synchronous; only the executor changes.
#[tauri::command]
async fn backend_status(state: State<'_, AppState>) -> Result<BackendStatus, String> {
    state.refresh_backend_state()?;
    let manager = state
        .backend_manager
        .lock()
        .map_err(|_| "backend manager lock poisoned".to_string())?;
    Ok(manager.status())
}

#[tauri::command]
async fn backend_endpoint(state: State<'_, AppState>) -> Result<Option<BackendEndpoint>, String> {
    state.refresh_backend_state()?;
    let manager = state
        .backend_manager
        .lock()
        .map_err(|_| "backend manager lock poisoned".to_string())?;
    Ok(manager.endpoint())
}

#[tauri::command]
async fn restart_backend(
    app_handle: tauri::AppHandle,
    state: State<'_, AppState>,
) -> Result<(), String> {
    // Hold the restart lock across the entire stop+start so a second restart
    // (double-click) or the app-exit shutdown cannot interleave with this one.
    let _restart = state
        .restart_lock
        .lock()
        .map_err(|_| "backend restart lock poisoned".to_string())?;

    if state.shutting_down.load(Ordering::SeqCst) {
        return Err("desktop backend is shutting down".to_string());
    }

    // Bring the lifecycle up to date (process exit, startup timeout) and re-check
    // it while still holding `restart_lock`, so the decision to restart cannot be
    // raced by the stop/start we are about to perform.
    state.refresh_backend_process_exit()?;
    state.refresh_backend_startup_deadline()?;
    {
        let manager = state
            .backend_manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        let lifecycle = manager.status().lifecycle;
        if !matches!(
            lifecycle,
            backend::BackendLifecycle::Failed | backend::BackendLifecycle::Stopped
        ) {
            return Err("backend is not in a failed or stopped state".to_string());
        }
    }
    state.stop_backend_inner()?;
    state.start_backend_inner(&app_handle)
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
    if backend_lifecycle != BackendLifecycle::Ready {
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
fn restart_after_setup_with_readiness<Swap, Wait>(
    restart_lock: &Mutex<()>,
    swap: Swap,
    wait: Wait,
) -> Result<(), String>
where
    Swap: FnOnce() -> Result<(), String>,
    Wait: FnOnce() -> Result<(), String>,
{
    {
        let _restart = restart_lock
            .lock()
            .map_err(|_| "backend restart lock poisoned".to_string())?;
        swap()?;
    }
    wait()
}

fn wait_for_backend_ready<Refresh>(
    timeout: Duration,
    poll_interval: Duration,
    mut refresh: Refresh,
) -> Result<(), String>
where
    Refresh: FnMut() -> Result<BackendLifecycle, String>,
{
    let started = Instant::now();
    loop {
        match refresh()? {
            BackendLifecycle::Ready => return Ok(()),
            BackendLifecycle::Failed => {
                return Err("the restarted backend failed during startup".to_string())
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
async fn restart_backend_after_setup(
    app_handle: tauri::AppHandle,
    app_state: State<'_, AppState>,
    setup_state: State<'_, SetupRunnerState>,
) -> Result<(), String> {
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
                || {
                    app_state
                        .start_backend_inner(&app_handle)
                        .inspect_err(|error| {
                            if let Ok(mut manager) = app_state.backend_manager.lock() {
                                manager.mark_failed(error.clone());
                            }
                        })
                },
            )
        },
        // Readiness wait runs without `restart_lock`, so app-exit shutdown (which
        // grabs the lock, then stops the orphan-guarded child) can interrupt a
        // wedged startup instead of blocking on the lock for the full deadline.
        || {
            let readiness = wait_for_backend_ready(
                BACKEND_STARTUP_DEADLINE,
                Duration::from_millis(100),
                || {
                    app_state.refresh_backend_state()?;
                    let manager = app_state
                        .backend_manager
                        .lock()
                        .map_err(|_| "backend manager lock poisoned".to_string())?;
                    Ok(manager.status().lifecycle)
                },
            );
            if let Err(error) = readiness {
                // Re-take `restart_lock` for the cleanup stop (its contract), and
                // defer to a shutdown already in progress rather than racing it.
                if let Ok(_restart) = app_state.restart_lock.lock() {
                    if !app_state.shutting_down.load(Ordering::SeqCst) {
                        let _ = app_state.stop_backend_inner();
                        if let Ok(mut manager) = app_state.backend_manager.lock() {
                            manager.mark_failed(error.clone());
                        }
                    }
                }
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

#[tauri::command]
fn open_app_data_folder(app_handle: tauri::AppHandle) -> Result<(), String> {
    let app_data_dir = app_handle
        .path()
        .app_data_dir()
        .map_err(|error| format!("failed to resolve app data directory: {error}"))?;
    let runtime_paths = resolve_desktop_runtime_paths(app_data_dir)?;
    std::fs::create_dir_all(&runtime_paths.app_data_dir)
        .map_err(|error| format!("failed to create app data directory: {error}"))?;
    let app_data_dir = runtime_paths
        .app_data_dir
        .canonicalize()
        .map_err(|error| format!("failed to resolve app data directory: {error}"))?;
    open::that(app_data_dir).map_err(|error| format!("failed to open app data folder: {error}"))
}

fn main() {
    let app = tauri::Builder::default()
        .manage(AppState::default())
        .manage(SetupRunnerState::default())
        .manage(ExitCoordinator::default())
        .setup(|app| {
            let state = app.state::<AppState>();
            if let Err(error) = state.start_backend(app.handle()) {
                if let Ok(mut guard) = state.backend_manager.lock() {
                    guard.mark_failed(error.clone());
                }
                eprintln!("failed to launch desktop backend: {error}");
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
        .build(tauri::generate_context!())
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
                        if let Some(coordinator) = cleanup_handle.try_state::<ExitCoordinator>() {
                            coordinator.mark_finished();
                        }
                        // Allowed through by the coordinator's `Finished` phase.
                        cleanup_handle.exit(exit_code);
                    });
                }
                ExitAction::Prevent => {
                    api.prevent_exit();
                }
                ExitAction::Allow => {}
            }
        }
        // Last-chance backend teardown if ExitRequested cleanup did not drain the
        // slot (Cmd+Q / abrupt exit). Idempotent when ordinary stop already ran.
        tauri::RunEvent::Exit => {
            if let Some(state) = app_handle.try_state::<AppState>() {
                emergency_teardown_backend_slot(&state.backend_process);
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod exit_coordinator_tests {
    use super::{next_exit_action, ExitAction, ExitCoordinator, ExitPhase};

    #[test]
    fn first_request_starts_cleanup() {
        assert_eq!(
            next_exit_action(ExitPhase::Running),
            (ExitAction::StartCleanup, ExitPhase::CleaningUp)
        );
    }

    #[test]
    fn request_while_cleaning_up_only_prevents() {
        assert_eq!(
            next_exit_action(ExitPhase::CleaningUp),
            (ExitAction::Prevent, ExitPhase::CleaningUp)
        );
    }

    #[test]
    fn request_after_cleanup_is_allowed() {
        assert_eq!(
            next_exit_action(ExitPhase::Finished),
            (ExitAction::Allow, ExitPhase::Finished)
        );
    }

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
}

#[cfg(test)]
mod qualification_runtime_resolve_tests {
    use crate::paths::{
        resolve_desktop_runtime_paths_with_env, DesktopRuntimePaths, QUALIFICATION_SENTINEL_NAME,
    };
    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn unique_temp_dir(label: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("unix epoch")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("orpheus-main-paths-{label}-{nanos}"));
        fs::create_dir_all(&path).expect("temp dir");
        path
    }

    #[test]
    fn start_backend_path_resolution_honors_validated_qualification_root() {
        let live = unique_temp_dir("live");
        let runtime = unique_temp_dir("qual");
        fs::write(runtime.join(QUALIFICATION_SENTINEL_NAME), "1.0.0-final\n").expect("sentinel");

        let resolved = resolve_desktop_runtime_paths_with_env(
            &live,
            Some(runtime.as_os_str().to_owned()),
            Some(std::ffi::OsString::from("1.0.0-final")),
        )
        .expect("qualification resolve");

        assert_eq!(
            resolved.app_data_dir,
            runtime.canonicalize().expect("canonical")
        );
        assert_ne!(resolved, DesktopRuntimePaths::from_app_data_dir(&live));

        let _ = fs::remove_dir_all(live);
        let _ = fs::remove_dir_all(runtime);
    }

    #[test]
    fn incomplete_qualification_override_does_not_fall_back_for_backend_start() {
        let live = unique_temp_dir("live-incomplete");
        let err = resolve_desktop_runtime_paths_with_env(
            &live,
            Some(std::ffi::OsString::from("/tmp/unused-qual-root")),
            None,
        )
        .expect_err("incomplete pair must fail closed");
        assert!(err.contains("ORPHEUS_QUALIFICATION_RELEASE_ID"), "{err}");
        assert!(err.to_ascii_lowercase().contains("refusing"), "{err}");
        let _ = fs::remove_dir_all(live);
    }
}
