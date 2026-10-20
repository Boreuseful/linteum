//! Session picker.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::TuiState;

pub fn picker_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled("Sessions", theme.accent))];
    for session in &state.picker.sessions {
        lines.push(Line::from(format!(
            "{:<24} {:<12} {:>4} msgs  {:>10.0}  {}",
            session
                .title
                .as_deref()
                .unwrap_or(session.session_id.as_str()),
            session.phase,
            session.message_count,
            session.updated_at,
            session.workspace.as_deref().unwrap_or("")
        )));
    }
    if state.picker.has_more {
        lines.push(Line::from(Span::styled("Load more", theme.accent)));
    }
    if let Some(session) = state.picker.selected_session() {
        lines.push(Line::from(Span::styled(
            format!("selected: {}", session.session_id),
            theme.muted,
        )));
    }
    lines.push(Line::from(
        "a all workspaces · r refresh · n new · Enter attach · Esc back",
    ));
    lines
}

pub fn render_picker(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    render_lines(area, buf, &picker_lines(state, theme));
}
