//! Transcript rendering and scroll position.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Widget;

use crate::render::markdown::{render_markdown, wrap_lines};
use crate::ui::Theme;
use crate::viewmodel::{Item, TuiState};

use super::compaction_card::compaction_card_lines;
use super::error_card::error_card_lines;
use super::tool_card::tool_card_lines;

pub fn transcript_lines(state: &TuiState, width: u16, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    for item in &state.transcript.items {
        match item {
            Item::User(user) => {
                lines.push(Line::from(Span::styled("You", theme.accent)));
                lines.extend(plain_lines(&user.text, theme.text));
                lines.push(Line::from(""));
            }
            Item::Assistant(assistant) => {
                lines.push(Line::from(Span::styled("Assistant", theme.accent)));
                if assistant.final_message.is_some() {
                    lines.extend(render_markdown(&assistant.text, width, theme));
                } else {
                    lines.extend(plain_lines(&assistant.text, theme.text));
                }
                if state.prefs.show_reasoning && !assistant.reasoning.is_empty() {
                    lines.extend(plain_lines(&assistant.reasoning, theme.reasoning));
                }
                lines.push(Line::from(""));
            }
            Item::Tool(tool) => lines.extend(tool_card_lines(tool, theme)),
            Item::Approval(approval) => lines.push(Line::from(Span::styled(
                format!("approval {} · {}", approval.name, approval.call_id),
                theme.warning,
            ))),
            Item::Compaction(compaction) => lines.extend(compaction_card_lines(compaction, theme)),
            Item::Error(error) => lines.extend(error_card_lines(error, theme)),
            Item::Gap(gap) => lines.push(Line::from(Span::styled(
                format!("History gap: frames {}–{}", gap.from_seq, gap.to_seq),
                theme.warning,
            ))),
        }
    }
    // Every item — including card lines that do not wrap themselves — must fit
    // the transcript pane. Otherwise a wide or unbroken line is clipped at the
    // sidebar border and looks like it continues underneath the sidebar.
    wrap_lines(lines, width)
}

fn plain_lines(text: &str, style: Style) -> Vec<Line<'static>> {
    let lines: Vec<Line<'static>> = text
        .lines()
        .map(|line| Line::from(Span::styled(line.to_owned(), style)))
        .collect();
    if lines.is_empty() {
        vec![Line::from("")]
    } else {
        lines
    }
}

pub fn visible_offset(state: &TuiState, total: usize, viewport: usize) -> usize {
    if viewport == 0 {
        return 0;
    }
    state.transcript.scroll.visible_offset(total, viewport)
}

pub fn render_transcript(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let lines = transcript_lines(state, area.width, theme);
    let offset = visible_offset(state, lines.len(), area.height as usize);
    for (row, line) in lines
        .iter()
        .skip(offset)
        .take(area.height as usize)
        .enumerate()
    {
        line.clone()
            .render(Rect::new(area.x, area.y + row as u16, area.width, 1), buf);
    }
}
