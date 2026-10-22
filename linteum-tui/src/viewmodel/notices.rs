//! Notice routing into activity, banners, and paused state.

use serde_json::Value;

use crate::protocol::ErrorInfo;

use super::state::TuiState;
use super::transcript::{Item, ToolState};

/// A routed notice. The view-model currently keeps the kind/text and lets the
/// UI decide how much detail to render.
#[derive(Debug, Clone)]
pub struct Notice {
    pub kind: String,
    pub text: String,
    pub error_info: Option<ErrorInfo>,
}

pub fn route_notice(state: &mut TuiState, payload: &Value) {
    let kind = payload
        .get("kind")
        .and_then(Value::as_str)
        .unwrap_or("notice");
    let text = payload
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| payload.to_string());

    if kind == "budget"
        && let Some(session) = state.session.as_mut()
    {
        session.phase = "paused".to_owned();
    }

    if kind == "interrupt" {
        for item in state.transcript.items.iter_mut() {
            if let Item::Tool(card) = item
                && matches!(
                    card.state,
                    ToolState::Requested | ToolState::Started | ToolState::Streaming
                )
            {
                card.state = ToolState::Cancelled;
            }
        }
        for card in state.pending_tools.values_mut() {
            if matches!(
                card.state,
                ToolState::Requested | ToolState::Started | ToolState::Streaming
            ) {
                card.state = ToolState::Cancelled;
            }
        }
    }

    state.activity.push(kind, text);
}
