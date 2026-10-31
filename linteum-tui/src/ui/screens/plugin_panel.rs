//! Read-only plugin catalog panel (§11.4).
//!
//! Every value comes from a `plugin.list` snapshot; the panel never reads the
//! host, the bus, or a plugin directory directly.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

use crate::ui::Theme;
use crate::ui::widgets::render_lines;
use crate::viewmodel::{ConnectionState, PluginEntry, PluginRow, TuiState};

const MAX_NAMES_PER_KIND: usize = 8;
const MIN_LIST_ROWS: usize = 8;
const MAX_DETAIL_LINES: usize = 6;

pub fn plugin_panel_lines(
    state: &TuiState,
    theme: &Theme,
    width: u16,
    height: u16,
) -> Vec<Line<'static>> {
    let rows = state.plugins.filtered_rows();
    let has_extra = state.plugins.error.is_some() || !state.plugins.warnings.is_empty();
    // With no rows the status line already carries the command error; do not
    // repeat it in the extra slot (§11.4.7).
    let error_in_status = rows.is_empty() && state.plugins.error.is_some();
    let show_extra = has_extra && !error_in_status;
    let extra_count = usize::from(show_extra);
    let detail_budget = usize::from(height)
        .saturating_sub(3 + extra_count + MIN_LIST_ROWS)
        .clamp(1, MAX_DETAIL_LINES);
    let detail = detail_lines(state, theme, detail_budget, width);
    let list_height = usize::from(height)
        .saturating_sub(3 + extra_count + detail.len())
        .max(1);

    let mut lines = Vec::new();
    lines.push(header_line(state, theme, width));
    lines.push(filter_line(state, theme, width));

    if rows.is_empty() {
        lines.push(status_line(state, theme, width));
    } else {
        let offset = visible_offset(state.plugins.selected, list_height);
        lines.extend(
            rows.iter()
                .skip(offset)
                .take(list_height)
                .enumerate()
                .map(|(index, row)| {
                    row_line(row, theme, width, offset + index == state.plugins.selected)
                }),
        );
    }

    lines.extend(detail);
    if show_extra {
        lines.push(extra_line(state, theme, width));
    }
    lines.push(footer_line(state, theme, width));
    lines
}

pub fn render_plugin_panel(area: Rect, buf: &mut Buffer, state: &TuiState, theme: &Theme) {
    let mut lines = plugin_panel_lines(state, theme, area.width, area.height);
    while lines.len() < area.height as usize {
        lines.push(Line::from(""));
    }
    render_lines(area, buf, &lines);
}

fn header_line(state: &TuiState, theme: &Theme, width: u16) -> Line<'static> {
    let plugins = state.plugins.entries.len();
    let invalid = state.plugins.invalid.len();
    let total = plugins + invalid;
    let active = state
        .plugins
        .entries
        .iter()
        .filter(|entry| entry.state == "active")
        .count();
    let warnings = state.plugins.warnings.len();
    let mut remaining = width as usize;
    let mut spans = Vec::new();
    push_fit(
        &mut spans,
        &mut remaining,
        "Plugins".to_owned(),
        theme.accent,
    );
    push_fit(
        &mut spans,
        &mut remaining,
        format!(" · {total} total · {active} active"),
        theme.text,
    );
    push_fit(
        &mut spans,
        &mut remaining,
        format!(" · {invalid} invalid"),
        if invalid > 0 {
            theme.danger
        } else {
            theme.muted
        },
    );
    push_fit(
        &mut spans,
        &mut remaining,
        format!(" · {warnings} warnings"),
        if warnings > 0 {
            theme.warning
        } else {
            theme.muted
        },
    );
    if state.plugins.loading || state.plugins.stale {
        push_fit(
            &mut spans,
            &mut remaining,
            " · refreshing…".to_owned(),
            theme.muted,
        );
    }
    Line::from(spans)
}

fn push_fit(
    spans: &mut Vec<Span<'static>>,
    remaining: &mut usize,
    text: String,
    style: ratatui::style::Style,
) {
    if *remaining == 0 {
        return;
    }
    let text = fit(&text, *remaining);
    *remaining = remaining.saturating_sub(UnicodeWidthStr::width(text.as_str()));
    spans.push(Span::styled(text, style));
}

fn filter_line(state: &TuiState, theme: &Theme, width: u16) -> Line<'static> {
    let (text, style) = if state.plugins.filter.is_empty() {
        ("filter: (type to filter)".to_owned(), theme.muted)
    } else {
        (format!("filter: {}", state.plugins.filter), theme.text)
    };
    Line::from(Span::styled(fit(&text, width as usize), style))
}

fn status_line(state: &TuiState, theme: &Theme, width: u16) -> Line<'static> {
    let (text, style) = if let Some(error) = &state.plugins.error {
        (format!("{error} · Ctrl+R retry · Esc close"), theme.danger)
    } else if state.plugins.loading || !state.plugins.loaded {
        ("loading plugins…".to_owned(), theme.muted)
    } else if !state.plugins.filter.trim().is_empty() {
        ("no matching plugins".to_owned(), theme.muted)
    } else {
        (
            "no plugins discovered · put a plugin in ~/.linteum/plugins and add its id to agent.plugins.allow"
                .to_owned(),
            theme.muted,
        )
    };
    Line::from(Span::styled(fit(&text, width as usize), style))
}

