//! Background-task projection: one tool card, marked `[background]`, updated in
//! place when the task terminal arrives.

use linteum_tui::protocol::Frame;
use linteum_tui::ui::Theme;
use linteum_tui::ui::widgets::tool_card::tool_card_lines;
use linteum_tui::viewmodel::{Item, SessionView, ToolState, TuiState, apply_frame};

fn frame(name: &str, seq: u64, payload: serde_json::Value) -> Frame {
    Frame {
        v: 1,
        id: format!("f-{seq}"),
        frame: name.into(),
        payload,
        session_id: Some("s1".into()),
        seq: Some(seq),
        ephemeral: false,
        replay: false,
        ordinal: 0,
    }
}

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

fn tool_card<'a>(state: &'a TuiState, call_id: &str) -> &'a linteum_tui::viewmodel::ToolCard {
    state
        .transcript
        .items
        .iter()
        .find_map(|item| match item {
            Item::Tool(card) if card.call_id == call_id => Some(card),
            _ => None,
        })
        .expect("tool card")
}

#[test]
fn started_marks_the_start_card_as_background() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "tool.result",
            1,
            serde_json::json!({
                "call_id": "c1",
                "name": "bash",
                "ok": true,
                "output": "{\"status\":\"started\",\"task_id\":\"bg_1\"}",
                "background": true,
                "task_id": "bg_1",
                "background_status": "running",
            }),
        ),
        false,
    );

    let card = tool_card(&s, "c1");
    assert_eq!(card.name, "bash");
    assert!(card.background);
    assert_eq!(card.background_status.as_deref(), Some("running"));
    assert_eq!(card.task_id.as_deref(), Some("bg_1"));
}

#[test]
fn background_terminal_updates_the_same_card() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "tool.result",
            1,
            serde_json::json!({
                "call_id": "c1",
                "name": "bash",
                "ok": true,
                "output": "{\"status\":\"started\"}",
                "background": true,
                "task_id": "bg_1",
                "background_status": "running",
            }),
        ),
        false,
    );
    apply_frame(
        &mut s,
        &frame(
            "background.result",
            2,
            serde_json::json!({
                "task_id": "bg_1",
                "start_call_id": "c1",
                "name": "bash",
                "kind": "tool",
                "status": "completed",
                "ok": true,
                "output": "data-1\ndata-2",
                "duration_ms": 1234,
                "background": true,
                "background_status": "completed",
            }),
        ),
        false,
    );

    let card = tool_card(&s, "c1");
    assert!(card.background);
    assert_eq!(card.background_status.as_deref(), Some("completed"));
    assert_eq!(card.state, ToolState::Success);
    assert_eq!(card.output.as_deref(), Some("data-1\ndata-2"));
    assert_eq!(s.transcript.items.len(), 1, "no duplicate card");
}

#[test]
fn stopped_terminal_marks_the_card_cancelled() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "tool.result",
            1,
            serde_json::json!({
                "call_id": "c1", "name": "bash", "ok": true,
                "background": true, "task_id": "bg_1",
                "background_status": "running",
            }),
        ),
        false,
    );
    apply_frame(
        &mut s,
        &frame(
            "background.stopped",
            2,
            serde_json::json!({
                "task_id": "bg_1", "start_call_id": "c1", "name": "bash",
                "status": "stopped", "ok": false,
                "requested_by": "stop call c2", "duration_ms": 10,
                "background": true, "background_status": "stopped",
            }),
        ),
        false,
    );

    let card = tool_card(&s, "c1");
    assert_eq!(card.background_status.as_deref(), Some("stopped"));
    assert_eq!(card.state, ToolState::Cancelled);
}

#[test]
fn terminal_without_a_prior_start_creates_a_card() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "background.failed",
            1,
            serde_json::json!({
                "task_id": "bg_9", "start_call_id": "c9", "name": "bash",
                "status": "failed", "ok": false, "code": "E_TIMEOUT",
                "background": true, "background_status": "failed",
            }),
        ),
        false,
    );

    let card = tool_card(&s, "c9");
    assert!(card.background);
    assert_eq!(card.background_status.as_deref(), Some("failed"));
    assert_eq!(card.state, ToolState::Failure);
    assert_eq!(card.task_id.as_deref(), Some("bg_9"));
}

#[test]
fn snapshot_recreates_running_background_cards_once() {
    let mut s = state();
    let snapshot = serde_json::json!({
        "last_seq": 5, "phase": "idle", "round": 1, "busy": false,
        "pending": [], "streaming": null, "pending_approvals": [],
        "usage": {}, "bound_model": null, "context_digest": null,
        "last_error": null,
        "background": [{
            "task_id": "bg_1", "name": "bash", "kind": "tool",
            "status": "running", "started_at": 1.0, "duration_ms": 100
        }],
        "background_running": 1,
        "background_task_ids": ["bg_1"],
    });
    apply_frame(&mut s, &frame("snapshot", 5, snapshot.clone()), true);
    apply_frame(&mut s, &frame("snapshot", 5, snapshot), true);

    let cards: Vec<_> = s
        .transcript
        .items
        .iter()
        .filter_map(|item| match item {
            Item::Tool(card) if card.background => Some(card),
            _ => None,
        })
        .collect();
    assert_eq!(cards.len(), 1, "snapshot replay must not duplicate");
    assert_eq!(cards[0].name, "bash");
    assert_eq!(cards[0].background_status.as_deref(), Some("running"));
    assert_eq!(cards[0].task_id.as_deref(), Some("bg_1"));
}

#[test]
fn header_contains_the_background_marker_and_terminal_status() {
    let card = linteum_tui::viewmodel::ToolCard {
        name: "bash".into(),
        call_id: "c1".into(),
        background: true,
        background_status: Some("completed".into()),
        task_id: Some("bg_1".into()),
        state: ToolState::Success,
        ..Default::default()
    };
    let rendered: String = tool_card_lines(&card, &Theme::dark())
        .iter()
        .map(|line| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n");
    assert!(rendered.contains("[background] bash"));
    assert!(rendered.contains("completed"));
}
