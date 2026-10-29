//! Command palette: one entry per user action.

use ratatui::layout::{Position, Rect};
use ratatui::text::{Line, Span};
use serde_json::{Map, Value};
use unicode_width::UnicodeWidthStr;

use crate::ui::Theme;
use crate::viewmodel::{
    ArtifactViewerState, CommandSpec, Effect, Input, Item, Overlay, SidebarPanel, TuiState,
    UiAction, edit, reduce,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PaletteAction {
    pub id: &'static str,
    pub label: &'static str,
}

pub fn palette_actions() -> Vec<PaletteAction> {
    vec![
        PaletteAction {
            id: "quit",
            label: "Quit",
        },
        PaletteAction {
            id: "toggle_reasoning",
            label: "Toggle reasoning",
        },
        PaletteAction {
            id: "sessions",
            label: "Sessions",
        },
        PaletteAction {
            id: "activity",
            label: "Activity",
        },
        PaletteAction {
            id: "approvals",
            label: "Approvals",
        },
        PaletteAction {
            id: "sidebar_subagents",
            label: "Sidebar: subagents",
        },
        PaletteAction {
            id: "subagents",
            label: "Subagents",
        },
        PaletteAction {
            id: "mode",
            label: "Session mode",
        },
        PaletteAction {
            id: "help",
            label: "Help",
        },
        PaletteAction {
            id: "new_session",
            label: "New session",
        },
        PaletteAction {
            id: "reconnect",
            label: "Reconnect now",
        },
        PaletteAction {
            id: "resume",
            label: "Resume agent",
        },
        PaletteAction {
            id: "copy_last",
            label: "Copy last assistant message",
        },
        PaletteAction {
            id: "open_artifact",
            label: "Open last artifact",
        },
        PaletteAction {
            id: "toggle_last_tool",
            label: "Expand or collapse last tool call",
        },
    ]
}

pub fn filtered_actions(filter: &str) -> Vec<PaletteAction> {
    let needle = filter.to_ascii_lowercase();
    palette_actions()
        .into_iter()
        .filter(|action| {
            needle.is_empty()
                || action.id.contains(needle.as_str())
                || action.label.to_ascii_lowercase().contains(needle.as_str())
        })
        .collect()
}

pub fn execute(action: PaletteAction, state: &mut TuiState) -> Vec<Effect> {
    match action.id {
        "quit" => vec![Effect::Quit {
            mode: state.quit_mode,
        }],
        "toggle_reasoning" => {
            state.prefs.show_reasoning = !state.prefs.show_reasoning;
            Vec::new()
        }
        "sessions" => {
            state.overlay = Some(Overlay::Picker);
            Vec::new()
        }
        "activity" => {
            state.overlay = Some(Overlay::Activity);
            Vec::new()
        }
        "approvals" => {
            state.overlay = Some(Overlay::Approvals);
            Vec::new()
        }
        "subagents" => {
            if state.subagents.is_empty() {
                state
                    .activity
                    .push("subagents", "no subagents in this session");
            } else {
                state.overlay = Some(Overlay::Subagents);
            }
            Vec::new()
        }
        "sidebar_subagents" => reduce(
            state,
            Input::Action(UiAction::SidebarShowPanel(SidebarPanel::Subagents)),
        ),
        "mode" => crate::viewmodel::reduce(state, Input::Action(UiAction::OpenModePicker)),
        "help" => {
            state.overlay = Some(Overlay::Help);
            Vec::new()
        }
        "new_session" => {
            state.overlay = Some(Overlay::NewSession);
            Vec::new()
        }
        "reconnect" => vec![Effect::ReconnectNow],
        "resume" => {
            if state.session.as_ref().map(|session| session.phase.as_str()) == Some("paused") {
                vec![Effect::SendCommand(CommandSpec {
                    name: "agent.resume".to_owned(),
                    args: Map::new(),
                    session_id: state.session.as_ref().map(|session| session.id.clone()),
                })]
            } else {
                Vec::new()
            }
        }
        "copy_last" => {
            let text = state
                .transcript
                .items
                .iter()
                .rev()
                .find_map(|item| match item {
                    Item::Assistant(assistant) if !assistant.text.is_empty() => {
                        Some(assistant.text.clone())
                    }
                    _ => None,
                });
            text.map(Effect::CopyToClipboard).into_iter().collect()
        }
        "open_artifact" => {
            let reference = state
                .transcript
                .items
                .iter()
                .rev()
                .find_map(|item| match item {
                    Item::Tool(tool) => tool.output_ref.clone(),
                    Item::Compaction(compaction) => compaction.summary_ref.clone(),
                    _ => None,
                });
            if let Some(reference) = reference {
                state.artifact = Some(ArtifactViewerState {
                    reference: reference.clone(),
                    ..Default::default()
                });
                state.overlay = Some(Overlay::Artifact);
                let mut args = Map::new();
                args.insert("ref".into(), Value::String(reference));
                args.insert("offset".into(), Value::from(0_u64));
                args.insert("limit".into(), Value::from(65536_u64));
                vec![Effect::SendCommand(CommandSpec {
                    name: "artifact.read".to_owned(),
                    args,
                    session_id: state.session.as_ref().map(|session| session.id.clone()),
                })]
            } else {
                Vec::new()
            }
        }
        "toggle_last_tool" => {
            if let Some(Item::Tool(card)) = state
                .transcript
                .items
                .iter_mut()
                .rev()
                .find(|item| matches!(item, Item::Tool(_)))
            {
                card.expanded = !card.expanded;
            }
            Vec::new()
        }
        _ => Vec::new(),
    }
}

pub fn palette_lines(state: &TuiState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = vec![Line::from(vec![
        Span::styled("> ", theme.text),
        Span::styled(state.palette.filter.clone(), theme.text),
    ])];
    lines.extend(
        filtered_actions(&state.palette.filter)
            .iter()
            .enumerate()
            .map(|(index, action)| {
                let selected = index == state.palette.selected;
                let marker = if selected { "» " } else { "  " };
                Line::from(Span::styled(
                    format!("{marker}{}  {}", action.id, action.label),
                    if selected { theme.accent } else { theme.text },
                ))
            }),
    );
    lines
}

/// The terminal cell of the palette filter's cursor.
pub fn palette_cursor(area: Rect, state: &TuiState) -> Option<Position> {
    if area.width == 0 || area.height == 0 {
        return None;
    }
    let cursor = edit::clamp(&state.palette.filter, state.palette.cursor);
    let width = UnicodeWidthStr::width(&state.palette.filter[..cursor]);
    let x = area
        .x
        .saturating_add(2)
        .saturating_add(u16::try_from(width).unwrap_or(u16::MAX));
    Some(Position::new(x.min(area.right().saturating_sub(1)), area.y))
}
