use linteum_tui::protocol::{CommandResult, ErrorInfo, Frame};
use linteum_tui::viewmodel::{
    ConnectionEvent, ConnectionState, Effect, Input, Overlay, QueueEntryState, SessionView,
    TuiState, UiAction, reduce,
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

fn result_ok(id: &str, accepted: bool, queued: bool) -> CommandResult {
    CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: true,
        result: serde_json::json!({
            "client_message_id": id, "accepted": accepted,
            "queued": queued, "event_id": "e", "seq": 1
        }),
        error: None,
    }
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
fn send_creates_sending_entry_before_result() {
    let mut s = state();
    let effects = reduce(&mut s, Input::Action(UiAction::Send));
    assert!(effects.is_empty());
    s.composer.text = "hello".into();
    let effects = reduce(&mut s, Input::Action(UiAction::Send));
    assert_eq!(s.queue.entries.len(), 1);
    assert_eq!(s.queue.entries[0].state, QueueEntryState::Sending);
    assert!(matches!(effects[0], Effect::SendCommand(ref c) if c.name == "user.message"));
}

#[test]
fn frame_before_result_delivers_once() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let id = s.queue.entries[0].client_message_id.clone();
    let f = frame(
        "user.message",
        1,
        serde_json::json!({
            "text": "hello", "client_message_id": id
        }),
    );
    reduce(&mut s, Input::Frame(f.clone()));
    reduce(&mut s, Input::CommandResult(result_ok(&id, true, false)));
    assert_eq!(s.transcript.items.len(), 1);
    assert!(s.queue.entries.is_empty());
}

#[test]
fn duplicate_receipt_does_not_duplicate() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let id = s.queue.entries[0].client_message_id.clone();
    reduce(
        &mut s,
        Input::Frame(frame(
            "user.message",
            1,
            serde_json::json!({
                "text": "hello", "client_message_id": id
            }),
        )),
    );
    let mut result = result_ok(&id, true, false);
    result.result["duplicate"] = serde_json::json!(true);
    reduce(&mut s, Input::CommandResult(result));
    assert_eq!(s.transcript.items.len(), 1);
}

#[test]
fn queued_receipt_marks_queued_then_frame_delivers() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let id = s.queue.entries[0].client_message_id.clone();
    reduce(&mut s, Input::CommandResult(result_ok(&id, true, true)));
    assert_eq!(s.queue.entries[0].state, QueueEntryState::Queued);
    reduce(
        &mut s,
        Input::Frame(frame(
            "user.message",
            2,
            serde_json::json!({
                "text": "hello", "client_message_id": id
            }),
        )),
    );
    assert!(s.queue.entries.is_empty());
    assert_eq!(s.transcript.items.len(), 1);
}

#[test]
fn accepted_false_restores_draft() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let id = s.queue.entries[0].client_message_id.clone();
    reduce(&mut s, Input::CommandResult(result_ok(&id, false, false)));
    assert_eq!(s.composer.text, "hello");
    assert!(s.queue.entries.is_empty());
}

#[test]
fn e_busy_restores_draft() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let id = s.queue.entries[0].client_message_id.clone();
    reduce(&mut s, Input::CommandResult(result_err(&id, "E_BUSY")));
    assert_eq!(s.composer.text, "hello");
    assert!(s.queue.entries.is_empty());
}

#[test]
fn e_session_locked_enters_read_only() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let id = s.queue.entries[0].client_message_id.clone();
    reduce(
        &mut s,
        Input::CommandResult(result_err(&id, "E_SESSION_LOCKED")),
    );
    assert_eq!(s.connection, ConnectionState::ReadOnly);
}

