//! Subagent task list and child-context viewer.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Clear, Widget};

use crate::render::wrap_lines;
use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::{SubagentStatus, TuiState};

pub fn subagent_list_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(Span::styled("Subagents", theme.accent))];
    if state.subagents.is_empty() {
        lines.push(Line::from(Span::styled(
            "No subagents in this session.",
            theme.muted,
        )));
        return lines;
    }
    for (index, entry) in state.subagents.entries.iter().enumerate() {
        let selected = index == state.subagents.selected;
        let marker = if selected { "▶ " } else { "  " };
        let phase = entry.phase.as_deref().unwrap_or("-");
        let tool = entry.last_tool.as_deref().unwrap_or("-");
        let mut spans = vec![
            Span::styled(
                marker.to_owned(),
                if selected {
                    theme.selection
                } else {
                    theme.text
                },
            ),
            Span::styled(entry.title_preview(48), theme.text),
            Span::raw("  "),
            Span::styled(
                entry.status.label().to_owned(),
                status_style(entry.status, theme),
            ),
            Span::styled(format!(" · {phase} r{} · {tool}", entry.round), theme.muted),
        ];
        if let Some(child) = &entry.child_session_id {
            spans.push(Span::styled(format!(" · {}", short(child)), theme.muted));
        }
        if entry.read_only {
            spans.push(Span::styled(" · read-only", theme.muted));
        }
        lines.push(Line::from(spans));
        if let Some(summary) = &entry.summary {
            lines.push(Line::from(Span::styled(
                format!("  {}", preview(summary, 96)),
                theme.muted,
            )));
        }
        if let (Some(code), Some(error)) = (&entry.code, &entry.error) {
            lines.push(Line::from(Span::styled(
                format!("  {code}: {}", preview(&error.message, 96)),
                theme.danger,
            )));
        }
    }
    lines.push(Line::from(Span::styled(
        "Enter: context · ↑/↓: select · Esc: close",
        theme.muted,
    )));
    lines
}

pub fn subagent_context_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = Vec::new();
    if let Some(child) = &state.subagents.loading {
        lines.push(Line::from(Span::styled(
            format!("Loading context for {}…", short(child)),
            theme.muted,
        )));
        return lines;
    }
    if let Some(error) = &state.subagents.error {
        lines.push(Line::from(Span::styled(
            format!("Context unavailable: {error}"),
            theme.danger,
        )));
        return lines;
    }
    let Some(context) = &state.subagents.detail else {
        lines.push(Line::from(Span::styled(
            "No context loaded. Press Enter on a subagent.",
            theme.muted,
        )));
        return lines;
    };

    lines.push(Line::from(Span::styled("Subagent context", theme.accent)));
    lines.push(Line::from(format!(
        "child {} · kind {} · phase {} · round {}",
        short(&context.session_id),
        context.kind,
        context.phase.as_deref().unwrap_or("-"),
        context.round,
    )));
    lines.push(Line::from(Span::styled(
        format!(
            "source {} · ~{} tokens · digest {}",
            context.source,
            context.estimated_tokens,
            preview(&context.digest, 16),
        ),
        theme.muted,
    )));
    if !context.tools.is_empty() {
        lines.push(Line::from(Span::styled(
            format!("tools: {}", context.tools.join(", ")),
            theme.muted,
        )));
    }
    if !context.system_blocks_included {
        lines.push(Line::from(Span::styled(
            "system blocks are not persisted for completed children",
            theme.warning,
        )));
    }
    lines.push(Line::from(""));
    if context.messages.is_empty() {
        lines.push(Line::from(Span::styled("(no messages)", theme.muted)));
    }
    for message in &context.messages {
        lines.push(Line::from(Span::styled(
            format!("[{}]", message.role),
            theme.accent,
        )));
        for line in message.text.lines() {
            lines.push(Line::from(Span::styled(line.to_owned(), theme.text)));
        }
        lines.push(Line::from(""));
    }
    lines.push(Line::from(Span::styled(
        "Esc: back · r: reload · ↑/↓/PgUp/PgDn: scroll",
        theme.muted,
    )));
    lines
}

/// Largest useful scroll offset for the current context at `width x height`.
///
/// The UI layer uses this to stop Down/PageDown at the last line instead of
/// accumulating an unbounded offset; the renderer clamps again as a safety
/// net.  Theme styles do not affect the line count.
pub fn subagent_context_scroll_limit(state: &TuiState, width: u16, height: u16) -> usize {
    let lines = wrap_lines(subagent_context_lines(state, &Theme::mono()), width);
    lines.len().saturating_sub(height as usize)
}

pub fn render_subagents(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    // The overlay is modal: clear the page first so a short list cannot leave
    // the transcript visible underneath it.
    Clear.render(area, buf);
    let lines = wrap_lines(subagent_list_lines(state, theme), area.width);
    render_lines(area, buf, &lines);
}

pub fn render_subagent_context(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    Clear.render(area, buf);
    let lines = wrap_lines(subagent_context_lines(state, theme), area.width);
    // Keep the viewport full: never scroll past the last screenful.  Without
    // this, a PageDown at the end moves the context up and exposes the agent
    // page below it.
    let max_scroll = lines.len().saturating_sub(area.height as usize);
    let start = state.subagents.scroll.min(max_scroll);
    render_lines(area, buf, &lines[start..]);
}

fn status_style(status: SubagentStatus, theme: &Theme) -> Style {
    match status {
        SubagentStatus::Requested => theme.muted,
        SubagentStatus::Running => theme.accent,
        SubagentStatus::Completed => theme.success,
        SubagentStatus::Failed => theme.danger,
        SubagentStatus::Cancelled => theme.warning,
        SubagentStatus::Timeout => theme.warning,
    }
}

fn preview(text: &str, limit: usize) -> String {
    let first = text.lines().next().unwrap_or("").trim();
    if first.chars().count() <= limit {
        first.to_owned()
    } else {
        first
            .chars()
            .take(limit.saturating_sub(1))
            .collect::<String>()
            + "…"
    }
}

fn short(value: &str) -> String {
    if value.chars().count() <= 10 {
        value.to_owned()
    } else {
        value.chars().take(8).collect::<String>() + "…"
    }
}
