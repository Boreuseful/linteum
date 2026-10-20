//! Inline context-compaction card.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::viewmodel::CompactionCard;

use super::render_lines;

pub fn compaction_card_lines(card: &CompactionCard, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled(
        format!("compaction {}..{}", card.from_index, card.to_index),
        theme.muted,
    ))];
    if let Some(summary) = &card.summary {
        lines.push(Line::from(Span::styled(summary.clone(), theme.text)));
    }
    if let Some(tokens) = card.tokens {
        lines.push(Line::from(Span::styled(
            format!("{tokens} tokens"),
            theme.muted,
        )));
    }
    if card.fallback {
        lines.push(Line::from(Span::styled("fallback", theme.warning)));
    }
    if let Some(error) = &card.error {
        lines.push(Line::from(Span::styled(
            format!("{}: {}", error.code, error.message),
            theme.danger,
        )));
    }
    lines
}

pub fn render_compaction_card(area: Rect, buf: &mut Buffer, card: &CompactionCard, theme: &Theme) {
    render_lines(area, buf, &compaction_card_lines(card, theme));
}
