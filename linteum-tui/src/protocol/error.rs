//! Structured errors and the standalone control-error frame.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// The shared `error_info` shape (bus, gateway, tool, and agent errors).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErrorInfo {
    pub code: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub location: String,
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub exc_type: Option<String>,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub retryable: bool,
    #[serde(default)]
    pub enters_history: bool,
    #[serde(default)]
    pub context: Value,
}

/// A connection-level `kind:"error"` frame (no `reply_to`).
#[derive(Debug, Clone, Deserialize)]
pub struct ControlError {
    pub error_info: ErrorInfo,
}

/// Codec / envelope errors.
#[derive(Debug, thiserror::Error)]
pub enum ProtocolError {
    #[error("invalid JSON envelope: {0}")]
    Json(#[from] serde_json::Error),
    #[error("invalid envelope: {0}")]
    Invalid(String),
}
