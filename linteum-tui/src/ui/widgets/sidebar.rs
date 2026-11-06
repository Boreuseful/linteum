//! Right sidebar: workspace tree, preview, and info panels.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, Paragraph, Widget};
use unicode_width::UnicodeWidthStr;

use crate::ui::Theme;
use crate::viewmodel::{
    Focus, Item, SidebarLayout, SidebarPanel, SubagentStatus, ToolCard, TuiState,
};

pub fn render_sidebar(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    match state.sidebar.layout {
        SidebarLayout::Merged => render_panel(area, buf, state, theme, state.sidebar.panel),
        SidebarLayout::Split => {
            let chunks = Layout::vertical([
                Constraint::Percentage(50),
                Constraint::Percentage(25),
                Constraint::Percentage(25),
            ])
            .split(area);
            render_panel(chunks[0], buf, state, theme, SidebarPanel::Files);
            render_panel(chunks[1], buf, state, theme, SidebarPanel::Preview);
            render_panel(chunks[2], buf, state, theme, SidebarPanel::Info);
        }
    }
}

fn render_panel(
    area: Rect,
    buf: &mut Buffer,
    state: &TuiState,
    theme: &Theme,
    panel: SidebarPanel,
) {
    if area.width == 0 || area.height == 0 {
        return;
    }
    let focused = state.focus == Focus::Sidebar && panel == SidebarPanel::Files;
    let title = match panel {
        SidebarPanel::Files if focused => " Files · ↑↓ ".to_owned(),
        SidebarPanel::Files => " Files · Tab ".to_owned(),
        SidebarPanel::Subagents | SidebarPanel::Info if !state.subagents.is_empty() => format!(
            " Subagents · {}/{} ",
            state.subagents.running_count(),
            state.subagents.entries.len()
        ),
        SidebarPanel::Background if !background_tasks(state).is_empty() => {
            let (running, total) = background_counts(state);
            format!(" Background · {running}/{total} ")
        }
        _ => format!(" {} ", panel.title()),
    };
    let block = Block::bordered()
        .title(title)
        .border_style(if focused { theme.accent } else { theme.border })
        .style(theme.background);
    Clear.render(area, buf);
    let inner = block.inner(area);
    block.render(area, buf);
    let lines = match panel {
        SidebarPanel::Files => files_lines(state, inner.width, theme),
        SidebarPanel::Preview => placeholder_lines("No preview yet", theme),
        SidebarPanel::Info => info_lines(state, inner.width, theme),
        SidebarPanel::Subagents => subagent_lines(state, inner.width, theme),
        SidebarPanel::Background => background_lines(state, inner.width, theme),
    };
    Paragraph::new(lines).style(theme.text).render(inner, buf);
}

fn files_lines(state: &TuiState, width: u16, theme: &Theme) -> Vec<Line<'static>> {
    let sidebar = &state.sidebar;
    let mut lines = Vec::new();
    if let Some(error) = &sidebar.error {
        lines.push(Line::from(Span::styled(
            format!("error: {error}"),
            theme.danger,
        )));
    }
    if sidebar.root.is_none() {
        lines.push(Line::from(Span::styled("no workspace", theme.muted)));
        return lines;
    }
    if sidebar.rows.is_empty() {
        lines.push(Line::from(Span::styled("loading…", theme.muted)));
        return lines;
    }

    let inner = width.max(1) as usize;
    lines.extend(sidebar.rows.iter().enumerate().map(|(index, row)| {
        let marker = if row.is_dir {
            if sidebar.pending.contains(&row.path) {
                "… "
            } else if row.expanded {
                "▾ "
            } else {
                "▸ "
            }
        } else {
            "  "
        };
        let indent = "  ".repeat(row.depth);
        let mut text = format!("{indent}{marker}{}", row.name);
        let used = UnicodeWidthStr::width(text.as_str());
        if used < inner {
            text.push_str(&" ".repeat(inner - used));
        }
        let mut line = Line::from(Span::styled(
            text,
            if row.is_dir { theme.accent } else { theme.text },
        ));
        if index == sidebar.selected {
            line = line.style(theme.selection);
        }
        line
    }));
    lines
}

fn info_lines(state: &TuiState, width: u16, theme: &Theme) -> Vec<Line<'static>> {
    if state.subagents.is_empty() {
        return placeholder_lines("No content yet", theme);
    }
    subagent_lines(state, width, theme)
}

