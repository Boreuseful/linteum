use linteum_tui::protocol::Frame;
use linteum_tui::viewmodel::{
    ConnectionEvent, ConnectionState, Effect, Input, Item, Overlay, SessionView, ToolState,
    TuiState, UiAction, apply_frame, reduce,
};

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

fn apply_snapshot(state: &mut TuiState, phase: &str) {
    apply_frame(
        state,
        &frame(
            "snapshot",
            1,
            serde_json::json!({
                "last_seq": 1, "phase": phase, "round": 0, "busy": false,
                "pending": [], "streaming": null, "pending_approvals": [],
                "usage": {}, "bound_model": null, "context_digest": null, "last_error": null
            }),
        ),
        true,
    );
}

#[test]
fn replayed_assistant_message_does_not_add_usage() {
    let mut s = state();
    let mut f = frame(
        "assistant.message",
        5,
        serde_json::json!({
            "message": {"role": "assistant", "content": "old"},
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "cost": {"total_usd": 1.0}, "tool_calls": [], "round": 1
        }),
    );
    f.replay = true;
    apply_frame(&mut s, &f, true);
    assert_eq!(s.usage.input_tokens, 0);
    assert_eq!(s.usage.output_tokens, 0);
    assert_eq!(s.usage.usd, 0.0);
    assert_eq!(s.transcript.items.len(), 1);
}

#[test]
fn replayed_phase_changed_does_not_regress_status() {
    let mut s = state();
    apply_snapshot(&mut s, "thinking");
    let mut f = frame(
        "phase.changed",
        6,
        serde_json::json!({"from": "idle", "to": "idle"}),
    );
    f.replay = true;
    apply_frame(&mut s, &f, true);
    assert_eq!(s.session.as_ref().unwrap().phase, "thinking");
}

#[test]
fn replayed_assistant_started_is_ignored() {
    let mut s = state();
    let mut f = frame(
        "assistant.started",
        7,
        serde_json::json!({"provider": "fake"}),
    );
    f.replay = true;
    apply_frame(&mut s, &f, true);
    assert!(s.transcript.items.is_empty());
}

#[test]
fn duplicate_frames_are_rendered_once() {
    let mut s = state();
    let f = frame(
        "user.message",
        3,
        serde_json::json!({"text": "hi", "client_message_id": "m"}),
    );
    apply_frame(&mut s, &f, false);
    let mut duplicate = f.clone();
    duplicate.id = "different-id".into();
    apply_frame(&mut s, &duplicate, false);
    assert_eq!(s.transcript.items.len(), 1);
}

#[test]
fn frame_id_is_not_a_dedupe_key() {
    let mut s = state();
    let a = frame(
        "user.message",
        4,
        serde_json::json!({"text": "a", "client_message_id": "m-a"}),
    );
    let mut b = a.clone();
    b.id = "other".into();
    assert!(s.cursor.observe(&a));
    assert!(!s.cursor.observe(&b));
}

#[test]
fn ephemeral_frames_do_not_advance_cursor() {
    let mut s = state();
    let mut d = frame("assistant.delta", 9, serde_json::json!({"text": "x"}));
    d.ephemeral = true;
    assert!(!s.cursor.observe(&d));
    assert_eq!(s.cursor.after_seq(), 0);
}

#[test]
fn gap_resubscribes_from_to_seq() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame("gap", 9, serde_json::json!({"from_seq": 5, "to_seq": 9})),
        false,
    );
    assert_eq!(s.cursor.after_seq(), 9);
    assert!(
        matches!(s.transcript.items.last(), Some(Item::Gap(g)) if g.from_seq == 5 && g.to_seq == 9)
    );
}

#[test]
fn bye_session_deleted_returns_to_picker() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame("bye", 10, serde_json::json!({"reason": "session deleted"})),
        false,
    );
    assert!(matches!(s.overlay, Some(Overlay::Picker)));
    assert_eq!(s.connection, ConnectionState::Offline);
}

#[test]
fn offloaded_replay_sets_degraded_marker() {
    let mut s = state();
    let mut f = frame(
        "compaction.applied",
        11,
        serde_json::json!({
            "from_index": 1, "to_index": 2, "summary": "preview",
            "_offloaded": true, "artifact": "art:abc"
        }),
    );
    f.replay = true;
    apply_frame(&mut s, &f, true);
    assert!(s.degraded_replay);
}

