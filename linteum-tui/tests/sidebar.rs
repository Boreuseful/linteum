#![allow(clippy::field_reassign_with_default)]

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use linteum_tui::protocol::Frame;
use linteum_tui::ui::{App, Theme, layout, render};
use linteum_tui::viewmodel::{
    AssistantItem, Effect, Focus, Input, Item, SidebarLayout, SidebarPanel, TuiState, UiAction,
    WorkspaceEntry, reduce, slash,
};
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use serde_json::json;

fn frame(seq: u64, name: &str, payload: serde_json::Value) -> Frame {
    Frame {
        v: 1,
        id: format!("f-{seq}"),
        frame: name.into(),
        payload,
        session_id: Some("parent".into()),
        seq: Some(seq),
        ephemeral: false,
        replay: false,
        ordinal: 0,
    }
}

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

fn with_files() -> TuiState {
    let mut state = TuiState::default();
    state.workspace_root = Some("/work/demo".into());
    let effects = reduce(
        &mut state,
        Input::Action(UiAction::SidebarShowPanel(SidebarPanel::Files)),
    );
    assert!(matches!(
        effects.as_slice(),
        [Effect::ReadWorkspaceDir { .. }]
    ));
    let _ = reduce(
        &mut state,
        Input::WorkspaceDir {
            path: "/work/demo".into(),
            entries: vec![
                WorkspaceEntry {
                    name: "src".into(),
                    path: "/work/demo/src".into(),
                    is_dir: true,
                },
                WorkspaceEntry {
                    name: "README.md".into(),
                    path: "/work/demo/README.md".into(),
                    is_dir: false,
                },
            ],
            error: None,
        },
    );
    state
}

fn rendered(state: &TuiState) -> String {
    let backend = TestBackend::new(80, 24);
    let mut terminal = Terminal::new(backend).unwrap();
    terminal
        .draw(|frame| render(frame, state, &Theme::dark()))
        .unwrap();
    terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}

#[test]
fn subagents_panel_shows_live_status() {
    let mut state = TuiState::default();
    state.workspace_root = Some("/work/demo".into());
    reduce(
        &mut state,
        Input::Frame(frame(
            1,
            "subagent.requested",
            json!({"call_id": "c1", "task": "review the module"}),
        )),
    );
    reduce(
        &mut state,
        Input::Frame(frame(
            2,
            "subagent.progress",
            json!({"call_id": "c1", "phase": "acting", "round": 2, "last_tool": "read_file"}),
        )),
    );
    let _ = reduce(
        &mut state,
        Input::Action(UiAction::SidebarShowPanel(SidebarPanel::Subagents)),
    );
    assert!(state.sidebar.visible);
    assert_eq!(state.sidebar.panel, SidebarPanel::Subagents);
    let rendered = rendered(&state);
    assert!(rendered.contains("Subagents"));
    assert!(rendered.contains("review"));
    assert!(rendered.contains("running"));
    assert!(rendered.contains("acting"));
    assert!(rendered.contains("read_file"));
}

#[test]
fn split_info_panel_shows_subagents_when_present() {
    let mut state = with_files();
    reduce(
        &mut state,
        Input::Frame(frame(
            10,
            "subagent.requested",
            json!({"call_id": "c1", "task": "review the module"}),
        )),
    );
    reduce(
        &mut state,
        Input::Frame(frame(
            11,
            "subagent.progress",
            json!({"call_id": "c1", "phase": "acting", "round": 2, "last_tool": "read_file"}),
        )),
    );
    reduce(&mut state, Input::Action(UiAction::SidebarSplit));
    let rendered = rendered(&state);
    assert!(rendered.contains("Subagents"));
    assert!(rendered.contains("running"));
    assert!(rendered.contains("review"));
}

#[test]
fn subagents_slash_command_selects_the_panel() {
    let command = slash::COMMANDS
        .iter()
        .find(|command| command.id == "subagents")
        .expect("subagents slash command");
    assert!(matches!(
        command.action,
        UiAction::SidebarShowPanel(SidebarPanel::Subagents)
    ));
}

#[test]
fn sidebar_is_closed_by_default() {
    let state = TuiState::default();
    assert!(!state.sidebar.visible);
    assert_eq!(state.focus, Focus::Composer);
    let rects = layout(Rect::new(0, 0, 80, 24), &state);
    assert_eq!(rects.sidebar.width, 0);
    assert_eq!(rects.transcript.width, 80);
}

#[test]
fn files_command_opens_the_sidebar_and_requests_the_workspace() {
    let mut state = TuiState::default();
    state.workspace_root = Some("/work".into());
    let effects = reduce(
        &mut state,
        Input::Action(UiAction::SidebarShowPanel(SidebarPanel::Files)),
    );
    assert!(state.sidebar.visible);
    assert_eq!(state.sidebar.root.as_deref(), Some("/work"));
    assert!(matches!(
        effects.as_slice(),
        [Effect::ReadWorkspaceDir { path }] if path == "/work"
    ));
}

