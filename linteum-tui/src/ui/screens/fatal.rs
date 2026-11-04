//! Blocking fatal screen.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::TuiState;

pub fn fatal_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled("Cannot continue", theme.danger))];
    if let Some(error) = &state.last_error {
        lines.push(Line::from(format!("{}: {}", error.code, error.message)));
        if !error.detail.is_empty() {
            lines.push(Line::from(error.detail.clone()));
        }
    }
    if !state.child_stderr_tail.is_empty() {
        lines.push(Line::from("Last stderr:"));
        lines.extend(
            state
                .child_stderr_tail
                .iter()
                .map(|line| Line::from(line.clone())),
        );
    }
    lines.push(Line::from(""));
    lines.push(Line::from("[ Quit (q) ]"));
    lines
}

pub fn render_fatal(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    render_lines(area, buf, &fatal_lines(state, theme));
}