fn subagent_lines(state: &TuiState, width: u16, theme: &Theme) -> Vec<Line<'static>> {
    if state.subagents.is_empty() {
        return placeholder_lines("no subagents", theme);
    }
    let mut lines = Vec::new();
    for entry in &state.subagents.entries {
        let (marker, style) = status_marker(entry.status, theme);
        let prefix = format!("{marker} {} · ", entry.status.label());
        let room = (width as usize)
            .saturating_sub(UnicodeWidthStr::width(prefix.as_str()))
            .max(8);
        lines.push(Line::from(Span::styled(
            format!("{prefix}{}", entry.title_preview(room)),
            style,
        )));
        let detail = match entry.status {
            SubagentStatus::Requested => "  waiting for child session".to_owned(),
            SubagentStatus::Running => {
                let phase = entry.phase.as_deref().unwrap_or("-");
                let mut text = format!("  {phase} r{}", entry.round);
                if let Some(tool) = &entry.last_tool {
                    text.push_str(&format!(" · {tool}"));
                }
                if entry.tokens_input + entry.tokens_output > 0 {
                    text.push_str(&format!(
                        " · {}/{} tok",
                        entry.tokens_input, entry.tokens_output
                    ));
                }
                text
            }
            SubagentStatus::Completed => {
                let mut text = "  completed".to_owned();
                if entry.rounds > 0 {
                    text.push_str(&format!(" · {}r", entry.rounds));
                }
                if let Some(duration) = entry.duration_ms {
                    text.push_str(&format!(" · {:.1}s", duration as f64 / 1000.0));
                }
                text
            }
            SubagentStatus::Failed | SubagentStatus::Cancelled | SubagentStatus::Timeout => {
                let mut text = format!("  {}", entry.status.label());
                if let Some(code) = &entry.code {
                    text.push_str(&format!(" · {code}"));
                }
                text
            }
        };
        lines.push(Line::from(Span::styled(detail, theme.muted)));
    }
    lines
}

fn is_background_running(card: &ToolCard) -> bool {
    matches!(card.background_status.as_deref(), None | Some("running"))
}

/// Background tool cards, active tasks first, in transcript order otherwise.
fn background_tasks(state: &TuiState) -> Vec<&ToolCard> {
    let mut tasks: Vec<&ToolCard> = state
        .transcript
        .items
        .iter()
        .filter_map(|item| match item {
            Item::Tool(card) if card.background => Some(card),
            _ => None,
        })
        .collect();
    tasks.sort_by_key(|card| if is_background_running(card) { 0 } else { 1 });
    tasks
}

fn background_counts(state: &TuiState) -> (usize, usize) {
    let tasks = background_tasks(state);
    let running = tasks
        .iter()
        .filter(|card| is_background_running(card))
        .count();
    (running, tasks.len())
}

fn background_lines(state: &TuiState, _width: u16, theme: &Theme) -> Vec<Line<'static>> {
    let tasks = background_tasks(state);
    if tasks.is_empty() {
        return placeholder_lines("no background tasks", theme);
    }
    let mut lines = Vec::new();
    for card in tasks.iter().take(16) {
        let status = card.background_status.as_deref().unwrap_or("running");
        let (marker, style) = match status {
            "running" => ("●", theme.accent),
            "completed" => ("✔", theme.success),
            "failed" | "lost" => ("✖", theme.danger),
            "stopped" => ("⊘", theme.warning),
            _ => ("·", theme.muted),
        };
        let header = format!("{marker} {} · {status}", card.name);
        lines.push(Line::from(Span::styled(header, style)));
        let mut detail = format!("  {}", card.task_id.as_deref().unwrap_or("-"));
        if let Some(duration_ms) = card.background_duration_ms {
            detail.push_str(&format!(" · {:.1}s", duration_ms as f64 / 1000.0));
        }
        lines.push(Line::from(Span::styled(detail, theme.muted)));
    }
    lines
}

fn status_marker(status: SubagentStatus, theme: &Theme) -> (&'static str, ratatui::style::Style) {
    match status {
        SubagentStatus::Requested => ("…", theme.muted),
        SubagentStatus::Running => ("●", theme.accent),
        SubagentStatus::Completed => ("✔", theme.success),
        SubagentStatus::Failed => ("✖", theme.danger),
        SubagentStatus::Cancelled | SubagentStatus::Timeout => ("⊘", theme.warning),
    }
}

fn placeholder_lines(message: &str, theme: &Theme) -> Vec<Line<'static>> {
    vec![Line::from(Span::styled(message.to_owned(), theme.muted))]
}
