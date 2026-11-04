//! New-session dialog.

use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};
use ratatui::text::{Line, Span};
use unicode_width::UnicodeWidthStr;

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::{TuiState, edit};

/// Both `Workspace  [` and `Title      [` occupy 12 columns.
const FIELD_PREFIX_WIDTH: u16 = 12;

pub fn new_session_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let form = &state.new_session;
    vec![
        Line::from(Span::styled("New session", theme.accent)),
        Line::from(format!("Workspace  [{} ]", form.workspace)),
        Line::from("           path on the gateway host (attach mode)"),
        Line::from(format!("Title      [{} ]", form.title)),
        Line::from(format!("Profile    {} (read-only)", form.profile)),
        Line::from(""),
        Line::from(if let Some(error) = &form.error {
            Span::styled(error.clone(), theme.danger)
        } else {
            Span::styled("Enter create · Tab next · Esc cancel", theme.muted)
        }),
    ]
}

/// The terminal cell of the active field's cursor.
pub fn new_session_cursor(area: Rect, state: &TuiState) -> Option<Position> {
    if area.width == 0 || area.height == 0 {
        return None;
    }
    let form = &state.new_session;
    let (line, text, cursor) = if form.focus == 0 {
        (1, form.workspace.as_str(), form.workspace_cursor)
    } else {
        (3, form.title.as_str(), form.title_cursor)
    };
    let y = area.y.checked_add(line)?;
    if y >= area.bottom() {
        return None;
    }
    let cursor = edit::clamp(text, cursor);
    let width = UnicodeWidthStr::width(&text[..cursor]);
    let x = area
        .x
        .saturating_add(FIELD_PREFIX_WIDTH)
        .saturating_add(u16::try_from(width).unwrap_or(u16::MAX));
    Some(Position::new(x.min(area.right().saturating_sub(1)), y))
}

pub fn render_new_session(
    area: Rect,
    buf: &mut Buffer,
    state: &TuiState,
    theme: &Theme,
) -> Option<Position> {
    render_lines(area, buf, &new_session_lines(state, theme));
    new_session_cursor(area, state)
}
