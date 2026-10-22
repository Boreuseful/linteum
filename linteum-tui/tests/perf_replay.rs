use std::time::{Duration, Instant};

use linteum_tui::protocol::Frame;
use linteum_tui::viewmodel::{Item, TuiState, apply_frame};

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

#[test]
fn replay_1000_frames_under_two_seconds() {
    let mut state = TuiState::default();
    let started = Instant::now();
    for seq in 1..=1_000 {
        let frame = frame(
            "user.message",
            seq,
            serde_json::json!({
                "text": format!("message {seq}"),
                "client_message_id": format!("client-{seq}")
            }),
        );
        apply_frame(&mut state, &frame, true);
    }
    let elapsed = started.elapsed();
    assert!(elapsed < Duration::from_secs(2), "replay took {elapsed:?}");
    assert_eq!(state.transcript.items.len(), 1_000);
}

#[test]
fn memory_is_o_items_not_deltas() {
    let mut state = TuiState::default();
    apply_frame(
        &mut state,
        &frame(
            "assistant.started",
            1,
            serde_json::json!({"provider": "fake", "model": "m"}),
        ),
        false,
    );
    for seq in 1..=100_000 {
        let mut delta = frame("assistant.delta", seq, serde_json::json!({"text": "x"}));
        delta.ephemeral = true;
        apply_frame(&mut state, &delta, false);
    }
    assert_eq!(state.transcript.items.len(), 1);
    match &state.transcript.items[0] {
        Item::Assistant(assistant) => assert_eq!(assistant.text.len(), 100_000),
        other => panic!("expected one assistant item, got {other:?}"),
    }
}
