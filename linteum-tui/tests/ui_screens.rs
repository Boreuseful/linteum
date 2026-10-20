#![allow(clippy::field_reassign_with_default)]

use linteum_tui::protocol::ErrorInfo;
use linteum_tui::ui::Theme;
use linteum_tui::ui::overlays::artifact::artifact_lines;
use linteum_tui::ui::screens::fatal::fatal_lines;
use linteum_tui::ui::screens::new_session::new_session_lines;
use linteum_tui::ui::screens::session_picker::picker_lines;
use linteum_tui::viewmodel::{ArtifactViewerState, NewSessionForm, SessionInfo, TuiState};
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

#[test]
fn picker_renders_columns_and_load_more() {
    let mut state = TuiState::default();
    state.picker.sessions = vec![SessionInfo {
        session_id: "s1".into(),
        title: Some("one".into()),
        phase: "idle".into(),
        message_count: 3,
        ..Default::default()
    }];
    state.picker.has_more = true;
    let rendered = joined(&picker_lines(&state, &Theme::dark()));
    assert!(rendered.contains("one"));
    assert!(rendered.contains("Load more"));
}

#[test]
fn picker_selection_line_shows_the_title_and_id() {
    let mut state = TuiState::default();
    state.picker.sessions = vec![
        SessionInfo {
            session_id: "s1".into(),
            title: Some("build the parser".into()),
            ..Default::default()
        },
        SessionInfo {
            session_id: "s2".into(),
            ..Default::default()
        },
    ];

    state.picker.selected = 0;
    let rendered = joined(&picker_lines(&state, &Theme::dark()));
    assert!(rendered.contains("selected: build the parser · s1"));

    state.picker.selected = 1;
    let rendered = joined(&picker_lines(&state, &Theme::dark()));
    assert!(rendered.contains("selected: (untitled) · s2"));
}

#[test]
fn picker_highlights_the_selected_row() {
    let mut state = TuiState::default();
    let theme = Theme::dark();
    state.picker.sessions = vec![
        SessionInfo {
            session_id: "s1".into(),
            title: Some("one".into()),
            ..Default::default()
        },
        SessionInfo {
            session_id: "s2".into(),
            title: Some("two".into()),
            ..Default::default()
        },
    ];
    state.picker.selected = 1;

    let lines = picker_lines(&state, &theme);
    assert_ne!(lines[1].style.bg, theme.selection.bg);
    assert_eq!(lines[2].style.bg, theme.selection.bg);
}

#[test]
fn new_session_shows_workspace_title_profile() {
    let mut state = TuiState::default();
    state.new_session = NewSessionForm {
        workspace: "/tmp".into(),
        title: "demo".into(),
        profile: "default".into(),
        ..Default::default()
    };
    let rendered = joined(&new_session_lines(&state, &Theme::dark()));
    assert!(rendered.contains("Workspace"));
    assert!(rendered.contains("/tmp"));
    assert!(rendered.contains("demo"));
    assert!(rendered.contains("default"));
}

#[test]
fn fatal_shows_error_and_child_stderr_tail() {
    let mut state = TuiState::default();
    state.last_error = Some(ErrorInfo {
        code: "E_SPAWN_FAILED".into(),
        source: "gateway".into(),
        location: "test".into(),
        message: "not found".into(),
        exc_type: None,
        detail: String::new(),
        retryable: false,
        enters_history: false,
        context: serde_json::Value::Null,
    });
    state.child_stderr_tail = vec!["boom".into()];
    let rendered = joined(&fatal_lines(&state, &Theme::dark()));
    assert!(rendered.contains("E_SPAWN_FAILED"));
    assert!(rendered.contains("boom"));
}

#[test]
fn artifact_viewer_marks_binary() {
    let mut state = TuiState::default();
    state.artifact = Some(ArtifactViewerState {
        size: 2,
        binary: true,
        ..Default::default()
    });
    let rendered = joined(&artifact_lines(&state, &Theme::dark()));
    assert!(rendered.contains("binary artifact"));
}

#[test]
fn new_session_shows_error() {
    let mut state = TuiState::default();
    state.new_session.error = Some("workspace is required".into());
    let rendered = joined(&new_session_lines(&state, &Theme::dark()));
    assert!(rendered.contains("workspace is required"));
}
