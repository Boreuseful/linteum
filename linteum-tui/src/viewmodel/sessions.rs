//! Session catalogue, picker, and new-session form state.

use serde_json::Value;

use crate::protocol::CommandResult;

use super::edit;

#[derive(Debug, Clone, Default)]
pub struct SessionInfo {
    pub session_id: String,
    pub title: Option<String>,
    pub workspace: Option<String>,
    pub phase: String,
    pub round: u64,
    pub message_count: u64,
    pub last_seq: u64,
    pub updated_at: f64,
    pub created_at: f64,
}

impl SessionInfo {
    pub fn from_json(value: &Value) -> Option<Self> {
        Some(Self {
            session_id: value.get("session_id")?.as_str()?.to_owned(),
            title: value
                .get("title")
                .and_then(Value::as_str)
                .map(str::to_owned),
            workspace: value
                .get("workspace")
                .and_then(Value::as_str)
                .map(str::to_owned),
            phase: value
                .get("phase")
                .and_then(Value::as_str)
                .unwrap_or("idle")
                .to_owned(),
            round: value.get("round").and_then(Value::as_u64).unwrap_or(0),
            message_count: value
                .get("message_count")
                .and_then(Value::as_u64)
                .unwrap_or(0),
            last_seq: value.get("last_seq").and_then(Value::as_u64).unwrap_or(0),
            updated_at: value
                .get("updated_at")
                .and_then(Value::as_f64)
                .unwrap_or(0.0),
            created_at: value
                .get("created_at")
                .and_then(Value::as_f64)
                .unwrap_or(0.0),
        })
    }
}

#[derive(Debug, Clone)]
pub struct SessionPickerState {
    pub sessions: Vec<SessionInfo>,
    pub offset: usize,
    pub limit: usize,
    pub has_more: bool,
    pub all_workspaces: bool,
    pub selected: usize,
}

impl Default for SessionPickerState {
    fn default() -> Self {
        Self {
            sessions: Vec::new(),
            offset: 0,
            limit: 50,
            has_more: false,
            all_workspaces: false,
            selected: 0,
        }
    }
}

impl SessionPickerState {
    /// Apply a `session.list` result. The protocol has no total count, so
    /// `has_more` is inferred from a full page.
    pub fn apply_list(&mut self, result: &CommandResult) {
        if !result.ok {
            return;
        }
        let limit = result
            .result
            .get("limit")
            .and_then(Value::as_u64)
            .map(|value| value as usize)
            .unwrap_or(self.limit);
        let offset = result
            .result
            .get("offset")
            .and_then(Value::as_u64)
            .map(|value| value as usize)
            .unwrap_or(self.offset);
        let mut page: Vec<SessionInfo> = result
            .result
            .get("sessions")
            .and_then(Value::as_array)
            .map(|items| items.iter().filter_map(SessionInfo::from_json).collect())
            .unwrap_or_default();
        page.sort_by(|a, b| {
            b.updated_at
                .partial_cmp(&a.updated_at)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        self.has_more = limit > 0 && page.len() == limit;
        if offset == 0 {
            self.sessions = page;
        } else {
            self.sessions.extend(page);
        }
        self.limit = if limit == 0 { 50 } else { limit };
        self.offset = offset;
        self.selected = self.selected.min(self.sessions.len().saturating_sub(1));
    }

    /// History paging is deferred in v1; the state still accepts a page so a
    /// future `history.read` integration can reuse it.
    pub fn apply_history_page(&mut self, payload: &Value) {
        if let Some(has_more) = payload.get("has_more").and_then(Value::as_bool) {
            self.has_more = has_more;
        }
    }

    pub fn selected_session(&self) -> Option<&SessionInfo> {
        self.sessions.get(self.selected)
    }

    pub fn move_selection(&mut self, delta: isize) {
        if self.sessions.is_empty() {
            self.selected = 0;
            return;
        }
        let last = self.sessions.len() as isize - 1;
        self.selected = (self.selected as isize + delta).clamp(0, last) as usize;
    }
}

#[derive(Debug, Clone, Default)]
pub struct NewSessionForm {
    pub workspace: String,
    pub title: String,
    pub profile: String,
    pub focus: usize,
    pub error: Option<String>,
    pub workspace_cursor: usize,
    pub title_cursor: usize,
}

impl NewSessionForm {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.workspace.trim().is_empty() {
            return Err("workspace is required");
        }
        if self.title.chars().count() > 512 {
            return Err("title is too long");
        }
        Ok(())
    }

    fn active_field(&mut self) -> (&mut String, &mut usize) {
        if self.focus == 0 {
            (&mut self.workspace, &mut self.workspace_cursor)
        } else {
            (&mut self.title, &mut self.title_cursor)
        }
    }

    pub fn insert_char(&mut self, ch: char) {
        let (text, cursor) = self.active_field();
        edit::insert_char(text, cursor, ch);
    }

    /// Insert pasted text into the focused single-line field.
    pub fn insert_str(&mut self, value: &str) {
        let (text, cursor) = self.active_field();
        edit::insert(text, cursor, &value.replace('\n', " "));
    }

    pub fn backspace(&mut self) {
        let (text, cursor) = self.active_field();
        edit::backspace(text, cursor);
    }

    pub fn delete(&mut self) {
        let (text, cursor) = self.active_field();
        edit::delete(text, *cursor);
    }

    pub fn move_left(&mut self) {
        let (text, cursor) = self.active_field();
        *cursor = edit::left(text, *cursor);
    }

    pub fn move_right(&mut self) {
        let (text, cursor) = self.active_field();
        *cursor = edit::right(text, *cursor);
    }

    pub fn move_home(&mut self) {
        let (text, cursor) = self.active_field();
        *cursor = edit::line_start(text, *cursor);
    }

    pub fn move_end(&mut self) {
        let (text, cursor) = self.active_field();
        *cursor = edit::line_end(text, *cursor);
    }
}