#[test]
fn directory_results_build_the_tree_directories_first() {
    let state = with_files();
    let names: Vec<&str> = state
        .sidebar
        .rows
        .iter()
        .map(|row| row.name.as_str())
        .collect();
    assert_eq!(names, vec!["demo", "src", "README.md"]);
    assert_eq!(state.sidebar.rows[0].depth, 0);
    assert_eq!(state.sidebar.rows[1].depth, 1);
}

#[test]
fn collapse_and_expand_commands_toggle_the_selected_folder() {
    let mut state = with_files();
    assert_eq!(state.sidebar.rows.len(), 3);

    let effects = reduce(&mut state, Input::Action(UiAction::SidebarCollapseFolder));
    assert!(effects.is_empty());
    assert_eq!(state.sidebar.rows.len(), 1);
    assert!(!state.sidebar.rows[0].expanded);

    let effects = reduce(&mut state, Input::Action(UiAction::SidebarExpandFolder));
    assert!(effects.is_empty()); // already loaded
    assert_eq!(state.sidebar.rows.len(), 3);
}

#[test]
fn collapse_on_a_file_toasts() {
    let mut state = with_files();
    state.sidebar.selected = 2; // README.md
    let effects = reduce(&mut state, Input::Action(UiAction::SidebarCollapseFolder));
    assert!(matches!(
        effects.as_slice(),
        [Effect::ShowToast(toast)] if toast.text.contains("not a folder")
    ));
}

#[test]
fn panel_command_without_split_renders_one_panel() {
    for panel in [SidebarPanel::Files, SidebarPanel::Subagents] {
        let mut state = TuiState::default();
        state.workspace_root = Some("/work".into());
        reduce(&mut state, Input::Action(UiAction::SidebarShowPanel(panel)));
        assert_eq!(state.sidebar.layout, SidebarLayout::Merged);
        let rendered = rendered(&state);
        assert!(rendered.contains(panel.title()));
        assert!(!rendered.contains("Preview"));
        assert!(!rendered.contains("No content yet"));
    }
}

#[test]
fn panel_commands_leave_split_mode() {
    let mut state = with_files();
    reduce(&mut state, Input::Action(UiAction::SidebarSplit));
    assert_eq!(state.sidebar.layout, SidebarLayout::Split);

    reduce(
        &mut state,
        Input::Action(UiAction::SidebarShowPanel(SidebarPanel::Subagents)),
    );
    assert_eq!(state.sidebar.layout, SidebarLayout::Merged);
    assert_eq!(state.sidebar.panel, SidebarPanel::Subagents);
    let rendered = rendered(&state);
    assert!(rendered.contains("Subagents"));
    assert!(!rendered.contains("Preview"));
}

#[test]
fn toggle_sidebar_preserves_an_explicit_split() {
    let mut state = with_files();
    reduce(&mut state, Input::Action(UiAction::SidebarSplit));
    reduce(&mut state, Input::Action(UiAction::ToggleSidebar));
    assert!(!state.sidebar.visible);
    reduce(&mut state, Input::Action(UiAction::ToggleSidebar));
    assert!(state.sidebar.visible);
    assert_eq!(state.sidebar.layout, SidebarLayout::Split);
}

#[test]
fn merge_and_split_commands_open_the_sidebar() {
    let mut state = TuiState::default();
    state.workspace_root = Some("/work".into());
    reduce(&mut state, Input::Action(UiAction::SidebarSplit));
    assert!(state.sidebar.visible);
    assert_eq!(state.sidebar.layout, SidebarLayout::Split);
    reduce(&mut state, Input::Action(UiAction::SidebarMerge));
    assert_eq!(state.sidebar.layout, SidebarLayout::Merged);
}

#[test]
fn sidebar_layout_splits_the_body_when_visible() {
    let state = with_files();
    let rects = layout(Rect::new(0, 0, 80, 24), &state);
    assert!(rects.sidebar.width > 0);
    assert_eq!(
        rects.transcript.width + rects.sidebar.width,
        rects.header.width
    );
    assert_eq!(rects.transcript.y, rects.sidebar.y);
    assert_eq!(rects.transcript.height, rects.sidebar.height);
    assert_eq!(rects.sidebar.x, rects.transcript.x + rects.transcript.width);
}

