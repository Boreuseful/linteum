//! Help overlay.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::Line;

use crate::ui::Theme;
use crate::ui::widgets::render_lines;

pub fn help_lines(theme: &Theme) -> Vec<Line<'static>> {
    vec![
        Line::from("Keys"),
        Line::from("Enter send · Ctrl+J newline · Ctrl+C interrupt"),
        Line::from("F1 help · F2 sessions · F3 activity · F4 approvals"),
        Line::from("F5 reconnect · F6 subagents"),
        Line::from("Ctrl+N new session · Ctrl+P palette · Ctrl+R reasoning · Ctrl+G resume"),
        Line::from("Ctrl+Q quit · Esc close overlay"),
        Line::from("/mode · /mode status · /mode <name-or-id>"),
        Line::from(""),
        Line::from(ratatui::text::Span::styled(
            "All actions are available in the command palette.",
            theme.muted,
        )),
    ]
}

pub fn render_help(area: Rect, buf: &mut Buffer, theme: &Theme) {
    render_lines(area, buf, &help_lines(theme));
}
