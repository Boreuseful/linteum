//! Slash-command menu anchored above the composer.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, Paragraph, Widget};

use crate::ui::Theme;
use crate::ui::layout;
use crate::viewmodel::{TuiState, slash};

pub fn slash_menu_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let query = slash::query(&state.composer.text);
    let matches = slash::filtered(query);
    if matches.is_empty() {
        return vec![Line::from(Span::styled(
            format!("No commands match /{query}"),
            theme.muted,
        ))];
    }

    let selected = state.slash.selected.min(matches.len() - 1);
    matches
        .iter()
        .enumerate()
        .map(|(index, command)| {
            let marker = if index == selected { "› " } else { "  " };
            let mut line = Line::from(vec![
                Span::styled(marker, theme.accent),
                Span::styled(format!("/{}", command.id), theme.accent),
                Span::raw("  "),
                Span::styled(command.label, theme.text),
                Span::styled(format!(" · {}", command.description), theme.muted),
            ]);
            if index == selected {
                line = line.style(theme.selection);
            }
            line
        })
        .collect()
}

pub fn render_slash_menu(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    if area.width < 8 || area.height < 6 {
        return;
    }
    let lines = slash_menu_lines(state, theme);
    let composer = layout::layout(area, state).composer;
    let available = composer.y.saturating_sub(area.y);
    let height = u16::try_from(lines.len().saturating_add(2))
        .unwrap_or(u16::MAX)
        .min(available);
    let width = area
        .width
        .saturating_sub(4)
        .clamp(24, 72)
        .min(area.width.saturating_sub(2));
    if height < 3 || width < 3 {
        return;
    }

    let popup = Rect::new(area.x + 2, composer.y - height, width, height);
    Clear.render(popup, buf);
    let block = Block::bordered()
        .title(" Commands ")
        .title_style(theme.accent)
        .border_style(theme.border)
        .style(theme.background);
    Paragraph::new(lines)
        .block(block)
        .style(theme.text)
        .render(popup, buf);
}
