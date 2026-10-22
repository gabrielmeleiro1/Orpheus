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
            Value::Object(map) => map
                .get("identifier")
                .and_then(Value::as_str)
                .map(str::to_string),
            _ => None,
        })
        .collect()
}

fn release_csp_directives() -> BTreeMap<String, Vec<String>> {
    let config: Value = serde_json::from_str(include_str!("../tauri.conf.json"))
        .expect("tauri.conf.json is valid JSON");
    let csp = config["app"]["security"]["csp"]
        .as_str()
        .expect("a release CSP is configured");
    csp.split(';')
        .filter_map(|directive| {
            let mut parts = directive.split_whitespace();
            let name = parts.next()?.to_string();
            Some((name, parts.map(str::to_string).collect()))
        })
        .collect()
}

#[test]
fn release_csp_grants_only_used_sources() {
    let directives = release_csp_directives();
    let sources = |name: &str| directives.get(name).cloned().unwrap_or_default();

    for (name, values) in &directives {
        assert!(
            !values.iter().any(|value| value == "'unsafe-eval'"),
            "{name} allows eval"
        );
    }
    assert_eq!(sources("script-src"), ["'self'"]);
    assert_eq!(sources("object-src"), ["'none'"]);
    assert_eq!(sources("font-src"), ["'self'"], "fonts are bundled files");
    // The backend is the only network peer: loopback API and SSE, tokenized
    // audio, plus blob previews and the bundled CSS data-URI icon.
    assert_eq!(
        sources("connect-src"),
        [
            "'self'",
            "ipc:",
            "http://ipc.localhost",
            "http://127.0.0.1:*"
        ]
    );
    assert_eq!(
        sources("media-src"),
        ["'self'", "http://127.0.0.1:*", "blob:"]
    );
    assert_eq!(sources("img-src"), ["'self'", "data:", "blob:"]);
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
