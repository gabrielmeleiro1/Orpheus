#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;
mod paths;

use backend::{
    launch_dev_backend, launch_sidecar_backend, read_ready_payload_from_file, resolve_sidecar_path,
    BackendEndpoint, BackendManager, BackendStatus, RunningBackend,
};
use paths::DesktopRuntimePaths;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tauri::{Manager, State};

#[derive(Default)]
struct AppState {
    backend_manager: Arc<Mutex<BackendManager>>,
    backend_process: Mutex<Option<RunningBackend>>,
    backend_ready_file: Mutex<Option<PathBuf>>,
}

fn env_flag(name: &str) -> bool {
    std::env::var(name)
        .map(|value| value.trim().to_ascii_lowercase())
        .map(|value| matches!(value.as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(false)
}

impl AppState {
    fn start_backend(&self, app_handle: &tauri::AppHandle) -> Result<(), String> {
        let app_data_dir = app_handle
            .path()
            .app_data_dir()
            .map_err(|error| format!("failed to resolve app data directory: {error}"))?;
        let runtime_paths = DesktopRuntimePaths::from_app_data_dir(app_data_dir);
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

        let mut running = self
            .backend_process
            .lock()
            .map_err(|_| "backend process lock poisoned".to_string())?;
        *running = Some(process);

        let mut ready_file = self
            .backend_ready_file
            .lock()
            .map_err(|_| "backend ready file lock poisoned".to_string())?;
        *ready_file = Some(ready_file_path);
        Ok(())
    }

    fn stop_backend(&self) -> Result<(), String> {
        let ready_file_path = self
            .backend_ready_file
            .lock()
            .map_err(|_| "backend ready file lock poisoned".to_string())?
            .take();

        let mut running = self
            .backend_process
            .lock()
            .map_err(|_| "backend process lock poisoned".to_string())?;

        if let Some(process) = running.take() {
            process.stop(&self.backend_manager);
            if let Some(path) = ready_file_path {
                let _ = std::fs::remove_file(path);
            }
            return Ok(());
        }

        let mut manager = self
            .backend_manager
            .lock()
            .map_err(|_| "backend manager lock poisoned".to_string())?;
        manager.mark_stopped();
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

        let ready_file_path = self
            .backend_ready_file
            .lock()
            .map_err(|_| "backend ready file lock poisoned".to_string())?
            .clone();

        let Some(path) = ready_file_path else {
            return Ok(());
        };
        if !path.is_file() {
            return Ok(());
        }

        let payload = read_ready_payload_from_file(&path)?;
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
}

#[tauri::command]
fn backend_status(state: State<'_, AppState>) -> Result<BackendStatus, String> {
    state.refresh_backend_ready_from_file()?;
    state.refresh_backend_process_exit()?;
    let manager = state
        .backend_manager
        .lock()
        .map_err(|_| "backend manager lock poisoned".to_string())?;
    Ok(manager.status())
}

#[tauri::command]
fn backend_endpoint(state: State<'_, AppState>) -> Result<Option<BackendEndpoint>, String> {
    state.refresh_backend_ready_from_file()?;
    state.refresh_backend_process_exit()?;
    let manager = state
        .backend_manager
        .lock()
        .map_err(|_| "backend manager lock poisoned".to_string())?;
    Ok(manager.endpoint())
}

#[tauri::command]
fn choose_source_files() -> Vec<String> {
    rfd::FileDialog::new()
        .add_filter(
            "Orpheus Sources",
            &["pdf", "epub", "txt", "md", "markdown"],
        )
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
    std::fs::create_dir_all(&app_data_dir)
        .map_err(|error| format!("failed to create app data directory: {error}"))?;
    let app_data_dir = app_data_dir
        .canonicalize()
        .map_err(|error| format!("failed to resolve app data directory: {error}"))?;
    open::that(app_data_dir).map_err(|error| format!("failed to open app data folder: {error}"))
}

fn main() {
    let app = tauri::Builder::default()
        .manage(AppState::default())
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
            choose_source_files,
            open_app_data_folder
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        if matches!(
            event,
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit
        ) {
            if let Some(state) = app_handle.try_state::<AppState>() {
                if let Err(error) = state.stop_backend() {
                    eprintln!("failed to stop desktop backend: {error}");
                }
            }
        }
    });
}
