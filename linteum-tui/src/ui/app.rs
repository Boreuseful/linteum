//! Application shell and terminal lifecycle.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::layout::Rect;

use crate::viewmodel::{Effect, Focus, Input, TuiState, UiAction, slash};

use super::theme::Theme;
use super::{layout, widgets};

#[derive(Debug, thiserror::Error)]
pub enum UiError {
    #[error("terminal: {0}")]
    Terminal(String),
}

pub trait TerminalOps {
    fn enter(&mut self) -> Result<(), UiError>;
    fn leave(&mut self) -> Result<(), UiError>;
    fn draw(&mut self, state: &TuiState) -> Result<(), UiError>;
}

/// Restores the terminal on drop, including on panic.
pub struct TerminalGuard<T: TerminalOps> {
    ops: Option<T>,
}

impl<T: TerminalOps> TerminalGuard<T> {
    pub fn new(mut ops: T) -> Result<Self, UiError> {
        if let Err(error) = ops.enter() {
            let _ = ops.leave();
            return Err(error);
        }
        Ok(Self { ops: Some(ops) })
    }

    pub fn ops_mut(&mut self) -> &mut T {
        self.ops.as_mut().expect("terminal guard is active")
    }
}

impl<T: TerminalOps> Drop for TerminalGuard<T> {
    fn drop(&mut self) {
        if let Some(mut ops) = self.ops.take() {
            let _ = ops.leave();
        }
    }
}

pub struct App {
    pub state: TuiState,
    pub theme: Theme,
    pub should_quit: bool,
}

impl App {
    pub fn new(state: TuiState, theme: Theme) -> Self {
        Self {
            state,
            theme,
            should_quit: false,
        }
    }

    pub fn handle_action(&mut self, action: UiAction) -> Vec<Effect> {
        if self.state.focus == Focus::Sidebar
            && matches!(
                action,
                UiAction::Send
                    | UiAction::Newline
                    | UiAction::CursorLeft
                    | UiAction::CursorRight
                    | UiAction::CursorUp
                    | UiAction::CursorDown
                    | UiAction::CursorHome
                    | UiAction::CursorEnd
                    | UiAction::DeleteBackward
                    | UiAction::DeleteForward
                    | UiAction::ClearDraft
            )
        {
            return Vec::new();
        }
        if matches!(
            action,
            UiAction::ScrollUp
                | UiAction::ScrollDown
                | UiAction::ScrollPageUp
                | UiAction::ScrollPageDown
        ) {
            let (total_lines, viewport) = self.transcript_metrics();
            return crate::viewmodel::reduce(
                &mut self.state,
                Input::Scroll {
                    action,
                    total_lines,
                    viewport,
                },
            );
        }
        crate::viewmodel::reduce(&mut self.state, Input::Action(action))
    }

    /// Handle a key while the `/` command menu is open. Returns `None` when
    /// the menu is closed or the key belongs to a global shortcut.
    pub fn handle_slash_key(&mut self, key: KeyEvent) -> Option<Vec<Effect>> {
        if self.state.overlay.is_some()
            || self.state.focus != Focus::Composer
            || !self.state.composer.text.starts_with('/')
        {
            return None;
        }
        match key.code {
            KeyCode::Up => {
                self.state.slash.selected = self.state.slash.selected.saturating_sub(1);
                Some(Vec::new())
            }
            KeyCode::Down => {
                let count = slash::filtered(slash::query(&self.state.composer.text)).len();
                if count > 0 {
                    self.state.slash.selected = (self.state.slash.selected + 1).min(count - 1);
                }
                Some(Vec::new())
            }
            KeyCode::Enter => Some(self.handle_action(UiAction::ExecuteSlashCommand)),
            KeyCode::Tab => Some(self.handle_action(UiAction::CompleteSlashCommand)),
            KeyCode::Esc => Some(self.handle_action(UiAction::CloseSlashMenu)),
            KeyCode::Left => Some(self.handle_action(UiAction::CursorLeft)),
            KeyCode::Right => Some(self.handle_action(UiAction::CursorRight)),
            KeyCode::Home => Some(self.handle_action(UiAction::CursorHome)),
            KeyCode::End => Some(self.handle_action(UiAction::CursorEnd)),
            KeyCode::Backspace => {
                let effects = self.handle_action(UiAction::DeleteBackward);
                self.state.slash.selected = 0;
                Some(effects)
            }
            KeyCode::Delete => {
                let effects = self.handle_action(UiAction::DeleteForward);
                self.state.slash.selected = 0;
                Some(effects)
            }
            KeyCode::Char(ch)
                if !key.modifiers.contains(KeyModifiers::CONTROL)
                    && !key.modifiers.contains(KeyModifiers::ALT) =>
            {
                self.state.composer.insert_char(ch);
                self.state.slash.selected = 0;
                Some(Vec::new())
            }
            _ => None,
        }
    }

    /// Handle a key while the workspace sidebar owns focus. Returns `None`
    /// for global shortcuts that should keep working.
    pub fn handle_sidebar_key(&mut self, key: KeyEvent) -> Option<Vec<Effect>> {
        if self.state.overlay.is_some()
            || !self.state.sidebar.visible
            || self.state.focus != Focus::Sidebar
        {
            return None;
        }
        let action = match key.code {
            KeyCode::Up => UiAction::SidebarPrevious,
            KeyCode::Down => UiAction::SidebarNext,
            KeyCode::Home => UiAction::SidebarFirst,
            KeyCode::End => UiAction::SidebarLast,
            KeyCode::Left => UiAction::SidebarCollapseFolder,
            KeyCode::Right => UiAction::SidebarExpandFolder,
            KeyCode::Enter | KeyCode::Char(' ') => UiAction::SidebarToggleFolder,
            KeyCode::Tab | KeyCode::BackTab | KeyCode::Esc => UiAction::ToggleFocus,
            KeyCode::Backspace | KeyCode::Delete => return Some(Vec::new()),
            KeyCode::Char('/')
                if !key.modifiers.contains(KeyModifiers::CONTROL)
                    && !key.modifiers.contains(KeyModifiers::ALT) =>
            {
                self.state.focus = Focus::Composer;
                if self.state.composer.text.is_empty() {
                    self.state.composer.insert_char('/');
                    self.state.slash = Default::default();
                }
                return Some(Vec::new());
            }
            _ => return None,
        };
        Some(self.handle_action(action))
    }

    /// Measure the rendered transcript for viewport-dependent scrolling.
    fn transcript_metrics(&self) -> (usize, usize) {
        let (width, height) =
            crossterm::terminal::size().unwrap_or((layout::MIN_WIDTH, layout::MIN_HEIGHT));
        let full = Rect::new(0, 0, width, height);
        let transcript =
            layout::transcript_viewport(layout::layout(full, &self.state).transcript, &self.state);
        let lines = widgets::transcript_lines(&self.state, transcript.width, &self.theme);
        (lines.len(), transcript.height as usize)
    }

    pub fn draw<T: TerminalOps>(&self, ops: &mut T) -> Result<(), UiError> {
        ops.draw(&self.state)
    }
}
