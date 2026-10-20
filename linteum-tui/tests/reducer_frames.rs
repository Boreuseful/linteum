use linteum_tui::protocol::Frame;
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

#[test]
fn snapshot_initializes_status() {
    let mut s = state();
    let snapshot = serde_json::json!({
        "last_seq": 12, "phase": "thinking", "round": 2, "busy": true,
        "pending": [], "streaming": null, "pending_approvals": [],
        "usage": {"input_tokens": 100, "output_tokens": 20, "tokens": 120,
                  "cache_read": 5, "cache_write": 0, "usd": 0.001},
        "bound_model": "fake/model", "context_digest": "sha256:abc", "last_error": null
    });
    apply_frame(&mut s, &frame("snapshot", 12, snapshot), true);
    assert_eq!(s.session.as_ref().unwrap().phase, "thinking");
    assert_eq!(s.session.as_ref().unwrap().round, 2);
    assert_eq!(s.usage.input_tokens, 100);
    assert_eq!(s.usage.output_tokens, 20);
    assert_eq!(s.usage.usd, 0.001);
    assert_eq!(
        s.session.as_ref().unwrap().bound_model.as_deref(),
        Some("fake/model")
    );
}

#[test]
fn live_user_message_creates_item() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "user.message",
            1,
            serde_json::json!({
                "text": "hello", "client_message_id": "m-1", "event_id": "e-1"
            }),
        ),
        false,
    );
    assert_eq!(s.transcript.items.len(), 1);
    assert!(matches!(s.transcript.items[0], Item::User(_)));
}

#[test]
fn assistant_delta_appends_to_active_item() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "assistant.started",
            2,
            serde_json::json!({
                "provider": "fake", "model": "m", "round": 1
            }),
        ),
        false,
    );
    let mut delta = frame(
        "assistant.delta",
        2,
        serde_json::json!({"text": "Hel", "request_id": "r"}),
    );
    delta.ephemeral = true;
    apply_frame(&mut s, &delta, false);
    let mut delta2 = frame(
        "assistant.delta",
        2,
        serde_json::json!({"text": "lo", "request_id": "r"}),
    );
    delta2.ephemeral = true;
    apply_frame(&mut s, &delta2, false);
    assert!(matches!(&s.transcript.items[0], Item::Assistant(a) if a.text == "Hello"));
}

#[test]
fn assistant_message_finalizes_and_adds_usage() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "assistant.started",
            2,
            serde_json::json!({
                "provider": "fake", "model": "m", "round": 1
            }),
        ),
        false,
    );
    let mut delta = frame("assistant.delta", 2, serde_json::json!({"text": "partial"}));
    delta.ephemeral = true;
    apply_frame(&mut s, &delta, false);
    apply_frame(
        &mut s,
        &frame(
            "assistant.message",
            2,
            serde_json::json!({
                "message": {"role": "assistant", "content": "final"},
                "tool_calls": [], "finish": "stop",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "cost": {"total_usd": 0.002}, "provider": "fake", "model_actual": "m", "round": 1
            }),
        ),
        false,
    );
    let assistant = s
        .transcript
        .items
        .iter()
        .find_map(|i| match i {
            Item::Assistant(a) => Some(a),
            _ => None,
        })
        .unwrap();
    assert_eq!(assistant.text, "final");
    assert_eq!(s.usage.input_tokens, 10);
    assert_eq!(s.usage.output_tokens, 5);
    assert!((s.usage.usd - 0.002).abs() < 1e-9);
}

#[test]
fn tool_lifecycle_creates_and_finalizes_card() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "tool.requested",
            3,
            serde_json::json!({
                "call_id": "c1", "name": "echo", "args": {"text": "hi"}
            }),
        ),
        false,
    );
    apply_frame(
        &mut s,
        &frame(
            "tool.started",
            3,
            serde_json::json!({
                "call_id": "c1", "name": "echo", "side_effect": "pure"
            }),
        ),
        false,
    );
    apply_frame(
        &mut s,
        &frame(
            "tool.result",
            3,
            serde_json::json!({
                "call_id": "c1", "name": "echo", "ok": true, "output": "hi"
            }),
        ),
        false,
    );
    let tool = s
        .transcript
        .items
        .iter()
        .find_map(|i| match i {
            Item::Tool(t) => Some(t),
            _ => None,
        })
        .unwrap();
    assert_eq!(tool.state, ToolState::Success);
    assert_eq!(tool.output.as_deref(), Some("hi"));
}

#[test]
fn phase_changed_updates_status() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "snapshot",
            1,
            serde_json::json!({
                "last_seq": 1, "phase": "idle", "round": 0, "busy": false,
                "pending": [], "streaming": null, "pending_approvals": [],
                "usage": {}, "bound_model": null, "context_digest": null, "last_error": null
            }),
        ),
        true,
    );
    apply_frame(
        &mut s,
        &frame(
            "phase.changed",
            2,
            serde_json::json!({
                "from": "idle", "to": "thinking"
            }),
        ),
        false,
    );
    assert_eq!(s.session.as_ref().unwrap().phase, "thinking");
}

#[test]
fn unknown_frame_is_ignored() {
    let mut s = state();
    let before = s.transcript.items.len();
    apply_frame(
        &mut s,
        &frame("future.frame", 1, serde_json::json!({})),
        false,
    );
    assert_eq!(s.transcript.items.len(), before);
}

#[test]
fn snapshot_streaming_partial_creates_item() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "snapshot",
            5,
            serde_json::json!({
                "last_seq": 5, "phase": "thinking", "round": 1, "busy": true,
                "pending": [], "pending_approvals": [], "usage": {}, "bound_model": null,
                "context_digest": null, "last_error": null,
                "streaming": {"request_id": "r1", "kind": "text", "partial_text": "par", "truncated": false}
            }),
        ),
        true,
    );
    assert!(matches!(&s.transcript.items[0], Item::Assistant(a) if a.text == "par"));
}
