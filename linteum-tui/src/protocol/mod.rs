//! Gateway Protocol v1 envelopes.
//!
//! The wire contract is owned by `docs/gateway-protocol.md` in the Python
//! repository. This module only models the `cmd` / `result` / `frame` / `error`
//! envelopes and preserves unknown kinds for diagnostics.

pub mod command;
pub mod error;
pub mod frame;

pub use command::{Command, command, encode_command};
pub use error::{ControlError, ErrorInfo, ProtocolError};
pub use frame::{CommandResult, Frame};

use serde_json::Value;

/// The only supported protocol major version.
pub const PROTOCOL_VERSION: u8 = 1;

/// A decoded gateway envelope.
#[derive(Debug, Clone)]
pub enum Envelope {
    Cmd(Command),
    Result(CommandResult),
    Frame(Frame),
    Error(ControlError),
    Unknown { kind: String, body: Value },
}

/// Decode one JSON line/message from the gateway.
///
/// The `kind` tag is removed before the typed deserialization so that unknown
/// fields are ignored and unknown kinds are retained verbatim.
pub fn decode_envelope(line: &str) -> Result<Envelope, ProtocolError> {
    let mut value: Value = serde_json::from_str(line)?;
    let object = value
        .as_object_mut()
        .ok_or_else(|| ProtocolError::Invalid("envelope must be a JSON object".into()))?;
    let kind_value = object
        .remove("kind")
        .ok_or_else(|| ProtocolError::Invalid("envelope is missing kind".into()))?;
    let kind = kind_value
        .as_str()
        .ok_or_else(|| ProtocolError::Invalid("envelope kind must be a string".into()))?
        .to_owned();

    match kind.as_str() {
        "cmd" => Ok(Envelope::Cmd(serde_json::from_value(value)?)),
        "result" => Ok(Envelope::Result(serde_json::from_value(value)?)),
        "frame" => Ok(Envelope::Frame(serde_json::from_value(value)?)),
        "error" => Ok(Envelope::Error(serde_json::from_value(value)?)),
        _ => Ok(Envelope::Unknown { kind, body: value }),
    }
}
