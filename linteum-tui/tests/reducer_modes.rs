use linteum_tui::protocol::{CommandResult, ErrorInfo, Frame};
use linteum_tui::ui::Theme;
use linteum_tui::viewmodel::{
    ConnectionState, Effect, Input, ModeOption, Overlay, SessionView, TuiState, UiAction, reduce,
};
use serde_json::{Value, json};

fn state() -> TuiState {
    TuiState {
        session: Some(SessionView {
            id: "s1".into(),
            phase: "idle".into(),
            ..Default::default()
        }),
        ..Default::default()
    }
}

fn option(name: &str, id: &str, source: &str) -> ModeOption {
    ModeOption {
        id: id.into(),
        name: name.into(),
        description: format!("{name} mode"),
        source: source.into(),
        builtin: source == "builtin",
        default: name == "standard",
    }
}

fn loaded_state() -> TuiState {
    let mut state = state();
    state.mode.options = vec![
        option("standard", "builtin/standard", "builtin"),
        option("code", "builtin/code", "builtin"),
        option("review", "plugin:acme.tools/review", "plugin:acme.tools"),
    ];
    state.mode.list_loaded = true;
    state
}

fn ok(result: Value) -> CommandResult {
    CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: true,
        result,
        error: None,
    }
}

fn err(code: &str) -> CommandResult {
    CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: false,
        result: json!({}),
        error: Some(ErrorInfo {
            code: code.into(),
            source: "gateway".into(),
            location: "test".into(),
            message: code.into(),
            exc_type: None,
            detail: String::new(),
            retryable: false,
            enters_history: false,
            context: json!({}),
        }),
    }
}

fn mode_frame(value: Value) -> Frame {
    Frame {
        v: 1,
        id: "f".into(),
        frame: "mode".into(),
        payload: value,
        session_id: Some("s1".into()),
        seq: Some(2),
        ephemeral: false,
        replay: false,
        ordinal: 0,
    }
}

fn snapshot_frame(mode: Value) -> Frame {
    Frame {
        v: 1,
        id: "f".into(),
        frame: "snapshot".into(),
        payload: json!({
            "last_seq": 1, "phase": "idle", "round": 0, "busy": false,
            "pending": [], "streaming": null, "usage": {},
            "pending_approvals": [], "bound_model": null,
            "context_digest": null, "last_error": null,
            "mode": mode,
        }),
        session_id: Some("s1".into()),
        seq: Some(1),
        ephemeral: false,
        replay: false,
        ordinal: 0,
    }
}

fn set_mode_effect(effects: &[Effect]) -> Option<(&str, &str)> {
    effects.iter().find_map(|effect| match effect {
        Effect::SendCommand(command) if command.name == "session.mode.set" => Some((
            command
                .args
                .get("mode")
                .and_then(Value::as_str)
                .unwrap_or(""),
            command
                .args
                .get("session_id")
                .and_then(Value::as_str)
                .unwrap_or(""),
        )),
        _ => None,
    })
}

fn list_effect(effects: &[Effect]) -> bool {
    effects
        .iter()
        .any(|effect| matches!(effect, Effect::SendCommand(command) if command.name == "mode.list"))
}

#[test]
fn slash_mode_opens_the_picker_and_requests_the_list() {
    let mut state = state();
    state.composer.text = "/mode".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert_eq!(state.overlay, Some(Overlay::Mode));
    assert!(state.composer.text.is_empty());
    assert!(list_effect(&effects));
    assert!(set_mode_effect(&effects).is_none());
}

#[test]
fn slash_mode_status_reports_name_id_and_source() {
    let mut state = loaded_state();
    state.mode.current = Some(linteum_tui::viewmodel::CurrentMode {
        id: "builtin/code".into(),
        name: "code".into(),
        source: "builtin".into(),
        digest: "sha256:abc".into(),
        refresh: false,
    });
    state.composer.text = "/mode status".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(set_mode_effect(&effects).is_none());
    assert!(!list_effect(&effects));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::ShowToast(toast)
            if toast.text.contains("code")
                && toast.text.contains("builtin/code")
                && toast.text.contains("sha256:abc")
    )));
    assert!(state.composer.text.is_empty());
}

#[test]
fn slash_mode_help_is_local_only() {
    let mut state = state();
    state.composer.text = "/mode help".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(set_mode_effect(&effects).is_none());
    assert!(!list_effect(&effects));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::ShowToast(toast) if toast.text.contains("/mode <name-or-id>")
    )));
}

#[test]
fn slash_mode_switches_by_name() {
    let mut state = loaded_state();
    state.composer.text = "/mode code".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert_eq!(set_mode_effect(&effects), Some(("code", "s1")));
    assert!(!list_effect(&effects));
    assert!(state.composer.text.is_empty());
}

#[test]
fn slash_mode_switches_by_id() {
    let mut state = loaded_state();
    state.composer.text = "/mode plugin:acme.tools/review".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert_eq!(set_mode_effect(&effects), Some(("review", "s1")));
}

