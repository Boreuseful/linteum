//! Inline error card.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::viewmodel::ErrorCard;

use super::render_lines;

pub fn error_card_lines(card: &ErrorCard, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled(
        format!("error {}: {}", card.code, card.message),
        theme.danger,
    ))];
    if let Some(info) = &card.error_info {
        if !info.detail.is_empty() {
            lines.push(Line::from(Span::styled(info.detail.clone(), theme.muted)));
        }
        lines.push(Line::from(Span::styled(
            format!(
                "{} · {} · retryable={}",
                info.source, info.location, info.retryable
            ),
            theme.muted,
        )));
    }
    lines
}

pub fn render_error_card(area: Rect, buf: &mut Buffer, card: &ErrorCard, theme: &Theme) {
    render_lines(area, buf, &error_card_lines(card, theme));
}
