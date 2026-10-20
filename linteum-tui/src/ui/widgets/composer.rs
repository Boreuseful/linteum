//! Multiline composer with a visible text cursor.

use ratatui::buffer::Buffer;
use ratatui::layout::{Position, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::Widget;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::ui::Theme;
use crate::viewmodel::{TuiState, edit};

pub struct ComposerWidget;

impl ComposerWidget {
    pub fn desired_height(text: &str) -> u16 {
        text.split('\n').count().max(1).clamp(1, 6) as u16
    }
}

/// Render the composer and return the terminal cell for its cursor.
pub fn render_composer(
    area: Rect,
    buf: &mut Buffer,
    state: &TuiState,
    theme: &Theme,
) -> Option<Position> {
    if area.width == 0 || area.height == 0 {
        return None;
    }
    let text = state.composer.text.as_str();
    if text.is_empty() {
        let mut spans = vec![Span::styled("> ", theme.text)];
        if state.overlay.is_none() {
            spans.push(Span::styled("/ for commands", theme.muted));
        }
        Line::from(spans).render(area, buf);
        return Some(Position::new(area.x.saturating_add(2), area.y));
    }

    let lines: Vec<&str> = text.split('\n').collect();
    let cursor = edit::clamp(text, state.composer.cursor);
    let cursor_line = text[..cursor].matches('\n').count();
    let cursor_line_start = edit::line_start(text, cursor);

    let visible = area.height as usize;
    let mut start = lines.len().saturating_sub(visible);
    if cursor_line < start {
        start = cursor_line;
    } else if cursor_line >= start + visible {
        start = cursor_line + 1 - visible;
    }

    let mut position = None;
    for (index, line) in lines.iter().enumerate().skip(start).take(visible) {
        let row = area.y + (index - start) as u16;
        let mut shown: &str = line;
        let mut prefix = if index == 0 { "> " } else { "  " };
        let mut cursor_x = None;

        if index == cursor_line {
            let in_line = cursor - cursor_line_start;
            let before_width = UnicodeWidthStr::width(&line[..in_line]);
            let available = area.width.saturating_sub(2).max(1) as usize;
            if before_width >= available {
                let shift_byte = char_at_width(line, before_width + 1 - available);
                shown = &line[shift_byte..];
                prefix = "… ";
            }
            let before_shown = in_line.saturating_sub(line.len().saturating_sub(shown.len()));
            let x = area.x.saturating_add(2).saturating_add(
                u16::try_from(UnicodeWidthStr::width(&shown[..before_shown])).unwrap_or(u16::MAX),
            );
            cursor_x = Some(x.min(area.right().saturating_sub(1)));
        }

        Line::from(Span::styled(format!("{prefix}{shown}"), theme.text))
            .render(Rect::new(area.x, row, area.width, 1), buf);
        if let Some(x) = cursor_x {
            position = Some(Position::new(x, row));
        }
    }
    position
}

/// Byte index reached after skipping `width` display columns.
fn char_at_width(text: &str, width: usize) -> usize {
    let mut consumed = 0;
    for (index, ch) in text.char_indices() {
        if consumed >= width {
            return index;
        }
        consumed += UnicodeWidthChar::width(ch).unwrap_or(0);
    }
    text.len()
}
