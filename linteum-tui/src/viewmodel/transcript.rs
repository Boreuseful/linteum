//! Transcript items and scroll state.

use serde_json::Value;

use crate::protocol::ErrorInfo;

use super::UiAction;

/// The rendered conversation plus its scroll position.
#[derive(Debug, Clone, Default)]
pub struct Transcript {
    pub items: Vec<Item>,
    pub scroll: ScrollState,
    /// Identity of the streaming item, if any. Task 3 uses this for replay.
    pub active_stream: Option<String>,
}

/// Scroll position. `stick_to_bottom` keeps live output pinned.
#[derive(Debug, Clone)]
pub struct ScrollState {
    pub offset: usize,
    pub stick_to_bottom: bool,
}

impl Default for ScrollState {
    fn default() -> Self {
        Self {
            offset: 0,
            stick_to_bottom: true,
        }
    }
}

impl ScrollState {
    /// Apply a scroll action using the rendered transcript metrics.
    ///
    /// The view-model cannot know how markdown and wrapping turn items into
    /// rows, so the UI layer supplies the rendered line count and the visible
    /// height. `offset` stays top-relative, which keeps the reading position
    /// stable when new output is appended while the user is scrolled up.
    pub fn apply(&mut self, action: UiAction, total_lines: usize, viewport: usize) {
        let max = total_lines.saturating_sub(viewport);
        let page = viewport.max(1);
        let current = if self.stick_to_bottom {
            max
        } else {
            self.offset.min(max)
        };

        match action {
            UiAction::ScrollUp => {
                self.offset = current.saturating_sub(1);
                self.stick_to_bottom = max == 0;
            }
            UiAction::ScrollDown => {
                self.offset = current.saturating_add(1).min(max);
                self.stick_to_bottom = self.offset == max;
            }
            UiAction::ScrollPageUp => {
                self.offset = current.saturating_sub(page);
                self.stick_to_bottom = max == 0;
            }
            UiAction::ScrollPageDown => {
                self.offset = current.saturating_add(page).min(max);
                self.stick_to_bottom = self.offset == max;
            }
            UiAction::ScrollTop => {
                self.offset = 0;
                self.stick_to_bottom = false;
            }
            UiAction::ScrollBottom => {
                self.offset = max;
                self.stick_to_bottom = true;
            }
            _ => {}
        }
    }

    /// The first rendered line to draw.
    pub fn visible_offset(&self, total_lines: usize, viewport: usize) -> usize {
        let max = total_lines.saturating_sub(viewport);
        if self.stick_to_bottom {
            max
        } else {
            self.offset.min(max)
        }
    }
}

/// One transcript row group.
#[derive(Debug, Clone)]
pub enum Item {
    User(UserItem),
    Assistant(AssistantItem),
    Tool(ToolCard),
    Approval(ApprovalCard),
    Compaction(CompactionCard),
    Error(ErrorCard),
    Gap(GapMarker),
}

#[derive(Debug, Clone, Default)]
pub struct UserItem {
    pub client_message_id: Option<String>,
    pub text: String,
}

#[derive(Debug, Clone, Default)]
pub struct AssistantItem {
    pub text: String,
    pub reasoning: String,
    pub final_message: Option<Value>,
    pub interrupted: bool,
    pub usage: Option<Value>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub round: Option<u64>,
}

#[derive(Debug, Clone, Default)]
pub struct ToolCard {
    pub call_id: String,
    pub name: String,
    pub side_effect: Option<String>,
    pub state: ToolState,
    pub args: Option<String>,
    pub output: Option<String>,
    pub output_ref: Option<String>,
    pub error: Option<ErrorInfo>,
    pub dedup: bool,
    pub safety: bool,
    pub expanded: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ToolState {
    #[default]
    Requested,
    Started,
    Streaming,
    Success,
    Failure,
    Denied,
    Cancelled,
}

#[derive(Debug, Clone, Default)]
pub struct ApprovalCard {
    pub call_id: String,
    pub name: String,
    pub side_effect: Option<String>,
    pub args_preview: String,
    pub requested_at: Option<f64>,
    pub expires_at: Option<f64>,
    pub decision: Option<String>,
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct CompactionCard {
    pub seq: u64,
    pub from_index: u64,
    pub to_index: u64,
    pub summary: Option<String>,
    pub summary_ref: Option<String>,
    pub tokens: Option<u64>,
    pub summarizer: Option<String>,
    pub fallback: bool,
    pub error: Option<ErrorInfo>,
}

#[derive(Debug, Clone, Default)]
pub struct ErrorCard {
    pub seq: Option<u64>,
    pub code: String,
    pub message: String,
    pub error_info: Option<ErrorInfo>,
}

#[derive(Debug, Clone, Default)]
pub struct GapMarker {
    pub from_seq: u64,
    pub to_seq: u64,
}
