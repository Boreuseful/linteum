//! Client → gateway commands.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use super::{PROTOCOL_VERSION, ProtocolError};

fn default_version() -> u8 {
    PROTOCOL_VERSION
}

/// A whitelisted client → gateway command (`kind:"cmd"`).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Command {
    #[serde(default = "default_version")]
    pub v: u8,
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub args: Map<String, Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub trace_id: Option<String>,
}

/// Build a command with the protocol version and no trace id.
pub fn command(
    id: impl Into<String>,
    name: &str,
    args: Map<String, Value>,
    session_id: Option<String>,
) -> Command {
    Command {
        v: PROTOCOL_VERSION,
        id: id.into(),
        name: name.to_owned(),
        args,
        session_id,
        trace_id: None,
    }
}

/// Serialize a command as one JSON line/message, adding `kind:"cmd"`.
pub fn encode_command(command: &Command) -> Result<String, ProtocolError> {
    let mut value = serde_json::to_value(command)?;
    let object = value
        .as_object_mut()
        .ok_or_else(|| ProtocolError::Invalid("command is not a JSON object".into()))?;
    object.insert("kind".into(), Value::String("cmd".into()));
    Ok(serde_json::to_string(&value)?)
}
