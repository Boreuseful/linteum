use linteum_tui::protocol::{CommandResult, Frame};
use linteum_tui::ui::Theme;
use linteum_tui::ui::overlays::{
    render_subagent_context, subagent_context_lines, subagent_context_scroll_limit,
    subagent_list_lines,
};
use linteum_tui::viewmodel::{Input, TuiState, reduce};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::text::Line;
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

fn joined(lines: &[Line<'static>]) -> String {
    lines
        .iter()
        .map(|line| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn list_renders_status_phase_and_task() {
    let mut state = TuiState::default();
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
            "subagent.started",
            json!({"call_id": "c1", "child_session_id": "child-1", "read_only": true}),
        )),
    );
    reduce(
        &mut state,
        Input::Frame(frame(
            3,
            "subagent.progress",
            json!({"call_id": "c1", "phase": "acting", "round": 2, "last_tool": "read_file"}),
        )),
    );
    let rendered = joined(&subagent_list_lines(&state, &Theme::dark()));
    assert!(rendered.contains("review the module"));
    assert!(rendered.contains("running"));
    assert!(rendered.contains("acting"));
    assert!(rendered.contains("read_file"));
    assert!(rendered.contains("read-only"));
    assert!(rendered.contains("Enter: context"));
}

#[test]
fn list_prefers_the_generated_title_over_the_raw_task() {
    let mut state = TuiState::default();
    reduce(
        &mut state,
        Input::Frame(frame(
            1,
            "subagent.requested",
            json!({
                "call_id": "c1",
                "title": "Review auth module",
                "task": "Detailed instructions: review the authentication module",
            }),
        )),
    );
    let rendered = joined(&subagent_list_lines(&state, &Theme::dark()));
    assert!(rendered.contains("Review auth module"));
    assert!(!rendered.contains("Detailed instructions"));
}

#[test]
fn context_renders_messages_and_replay_note() {
    let mut state = TuiState::default();
    state.subagents.apply_context_result(&json!({
        "session_id": "child-1",
        "kind": "subagent",
        "phase": "idle",
        "round": 3,
        "source": "replay",
        "system_blocks_included": false,
        "digest": "abc123",
        "estimated_tokens": 42,
        "tools": ["read_file"],
        "messages": [
            {"role": "user", "content": "[Task]\nreview"},
            {"role": "assistant", "content": [], "tool_calls": [
                {"id": "call_1", "name": "read_file", "arguments": {"path": "a"}}
            ]}
        ],
    }));
    let rendered = joined(&subagent_context_lines(&state, &Theme::dark()));
    assert!(rendered.contains("Subagent context"));
    assert!(rendered.contains("source replay"));
    assert!(rendered.contains("system blocks are not persisted"));
    assert!(rendered.contains("[Task]"));
    assert!(rendered.contains("[tool_call] read_file"));
}

#[test]
fn context_scroll_stops_at_the_last_screenful_and_hides_the_page() {
    let mut state = TuiState::default();
    let messages: Vec<_> = (0..40)
        .map(|index| json!({"role": "user", "content": format!("message {index}")}))
        .collect();
    state.subagents.apply_context_result(&json!({
        "session_id": "child-1",
        "kind": "subagent",
        "source": "live",
        "system_blocks_included": true,
        "messages": messages,
    }));
    state.subagents.scroll = 10_000;
    let area = Rect::new(0, 0, 40, 10);
    let mut buffer = Buffer::empty(area);
    for cell in &mut buffer.content {
        cell.set_symbol("X");
    }
    render_subagent_context(area, &mut buffer, &state, &Theme::dark());
    let rendered: String = buffer.content.iter().map(|cell| cell.symbol()).collect();
    assert!(
        !rendered.contains('X'),
        "the agent page leaked through the context overlay"
    );
    let limit = subagent_context_scroll_limit(&state, 40, 10);
    assert!(limit > 0);
    assert!(
        state.subagents.scroll > limit,
        "the renderer must still clamp an over-large offset"
    );
}

#[test]
fn short_context_has_no_scroll_room() {
    let state = TuiState::default();
    assert_eq!(subagent_context_scroll_limit(&state, 80, 24), 0);
}

#[test]
fn context_result_binds_to_the_loading_request() {
    let mut state = TuiState::default();
    state.subagents.loading = Some("child-1".into());
    reduce(
        &mut state,
        Input::CommandResult(CommandResult {
            v: 1,
            id: "r1".into(),
            reply_to: "c1".into(),
            ok: true,
            result: json!({
                "context": {
                    "session_id": "child-1",
                    "kind": "subagent",
                    "source": "live",
                    "system_blocks_included": true,
                    "messages": [],
                }
            }),
            error: None,
        }),
    );
    assert!(state.subagents.detail.is_some());
    assert!(state.subagents.loading.is_none());
}
