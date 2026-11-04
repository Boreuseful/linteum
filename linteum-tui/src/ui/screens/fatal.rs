//! Blocking fatal screen.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::{GatewayMode, TuiState};

pub fn fatal_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled("Cannot continue", theme.danger))];
    if let Some(error) = &state.last_error {
        lines.push(Line::from(format!("{}: {}", error.code, error.message)));
        if !error.detail.is_empty() {
            lines.push(Line::from(error.detail.clone()));
        }
    }
    if let Some(status) = state.child_exit_status {
        lines.push(Line::from(format!("exit status: {status}")));
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
    if state.gateway_mode == GatewayMode::Spawn {
        lines.push(Line::from("[ r restart · q quit ]"));
    } else {
        lines.push(Line::from("[ q quit ]"));
    }
    lines
}

pub fn render_fatal(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    render_lines(area, buf, &fatal_lines(state, theme));
}
