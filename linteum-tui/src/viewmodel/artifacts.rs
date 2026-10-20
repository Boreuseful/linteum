//! Artifact viewer state: base64 chunks, UTF-8 text, binary detection.

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use serde_json::Value;

use crate::protocol::ErrorInfo;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArtifactError {
    Binary,
    Decode,
    Missing,
}

#[derive(Debug, Clone, Default)]
pub struct ArtifactViewerState {
    pub reference: String,
    pub offset: u64,
    pub size: u64,
    pub text: String,
    pub eof: bool,
    pub binary: bool,
    pub error: Option<ErrorInfo>,
}

impl ArtifactViewerState {
    pub fn apply_chunk(&mut self, payload: &Value) -> Result<(), ArtifactError> {
        if let Some(reference) = payload.get("ref").and_then(Value::as_str) {
            self.reference = reference.to_owned();
        }
        if let Some(offset) = payload.get("offset").and_then(Value::as_u64) {
            self.offset = offset;
        }
        if let Some(size) = payload.get("size").and_then(Value::as_u64) {
            self.size = size;
        }
        let data = payload
            .get("data")
            .and_then(Value::as_str)
            .ok_or(ArtifactError::Decode)?;
        let bytes = STANDARD.decode(data).map_err(|_| ArtifactError::Decode)?;
        if bytes.contains(&0) {
            self.binary = true;
            return Err(ArtifactError::Binary);
        }
        self.binary = false;
        self.text.push_str(&String::from_utf8_lossy(&bytes));
        self.eof = payload.get("eof").and_then(Value::as_bool).unwrap_or(false);
        Ok(())
    }
}
