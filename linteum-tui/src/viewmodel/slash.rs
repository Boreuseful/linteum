//! Slash-command catalogue and menu cursor.

use super::UiAction;
use super::sidebar::SidebarPanel;

/// One entry in the `/` menu.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SlashCommand {
    pub id: &'static str,
    pub label: &'static str,
    pub description: &'static str,
    pub action: UiAction,
}

pub const COMMANDS: &[SlashCommand] = &[
    SlashCommand {
        id: "files",
        label: "Workspace files",
        description: "Show the workspace tree",
        action: UiAction::SidebarShowPanel(SidebarPanel::Files),
    },
    SlashCommand {
        id: "sidebar",
        label: "Toggle sidebar",
        description: "Show or hide the right sidebar",
        action: UiAction::ToggleSidebar,
    },
    SlashCommand {
        id: "merge",
        label: "Merge panels",
        description: "Merge the sidebar into one block",
        action: UiAction::SidebarMerge,
    },
    SlashCommand {
        id: "split",
        label: "Split panels",
        description: "Split the sidebar into three blocks",
        action: UiAction::SidebarSplit,
    },
    SlashCommand {
        id: "collapse",
        label: "Collapse folder",
        description: "Collapse the selected folder",
        action: UiAction::SidebarCollapseFolder,
    },
    SlashCommand {
        id: "expand",
        label: "Expand folder",
        description: "Expand the selected folder",
        action: UiAction::SidebarExpandFolder,
    },
    SlashCommand {
        id: "preview",
        label: "Preview panel",
        description: "Show the preview placeholder",
        action: UiAction::SidebarShowPanel(SidebarPanel::Preview),
    },
    SlashCommand {
        id: "info",
        label: "Info panel",
        description: "Show the info placeholder",
        action: UiAction::SidebarShowPanel(SidebarPanel::Info),
    },
    SlashCommand {
        id: "refresh",
        label: "Refresh files",
        description: "Reload the workspace tree",
        action: UiAction::SidebarRefresh,
    },
    SlashCommand {
        id: "exit",
        label: "Exit session",
        description: "Leave this session",
        action: UiAction::ExitSession,
    },
    SlashCommand {
        id: "quit",
        label: "Quit",
        description: "Exit linteum-tui",
        action: UiAction::Quit,
    },
];

/// The text after the leading `/`.
pub fn query(text: &str) -> &str {
    text.strip_prefix('/').unwrap_or(text)
}

/// Commands matching `query`, in catalogue order.
pub fn filtered(query: &str) -> Vec<&'static SlashCommand> {
    let needle = query.trim().to_ascii_lowercase();
    COMMANDS
        .iter()
        .filter(|command| {
            needle.is_empty()
                || command.id.contains(needle.as_str())
                || command.label.to_ascii_lowercase().contains(needle.as_str())
        })
        .collect()
}

/// The highlighted command for a query. The index is clamped so a stale
/// selection cannot point past a shorter filtered list.
pub fn selected(text: &str, selected: usize) -> Option<&'static SlashCommand> {
    let matches = filtered(query(text));
    if matches.is_empty() {
        None
    } else {
        Some(matches[selected.min(matches.len() - 1)])
    }
}

/// Menu cursor; the query itself lives in the composer.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct SlashState {
    pub selected: usize,
}
