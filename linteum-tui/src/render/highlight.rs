//! Code-block rendering. Syntax highlighting is feature-gated; the default is
//! monospace with the semantic code style.

use ratatui::text::{Line, Span};

use crate::ui::Theme;

pub fn highlight_code(lang: &str, code: &str, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    if !lang.is_empty() {
        lines.push(Line::from(Span::styled(format!("[{lang}]"), theme.muted)));
    }
    for line in code.lines() {
        lines.push(Line::from(Span::styled(line.to_owned(), theme.code)));
    }
    lines
}