#[test]
fn input_failed_fails_oldest_and_returns_text() {
    let mut s = state();
    s.composer.text = "one".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    s.composer.text = "two".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let effects = reduce(
        &mut s,
        Input::Frame(frame(
            "notice",
            3,
            serde_json::json!({
                "kind": "input_failed", "error_info": {"code": "E_BUSY"}
            }),
        )),
    );
    assert_eq!(effects.len(), 0);
    assert_eq!(s.composer.text, "one");
    assert_eq!(s.queue.entries.len(), 1);
    assert_eq!(s.queue.entries[0].text, "two");
}

#[test]
fn reconnect_resends_unconfirmed_once() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let id = s.queue.entries[0].client_message_id.clone();
    let effects = reduce(&mut s, Input::Connection(ConnectionEvent::Reconnected));
    let sends: Vec<_> = effects
        .into_iter()
        .filter_map(|effect| match effect {
            Effect::SendCommand(command) if command.name == "user.message" => Some(command),
            _ => None,
        })
        .collect();
    assert_eq!(sends.len(), 1);
    assert_eq!(sends[0].args["client_message_id"], id);
}

#[test]
fn interrupt_emits_user_interrupt_effect() {
    let mut s = state();
    s.session = Some(SessionView {
        phase: "thinking".into(),
        ..Default::default()
    });
    let effects = reduce(&mut s, Input::Action(UiAction::Interrupt));
    assert!(matches!(effects[0], Effect::SendCommand(ref c) if c.name == "user.interrupt"));
}

fn synthetic_err(code: &str) -> CommandResult {
    CommandResult {
        v: 1,
        id: String::new(),
        reply_to: String::new(),
        ok: false,
        result: serde_json::json!({}),
        error: Some(ErrorInfo {
            code: code.into(),
            source: "tui".into(),
            location: "test".into(),
            message: code.into(),
            exc_type: None,
            detail: String::new(),
            retryable: true,
            enters_history: false,
            context: serde_json::json!({}),
        }),
    }
}

#[test]
fn transport_error_keeps_queue_entry_for_resend() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    reduce(&mut s, Input::CommandResult(synthetic_err("E_TRANSPORT")));
    assert_eq!(s.queue.entries.len(), 1);
    assert_eq!(s.queue.entries[0].state, QueueEntryState::Sending);
    assert!(s.composer.text.is_empty());
}

#[test]
fn e_busy_without_id_restores_oldest() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    reduce(&mut s, Input::CommandResult(synthetic_err("E_BUSY")));
    assert!(s.queue.entries.is_empty());
    assert_eq!(s.composer.text, "hello");
}

#[test]
fn duplicate_user_message_by_client_id_is_rendered_once() {
    let mut s = state();
    reduce(
        &mut s,
        Input::Frame(frame(
            "user.message",
            1,
            serde_json::json!({"text": "hi", "client_message_id": "m"}),
        )),
    );
    reduce(
        &mut s,
        Input::Frame(frame(
            "user.message",
            2,
            serde_json::json!({"text": "hi", "client_message_id": "m"}),
        )),
    );
    assert_eq!(s.transcript.items.len(), 1);
}

#[test]
fn fatal_opens_screen_and_blocks_send() {
    let mut s = state();
    let error = synthetic_err("E_INTERNAL").error.unwrap();
    reduce(&mut s, Input::Connection(ConnectionEvent::Fatal { error }));
    assert!(matches!(s.overlay, Some(Overlay::Fatal)));
    s.composer.text = "hello".into();
    let effects = reduce(&mut s, Input::Action(UiAction::Send));
    assert!(
        effects
            .iter()
            .all(|effect| !matches!(effect, Effect::SendCommand(_)))
    );
    assert_eq!(s.composer.text, "hello");
}

#[test]
fn replayed_input_failed_is_ignored() {
    let mut s = state();
    s.composer.text = "hello".into();
    reduce(&mut s, Input::Action(UiAction::Send));
    let mut notice = frame("notice", 2, serde_json::json!({"kind": "input_failed"}));
    notice.replay = true;
    reduce(&mut s, Input::Frame(notice));
    assert_eq!(s.queue.entries.len(), 1);
}
