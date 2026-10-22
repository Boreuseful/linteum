use linteum_tui::protocol::{CommandResult, Frame};
use linteum_tui::viewmodel::{Effect, Input, Overlay, SubagentStatus, TuiState, UiAction, reduce};
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

#[test]
fn subagent_frames_track_status_and_progress() {
    let mut state = TuiState::default();
    reduce(
        &mut state,
        Input::Frame(frame(
            1,
            "subagent.requested",
            json!({"call_id": "c1", "task": "review the module", "tools": ["read_file"]}),
        )),
    );
    reduce(
        &mut state,
        Input::Frame(frame(
            2,
            "subagent.started",
            json!({"call_id": "c1", "child_session_id": "child-1", "read_only": true}),
        )),
    );
    reduce(
        &mut state,
        Input::Frame(frame(
            3,
            "subagent.progress",
            json!({
                "call_id": "c1", "phase": "acting", "round": 2,
                "last_tool": "read_file", "tokens_input": 12, "tokens_output": 4,
            }),
        )),
    );
    let entry = &state.subagents.entries[0];
    assert_eq!(entry.status, SubagentStatus::Running);
    assert_eq!(entry.child_session_id.as_deref(), Some("child-1"));
    assert_eq!(entry.phase.as_deref(), Some("acting"));
    assert_eq!(entry.last_tool.as_deref(), Some("read_file"));
    assert!(entry.read_only);
    assert_eq!(state.subagents.running_count(), 1);
}

#[test]
fn completed_and_failed_are_terminal() {
    let mut state = TuiState::default();
    reduce(
        &mut state,
        Input::Frame(frame(
            1,
            "subagent.result",
            json!({"call_id": "c1", "child_session_id": "child-1", "summary": "done"}),
        )),
    );
    reduce(
        &mut state,
        Input::Frame(frame(
            2,
            "subagent.error",
            json!({
                "call_id": "c2", "child_session_id": "child-2",
                "code": "E_SUBAGENT_TIMEOUT", "error": "late",
                "error_info": {
                    "code": "E_SUBAGENT_TIMEOUT", "source": "subagent",
                    "message": "late",
                },
            }),
        )),
    );
    assert_eq!(state.subagents.entries[0].status, SubagentStatus::Completed);
    assert_eq!(state.subagents.entries[1].status, SubagentStatus::Timeout);
    assert_eq!(state.subagents.running_count(), 0);
}

#[test]
fn generated_title_drives_the_display_preview() {
    let mut state = TuiState::default();
    reduce(
        &mut state,
        Input::Frame(frame(
            1,
            "subagent.requested",
            json!({
                "call_id": "c1",
                "title": "Review auth module",
                "task": "Review the authentication module in depth and report findings",
            }),
        )),
    );
    let entry = &state.subagents.entries[0];
    assert_eq!(entry.title.as_deref(), Some("Review auth module"));
    assert_eq!(entry.title_preview(48), "Review auth module");
}

#[test]
fn cancellation_notice_marks_running_entries() {
    let mut state = TuiState::default();
    reduce(
        &mut state,
        Input::Frame(frame(
            1,
            "subagent.started",
            json!({"call_id": "c1", "child_session_id": "child-1"}),
        )),
    );
    reduce(
        &mut state,
        Input::Frame(frame(
            2,
            "notice",
            json!({"kind": "subagent_cancel", "call_ids": ["c1"]}),
        )),
    );
    assert_eq!(state.subagents.entries[0].status, SubagentStatus::Cancelled);
}

#[test]
fn open_selected_subagent_requests_child_context() {
    let mut state = TuiState::default();
    reduce(
        &mut state,
        Input::Frame(frame(
            1,
            "subagent.started",
            json!({"call_id": "c1", "child_session_id": "child-1"}),
        )),
    );
    let effects = reduce(&mut state, Input::Action(UiAction::OpenSubagents));
    assert!(effects.is_empty());
    assert_eq!(state.overlay, Some(Overlay::Subagents));
    let effects = reduce(&mut state, Input::Action(UiAction::OpenSelectedSubagent));
    assert_eq!(state.overlay, Some(Overlay::SubagentContext));
    assert_eq!(state.subagents.loading.as_deref(), Some("child-1"));
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command)
            if command.name == "session.context"
                && command.session_id.as_deref() == Some("child-1")
    )));
}

#[test]
fn context_result_populates_messages_and_digest() {
    let mut state = TuiState::default();
    state.subagents.loading = Some("child-1".into());
    let result = CommandResult {
        v: 1,
        id: "r1".into(),
        reply_to: "c1".into(),
        ok: true,
        result: json!({
            "context": {
                "session_id": "child-1",
                "kind": "subagent",
                "phase": "acting",
                "round": 2,
                "source": "live",
                "system_blocks_included": true,
                "digest": "abc123",
                "estimated_tokens": 42,
                "tools": ["read_file"],
                "messages": [
                    {"role": "system", "content": "[Subagent role]\nreview"},
                    {"role": "user", "content": [{"type": "text", "text": "task"}]},
                    {"role": "assistant", "content": [], "tool_calls": [
                        {"id": "call_1", "name": "read_file", "arguments": {"path": "a"}}
                    ]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
                ],
            },
        }),
        error: None,
    };
    reduce(&mut state, Input::CommandResult(result));
    let detail = state.subagents.detail.as_ref().expect("context detail");
    assert_eq!(detail.session_id, "child-1");
    assert!(detail.system_blocks_included);
    assert_eq!(detail.estimated_tokens, 42);
    assert_eq!(detail.messages.len(), 4);
    assert_eq!(detail.messages[0].role, "system");
    assert!(detail.messages[0].text.contains("[Subagent role]"));
    assert!(detail.messages[2].text.contains("[tool_call] read_file"));
    assert!(detail.messages[3].text.contains("[tool_result call_1]"));
    assert!(state.subagents.loading.is_none());
}
