#![allow(clippy::field_reassign_with_default)]

use linteum_tui::protocol::ErrorInfo;
use linteum_tui::ui::Theme;
use linteum_tui::ui::screens::fatal::fatal_lines;
use linteum_tui::ui::screens::plugin_panel::plugin_panel_lines;
use linteum_tui::viewmodel::{
    ConnectionState, GatewayMode, InvalidPlugin, Overlay, PluginAction, PluginEntry, PluginError,
    TuiState,
};
use ratatui::text::Line;

fn text(line: &Line<'_>) -> String {
    line.spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect()
}

fn joined(lines: &[Line<'static>]) -> String {
    lines.iter().map(text).collect::<Vec<_>>().join("\n")
}

fn entry(id: &str, state: &str) -> PluginEntry {
    PluginEntry {
        id: id.into(),
        version: "1.0.0".into(),
        description: format!("{id} description"),
        state: state.into(),
        trust: "third-party".into(),
        source: id.replace('.', "-"),
        tools: vec!["web_search".into()],
        skills: vec!["web-research".into()],
        modes: vec!["research".into()],
        digest: "sha256:abc".into(),
        ..Default::default()
    }
}

fn state() -> TuiState {
    let mut state = TuiState::default();
    state.plugins.loaded = true;
    state.plugins.entries = vec![entry("acme.search", "active")];
    state.plugins.invalid = vec![InvalidPlugin {
        source: "broken".into(),
        code: "E_PLUGIN_MANIFEST".into(),
        error: "bad yaml".into(),
    }];
    state.plugins.warnings = vec!["plugin 'x' from allow was not found".into()];
    state
}

fn lines(state: &TuiState) -> Vec<Line<'static>> {
    plugin_panel_lines(state, &Theme::dark(), 80, 24)
}

#[test]
fn header_counts_plugins_and_invalid_directories() {
    let rendered = joined(&lines(&state()));
    assert!(rendered.contains("Plugins"));
    assert!(rendered.contains("2 total"));
    assert!(rendered.contains("1 active"));
    assert!(rendered.contains("1 invalid"));
    assert!(rendered.contains("1 warnings"));
}

#[test]
fn active_row_and_detail_show_capabilities() {
    let rendered = joined(&lines(&state()));
    assert!(rendered.contains("›"));
    assert!(rendered.contains("active"));
    assert!(rendered.contains("acme.search"));
    assert!(rendered.contains("1/1/1"));
    assert!(rendered.contains("acme.search description"));
    assert!(rendered.contains("source acme-search"));
    assert!(rendered.contains("digest sha256:abc"));
    assert!(rendered.contains("tools: 1 (web_search)"));
    assert!(rendered.contains("skills: 1 (web-research)"));
    assert!(rendered.contains("modes: 1 (research)"));
}

#[test]
fn long_ids_are_truncated_with_an_ellipsis() {
    let mut state = state();
    let long = "a".repeat(90);
    state.plugins.entries = vec![entry(&long, "active")];
    let rendered = joined(&lines(&state));
    assert!(rendered.contains('…'));
    assert!(!rendered.contains(&long));
}

#[test]
fn failed_plugin_shows_the_error() {
    let mut state = state();
    state.plugins.entries = vec![PluginEntry {
        state: "failed".into(),
        error: Some(PluginError {
            code: "E_PLUGIN_LOAD".into(),
            message: "boom".into(),
            hint: None,
        }),
        ..entry("acme.fail", "failed")
    }];
    let rendered = joined(&lines(&state));
    assert!(rendered.contains("error E_PLUGIN_LOAD: boom"));
}

#[test]
fn disabled_plugin_shows_the_config_hint() {
    let mut state = state();
    state.plugins.entries = vec![entry("other.disabled", "disabled")];
    let rendered = joined(&lines(&state));
    assert!(rendered.contains("not enabled"));
    assert!(rendered.contains("agent.plugins.allow"));
}

