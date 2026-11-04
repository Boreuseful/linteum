use linteum_tui::protocol::{CommandResult, ErrorInfo, Frame};
use linteum_tui::viewmodel::{
    ApprovalCard, ApprovalDecision, Effect, Input, Item, Overlay, SessionView, TuiState, UiAction,
    apply_frame, reduce,
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

fn state_with_pending_approval(call_id: &str) -> TuiState {
    let mut state = state();
    state.session.as_mut().unwrap().phase = "awaiting_approval".into();
    let card = ApprovalCard {
        call_id: call_id.into(),
        name: "write_file".into(),
        side_effect: Some("write".into()),
        args_preview: "{}".into(),
        ..Default::default()
    };
    state.transcript.items.push(Item::Approval(card.clone()));
    state.approvals.pending.push(card);
    state.approvals.open = Some(call_id.into());
    state.overlay = Some(Overlay::Approvals);
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
fn approval_required_adds_card_and_opens_modal() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "approval.required",
            4,
            serde_json::json!({
                "call_id": "c1", "name": "write_file", "side_effect": "write",
                "args_preview": "{\"path\":\"x\"}", "requested_at": 1760680000.0,
                "expires_at": null
            }),
        ),
        false,
    );
    assert_eq!(s.approvals.pending.len(), 1);
    assert_eq!(s.approvals.open.as_deref(), Some("c1"));
    assert!(matches!(s.overlay, Some(Overlay::Approvals)));
}

#[test]
fn bare_enter_never_grants() {
    let mut s = state_with_pending_approval("c1");
    let effects = reduce(&mut s, Input::Action(UiAction::Send));
    assert!(effects.iter().all(|effect| !matches!(
        effect,
        Effect::SendCommand(command) if command.name == "approval.decide"
    )));
}

#[test]
fn grant_emits_approval_decide() {
    let mut s = state_with_pending_approval("c1");
    let effects = s.decide("c1", ApprovalDecision::Grant);
    assert!(matches!(&effects[0], Effect::SendCommand(c)
        if c.name == "approval.decide" && c.args["decision"] == "grant"));
    assert_eq!(s.approvals.open.as_deref(), Some("c1"));
}

#[test]
fn deny_with_reason_emits_reason() {
    let mut s = state_with_pending_approval("c1");
    let effects = s.decide(
        "c1",
        ApprovalDecision::Deny {
            reason: Some("no".into()),
        },
    );
    assert_eq!(effects.len(), 1);
    match &effects[0] {
        Effect::SendCommand(command) => {
            assert_eq!(command.args["decision"], "deny");
            assert_eq!(command.args["reason"], "no");
        }
        other => panic!("expected SendCommand, got {other:?}"),
    }
}

#[test]
fn e_approval_gone_closes_modal() {
    let mut s = state_with_pending_approval("c1");
    s.decide("c1", ApprovalDecision::Grant);
    reduce(
        &mut s,
        Input::CommandResult(result_err("r", "E_APPROVAL_GONE")),
    );
    assert!(s.approvals.open.is_none());
    assert!(s.approvals.pending.is_empty());
}

#[test]
fn cross_client_resolution_closes_modal() {
    let mut s = state_with_pending_approval("c1");
    apply_frame(
        &mut s,
        &frame(
            "approval.resolved",
            5,
            serde_json::json!({
                "call_id": "c1", "decision": "granted", "reason": null, "client_id": 7
            }),
        ),
        false,
    );
    assert!(s.approvals.open.is_none());
    assert!(s.approvals.pending.is_empty());
    let card = s
        .transcript
        .items
        .iter()
        .find_map(|item| match item {
            Item::Approval(card) => Some(card),
            _ => None,
        })
        .unwrap();
    assert_eq!(card.decision.as_deref(), Some("granted"));
}

#[test]
fn interrupt_cancels_all_approvals() {
    let mut s = state_with_pending_approval("c1");
    reduce(&mut s, Input::Action(UiAction::Interrupt));
    assert!(s.approvals.open.is_none());
    assert!(s.approvals.pending.is_empty());
}

