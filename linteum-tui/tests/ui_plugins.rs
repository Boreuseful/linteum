#![allow(clippy::field_reassign_with_default)]

use linteum_tui::ui::Theme;
use linteum_tui::ui::screens::plugin_panel::plugin_panel_lines;
use linteum_tui::viewmodel::{
    ConnectionState, InvalidPlugin, Overlay, PluginEntry, PluginError, TuiState,
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
