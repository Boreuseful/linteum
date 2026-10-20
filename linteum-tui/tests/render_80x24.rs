#![allow(clippy::field_reassign_with_default)]

use linteum_tui::ui::{Theme, render};
use linteum_tui::viewmodel::{ApprovalCard, Item, Overlay, TuiState, UserItem};
use ratatui::Terminal;
use ratatui::backend::TestBackend;

fn rendered(width: u16, height: u16, state: &TuiState) -> String {
    let backend = TestBackend::new(width, height);
    let mut terminal = Terminal::new(backend).unwrap();
    let theme = Theme::dark();
    terminal.draw(|frame| render(frame, state, &theme)).unwrap();
    terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}

#[test]
fn chat_renders_at_80x24() {
    let mut state = TuiState::default();
    state.transcript.items.push(Item::User(UserItem {
        text: "hello-world".into(),
        ..Default::default()
    }));
    let text = rendered(80, 24, &state);
    assert!(text.contains("hello-world"));
    assert!(text.contains("linteum-tui"));
}

#[test]
fn empty_composer_hints_at_slash_commands() {
    let state = TuiState::default();
    let text = rendered(80, 24, &state);
    assert!(text.contains("/ for commands"));
}

#[test]
fn approval_overlay_renders_a_popup_over_the_page() {
    let mut state = TuiState::default();
    state.transcript.items.push(Item::User(UserItem {
        text: "page text".into(),
        ..Default::default()
    }));
    state.approvals.pending.push(ApprovalCard {
        call_id: "c1".into(),
        name: "write_file".into(),
        args_preview: "{}".into(),
        ..Default::default()
    });
    state.approvals.open = Some("c1".into());
    state.overlay = Some(Overlay::Approvals);

    let text = rendered(80, 24, &state);
    assert!(text.contains("Approval required"));
    assert!(text.contains("Grant (y)"));
    // The page stays visible, dimmed, behind the modal.
    assert!(text.contains("page text"));
}

#[test]
fn below_minimum_shows_message() {
    let state = TuiState::default();
    let text = rendered(40, 10, &state);
    assert!(text.contains("Terminal too small"));
}

#[test]
fn read_only_banner_is_rendered() {
    let mut state = TuiState::default();
    state.connection = linteum_tui::viewmodel::ConnectionState::ReadOnly;
    let text = rendered(80, 24, &state);
    assert!(text.contains("read-only"));
}

#[test]
fn paused_banner_is_rendered() {
    let mut state = TuiState::default();
    state.session = Some(linteum_tui::viewmodel::SessionView {
        phase: "paused".into(),
        ..Default::default()
    });
    let text = rendered(80, 24, &state);
    assert!(text.contains("Paused"));
}

#[test]
fn degraded_replay_banner_is_rendered() {
    let mut state = TuiState::default();
    state.degraded_replay = true;
    let text = rendered(80, 24, &state);
    assert!(text.contains("Replayed content unavailable"));
}