#[test]
fn slash_mode_unknown_with_loaded_list_is_local() {
    let mut state = loaded_state();
    state.composer.text = "/mode missing".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(set_mode_effect(&effects).is_none());
    assert!(!list_effect(&effects));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::ShowToast(toast) if toast.text.contains("missing")
    )));
}

#[test]
fn slash_mode_unknown_refreshes_once_then_switches() {
    let mut state = state();
    state.composer.text = "/mode code".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(list_effect(&effects));
    assert!(set_mode_effect(&effects).is_none());
    assert_eq!(state.mode.pending.as_deref(), Some("code"));

    let result = ok(json!({"modes": [
        {"id": "builtin/code", "name": "code", "description": "Code",
         "source": "builtin", "builtin": true, "default": false}
    ]}));
    let effects = reduce(&mut state, Input::CommandResult(result));
    assert_eq!(state.mode.pending, None);
    assert_eq!(set_mode_effect(&effects), Some(("code", "s1")));
}

#[test]
fn slash_mode_is_rejected_while_busy() {
    let mut state = loaded_state();
    state.session.as_mut().unwrap().phase = "thinking".into();
    state.composer.text = "/mode code".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(set_mode_effect(&effects).is_none());
    assert!(!list_effect(&effects));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::ShowToast(toast) if toast.text.contains("busy")
    )));
}

#[test]
fn slash_mode_is_rejected_for_read_only_attach() {
    let mut state = loaded_state();
    state.connection = ConnectionState::ReadOnly;
    state.composer.text = "/mode code".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(set_mode_effect(&effects).is_none());
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::ShowToast(toast) if toast.text.to_ascii_lowercase().contains("read")
    )));
}

#[test]
fn slash_mode_usage_rejects_extra_arguments() {
    let mut state = loaded_state();
    state.composer.text = "/mode code extra".into();
    let effects = reduce(&mut state, Input::Action(UiAction::ExecuteSlashCommand));
    assert!(set_mode_effect(&effects).is_none());
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::ShowToast(toast) if toast.text.contains("usage")
    )));
}

#[test]
fn mode_list_result_fills_options_without_touching_current() {
    let mut state = state();
    let result = ok(json!({
        "modes": [
            {"id": "builtin/standard", "name": "standard", "description": "General",
             "source": "builtin", "builtin": true, "default": true},
            {"id": "plugin:acme.tools/review", "name": "review", "description": "Review",
             "source": "plugin:acme.tools", "builtin": false, "default": false}
        ],
        "invalid": [], "warnings": []
    }));
    reduce(&mut state, Input::CommandResult(result));
    assert_eq!(state.mode.options.len(), 2);
    assert_eq!(state.mode.options[1].id, "plugin:acme.tools/review");
    assert!(state.mode.list_loaded);
    assert!(state.mode.current.is_none());
}

#[test]
fn snapshot_seeds_the_current_mode() {
    let mut state = state();
    reduce(
        &mut state,
        Input::Frame(snapshot_frame(json!({
            "id": "builtin/code", "name": "code", "source": "builtin",
            "digest": "sha256:abc", "refresh": false
        }))),
    );
    let current = state.mode.current.as_ref().expect("current mode");
    assert_eq!(current.id, "builtin/code");
    assert_eq!(current.name, "code");
}

#[test]
fn mode_frame_updates_the_current_mode() {
    let mut state = loaded_state();
    reduce(
        &mut state,
        Input::Frame(mode_frame(json!({
            "id": "plugin:acme.tools/review", "name": "review",
            "source": "plugin:acme.tools", "digest": "sha256:def",
            "from": "code", "refresh": true
        }))),
    );
    let current = state.mode.current.as_ref().expect("current mode");
    assert_eq!(current.id, "plugin:acme.tools/review");
    assert!(current.refresh);
}

#[test]
fn mode_set_result_updates_current_and_closes_the_picker() {
    let mut state = loaded_state();
    state.overlay = Some(Overlay::Mode);
    let result = ok(json!({
        "session_id": "s1", "changed": true,
        "mode": {
            "id": "builtin/code", "name": "code", "source": "builtin",
            "digest": "sha256:abc", "tools": 7, "skills": 2
        },
        "warnings": []
    }));
    let effects = reduce(&mut state, Input::CommandResult(result));
    assert_eq!(state.overlay, None);
    let current = state.mode.current.as_ref().expect("current mode");
    assert_eq!(current.name, "code");
    assert_eq!(state.mode.last_switch.as_ref().unwrap().tools, 7);
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::ShowToast(toast) if toast.text.contains("[mode] code")
    )));
}

#[test]
fn mode_error_preserves_the_current_mode() {
    let mut state = loaded_state();
    state.mode.current = Some(linteum_tui::viewmodel::CurrentMode {
        id: "builtin/standard".into(),
        name: "standard".into(),
        source: "builtin".into(),
        ..Default::default()
    });
    let effects = reduce(&mut state, Input::CommandResult(err("E_MODE_BUSY")));
    assert_eq!(state.mode.current.as_ref().unwrap().name, "standard");
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::ShowToast(toast) if toast.text.contains("E_MODE_BUSY")
    )));
}