#[test]
fn invalid_manifest_row_shows_the_code_and_message() {
    let mut state = TuiState::default();
    state.plugins.loaded = true;
    state.plugins.invalid = vec![InvalidPlugin {
        source: "broken".into(),
        code: "E_PLUGIN_MANIFEST".into(),
        error: "bad yaml".into(),
    }];
    let rendered = joined(&lines(&state));
    assert!(rendered.contains("invalid"));
    assert!(rendered.contains("broken"));
    assert!(rendered.contains("E_PLUGIN_MANIFEST: bad yaml"));
}

#[test]
fn empty_loading_and_error_states_are_distinct() {
    let mut state = TuiState::default();
    state.plugins.loading = true;
    assert!(joined(&lines(&state)).contains("loading plugins…"));

    let mut state = TuiState::default();
    state.plugins.loaded = true;
    assert!(joined(&lines(&state)).contains("no plugins discovered"));

    let mut state = TuiState::default();
    state.plugins.error = Some("E_UNKNOWN_COMMAND: nope".into());
    let rendered = joined(&lines(&state));
    assert_eq!(rendered.matches("E_UNKNOWN_COMMAND: nope").count(), 1);
}

#[test]
fn command_error_bar_replaces_the_warnings_summary() {
    let mut state = state();
    state.plugins.error = Some("E_PAYLOAD_TOO_LARGE: too big".into());
    let rendered = joined(&lines(&state));
    assert!(rendered.contains("E_PAYLOAD_TOO_LARGE: too big"));
    assert!(!rendered.contains("plugin 'x' from allow was not found"));
}

#[test]
fn filter_without_matches_says_so() {
    let mut state = state();
    state.plugins.insert_str("zzz");
    assert!(joined(&lines(&state)).contains("no matching plugins"));
}

#[test]
fn footer_names_ctrl_r_and_the_panel_fits_the_viewport() {
    let lines = lines(&state());
    assert!(lines.len() <= 24);
    let footer = text(lines.last().expect("footer"));
    assert!(footer.contains("Ctrl+R refresh"));
    assert!(footer.contains("Esc close"));
}

#[test]
fn offline_footer_is_marked() {
    let mut state = state();
    state.connection = ConnectionState::Offline;
    let footer = text(lines(&state).last().expect("footer"));
    assert!(footer.contains("offline"));
}

#[test]
fn viewport_follows_the_selection_at_80x24() {
    let mut state = TuiState::default();
    state.plugins.loaded = true;
    state.plugins.entries = (0..30)
        .map(|index| entry(&format!("p{index:02}.plugin"), "active"))
        .collect();
    state.plugins.selected = 29;
    let rendered = joined(&lines(&state));
    for index in 22..30 {
        assert!(rendered.contains(&format!("p{index:02}.plugin")));
    }
    assert!(!rendered.contains("p00.plugin"));
}

#[test]
fn overlay_renders_at_80x24() {
    use linteum_tui::ui::{Theme, render};
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    let mut state = state();
    state.overlay = Some(Overlay::Plugins);
    let backend = TestBackend::new(80, 24);
    let mut terminal = Terminal::new(backend).unwrap();
    let theme = Theme::dark();
    terminal
        .draw(|frame| render(frame, &state, &theme))
        .unwrap();
    let rendered: String = terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(rendered.contains("Plugins"));
    assert!(rendered.contains("Ctrl+R refresh"));
    assert!(rendered.contains("acme.search"));
}

fn managed_state() -> TuiState {
    let mut state = state();
    state.plugins.manage = true;
    state.plugins.entries = vec![
        PluginEntry {
            enabled: Some(true),
            ..entry("acme.search", "active")
        },
        PluginEntry {
            enabled: Some(false),
            state: "disabled".into(),
            ..entry("other.disabled", "disabled")
        },
        PluginEntry {
            enabled: None,
            ..entry("legacy.plugin", "active")
        },
    ];
    state
}

#[test]
fn rows_show_enabled_markers() {
    let rendered = joined(&lines(&managed_state()));
    assert!(rendered.contains("+active"));
    assert!(rendered.contains("-disabled"));
    assert!(rendered.contains("?active"));
}

