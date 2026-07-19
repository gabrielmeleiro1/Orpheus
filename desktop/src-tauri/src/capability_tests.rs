//! Guards the Tauri ACL that the webview's event bridge depends on.
//!
//! Tauri rejects every `plugin:*` IPC command that no capability grants. The
//! runtime-setup page and the drag-and-drop queue both rely on
//! `@tauri-apps/api/event`'s `listen`, which dispatches `plugin:event|listen`,
//! so an empty capability set silently disables both.

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;

use serde_json::Value;

const MAIN_WINDOW: &str = "main";

fn generated_capabilities() -> BTreeMap<String, Value> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("gen")
        .join("schemas")
        .join("capabilities.json");
    let raw = fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("failed to read {}: {err}", path.display()));
    serde_json::from_str(&raw)
        .unwrap_or_else(|err| panic!("failed to parse {}: {err}", path.display()))
}

fn grants_main_window(capability: &Value) -> bool {
    match capability.get("windows") {
        // A capability without an explicit window list applies to every window.
        None => true,
        Some(Value::Array(windows)) => windows
            .iter()
            .filter_map(Value::as_str)
            .any(|window| window == MAIN_WINDOW || window == "*"),
        _ => false,
    }
}

fn permissions(capability: &Value) -> Vec<String> {
    let Some(Value::Array(entries)) = capability.get("permissions") else {
        return Vec::new();
    };
    entries
        .iter()
        .filter_map(|entry| match entry {
            Value::String(identifier) => Some(identifier.clone()),
            Value::Object(map) => map.get("identifier").and_then(Value::as_str).map(str::to_string),
            _ => None,
        })
        .collect()
}

#[test]
fn generated_acl_is_not_empty() {
    let capabilities = generated_capabilities();
    assert!(
        !capabilities.is_empty(),
        "no Tauri capability is defined, so every plugin: IPC command is rejected by the ACL"
    );
}

#[test]
fn main_window_can_listen_to_events() {
    let capabilities = generated_capabilities();
    let granted: Vec<String> = capabilities
        .values()
        .filter(|capability| grants_main_window(capability))
        .flat_map(permissions)
        .collect();

    let allows_listen = granted.iter().any(|permission| {
        permission == "core:default"
            || permission == "core:event:default"
            || permission == "core:event:allow-listen"
    });

    assert!(
        allows_listen,
        "main window cannot call plugin:event|listen; granted permissions: {granted:?}"
    );
}
