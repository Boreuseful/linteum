//! Activity panel.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::TuiState;

pub fn activity_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    state
        .activity
        .entries
        .iter()
        .map(|entry| {
            Line::from(Span::styled(
                format!("[{}] {}", entry.kind, entry.text),
                theme.text,
            ))
        })
        .collect()
}

pub fn render_activity(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    render_lines(area, buf, &activity_lines(state, theme));
}