#[test]
fn pending_restart_is_marked_on_the_row_and_in_the_header() {
    let mut state = state();
    state.plugins.entries = vec![PluginEntry {
        enabled: Some(false),
        ..entry("acme.search", "active")
    }];
    let rendered = joined(&lines(&state));
    assert!(rendered.contains("active*"));
    assert!(rendered.contains("1 restart"));
    assert!(rendered.contains("config: disabled"));
    assert!(rendered.contains("runtime: active"));
    assert!(rendered.contains("restart required"));
}

#[test]
fn management_keys_appear_only_when_manage_is_enabled() {
    let read_only = text(lines(&state()).last().expect("footer"));
    assert!(!read_only.contains("Ctrl+E"));

    let managed = text(lines(&managed_state()).last().expect("footer"));
    assert!(managed.contains("Ctrl+E"));
    assert!(managed.contains("Ctrl+D"));
}

#[test]
fn enable_confirmation_replaces_the_footer() {
    let mut state = managed_state();
    state.plugins.pending = Some(PluginAction::Enable);
    let footer = text(lines(&state).last().expect("footer"));
    assert!(footer.contains("Enable acme.search"));
    assert!(footer.contains("third-party"));
    assert!(footer.contains("y/N"));
    assert!(!footer.contains("Ctrl+R refresh"));
}

#[test]
fn disable_confirmation_mentions_restart_semantics() {
    let mut state = managed_state();
    state.plugins.pending = Some(PluginAction::Disable);
    let footer = text(lines(&state).last().expect("footer"));
    assert!(footer.contains("Disable acme.search"));
    assert!(footer.contains("host restarts"));
    assert!(footer.contains("y/N"));
}

#[test]
fn manage_error_is_shown_once_when_the_list_is_empty() {
    let mut state = TuiState::default();
    state.plugins.loaded = true;
    state.plugins.last_manage_error = Some("E_PLUGIN_CONFIG: nope".into());
    let rendered = joined(&lines(&state));
    assert_eq!(rendered.matches("E_PLUGIN_CONFIG: nope").count(), 1);
}

fn restart_state() -> TuiState {
    let mut state = TuiState::default();
    state.plugins.loaded = true;
    state.plugins.entries = vec![
        PluginEntry {
            enabled: Some(true),
            ..entry("acme.search", "active")
        },
        PluginEntry {
            enabled: Some(false),
            state: "disabled".into(),
            ..entry("other.disabled", "disabled")
        },
    ];
    state.plugins.restart_required = true;
    state
}

#[test]
fn footer_and_confirmation_show_restart_in_spawn_mode() {
    let mut state = restart_state();
    state.gateway_mode = GatewayMode::Spawn;

    let before = lines(&state);
    assert!(before.len() <= 24);
    let footer = text(before.last().expect("footer"));
    assert!(footer.contains("Ctrl+T restart"));

    state.plugins.pending = Some(PluginAction::Restart);
    let confirmation = lines(&state);
    // The confirmation replaces the footer instead of adding a layout row.
    assert_eq!(confirmation.len(), before.len());
    let last = text(confirmation.last().expect("confirmation"));
    assert!(last.contains("y/N"));
    assert!(last.contains("Restart gateway"));
    assert!(last.contains("Active turn will be interrupted."));
    assert!(!last.contains("Ctrl+R refresh"));
}

#[test]
fn attach_mode_does_not_render_restart_footer() {
    let mut state = restart_state();
    state.gateway_mode = GatewayMode::Attach;
    let footer = text(lines(&state).last().expect("footer"));
    assert!(!footer.contains("Ctrl+T restart"));
    assert!(footer.contains("Ctrl+R refresh"));
}

#[test]
fn restarting_shows_progress_and_hides_the_restart_hint() {
    let mut state = restart_state();
    state.gateway_mode = GatewayMode::Spawn;
    state.plugins.restarting = true;
    let footer = text(lines(&state).last().expect("footer"));
    assert!(footer.contains("restarting…"));
    assert!(!footer.contains("Ctrl+T restart"));
}

