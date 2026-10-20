//! TUI log file and payload redaction.

use std::path::Path;

use serde_json::Value;
use tracing_appender::non_blocking::WorkerGuard;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

#[derive(Debug, thiserror::Error)]
pub enum LogError {
    #[error("io: {0}")]
    Io(String),
    #[error("filter: {0}")]
    Filter(String),
}

pub struct LogGuard {
    _guard: WorkerGuard,
}

/// Install a non-blocking file subscriber. If a global subscriber already
/// exists (for example in a test process), logging continues through that one;
/// the returned guard still owns this writer.
pub fn init_logging(path: &Path, level: &str, content: bool) -> Result<LogGuard, LogError> {
    let _ = content; // callers redact payloads with `redact_json` before logging.
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|error| LogError::Io(error.to_string()))?;
    }
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| LogError::Io(error.to_string()))?;
    let (writer, guard) = tracing_appender::non_blocking(file);
    let filter = tracing_subscriber::EnvFilter::try_new(level)
        .map_err(|error| LogError::Filter(error.to_string()))?;
    let _ = tracing_subscriber::registry()
        .with(filter)
        .with(
            tracing_subscriber::fmt::layer()
                .with_writer(writer)
                .with_ansi(false),
        )
        .try_init();
    tracing::info!(level = %level, content = content, "TUI logging initialized");
    Ok(LogGuard { _guard: guard })
}

/// With content logging off, callers log no payload at all. With it on,
/// secret-looking keys are masked recursively.
pub fn redact_json(value: &Value, content: bool) -> Value {
    if !content {
        return Value::Null;
    }
    redact(value)
}

fn redact(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut output = serde_json::Map::new();
            for (key, value) in map {
                if is_secret(key) {
                    output.insert(key.clone(), Value::String("***".to_owned()));
                } else {
                    output.insert(key.clone(), redact(value));
                }
            }
            Value::Object(output)
        }
        Value::Array(items) => Value::Array(items.iter().map(redact).collect()),
        other => other.clone(),
    }
}

fn is_secret(key: &str) -> bool {
    let key = key.to_ascii_lowercase();
    [
        "key",
        "token",
        "secret",
        "password",
        "auth",
        "cookie",
        "credential",
    ]
    .iter()
    .any(|needle| key.contains(needle))
}
