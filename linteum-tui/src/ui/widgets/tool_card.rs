//! Inline tool-call card.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};

use crate::ui::Theme;
use crate::viewmodel::ToolCard;

use super::render_lines;

pub fn tool_card_lines(card: &ToolCard, theme: &Theme) -> Vec<Line<'static>> {
    let marker = if card.expanded { "▾" } else { "▸" };
    let background = if card.background { "[background] " } else { "" };
    let background_status = card
        .background_status
        .as_ref()
        .map(|status| format!(" · {status}"))
        .unwrap_or_default();
    let mut lines = vec![Line::from(Span::styled(
        format!(
            "{marker} {background}{} · {:?} · {}{background_status}",
            card.name, card.state, card.call_id
        ),
        theme.muted,
    ))];
    if card.expanded {
        if let Some(args) = &card.args {
            lines.push(Line::from(Span::styled(args.clone(), theme.code)));
        }
        if let Some(task_id) = &card.task_id {
            lines.push(Line::from(Span::styled(
                format!("background task: {task_id}"),
                theme.muted,
            )));
        }
        if let Some(output) = &card.output {
            lines.push(Line::from(Span::styled(output.clone(), theme.text)));
        }
        if let Some(error) = &card.error {
            lines.push(Line::from(Span::styled(
                format!("{}: {}", error.code, error.message),
                theme.danger,
            )));
        }
        if let Some(reference) = &card.output_ref {
            lines.push(Line::from(Span::styled(
                format!("Open full output · {reference}"),
                theme.accent,
            )));
        }
    }
    lines
}

pub fn render_tool_card(area: Rect, buf: &mut Buffer, card: &ToolCard, theme: &Theme) {
    render_lines(area, buf, &tool_card_lines(card, theme));
}