/// Attach mode with runtime apply advertised, manage enabled, saved changes
/// pending a restart, and a parsed file-level allow list.
fn attach_apply_state() -> TuiState {
    let mut state = restart_state();
    state.gateway_mode = GatewayMode::Attach;
    state.features = vec!["view".into(), "plugins".into(), "plugins.runtime".into()];
    state.plugins.manage = true;
    state.plugins.allow = Some(vec!["acme.search".into()]);
    state
}

#[test]
fn attach_footer_shows_ctrl_a_apply_and_confirmation() {
    let state = attach_apply_state();
    let before = lines(&state);
    assert!(before.len() <= 24);
    let footer = text(before.last().expect("footer"));
    assert!(footer.contains("Ctrl+A apply"));
    // The apply hint sits before the manage hint so it fits at 80 cols.
    let apply_pos = footer.find("Ctrl+A apply").expect("apply hint");
    let manage_pos = footer.find("Ctrl+E").expect("manage hint");
    assert!(apply_pos < manage_pos);
    assert!(!footer.contains("Ctrl+T restart"));

    let mut pending = attach_apply_state();
    pending.plugins.pending = Some(PluginAction::Apply);
    let confirmation = lines(&pending);
    // The confirmation replaces the footer instead of adding a layout row.
    assert_eq!(confirmation.len(), before.len());
    let last = text(confirmation.last().expect("confirmation"));
    assert!(last.contains("Apply saved plugin changes"));
    assert!(last.contains("y/N"));
    assert!(!last.contains("Ctrl+R refresh"));
}

#[test]
fn applying_shows_progress_and_hides_the_apply_hint() {
    let mut state = attach_apply_state();
    state.plugins.applying = true;
    let footer = text(lines(&state).last().expect("footer"));
    assert!(footer.contains("applying…"));
    assert!(!footer.contains("Ctrl+A apply"));
}

#[test]
fn spawn_footer_keeps_ctrl_t_restart_and_has_no_ctrl_a() {
    let mut state = restart_state();
    state.gateway_mode = GatewayMode::Spawn;
    state.features = vec!["view".into(), "plugins".into(), "plugins.runtime".into()];
    state.plugins.manage = true;
    let footer = text(lines(&state).last().expect("footer"));
    assert!(footer.contains("Ctrl+T restart"));
    assert!(!footer.contains("Ctrl+A"));
}

#[test]
fn attach_without_runtime_feature_has_no_apply_hint() {
    let mut state = restart_state();
    state.gateway_mode = GatewayMode::Attach;
    state.plugins.manage = true;
    state.plugins.allow = Some(vec!["acme.search".into()]);
    // No plugins.runtime feature: apply is not offered even though saved
    // changes and management are present.
    let footer = text(lines(&state).last().expect("footer"));
    assert!(!footer.contains("Ctrl+A"));
    assert!(footer.contains("Ctrl+R refresh"));
}

#[test]
fn fatal_screen_offers_retry() {
    let mut state = TuiState::default();
    state.last_error = Some(ErrorInfo {
        code: "E_SPAWN_FAILED".into(),
        source: "gateway".into(),
        location: "test".into(),
        message: "not found".into(),
        exc_type: None,
        detail: "child failed to start".into(),
        retryable: true,
        enters_history: false,
        context: serde_json::Value::Null,
    });
    state.child_stderr_tail = vec!["boom".into()];

    state.gateway_mode = GatewayMode::Spawn;
    let rendered = joined(&fatal_lines(&state, &Theme::dark()));
    assert!(rendered.contains("r restart"));
    assert!(rendered.contains("q quit"));
    assert!(rendered.contains("E_SPAWN_FAILED"));
    assert!(rendered.contains("boom"));

    state.gateway_mode = GatewayMode::Attach;
    let rendered = joined(&fatal_lines(&state, &Theme::dark()));
    assert!(rendered.contains("q quit"));
    assert!(!rendered.contains("r restart"));
}
