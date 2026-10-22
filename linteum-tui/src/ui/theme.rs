//! Semantic color themes.

use ratatui::style::{Color, Modifier, Style};

use crate::viewmodel::ThemeChoice;

#[derive(Debug, Clone)]
pub struct Theme {
    pub background: Style,
    pub text: Style,
    pub muted: Style,
    pub accent: Style,
    pub success: Style,
    pub warning: Style,
    pub danger: Style,
    pub border: Style,
    pub selection: Style,
    pub code: Style,
    pub reasoning: Style,
    /// Applied to the page behind a modal. Terminals cannot blur glyphs, so
    /// the backdrop recedes by dimming instead.
    pub backdrop: Style,
}

impl Theme {
    pub fn dark() -> Self {
        Self {
            background: Style::default().bg(Color::Indexed(234)),
            text: Style::default().fg(Color::Indexed(252)),
            muted: Style::default().fg(Color::Indexed(240)),
            accent: Style::default().fg(Color::Indexed(75)),
            success: Style::default().fg(Color::Indexed(78)),
            warning: Style::default().fg(Color::Indexed(179)),
            danger: Style::default().fg(Color::Indexed(203)),
            border: Style::default().fg(Color::Indexed(238)),
            selection: Style::default().bg(Color::Indexed(24)),
            code: Style::default().fg(Color::Indexed(110)),
            reasoning: Style::default()
                .fg(Color::Indexed(244))
                .add_modifier(Modifier::ITALIC),
            backdrop: Style::default()
                .fg(Color::Indexed(240))
                .add_modifier(Modifier::DIM),
        }
    }

    pub fn light() -> Self {
        Self {
            background: Style::default(),
            text: Style::default().fg(Color::Indexed(235)),
            muted: Style::default().fg(Color::Indexed(245)),
            accent: Style::default().fg(Color::Indexed(25)),
            success: Style::default().fg(Color::Indexed(28)),
            warning: Style::default().fg(Color::Indexed(136)),
            danger: Style::default().fg(Color::Indexed(160)),
            border: Style::default().fg(Color::Indexed(250)),
            selection: Style::default().bg(Color::Indexed(153)),
            code: Style::default().fg(Color::Indexed(24)),
            reasoning: Style::default()
                .fg(Color::Indexed(244))
                .add_modifier(Modifier::ITALIC),
            backdrop: Style::default()
                .fg(Color::Indexed(250))
                .add_modifier(Modifier::DIM),
        }
    }

    pub fn mono() -> Self {
        Self {
            background: Style::default(),
            text: Style::default(),
            muted: Style::default().add_modifier(Modifier::DIM),
            accent: Style::default().add_modifier(Modifier::BOLD),
            success: Style::default().add_modifier(Modifier::BOLD),
            warning: Style::default().add_modifier(Modifier::BOLD),
            danger: Style::default().add_modifier(Modifier::BOLD),
            border: Style::default().add_modifier(Modifier::DIM),
            selection: Style::default().add_modifier(Modifier::REVERSED),
            code: Style::default(),
            reasoning: Style::default().add_modifier(Modifier::DIM),
            backdrop: Style::default().add_modifier(Modifier::DIM),
        }
    }

    pub fn from_choice(choice: ThemeChoice) -> Self {
        match choice {
            ThemeChoice::Dark => Self::dark(),
            ThemeChoice::Light => Self::light(),
            ThemeChoice::Mono => Self::mono(),
        }
    }
}
