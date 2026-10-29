//! Mode picker overlay.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::TuiState;

pub fn mode_picker_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled("Modes", theme.accent))];
    let options = state.mode.filtered_options();

    if !state.mode.list_loaded {
        lines.push(Line::from(Span::styled("loading modes…", theme.muted)));
    } else if options.is_empty() {
        lines.push(Line::from(Span::styled("no matching modes", theme.muted)));
    }

    for (index, option) in options.iter().enumerate() {
        let marker = if index == state.mode.picker.selected {
            "› "
        } else {
            "  "
        };
        let current = state
            .mode
            .current
            .as_ref()
            .is_some_and(|mode| mode.id == option.id);
        let default = if option.default { " default" } else { "" };
        let selected = if current { " current" } else { "" };
        let mut line = Line::from(format!(
            "{marker}{:<20} {:<38} [{}]{}{}",
            option.name, option.id, option.source, default, selected,
        ));
        if index == state.mode.picker.selected {
            line = line.style(theme.selection);
        } else if current {
            line = line.style(theme.accent);
        }
        lines.push(line);
    }

    if let Some(option) = state.mode.picker_current() {
        lines.push(Line::from(Span::styled(
            format!("{} — {}", option.id, option.description),
            theme.muted,
        )));
    }
    if let Some(error) = &state.mode.last_error {
        lines.push(Line::from(Span::styled(error.clone(), theme.danger)));
    }
    lines.push(Line::from(
        "type to filter · r refresh · Enter switch · Esc close",
    ));
    lines
}

pub fn render_mode_picker(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    let mut lines = mode_picker_lines(state, theme);
    // The picker is a full-page surface: pad to the viewport so transcript
    // glyphs cannot show through below the last mode row.
    while lines.len() < area.height as usize {
        lines.push(Line::from(""));
    }
    render_lines(area, buf, &lines);
}
