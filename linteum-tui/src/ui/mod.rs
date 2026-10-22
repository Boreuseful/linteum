//! Thin ratatui/crossterm layer over the pure view-model.

pub mod app;
pub mod keymap;
pub mod layout;
pub mod overlays;
pub mod palette;
pub mod render;
pub mod screens;
pub mod strings;
pub mod theme;
pub mod widgets;

pub use app::{App, TerminalGuard, TerminalOps, UiError};
pub use keymap::key_to_action;
pub use layout::{LayoutRects, MIN_HEIGHT, MIN_WIDTH, layout, status_bar};
pub use render::render;
pub use theme::Theme;