#[test]
fn reconnected_emits_subscribe_and_resend() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "user.message",
            5,
            serde_json::json!({"text": "hi", "client_message_id": "m"}),
        ),
        false,
    );
    s.composer.text = "pending".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let effects = reduce(&mut s, Input::Connection(ConnectionEvent::Reconnected));
    let subscribe = effects
        .iter()
        .find_map(|effect| match effect {
            Effect::SendCommand(command) if command.name == "session.subscribe" => Some(command),
            _ => None,
        })
        .expect("reconnect must re-subscribe");
    assert_eq!(subscribe.args["after_seq"], 5);
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command) if command.name == "user.message"
    )));
}

#[test]
fn gap_emits_resubscribe_effect() {
    let mut s = state();
    let effects = reduce(
        &mut s,
        Input::Frame(frame(
            "gap",
            9,
            serde_json::json!({"from_seq": 5, "to_seq": 9}),
        )),
    );
    assert!(effects.iter().any(|effect| matches!(
        effect,
        Effect::SendCommand(command)
            if command.name == "session.subscribe" && command.args["after_seq"] == 9
    )));
}

#[test]
fn replayed_assistant_messages_are_not_merged() {
    let mut s = state();
    let mut first = frame(
        "assistant.message",
        5,
        serde_json::json!({
            "message": {"role": "assistant", "content": "first"},
            "tool_calls": [], "usage": {}, "cost": {}
        }),
    );
    first.replay = true;
    apply_frame(&mut s, &first, true);
    let mut second = frame(
        "assistant.message",
        6,
        serde_json::json!({
            "message": {"role": "assistant", "content": "second"},
            "tool_calls": [], "usage": {}, "cost": {}
        }),
    );
    second.replay = true;
    apply_frame(&mut s, &second, true);
    let texts: Vec<&str> = s
        .transcript
        .items
        .iter()
        .filter_map(|item| match item {
            Item::Assistant(assistant) => Some(assistant.text.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(texts, vec!["first", "second"]);
}

#[test]
fn snapshot_pending_approvals_are_not_duplicated() {
    let mut s = state();
    let snapshot = serde_json::json!({
        "last_seq": 1, "phase": "awaiting_approval", "round": 1, "busy": true,
        "pending": [], "streaming": null, "usage": {}, "bound_model": null,
        "context_digest": null, "last_error": null,
        "pending_approvals": [{"call_id": "c1", "name": "bash",
            "side_effect": "dangerous", "args_preview": "{}",
            "requested_at": 1.0, "expires_at": null}]
    });
    apply_frame(&mut s, &frame("snapshot", 1, snapshot.clone()), true);
    apply_frame(&mut s, &frame("snapshot", 1, snapshot), true);
    let count = s
        .transcript
        .items
        .iter()
        .filter(|item| matches!(item, Item::Approval(card) if card.call_id == "c1"))
        .count();
    assert_eq!(count, 1);
    assert_eq!(s.approvals.pending.len(), 1);
}

#[test]
fn late_delta_after_final_message_is_ignored() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "assistant.started",
            1,
            serde_json::json!({"provider": "fake"}),
        ),
        false,
    );
    let mut delta = frame("assistant.delta", 1, serde_json::json!({"text": "Hello"}));
    delta.ephemeral = true;
    apply_frame(&mut s, &delta, false);
    apply_frame(
        &mut s,
        &frame(
            "assistant.message",
            2,
            serde_json::json!({
                "message": {"role": "assistant", "content": "Hello"},
                "tool_calls": [], "usage": {}, "cost": {}
            }),
        ),
        false,
    );
    let mut late = frame("assistant.delta", 2, serde_json::json!({"text": " world"}));
    late.ephemeral = true;
    apply_frame(&mut s, &late, false);
    let text = s
        .transcript
        .items
        .iter()
        .find_map(|item| match item {
            Item::Assistant(assistant) => Some(assistant.text.as_str()),
            _ => None,
        })
        .unwrap();
    assert_eq!(text, "Hello");
}

#[test]
fn late_tool_delta_after_result_is_ignored() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "tool.requested",
            1,
            serde_json::json!({"call_id": "c1", "name": "echo"}),
        ),
        false,
    );
    apply_frame(
        &mut s,
        &frame(
            "tool.result",
            2,
            serde_json::json!({"call_id": "c1", "name": "echo", "ok": true, "output": "done"}),
        ),
        false,
    );
    let mut late = frame(
        "tool.delta",
        2,
        serde_json::json!({"call_id": "c1", "text": "late"}),
    );
    late.ephemeral = true;
    apply_frame(&mut s, &late, false);
    let card = s
        .transcript
        .items
        .iter()
        .find_map(|item| match item {
            Item::Tool(card) => Some(card),
            _ => None,
        })
        .unwrap();
    assert_eq!(card.state, ToolState::Success);
    assert_eq!(card.output.as_deref(), Some("done"));
}