#[test]
fn focus_toggles_only_when_the_sidebar_is_visible() {
    let mut state = TuiState::default();
    reduce(&mut state, Input::Action(UiAction::ToggleFocus));
    assert_eq!(state.focus, Focus::Composer);

    state.workspace_root = Some("/work".into());
    reduce(
        &mut state,
        Input::Action(UiAction::SidebarShowPanel(SidebarPanel::Files)),
    );
    reduce(&mut state, Input::Action(UiAction::ToggleFocus));
    assert_eq!(state.focus, Focus::Sidebar);
    reduce(&mut state, Input::Action(UiAction::ToggleFocus));
    assert_eq!(state.focus, Focus::Composer);

    reduce(&mut state, Input::Action(UiAction::ToggleSidebar));
    assert!(!state.sidebar.visible);
    assert_eq!(state.focus, Focus::Composer);
}

#[test]
fn sidebar_keys_navigate_toggle_and_return_to_the_composer() {
    let mut app = App::new(with_files(), Theme::dark());
    app.state.focus = Focus::Sidebar;

    let effects = app.handle_sidebar_key(key(KeyCode::Down)).unwrap();
    assert!(effects.is_empty());
    assert_eq!(app.state.sidebar.selected, 1); // src

    let effects = app.handle_sidebar_key(key(KeyCode::Enter)).unwrap();
    assert!(matches!(
        effects.as_slice(),
        [Effect::ReadWorkspaceDir { path }] if path == "/work/demo/src"
    ));
    let _ = reduce(
        &mut app.state,
        Input::WorkspaceDir {
            path: "/work/demo/src".into(),
            entries: vec![WorkspaceEntry {
                name: "main.rs".into(),
                path: "/work/demo/src/main.rs".into(),
                is_dir: false,
            }],
            error: None,
        },
    );
    assert_eq!(app.state.sidebar.rows.len(), 4);

    let effects = app.handle_sidebar_key(key(KeyCode::Enter)).unwrap();
    assert!(effects.is_empty());
    assert_eq!(app.state.sidebar.rows.len(), 3);
    assert!(
        !app.state
            .sidebar
            .rows
            .iter()
            .any(|row| row.name == "main.rs")
    );

    let effects = app.handle_sidebar_key(key(KeyCode::Tab)).unwrap();
    assert!(effects.is_empty());
    assert_eq!(app.state.focus, Focus::Composer);
}

#[test]
fn slash_commands_cover_the_sidebar() {
    let ids: Vec<&str> = slash::COMMANDS.iter().map(|command| command.id).collect();
    for id in [
        "files", "sidebar", "merge", "split", "collapse", "expand", "preview", "info", "refresh",
    ] {
        assert!(ids.contains(&id), "missing /{id}");
    }
}

#[test]
fn files_slash_command_opens_the_tree() {
    let mut state = TuiState::default();
    state.workspace_root = Some("/work".into());
    state.composer.text = "/files".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(state.sidebar.visible);
    assert_eq!(state.sidebar.panel, SidebarPanel::Files);
    assert!(matches!(
        effects.as_slice(),
        [Effect::ReadWorkspaceDir { path }] if path == "/work"
    ));
}

#[test]
fn merged_sidebar_renders_the_file_tree() {
    let state = with_files();
    let text = rendered(&state);
    assert!(text.contains("Files"));
    assert!(text.contains("demo"));
    assert!(text.contains("src"));
    assert!(text.contains("README.md"));
}

#[test]
fn long_output_wraps_inside_the_transcript_pane() {
    let mut state = with_files();
    state.transcript.items.push(Item::Assistant(AssistantItem {
        text: "这是一段很长的中文输出，用来验证侧边栏打开时文字会在对话区域内完整换行，\
               而不是被侧边栏遮住。"
            .into(),
        ..Default::default()
    }));

    let text = rendered(&state);
    // Wide glyphs occupy two cells; the second cell is a blank placeholder, so
    // compare with spaces removed. The tail shares a wrapped row with the
    // sidebar's left edge and must be visible in the transcript pane rather
    // than clipped under the sidebar.
    assert!(text.replace(' ', "").contains("而不是被侧边栏遮住"));
}

#[test]
fn split_sidebar_renders_three_panels() {
    let mut state = with_files();
    state.sidebar.layout = SidebarLayout::Split;
    let text = rendered(&state);
    assert!(text.contains("Files"));
    assert!(text.contains("Preview"));
    assert!(text.contains("Info"));
    assert!(text.contains("No preview yet"));
    assert!(text.contains("No content yet"));
}

#[tokio::test]
async fn host_reads_workspace_directories() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("sub")).unwrap();
    std::fs::write(dir.path().join("a.txt"), "x").unwrap();

    let (entries, error) = linteum_tui::workspace::read_dir(dir.path().to_str().unwrap()).await;
    assert!(error.is_none());
    let mut names: Vec<&str> = entries.iter().map(|entry| entry.name.as_str()).collect();
    names.sort();
    assert_eq!(names, vec!["a.txt", "sub"]);
    assert!(
        entries
            .iter()
            .find(|entry| entry.name == "sub")
            .unwrap()
            .is_dir
    );
}
