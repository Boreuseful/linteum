//! Gateway → client results and frames.

use serde::Deserialize;
use serde_json::Value;

use super::{ErrorInfo, PROTOCOL_VERSION};

fn default_version() -> u8 {
    PROTOCOL_VERSION
}

/// The reply to one command (`kind:"result"`).
#[derive(Debug, Clone, Deserialize)]
pub struct CommandResult {
    #[serde(default = "default_version")]
    pub v: u8,
    pub id: String,
    pub reply_to: String,
    #[serde(default)]
    pub ok: bool,
    #[serde(default)]
    pub result: Value,
    #[serde(default)]
    pub error: Option<ErrorInfo>,
}

/// A semantic or control frame (`kind:"frame"`).
#[derive(Debug, Clone, Deserialize)]
pub struct Frame {
    #[serde(default = "default_version")]
    pub v: u8,
    pub id: String,
    pub frame: String,
    #[serde(default)]
    pub payload: Value,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub seq: Option<u64>,
    #[serde(default)]
    pub ephemeral: bool,
    #[serde(default)]
    pub replay: bool,
    #[serde(default)]
    pub ordinal: u32,
}