#[test]
fn reconnect_snapshot_auto_opens_pending() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "snapshot",
            8,
            serde_json::json!({
                "last_seq": 8, "phase": "awaiting_approval", "round": 1, "busy": true,
                "pending": [], "streaming": null, "usage": {}, "bound_model": null,
                "context_digest": null, "last_error": null,
                "pending_approvals": [{"call_id": "c9", "name": "bash",
                    "side_effect": "dangerous", "args_preview": "{}",
                    "requested_at": 1.0, "expires_at": null}]
            }),
        ),
        true,
    );
    assert_eq!(s.approvals.open.as_deref(), Some("c9"));
}

#[test]
fn budget_notice_sets_paused_banner() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "notice",
            9,
            serde_json::json!({
                "kind": "budget", "reason": "usd", "code": "E_BUDGET"
            }),
        ),
        false,
    );
    assert_eq!(s.session.as_ref().unwrap().phase, "paused");
    assert!(s.activity.entries.iter().any(|e| e.kind == "budget"));
}

#[test]
fn cancelled_error_after_interrupt_is_folded() {
    let mut s = state();
    reduce(&mut s, Input::Action(UiAction::Interrupt));
    apply_frame(
        &mut s,
        &frame(
            "error",
            10,
            serde_json::json!({
                "code": "E_CANCELLED", "error_info": {"code": "E_CANCELLED"}
            }),
        ),
        false,
    );
    assert!(
        s.transcript
            .items
            .iter()
            .all(|item| !matches!(item, Item::Error(_)))
    );
}

#[test]
fn unknown_notice_kind_is_kept_in_activity() {
    let mut s = state();
    apply_frame(
        &mut s,
        &frame(
            "notice",
            11,
            serde_json::json!({
                "kind": "future_notice", "x": 1
            }),
        ),
        false,
    );
    assert!(s.activity.entries.iter().any(|e| e.kind == "future_notice"));
}

#[test]
fn resolving_first_of_two_opens_second() {
    let mut s = state_with_pending_approval("c1");
    let second = ApprovalCard {
        call_id: "c2".into(),
        name: "bash".into(),
        side_effect: Some("dangerous".into()),
        args_preview: "{}".into(),
        ..Default::default()
    };
    s.transcript.items.push(Item::Approval(second.clone()));
    s.approvals.pending.push(second);

    apply_frame(
        &mut s,
        &frame(
            "approval.resolved",
            6,
            serde_json::json!({"call_id": "c1", "decision": "granted"}),
        ),
        false,
    );

    assert_eq!(s.approvals.pending.len(), 1);
    assert_eq!(s.approvals.open.as_deref(), Some("c2"));
    assert_eq!(s.overlay, Some(Overlay::Approvals));
}

#[test]
fn close_overlay_postpones_and_next_required_opens() {
    let mut s = state_with_pending_approval("c1");
    reduce(&mut s, Input::Action(UiAction::CloseOverlay));
    assert!(s.overlay.is_none());
    assert!(s.approvals.open.is_none());
    assert_eq!(s.approvals.pending.len(), 1);

    apply_frame(
        &mut s,
        &frame(
            "approval.required",
            7,
            serde_json::json!({
                "call_id": "c2", "name": "bash", "side_effect": "dangerous",
                "args_preview": "{}", "requested_at": 1.0, "expires_at": null
            }),
        ),
        false,
    );
    assert_eq!(s.approvals.open.as_deref(), Some("c2"));
    assert_eq!(s.overlay, Some(Overlay::Approvals));
}

#[test]
fn approval_can_retry_after_transient_error() {
    let mut s = state_with_pending_approval("c1");
    let first = s.decide("c1", ApprovalDecision::Grant);
    assert_eq!(first.len(), 1);
    reduce(&mut s, Input::CommandResult(result_err("r", "E_INTERNAL")));
    let retry = s.decide("c1", ApprovalDecision::Grant);
    assert_eq!(retry.len(), 1);
}
