//! Session picker.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::TuiState;

pub fn picker_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled("Sessions", theme.accent))];
    for (index, session) in state.picker.sessions.iter().enumerate() {
        let marker = if index == state.picker.selected {
            "› "
        } else {
            "  "
        };
        let mut line = Line::from(format!(
            "{marker}{:<22} {:<12} {:>4} msgs  {:>10.0}  {}",
            session
                .title
                .as_deref()
                .unwrap_or(session.session_id.as_str()),
            session.phase,
            session.message_count,
            session.updated_at,
            session.workspace.as_deref().unwrap_or("")
        ));
        if index == state.picker.selected {
            line = line.style(theme.selection);
        }
        lines.push(line);
    }
    if state.picker.has_more {
        lines.push(Line::from(Span::styled("Load more", theme.accent)));
    }
    if let Some(session) = state.picker.selected_session() {
        // The list truncates its title column, and untitled sessions fall back
        // to their UUID; repeat the full title here so the highlighted row is
        // identifiable before attaching.
        let title = session.title.as_deref().unwrap_or("(untitled)");
        lines.push(Line::from(Span::styled(
            format!("selected: {title} · {}", session.session_id),
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
