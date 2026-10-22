//! Approval modal.

use ratatui::buffer::Buffer;
use ratatui::layout::{Alignment, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, Paragraph, Widget, Wrap};
use unicode_width::UnicodeWidthStr;

use crate::ui::Theme;
use crate::viewmodel::TuiState;

pub fn approval_modal_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let Some(open) = state.approvals.open.as_deref() else {
        return Vec::new();
    };
    let Some(card) = state
        .approvals
        .pending
        .iter()
        .find(|card| card.call_id == open)
    else {
        return Vec::new();
    };

    let mut lines = vec![
        Line::from(Span::styled(
            format!("Approval required (1 of {})", state.approvals.pending.len()),
            theme.warning,
        )),
        Line::from(format!(
            "Tool: {} ({})",
            card.name,
            card.side_effect.as_deref().unwrap_or("unknown")
        )),
        Line::from(format!("Call: {}", card.call_id)),
        Line::from(format!("Arguments: {}", card.args_preview)),
    ];
    if let Some(requested_at) = card.requested_at {
        lines.push(Line::from(format!("requested_at: {requested_at:.3}")));
    }
    lines.push(Line::from("The agent is paused until you decide."));
    lines.push(Line::from(vec![
        Span::styled("[ Grant (y) ]", theme.success),
        Span::raw("  "),
        Span::styled("[ Deny (n) ]", theme.danger),
    ]));
    lines.push(Line::from("y grant · n deny · Esc postpone"));
    lines
}

/// The centered popup rectangle, or `None` when no approval is open.
pub fn approval_popup_area(area: Rect, state: &TuiState, theme: &Theme) -> Option<Rect> {
    let lines = approval_modal_lines(state, theme);
    popup_rect(area, &lines)
}

/// Size the popup to its content and center it, capped by the design's
/// 100-column / terminal-minus-4 rule.
fn popup_rect(area: Rect, lines: &[Line<'static>]) -> Option<Rect> {
    // The first line becomes the block title.
    if lines.len() < 2 || area.width < 3 || area.height < 3 {
        return None;
    }
    let width = area.width.saturating_sub(4).clamp(3, 100).min(area.width);
    let inner_width = width.saturating_sub(2).max(1) as usize;
    let content_rows = lines[1..]
        .iter()
        .map(|line| {
            let used = line
                .spans
                .iter()
                .map(|span| UnicodeWidthStr::width(span.content.as_ref()))
                .sum::<usize>();
            used.div_ceil(inner_width).max(1)
        })
        .sum::<usize>();
    let height = u16::try_from(content_rows.saturating_add(2))
        .unwrap_or(u16::MAX)
        .min(area.height);

    Some(Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    ))
}

pub fn render_approval_modal(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    let mut lines = approval_modal_lines(state, theme);
    let Some(popup) = popup_rect(area, &lines) else {
        return;
    };
    let title = lines.remove(0);

    // Terminals cannot blur glyphs, so dim the page to a frosted backdrop and
    // clear the popup itself. Without the clear, each modal line would leave
    // the page's characters in every cell past the end of its text.
    buf.set_style(area, theme.backdrop);
    Clear.render(popup, buf);

    let block = Block::bordered()
        .title(title)
        .title_alignment(Alignment::Center)
        .title_style(theme.warning)
        .border_style(theme.border)
        .style(theme.background);
    Paragraph::new(lines)
        .block(block)
        .wrap(Wrap { trim: false })
        .alignment(Alignment::Center)
        .style(theme.text)
        .render(popup, buf);
}
