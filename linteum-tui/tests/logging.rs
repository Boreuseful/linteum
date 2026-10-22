use linteum_tui::logging::{init_logging, redact_json};
use serde_json::json;

#[test]
fn log_redacts_secret_keys() {
    let value = json!({
        "api_key": "secret",
        "nested": {"token": "x", "ok": "v"},
        "text": "hello"
    });
    let redacted = redact_json(&value, true);
    assert_eq!(redacted["api_key"], "***");
    assert_eq!(redacted["nested"]["token"], "***");
    assert_eq!(redacted["nested"]["ok"], "v");
    assert_eq!(redacted["text"], "hello");
}

#[test]
fn debug_content_controls_payload_logging() {
    let value = json!({"api_key": "secret"});
    assert_eq!(redact_json(&value, false), serde_json::Value::Null);
    let redacted = redact_json(&value, true);
    assert_eq!(redacted["api_key"], "***");
}

#[test]
fn init_logging_writes_to_the_file() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("logs/tui.log");
    let guard = init_logging(&path, "info", false).unwrap();
    tracing::info!("hello-log");
    drop(guard);
    let contents = std::fs::read_to_string(&path).unwrap();
    assert!(contents.contains("hello-log"));
}
