//! Keyboard mapping. Every action also has a command-palette entry.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use crate::viewmodel::UiAction;

pub fn key_to_action(key: KeyEvent) -> Option<UiAction> {
    match (key.code, key.modifiers) {
        (KeyCode::Enter, KeyModifiers::NONE) => Some(UiAction::Send),
        (KeyCode::Char('j'), KeyModifiers::CONTROL) => Some(UiAction::Newline),
        (KeyCode::Char('c'), KeyModifiers::CONTROL) => Some(UiAction::Interrupt),
        (KeyCode::Char('n'), KeyModifiers::CONTROL) => Some(UiAction::NewSession),
        (KeyCode::Char('p'), KeyModifiers::CONTROL) => Some(UiAction::OpenPalette),
        (KeyCode::Char('r'), KeyModifiers::CONTROL) => Some(UiAction::ToggleReasoning),
        (KeyCode::Char('g'), KeyModifiers::CONTROL) => Some(UiAction::Resume),
        (KeyCode::Char('q'), KeyModifiers::CONTROL) => Some(UiAction::Quit),
        (KeyCode::F(1), _) | (KeyCode::Char('1'), KeyModifiers::ALT) => Some(UiAction::OpenHelp),
        (KeyCode::F(2), _) | (KeyCode::Char('2'), KeyModifiers::ALT) => Some(UiAction::OpenPicker),
        (KeyCode::F(3), _) | (KeyCode::Char('3'), KeyModifiers::ALT) => {
            Some(UiAction::OpenActivity)
        }
        (KeyCode::F(4), _) | (KeyCode::Char('4'), KeyModifiers::ALT) => {
            Some(UiAction::OpenApprovals)
        }
        (KeyCode::F(5), _) | (KeyCode::Char('5'), KeyModifiers::ALT) => {
            Some(UiAction::RetryConnection)
        }
        (KeyCode::Esc, _) => Some(UiAction::CloseOverlay),
        (KeyCode::Tab, _) | (KeyCode::BackTab, _) => Some(UiAction::ToggleFocus),
        (KeyCode::Left, _) => Some(UiAction::CursorLeft),
        (KeyCode::Right, _) => Some(UiAction::CursorRight),
        (KeyCode::Up, _) => Some(UiAction::CursorUp),
        (KeyCode::Down, _) => Some(UiAction::CursorDown),
        (KeyCode::Backspace, _) => Some(UiAction::DeleteBackward),
        (KeyCode::Delete, _) => Some(UiAction::DeleteForward),
        (KeyCode::Home, KeyModifiers::CONTROL) => Some(UiAction::ScrollTop),
        (KeyCode::End, KeyModifiers::CONTROL) => Some(UiAction::ScrollBottom),
        (KeyCode::Home, _) => Some(UiAction::CursorHome),
        (KeyCode::End, _) => Some(UiAction::CursorEnd),
        (KeyCode::PageUp, _) => Some(UiAction::ScrollPageUp),
        (KeyCode::PageDown, _) => Some(UiAction::ScrollPageDown),
        _ => None,
    }
}
