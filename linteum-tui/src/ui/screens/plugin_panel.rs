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
use crate::viewmodel::{
    ConnectionState, GatewayMode, PluginAction, PluginEntry, PluginRow, TuiState, pending_restart,
};

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
    let has_extra = state.plugins.error.is_some()
        || state.plugins.last_manage_error.is_some()
        || !state.plugins.warnings.is_empty();
    // With no rows the status line already carries the command error; do not
    // repeat it in the extra slot (§11.4.7).
    let error_in_status = rows.is_empty()
        && (state.plugins.error.is_some() || state.plugins.last_manage_error.is_some());
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
    let restart = state
        .plugins
        .entries
        .iter()
        .filter(|entry| pending_restart(entry))
        .count();
    if restart > 0 {
        push_fit(
            &mut spans,
            &mut remaining,
            format!(" · {restart} restart"),
            theme.warning,
        );
    }
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
    let status_error = state
        .plugins
        .error
        .as_ref()
        .or(state.plugins.last_manage_error.as_ref());
    let (text, style) = if let Some(error) = status_error {
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
    let fixed = 2 + 1 + 10 + 1 + 8 + 1 + 9 + 1 + if with_trust { 12 } else { 0 };
    let id_width = usize::from(width).saturating_sub(fixed).max(16);
    let (tools, skills, modes) = row.caps();
    let marker = if selected { "› " } else { "  " };
    let sign = match row.enabled() {
        Some(true) => '+',
        Some(false) => '-',
        None => '?',
    };
    let state_text = if matches!(row, PluginRow::Plugin(entry) if pending_restart(entry)) {
        format!("{}*", row.state())
    } else {
        row.state().to_owned()
    };
    let mut text = format!(
        "{marker}{sign}{:<10} {:<id_width$} {:<8} {:<9}",
        fit(&state_text, 10),
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
            if let Some(enabled) = entry.enabled {
                // Config vs. runtime are deliberately separate (§11.5.6).
                let mut line = format!(
                    "config: {} · runtime: {}",
                    if enabled { "enabled" } else { "disabled" },
                    entry.state
                );
                if pending_restart(entry) {
                    line.push_str(" · restart required");
                }
                lines.push(Line::from(fit(&line, width)));
            }
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
            if entry.enabled == Some(true) {
                parts.push("enabled; restart the host to load".to_owned());
            } else {
                parts.push("not enabled; add the id to agent.plugins.allow and restart".to_owned());
            }
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
    if let Some(error) = &state.plugins.last_manage_error {
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
    if let Some(action) = state.plugins.pending {
        // The confirmation replaces the footer, not the layout (§11.5.6).
        let entry = match state.plugins.selected_row() {
            Some(PluginRow::Plugin(entry)) => Some(entry),
            _ => None,
        };
        let (id, trust) = entry
            .map(|entry| (entry.id.as_str(), entry.trust.as_str()))
            .unwrap_or(("", ""));
        let text = match action {
            PluginAction::Enable => format!(
                "y/N · Enable {id} [{trust}]? Plugin code loads on the next host \
                 start. Writes agent.plugins.allow."
            ),
            PluginAction::Disable => format!(
                "y/N · Disable {id}? Running plugin stays active until the host \
                 restarts. Writes agent.plugins.allow."
            ),
            // No prefix: the sentence is the whole one-line bar, and a `y/N ·`
            // prefix would push its tail past 80 columns (§11.5.5).
            PluginAction::Restart => {
                "Restart gateway to apply saved changes? Active turn will be interrupted. y/N"
                    .to_owned()
            }
            PluginAction::Apply => {
                "y/N · Apply saved plugin changes to the running gateway now?".to_owned()
            }
        };
        return Line::from(Span::styled(fit(&text, width as usize), theme.warning));
    }
    let offline = matches!(
        state.connection,
        ConnectionState::Offline | ConnectionState::Reconnecting { .. } | ConnectionState::Fatal
    );
    let restart = if state.plugins.restarting {
        " · restarting…"
    } else if state.gateway_mode == GatewayMode::Spawn && state.plugins.restart_required {
        " · Ctrl+T restart"
    } else {
        ""
    };
    // Attach mode can apply the saved allow list without a restart when the
    // gateway advertises runtime management (post-v0.4 extension).
    let apply = if state.plugins.applying {
        " · applying…"
    } else if state.gateway_mode == GatewayMode::Attach
        && state.plugins.restart_required
        && state
            .features
            .iter()
            .any(|feature| feature == "plugins.runtime")
        && state.plugins.manage
    {
        " · Ctrl+A apply"
    } else {
        ""
    };
    let manage = if state.plugins.manage {
        " · Ctrl+E/Ctrl+D manage"
    } else {
        ""
    };
    let text = if offline {
        format!(
            "offline · type to filter · ↑↓ select · PgUp/PgDn · Ctrl+R refresh{restart}{apply}{manage} · Esc close"
        )
    } else {
        format!(
            "type to filter · ↑↓ select · PgUp/PgDn · Ctrl+R refresh{restart}{apply}{manage} · Esc close"
        )
    };
    Line::from(Span::styled(fit(&text, width as usize), theme.muted))
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
