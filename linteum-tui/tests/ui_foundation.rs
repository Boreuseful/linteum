use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use linteum_tui::ui::{
    MIN_HEIGHT, MIN_WIDTH, TerminalGuard, TerminalOps, Theme, UiError, key_to_action, layout,
    status_bar,
};
use linteum_tui::viewmodel::{ApprovalCard, ConnectionState, SessionView, TuiState, UiAction};
use ratatui::layout::Rect;

fn key(code: KeyCode, modifiers: KeyModifiers) -> KeyEvent {
    KeyEvent::new(code, modifiers)
}

#[test]
fn keymap_maps_enter_to_send_and_ctrl_j_to_newline() {
    assert_eq!(
        key_to_action(key(KeyCode::Enter, KeyModifiers::NONE)),
        Some(UiAction::Send)
    );
    assert_eq!(
        key_to_action(key(KeyCode::Char('j'), KeyModifiers::CONTROL)),
        Some(UiAction::Newline)
    );
}

#[test]
fn keymap_pages_the_transcript_and_edits_the_composer() {
    assert_eq!(
        key_to_action(key(KeyCode::PageUp, KeyModifiers::NONE)),
        Some(UiAction::ScrollPageUp)
    );
    assert_eq!(
        key_to_action(key(KeyCode::PageDown, KeyModifiers::NONE)),
        Some(UiAction::ScrollPageDown)
    );
    assert_eq!(
        key_to_action(key(KeyCode::Home, KeyModifiers::NONE)),
        Some(UiAction::CursorHome)
    );
    assert_eq!(
        key_to_action(key(KeyCode::End, KeyModifiers::NONE)),
        Some(UiAction::CursorEnd)
    );
    assert_eq!(
        key_to_action(key(KeyCode::Home, KeyModifiers::CONTROL)),
        Some(UiAction::ScrollTop)
    );
    assert_eq!(
        key_to_action(key(KeyCode::End, KeyModifiers::CONTROL)),
        Some(UiAction::ScrollBottom)
    );
    assert_eq!(
        key_to_action(key(KeyCode::Left, KeyModifiers::NONE)),
        Some(UiAction::CursorLeft)
    );
    assert_eq!(
        key_to_action(key(KeyCode::Backspace, KeyModifiers::NONE)),
        Some(UiAction::DeleteBackward)
    );
}

#[test]
fn keymap_has_alternates_for_fkeys() {
    assert_eq!(
        key_to_action(key(KeyCode::F(2), KeyModifiers::NONE)),
        Some(UiAction::OpenPicker)
    );
    assert_eq!(
        key_to_action(key(KeyCode::Char('2'), KeyModifiers::ALT)),
        Some(UiAction::OpenPicker)
    );
}

#[test]
fn layout_at_80x24_has_status_and_composer() {
    let rects = layout(Rect::new(0, 0, 80, 24), &TuiState::default());
    assert!(rects.status.height >= 1);
    assert!(rects.composer.height >= 1);
    assert!(rects.transcript.height >= 1);
    assert_eq!(MIN_WIDTH, 80);
    assert_eq!(MIN_HEIGHT, 24);
}

#[test]
fn layout_below_minimum_shows_message() {
    let rects = layout(Rect::new(0, 0, 40, 10), &TuiState::default());
    assert_eq!(rects.transcript.height, 0);
}

#[test]
fn theme_mono_has_no_colors() {
    assert_eq!(Theme::mono().accent.fg, None);
}

#[test]
fn terminal_guard_restores_on_drop() {
    struct FakeTerminal {
        entered: Arc<AtomicBool>,
        left: Arc<AtomicBool>,
    }

    impl TerminalOps for FakeTerminal {
        fn enter(&mut self) -> Result<(), UiError> {
            self.entered.store(true, Ordering::SeqCst);
            Ok(())
        }

        fn leave(&mut self) -> Result<(), UiError> {
            self.left.store(true, Ordering::SeqCst);
            Ok(())
        }

        fn draw(&mut self, _state: &TuiState) -> Result<(), UiError> {
            Ok(())
        }
    }

    let entered = Arc::new(AtomicBool::new(false));
    let left = Arc::new(AtomicBool::new(false));
    {
        let _guard = TerminalGuard::new(FakeTerminal {
            entered: entered.clone(),
            left: left.clone(),
        })
        .unwrap();
        assert!(entered.load(Ordering::SeqCst));
    }
    assert!(left.load(Ordering::SeqCst));
}

#[test]
#[allow(clippy::field_reassign_with_default)]
fn status_bar_drops_segments_in_order() {
    let mut state = TuiState::default();
    state.connection = ConnectionState::Live;
    state.session = Some(SessionView {
        phase: "thinking".into(),
        round: 2,
        bound_model: Some("fake/model".into()),
        ..Default::default()
    });
    state.usage.input_tokens = 1200;
    state.usage.output_tokens = 34;
    state.usage.cache_read = 5;
    state.usage.usd = 0.0012;
    state.approvals.pending.push(ApprovalCard {
        call_id: "c1".into(),
        ..Default::default()
    });
    state.queue.enqueue("hello".into(), "m1".into());

    let narrow = status_bar(&state, 40);
    assert!(!narrow.contains("cache"));
    assert!(narrow.contains("live"));
}