#[test]
fn picker_actions_move_and_select() {
    let mut state = loaded_state();
    state.overlay = Some(Overlay::Mode);
    reduce(&mut state, Input::Action(UiAction::ModeSelectNext));
    assert_eq!(state.mode.picker.selected, 1);
    reduce(&mut state, Input::Action(UiAction::ModeSelectPrevious));
    assert_eq!(state.mode.picker.selected, 0);
    let effects = reduce(&mut state, Input::Action(UiAction::ModeSelect));
    assert_eq!(set_mode_effect(&effects), Some(("standard", "s1")));
}

#[test]
fn new_session_sends_the_mode_name() {
    let mut state = loaded_state();
    state.new_session.workspace = "/tmp".into();
    state.new_session.mode = "code".into();
    let effects = reduce(&mut state, Input::Action(UiAction::SubmitNewSession));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command)
            if command.name == "session.create"
                && command.args.get("mode").and_then(Value::as_str) == Some("code")
    )));
}

#[test]
fn new_session_resolves_an_id_before_creating() {
    let mut state = state();
    state.new_session.workspace = "/tmp".into();
    state.new_session.mode = "plugin:acme.tools/review".into();
    let effects = reduce(&mut state, Input::Action(UiAction::SubmitNewSession));
    assert!(list_effect(&effects));
    assert!(state.mode.pending_create.is_some());

    let result = ok(json!({"modes": [
        {"id": "plugin:acme.tools/review", "name": "review", "description": "Review",
         "source": "plugin:acme.tools", "builtin": false, "default": false}
    ]}));
    let effects = reduce(&mut state, Input::CommandResult(result));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command)
            if command.name == "session.create"
                && command.args.get("mode").and_then(Value::as_str) == Some("review")
    )));
}

#[test]
fn new_session_unknown_mode_does_not_create() {
    let mut state = loaded_state();
    state.new_session.workspace = "/tmp".into();
    state.new_session.mode = "missing".into();
    let effects = reduce(&mut state, Input::Action(UiAction::SubmitNewSession));
    assert!(!effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command) if command.name == "session.create"
    )));
    assert!(
        state
            .new_session
            .error
            .as_deref()
            .is_some_and(|error| { error.contains("missing") })
    );
}

#[test]
fn status_bar_shows_the_current_mode() {
    let mut state = loaded_state();
    state.mode.current = Some(linteum_tui::viewmodel::CurrentMode {
        id: "plugin:acme.tools/review".into(),
        name: "review".into(),
        source: "plugin:acme.tools".into(),
        ..Default::default()
    });
    let status = linteum_tui::ui::status_bar(&state, 120);
    assert!(status.contains("mode review"));
    assert!(status.contains("plugin:acme.tools"));
}

#[test]
fn mode_picker_lists_options_and_marks_current() {
    let mut state = loaded_state();
    state.mode.current = Some(linteum_tui::viewmodel::CurrentMode {
        id: "builtin/code".into(),
        name: "code".into(),
        source: "builtin".into(),
        ..Default::default()
    });
    state.mode.picker.filter = "code".into();
    let lines = linteum_tui::ui::screens::mode_picker_lines(&state, &Theme::dark());
    let text: String = lines
        .iter()
        .flat_map(|line| line.spans.iter())
        .map(|span| span.content.as_ref())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(text.contains("builtin/code"));
    assert!(text.contains("current"));
}

#[test]
fn picker_filter_matches_name_id_and_source() {
    let mut state = loaded_state();
    state.overlay = Some(Overlay::Mode);
    assert_eq!(state.mode.filtered_options().len(), 3);
    state.mode.picker.filter = "plugin".into();
    assert_eq!(state.mode.filtered_options().len(), 1);
    assert_eq!(state.mode.filtered_options()[0].name, "review");
    state.mode.picker.filter = "builtin/code".into();
    assert_eq!(state.mode.filtered_options()[0].name, "code");
}

#[test]
fn mode_picker_clears_the_whole_page() {
    use ratatui::buffer::Buffer;
    use ratatui::layout::Rect;

    let mut state = loaded_state();
    state.overlay = Some(Overlay::Mode);
    let area = Rect::new(0, 0, 80, 24);
    let theme = Theme::dark();
    let mut buf = Buffer::empty(area);
    for cell in &mut buf.content {
        cell.set_symbol("X");
    }

    linteum_tui::ui::screens::render_mode_picker(area, &mut buf, &state, &theme);

    let leftover: String = buf
        .content
        .iter()
        .map(|cell| cell.symbol())
        .filter(|symbol| *symbol == "X")
        .collect();
    assert!(
        leftover.is_empty(),
        "transcript glyphs leaked through the mode picker: {leftover}"
    );
}
