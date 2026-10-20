//! Right sidebar: workspace tree, preview, and info panels.

use ratatui::buffer::Buffer;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, Paragraph, Widget};
use unicode_width::UnicodeWidthStr;

use crate::ui::Theme;
use crate::viewmodel::{Focus, SidebarLayout, SidebarPanel, TuiState};

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
        SidebarPanel::Info => placeholder_lines("No content yet", theme),
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

fn placeholder_lines(message: &str, theme: &Theme) -> Vec<Line<'static>> {
    vec![Line::from(Span::styled(message.to_owned(), theme.muted))]
}