fn row_line(row: &PluginRow<'_>, theme: &Theme, width: u16, selected: bool) -> Line<'static> {
    let with_trust = width >= 96;
    let fixed = 2 + 10 + 1 + 8 + 1 + 9 + 1 + if with_trust { 12 } else { 0 };
    let id_width = usize::from(width).saturating_sub(fixed).max(16);
    let (tools, skills, modes) = row.caps();
    let marker = if selected { "› " } else { "  " };
    let mut text = format!(
        "{marker}{:<10} {:<id_width$} {:<8} {:<9}",
        fit(row.state(), 10),
        fit(row.id(), id_width),
        fit(row.version(), 8),
        fit(&format!("{tools}/{skills}/{modes}"), 9),
    );
    if with_trust {
        text.push_str(&format!(" {}", fit(row.trust(), 11)));
    }
    let style = if selected {
        theme.selection
    } else {
        match row.state() {
            "failed" | "invalid" => theme.danger,
            "loading" | "quiescing" => theme.warning,
            "active" => theme.success,
            "disabled" | "discovered" | "unloaded" => theme.muted,
            _ => theme.text,
        }
    };
    Line::from(Span::styled(text, style))
}

fn detail_lines(state: &TuiState, theme: &Theme, budget: usize, width: u16) -> Vec<Line<'static>> {
    let Some(row) = state.plugins.selected_row() else {
        return Vec::new();
    };
    let width = width as usize;
    let mut lines = Vec::new();
    match row {
        PluginRow::Plugin(entry) => {
            lines.push(Line::from(fit(
                &format!(
                    "{} {} · {} · {}",
                    entry.id, entry.version, entry.state, entry.trust
                ),
                width,
            )));
            if let Some(error) = &entry.error {
                lines.push(Line::from(Span::styled(
                    fit(&format!("error {}: {}", error.code, error.message), width),
                    theme.danger,
                )));
            }
            if entry.description.is_empty() {
                lines.push(Line::from(Span::styled("(no description)", theme.muted)));
            } else {
                lines.push(Line::from(fit(&entry.description, width)));
            }
            lines.push(Line::from(fit(
                &format!(
                    "source {} · digest {}",
                    entry.source,
                    if entry.digest.is_empty() {
                        "—"
                    } else {
                        entry.digest.as_str()
                    }
                ),
                width,
            )));
            lines.push(Line::from(fit(&capabilities_line(entry), width)));
            if let Some(status) = status_detail(entry) {
                lines.push(Line::from(fit(&status, width)));
            }
        }
        PluginRow::Invalid(invalid) => {
            lines.push(Line::from(fit(
                &format!("invalid · {}", invalid.source),
                width,
            )));
            lines.push(Line::from(Span::styled(
                fit(&format!("{}: {}", invalid.code, invalid.error), width),
                theme.danger,
            )));
        }
    }
    lines.truncate(budget.max(1));
    lines
}

fn capabilities_line(entry: &PluginEntry) -> String {
    format!(
        "{} · {} · {}",
        kind_count("tools", &entry.tools),
        kind_count("skills", &entry.skills),
        kind_count("modes", &entry.modes),
    )
}

fn kind_count(label: &str, items: &[String]) -> String {
    if items.is_empty() {
        return format!("{label}: 0");
    }
    let shown = items
        .iter()
        .take(MAX_NAMES_PER_KIND)
        .cloned()
        .collect::<Vec<_>>()
        .join(", ");
    let more = if items.len() > MAX_NAMES_PER_KIND {
        format!(" +{}", items.len() - MAX_NAMES_PER_KIND)
    } else {
        String::new()
    };
    format!("{label}: {} ({shown}{more})", items.len())
}

fn status_detail(entry: &PluginEntry) -> Option<String> {
    let mut parts = Vec::new();
    match entry.state.as_str() {
        "disabled" => {
            parts.push("not enabled; add the id to agent.plugins.allow and restart".to_owned())
        }
        "discovered" => parts.push("not loaded".to_owned()),
        _ => {}
    }
    for warning in entry.warnings.iter().take(2) {
        parts.push(warning.clone());
    }
    if entry.warnings.len() > 2 {
        parts.push(format!("+{} more", entry.warnings.len() - 2));
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join(" · "))
    }
}

fn extra_line(state: &TuiState, theme: &Theme, width: u16) -> Line<'static> {
    if let Some(error) = &state.plugins.error {
        return Line::from(Span::styled(fit(error, width as usize), theme.danger));
    }
    if state.plugins.warnings.is_empty() {
        return Line::from("");
    }
    let first = &state.plugins.warnings[0];
    if state.plugins.warnings.len() > 1 {
        Line::from(Span::styled(
            fit(
                &format!(
                    "{first} · +{} more (linteum plugin list)",
                    state.plugins.warnings.len() - 1
                ),
                width as usize,
            ),
            theme.warning,
        ))
    } else {
        Line::from(Span::styled(fit(first, width as usize), theme.warning))
    }
}

fn footer_line(state: &TuiState, theme: &Theme, width: u16) -> Line<'static> {
    let offline = matches!(
        state.connection,
        ConnectionState::Offline | ConnectionState::Reconnecting { .. } | ConnectionState::Fatal
    );
    let text = if offline {
        "offline · type to filter · ↑↓ select · PgUp/PgDn · Ctrl+R refresh · Esc close"
    } else {
        "type to filter · ↑↓ select · PgUp/PgDn · Ctrl+R refresh · Esc close"
    };
    Line::from(Span::styled(fit(text, width as usize), theme.muted))
}

fn visible_offset(selected: usize, height: usize) -> usize {
    if height == 0 || selected < height {
        0
    } else {
        selected + 1 - height
    }
}

fn fit(text: &str, width: usize) -> String {
    if width == 0 {
        return String::new();
    }
    if UnicodeWidthStr::width(text) <= width {
        return text.to_owned();
    }
    let mut out = String::new();
    let mut used = 0usize;
    for ch in text.chars() {
        let char_width = ch.width().unwrap_or(0);
        if used + char_width > width.saturating_sub(1) {
            break;
        }
        out.push(ch);
        used += char_width;
    }
    out.push('…');
    out
}
