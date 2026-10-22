use linteum_tui::protocol::{CommandResult, ErrorInfo};
use linteum_tui::viewmodel::{
    ArtifactError, ArtifactViewerState, ConnectionState, Effect, Input, Overlay, SessionInfo,
    SessionPickerState, SessionView, TuiState, UiAction, reduce,
};

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

fn state_with_sessions() -> TuiState {
    let mut state = state();
    state.picker.sessions = vec![SessionInfo {
        session_id: "s1".into(),
        title: Some("one".into()),
        phase: "idle".into(),
        ..Default::default()
    }];
    state
}

fn result_err(id: &str, code: &str) -> CommandResult {
    CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: false,
        result: serde_json::json!({"client_message_id": id}),
        error: Some(ErrorInfo {
            code: code.into(),
            source: "gateway".into(),
            location: "test".into(),
            message: code.into(),
            exc_type: None,
            detail: String::new(),
            retryable: false,
            enters_history: false,
            context: serde_json::json!({}),
        }),
    }
}

#[test]
fn session_list_fills_picker_and_infers_has_more() {
    let mut picker = SessionPickerState::default();
    let result = CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: true,
        result: serde_json::json!({"limit": 2, "offset": 0, "sessions": [
            {"session_id": "s1", "title": "one", "phase": "idle", "round": 1,
             "message_count": 4, "last_seq": 9, "updated_at": 2.0, "created_at": 1.0},
            {"session_id": "s2", "title": null, "phase": "paused", "round": 0,
             "message_count": 0, "last_seq": 0, "updated_at": 1.0, "created_at": 1.0}
        ]}),
        error: None,
    };
    picker.apply_list(&result);
    assert_eq!(picker.sessions.len(), 2);
    assert!(picker.has_more);
    assert_eq!(picker.sessions[0].session_id, "s1");
}

#[test]
fn selecting_session_emits_subscribe() {
    let mut s = state_with_sessions();
    let effects = reduce(&mut s, Input::Action(UiAction::OpenSelectedSession));
    assert!(matches!(&effects[0], Effect::SendCommand(c)
        if c.name == "session.subscribe" && c.args["session_id"] == "s1"));
}

#[test]
fn new_session_validates_workspace_and_title() {
    let mut s = state();
    s.new_session.workspace.clear();
    let effects = reduce(&mut s, Input::Action(UiAction::SubmitNewSession));
    assert!(effects.is_empty());
    s.new_session.workspace = "/tmp".into();
    s.new_session.title = "x".repeat(513);
    let effects = reduce(&mut s, Input::Action(UiAction::SubmitNewSession));
    assert!(effects.is_empty());
}

#[test]
fn artifact_chunk_appends_and_tracks_eof() {
    let mut artifact = ArtifactViewerState::default();
    artifact
        .apply_chunk(&serde_json::json!({"ref": "art:1", "offset": 0, "size": 6,
            "encoding": "base64", "data": "aGVsbG8=", "eof": true}))
        .unwrap();
    assert_eq!(artifact.text, "hello");
    assert!(artifact.eof);
}

#[test]
fn artifact_binary_is_marked_not_displayable() {
    let mut artifact = ArtifactViewerState::default();
    assert_eq!(
        artifact.apply_chunk(&serde_json::json!({"ref": "art:1", "offset": 0,
            "size": 2, "encoding": "base64", "data": "AAE=", "eof": true})),
        Err(ArtifactError::Binary)
    );
}

#[test]
fn unknown_ref_error_is_preserved() {
    let mut s = state();
    reduce(
        &mut s,
        Input::CommandResult(result_err("r", "E_UNKNOWN_REF")),
    );
    assert_eq!(s.last_error.as_ref().unwrap().code, "E_UNKNOWN_REF");
}

#[test]
fn session_list_appends_pages() {
    let mut picker = SessionPickerState::default();
    let page = |offset: usize, ids: &[&str], limit: usize| CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: true,
        result: serde_json::json!({
            "limit": limit,
            "offset": offset,
            "sessions": ids.iter().map(|id| serde_json::json!({
                "session_id": id, "phase": "idle", "updated_at": 1.0
            })).collect::<Vec<_>>()
        }),
        error: None,
    };
    picker.apply_list(&page(0, &["a", "b"], 2));
    assert!(picker.has_more);
    picker.apply_list(&page(2, &["c"], 2));
    assert_eq!(picker.sessions.len(), 3);
    assert!(!picker.has_more);
}

#[test]
fn workspace_filter_is_sent() {
    let mut s = state();
    s.picker_workspace = Some("/work".into());
    let effects = reduce(&mut s, Input::Action(UiAction::RefreshSessions));
    assert!(matches!(&effects[0], Effect::SendCommand(command)
        if command.name == "session.list" && command.args["workspace"] == "/work"));
}

#[test]
fn switching_sessions_resets_state_and_saves_drafts() {
    let mut s = state();
    s.picker.sessions = vec![SessionInfo {
        session_id: "s2".into(),
        phase: "idle".into(),
        ..Default::default()
    }];
    s.usage.input_tokens = 10;
    s.queue.enqueue("pending".into(), "m1".into());
    s.composer.text = "draft".into();

    let effects = reduce(&mut s, Input::Action(UiAction::OpenSelectedSession));

    assert_eq!(s.session.as_ref().unwrap().id, "s2");
    assert_eq!(s.usage.input_tokens, 0);
    assert!(s.queue.entries.is_empty());
    assert_eq!(s.composer.text, "");
    assert_eq!(s.drafts.get("s1").map(String::as_str), Some("draft"));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command) if command.name == "session.unsubscribe"
    )));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command) if command.name == "session.subscribe"
    )));
}

#[test]
fn new_session_invalid_sets_error() {
    let mut s = state();
    let effects = reduce(&mut s, Input::Action(UiAction::SubmitNewSession));
    assert!(effects.is_empty());
    assert!(s.new_session.error.is_some());
}

#[test]
fn session_not_found_resets_to_picker() {
    let mut s = state();
    reduce(
        &mut s,
        Input::CommandResult(result_err("r", "E_SESSION_NOT_FOUND")),
    );
    assert!(s.session.is_none());
    assert_eq!(s.overlay, Some(Overlay::Picker));
}

#[test]
fn empty_session_list_opens_new_session_dialog() {
    let mut s = state();
    s.overlay = Some(Overlay::Picker);
    let result = CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: true,
        result: serde_json::json!({"limit": 50, "offset": 0, "sessions": []}),
        error: None,
    };
    reduce(&mut s, Input::CommandResult(result));
    assert_eq!(s.overlay, Some(Overlay::NewSession));
}

#[test]
fn subscribe_result_marks_live() {
    let mut s = state();
    s.connection = ConnectionState::Replaying;
    let result = CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: true,
        result: serde_json::json!({
            "subscribed": true, "mode": "view", "after_seq": 0, "snapshot_seq": 0
        }),
        error: None,
    };
    reduce(&mut s, Input::CommandResult(result));
    assert_eq!(s.connection, ConnectionState::Live);
}
