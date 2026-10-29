//! Pure view-model: state, inputs, effects, and the reducer.
//!
//! Nothing in this module performs I/O or depends on a terminal library.

pub mod approvals;
pub mod artifacts;
pub mod cursor;
pub mod edit;
pub mod modes;
pub mod notices;
pub mod queue;
pub mod sessions;
pub mod sidebar;
pub mod slash;
pub mod state;
pub mod subagents;
pub mod transcript;

pub use approvals::{ApprovalDecision, ApprovalState};
pub use artifacts::{ArtifactError, ArtifactViewerState};
pub use cursor::SessionCursor;
pub use modes::{CurrentMode, ModeOption, ModePickerState, ModeState, ModeSwitchResult};
pub use queue::{QueueEntryState, QueueState, QueuedMessage};
pub use sessions::{NewSessionForm, SessionInfo, SessionPickerState};
pub use sidebar::{SidebarLayout, SidebarPanel, SidebarState, TreeRow, WorkspaceEntry};
pub use slash::{SlashCommand, SlashState};
pub use state::{
    ActivityEntry, ActivityLog, ComposerState, ConnectionEvent, ConnectionState, Focus, Limits,
    Overlay, PaletteState, Prefs, QuitMode, SessionView, ThemeChoice, TuiState, UsageView,
};
pub use subagents::{
    ContextMessage, SubagentContextView, SubagentStatus, SubagentView, SubagentsState,
};
pub use transcript::{
    ApprovalCard, AssistantItem, CompactionCard, ErrorCard, GapMarker, Item, ScrollState, ToolCard,
    ToolState, Transcript, UserItem,
};

use std::time::Duration;

use serde_json::{Map, Value};

use crate::protocol::{CommandResult, Frame};

/// One reducer input.
#[derive(Debug, Clone)]
pub enum Input {
    Frame(Frame),
    CommandResult(CommandResult),
    Connection(ConnectionEvent),
    Action(UiAction),
    Text(String),
    Resize {
        width: u16,
        height: u16,
    },
    Tick(std::time::Instant),
    Paste(String),
    /// A viewport-dependent scroll. The UI layer supplies `total_lines` and
    /// `viewport` because only the renderer knows how text wraps.
    Scroll {
        action: UiAction,
        total_lines: usize,
        viewport: usize,
    },
    /// A directory read requested by the sidebar.
    WorkspaceDir {
        path: String,
        entries: Vec<WorkspaceEntry>,
        error: Option<String>,
    },
}

/// A user-visible action, produced by the UI keymap/palette.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UiAction {
    Send,
    Newline,
    Interrupt,
    ToggleReasoning,
    OpenPicker,
    OpenActivity,
    OpenApprovals,
    RetryConnection,
    NewSession,
    OpenPalette,
    OpenHelp,
    OpenModePicker,
    ModeSelectNext,
    ModeSelectPrevious,
    ModeSelect,
    ModeRefresh,
    Resume,
    Quit,
    ExitSession,
    ExecuteSlashCommand,
    CompleteSlashCommand,
    CloseSlashMenu,
    ToggleSidebar,
    ToggleFocus,
    SidebarShowPanel(SidebarPanel),
    SidebarMerge,
    SidebarSplit,
    SidebarRefresh,
    SidebarNext,
    SidebarPrevious,
    SidebarFirst,
    SidebarLast,
    SidebarToggleFolder,
    SidebarCollapseFolder,
    SidebarExpandFolder,
    CloseOverlay,
    ClearDraft,
    CursorLeft,
    CursorRight,
    CursorUp,
    CursorDown,
    CursorHome,
    CursorEnd,
    DeleteBackward,
    DeleteForward,
    ScrollUp,
    ScrollDown,
    ScrollPageUp,
    ScrollPageDown,
    ScrollTop,
    ScrollBottom,
    // Session picker / new-session (Task 6).
    OpenSelectedSession,
    SubmitNewSession,
    LoadMoreSessions,
    SelectNext,
    SelectPrevious,
    ToggleAllWorkspaces,
    RefreshSessions,
    OpenArtifact,
    CloseArtifact,
    OpenSubagents,
    SelectNextSubagent,
    SelectPreviousSubagent,
    OpenSelectedSubagent,
    ReloadSubagentContext,
}

/// A command the reducer wants the connection layer to send.
#[derive(Debug, Clone, PartialEq)]
pub struct CommandSpec {
    pub name: String,
    pub args: Map<String, Value>,
    pub session_id: Option<String>,
}

/// Side effects requested by the reducer.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    SendCommand(CommandSpec),
    ReconnectNow,
    RestartChild,
    Quit {
        mode: QuitMode,
    },
    SavePrefs(Prefs),
    CopyToClipboard(String),
    ShowToast(Toast),
    /// The host reads a workspace directory and feeds `Input::WorkspaceDir`.
    ReadWorkspaceDir {
        path: String,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Toast {
    pub text: String,
}

/// Apply one frame with an explicit replay flag.
pub fn apply_frame(state: &mut TuiState, frame: &Frame, replay: bool) {
    state.apply_frame_with(frame, replay);
}

/// Reduce one input into state changes and effects.
pub fn reduce(state: &mut TuiState, input: Input) -> Vec<Effect> {
    match input {
        Input::Frame(frame) => {
            if frame.frame == "artifact.chunk" {
                if let Some(artifact) = state.artifact.as_mut() {
                    let _ = artifact.apply_chunk(&frame.payload);
                }
                return Vec::new();
            }
            if frame.frame == "user.message" && !state.queue.apply_user_message(&frame) {
                return Vec::new();
            }
            if !frame.replay
                && frame.frame == "notice"
                && frame.payload.get("kind").and_then(Value::as_str) == Some("input_failed")
                && let Some(text) = state.queue.apply_input_failed()
            {
                state.composer.text = text;
            }
            let gap_after = (frame.frame == "gap")
                .then(|| frame.payload.get("to_seq").and_then(Value::as_u64))
                .flatten();
            let replay = frame.replay;
            state.apply_frame_with(&frame, replay);
            if let Some(after_seq) = gap_after {
                return subscribe_effect(state, after_seq);
            }
            Vec::new()
        }
        Input::CommandResult(result) => reduce_command_result(state, &result),
        Input::Connection(event) => {
            let connected = matches!(event, ConnectionEvent::Connected { .. });
            let reconnected = matches!(event, ConnectionEvent::Reconnected);
            apply_connection(state, event);
            if reconnected {
                let mut effects = subscribe_effect(state, state.cursor.after_seq());
                effects.extend(resend_effects(state));
                effects
            } else if connected {
                session_list_effect(state, 0)
            } else {
                Vec::new()
            }
        }
        Input::Action(action) => reduce_action(state, action),
        Input::Text(text) | Input::Paste(text) => {
            state.composer.insert(&text);
            if state.composer.text.starts_with('/') {
                state.slash = Default::default();
            }
            Vec::new()
        }
        Input::Scroll {
            action,
            total_lines,
            viewport,
        } => {
            state.transcript.scroll.apply(action, total_lines, viewport);
            Vec::new()
        }
        Input::WorkspaceDir {
            path,
            entries,
            error,
        } => {
            state.sidebar.apply_dir(&path, entries, error);
            Vec::new()
        }
        Input::Resize { .. } | Input::Tick(_) => Vec::new(),
    }
}

fn reduce_action(state: &mut TuiState, action: UiAction) -> Vec<Effect> {
    match action {
        UiAction::Send => send_action(state),
        UiAction::Interrupt => interrupt_action(state),
        UiAction::Newline => {
            state.composer.text.push('\n');
            Vec::new()
        }
        UiAction::ToggleReasoning => {
            state.prefs.show_reasoning = !state.prefs.show_reasoning;
            Vec::new()
        }
        UiAction::OpenPicker => {
            state.overlay = Some(Overlay::Picker);
            Vec::new()
        }
        UiAction::OpenActivity => {
            state.overlay = Some(Overlay::Activity);
            Vec::new()
        }
        UiAction::OpenApprovals => {
            state.overlay = Some(Overlay::Approvals);
            if state.approvals.open.is_none() {
                state.approvals.open = state
                    .approvals
                    .pending
                    .first()
                    .map(|card| card.call_id.clone());
            }
            Vec::new()
        }
        UiAction::NewSession => {
            state.overlay = Some(Overlay::NewSession);
            Vec::new()
        }
        UiAction::OpenPalette => {
            state.overlay = Some(Overlay::Palette);
            state.palette.cursor = state.palette.filter.len();
            Vec::new()
        }
        UiAction::OpenHelp => {
            state.overlay = Some(Overlay::Help);
            Vec::new()
        }
        UiAction::OpenModePicker => modes::open_picker(state),
        UiAction::ModeSelectNext => {
            modes::move_selection(state, 1);
            Vec::new()
        }
        UiAction::ModeSelectPrevious => {
            modes::move_selection(state, -1);
            Vec::new()
        }
        UiAction::ModeSelect => modes::select_current(state),
        UiAction::ModeRefresh => modes::refresh(state),
        UiAction::RetryConnection => vec![Effect::ReconnectNow],
        UiAction::Resume => {
            if state.session.as_ref().map(|s| s.phase.as_str()) == Some("paused") {
                vec![Effect::SendCommand(CommandSpec {
                    name: "agent.resume".to_owned(),
                    args: Map::new(),
                    session_id: state.session.as_ref().map(|s| s.id.clone()),
                })]
            } else {
                Vec::new()
            }
        }
        UiAction::Quit => vec![Effect::Quit {
            mode: state.quit_mode,
        }],
        UiAction::ExitSession => exit_session(state),
        UiAction::ExecuteSlashCommand => execute_slash_command(state),
        UiAction::CompleteSlashCommand => complete_slash_command(state),
        UiAction::CloseSlashMenu => close_slash_menu(state),
        UiAction::ToggleSidebar => toggle_sidebar(state),
        UiAction::ToggleFocus => toggle_focus(state),
        UiAction::SidebarShowPanel(panel) => open_sidebar_panel(state, panel),
        UiAction::SidebarMerge => set_sidebar_layout(state, SidebarLayout::Merged),
        UiAction::SidebarSplit => set_sidebar_layout(state, SidebarLayout::Split),
        UiAction::SidebarRefresh => refresh_sidebar(state),
        UiAction::SidebarNext => {
            state.sidebar.select_next();
            Vec::new()
        }
        UiAction::SidebarPrevious => {
            state.sidebar.select_previous();
            Vec::new()
        }
        UiAction::SidebarFirst => {
            state.sidebar.select_first();
            Vec::new()
        }
        UiAction::SidebarLast => {
            state.sidebar.select_last();
            Vec::new()
        }
        UiAction::SidebarToggleFolder => toggle_selected_folder(state),
        UiAction::SidebarCollapseFolder => collapse_selected_folder(state),
        UiAction::SidebarExpandFolder => expand_selected_folder(state),
        UiAction::CloseOverlay => {
            if state.overlay == Some(Overlay::Approvals) {
                state.approvals.open = None;
            }
            state.overlay = None;
            Vec::new()
        }
        UiAction::ClearDraft => {
            state.composer.text.clear();
            state.composer.cursor = 0;
            state.slash = Default::default();
            Vec::new()
        }
        UiAction::CursorLeft => {
            state.composer.move_left();
            Vec::new()
        }
        UiAction::CursorRight => {
            state.composer.move_right();
            Vec::new()
        }
        UiAction::CursorUp => {
            state.composer.move_up();
            Vec::new()
        }
        UiAction::CursorDown => {
            state.composer.move_down();
            Vec::new()
        }
        UiAction::CursorHome => {
            state.composer.move_home();
            Vec::new()
        }
        UiAction::CursorEnd => {
            state.composer.move_end();
            Vec::new()
        }
        UiAction::DeleteBackward => {
            state.composer.backspace();
            Vec::new()
        }
        UiAction::DeleteForward => {
            state.composer.delete();
            Vec::new()
        }
        // Line/page scrolling depends on the rendered line count and viewport
        // height, so the UI layer dispatches it as `Input::Scroll`.
        UiAction::ScrollUp
        | UiAction::ScrollDown
        | UiAction::ScrollPageUp
        | UiAction::ScrollPageDown => Vec::new(),
        UiAction::ScrollTop => {
            state.transcript.scroll.offset = 0;
            state.transcript.scroll.stick_to_bottom = false;
            Vec::new()
        }
        UiAction::ScrollBottom => {
            state.transcript.scroll.stick_to_bottom = true;
            Vec::new()
        }
        UiAction::OpenSelectedSession => open_selected_session(state),
        UiAction::SubmitNewSession => submit_new_session(state),
        UiAction::LoadMoreSessions => {
            let offset = state.picker.offset + state.picker.limit;
            session_list_effect(state, offset)
        }
        UiAction::SelectNext => {
            state.picker.move_selection(1);
            Vec::new()
        }
        UiAction::SelectPrevious => {
            state.picker.move_selection(-1);
            Vec::new()
        }
        UiAction::ToggleAllWorkspaces => {
            state.picker.all_workspaces = !state.picker.all_workspaces;
            session_list_effect(state, 0)
        }
        UiAction::RefreshSessions => session_list_effect(state, 0),
        UiAction::OpenArtifact => {
            if state.artifact.is_none() {
                state.artifact = Some(ArtifactViewerState::default());
            }
            state.overlay = Some(Overlay::Artifact);
            Vec::new()
        }
        UiAction::CloseArtifact => {
            state.artifact = None;
            if state.overlay == Some(Overlay::Artifact) {
                state.overlay = None;
            }
            Vec::new()
        }
        UiAction::OpenSubagents => open_subagents(state),
        UiAction::SelectNextSubagent => {
            state.subagents.select_next();
            Vec::new()
        }
        UiAction::SelectPreviousSubagent => {
            state.subagents.select_previous();
            Vec::new()
        }
        UiAction::OpenSelectedSubagent | UiAction::ReloadSubagentContext => {
            open_selected_subagent(state)
        }
    }
}

fn open_subagents(state: &mut TuiState) -> Vec<Effect> {
    if state.subagents.is_empty() {
        return vec![Effect::ShowToast(Toast {
            text: "no subagents in this session".to_owned(),
        })];
    }
    state.overlay = Some(Overlay::Subagents);
    Vec::new()
}

fn open_selected_subagent(state: &mut TuiState) -> Vec<Effect> {
    let Some(child_id) = state
        .subagents
        .selected_entry()
        .and_then(|entry| entry.child_session_id.clone())
    else {
        return vec![Effect::ShowToast(Toast {
            text: "subagent has no child session yet".to_owned(),
        })];
    };
    state.subagents.loading = Some(child_id.clone());
    state.subagents.detail = None;
    state.subagents.error = None;
    state.subagents.scroll = 0;
    state.overlay = Some(Overlay::SubagentContext);
    vec![Effect::SendCommand(CommandSpec {
        name: "session.context".to_owned(),
        args: Map::new(),
        session_id: Some(child_id),
    })]
}

fn open_selected_session(state: &mut TuiState) -> Vec<Effect> {
    let Some(info) = state.picker.selected_session().cloned() else {
        return Vec::new();
    };
    let old_id = state
        .session
        .as_ref()
        .map(|session| session.id.clone())
        .filter(|id| !id.is_empty());
    if let Some(old) = &old_id
        && !state.composer.text.is_empty()
    {
        state
            .drafts
            .insert(old.clone(), state.composer.text.clone());
    }

    let mut effects = Vec::new();
    if let Some(old) = &old_id
        && old != &info.session_id
    {
        effects.push(unsubscribe_effect(old));
    }

    state.overlay = None;
    state.cursor.reset();
    state.transcript = Default::default();
    state.usage = Default::default();
    state.subagents = Default::default();
    state.queue = Default::default();
    state.approvals = Default::default();
    state.slash = Default::default();
    state.sidebar = Default::default();
    state.focus = Focus::Composer;
    state.pending_tools.clear();
    state.last_error = None;
    state.artifact = None;
    state.connection = ConnectionState::Replaying;
    state.composer.text = state.drafts.remove(&info.session_id).unwrap_or_default();
    state.composer.cursor = state.composer.text.len();
    state.session = Some(SessionView {
        id: info.session_id.clone(),
        title: info.title.clone(),
        workspace: info.workspace.clone(),
        phase: info.phase.clone(),
        round: info.round,
        ..Default::default()
    });
    effects.extend(subscribe_effect(state, 0));
    effects
}

fn submit_new_session(state: &mut TuiState) -> Vec<Effect> {
    if let Err(error) = state.new_session.validate() {
        state.new_session.error = Some(error.to_owned());
        return Vec::new();
    }
    state.new_session.error = None;
    let mut args = Map::new();
    args.insert(
        "workspace".into(),
        Value::String(state.new_session.workspace.clone()),
    );
    if !state.new_session.title.trim().is_empty() {
        args.insert(
            "title".into(),
            Value::String(state.new_session.title.clone()),
        );
    }
    if !state.new_session.profile.trim().is_empty() {
        args.insert(
            "profile".into(),
            Value::String(state.new_session.profile.clone()),
        );
    }
    modes::submit_create(state, args)
}

fn session_list_effect(state: &TuiState, offset: usize) -> Vec<Effect> {
    let mut args = Map::new();
    args.insert("limit".into(), Value::from(state.picker.limit as u64));
    args.insert("offset".into(), Value::from(offset as u64));
    if !state.picker.all_workspaces
        && let Some(workspace) = &state.picker_workspace
    {
        args.insert("workspace".into(), Value::String(workspace.clone()));
    }
    vec![Effect::SendCommand(CommandSpec {
        name: "session.list".to_owned(),
        args,
        session_id: None,
    })]
}

fn subscribe_effect(state: &TuiState, after_seq: u64) -> Vec<Effect> {
    let Some(session) = state.session.as_ref() else {
        return Vec::new();
    };
    let mut args = Map::new();
    args.insert("session_id".into(), Value::String(session.id.clone()));
    args.insert("mode".into(), Value::String("view".into()));
    args.insert("after_seq".into(), Value::from(after_seq));
    vec![Effect::SendCommand(CommandSpec {
        name: "session.subscribe".to_owned(),
        args,
        session_id: Some(session.id.clone()),
    })]
}

fn unsubscribe_effect(session_id: &str) -> Effect {
    let mut args = Map::new();
    args.insert("session_id".into(), Value::String(session_id.to_owned()));
    Effect::SendCommand(CommandSpec {
        name: "session.unsubscribe".to_owned(),
        args,
        session_id: Some(session_id.to_owned()),
    })
}

fn reduce_command_result(state: &mut TuiState, result: &CommandResult) -> Vec<Effect> {
    let mut effects = Vec::new();
    let mut mode_effects = Vec::new();
    if result.ok {
        if result.result.get("modes").is_some() {
            mode_effects.extend(modes::apply_list(state, &result.result));
        } else if result.result.get("mode").is_some() && result.result.get("changed").is_some() {
            mode_effects.extend(modes::apply_set_result(state, &result.result));
        }
        if let Some(context) = result.result.get("context") {
            state.subagents.apply_context_result(context);
        }
        if result.result.get("subscribed").and_then(Value::as_bool) == Some(true) {
            state.connection = ConnectionState::Live;
        }
        if result.result.get("sessions").is_some() {
            state.picker.apply_list(result);
            if state.picker.sessions.is_empty() && state.overlay == Some(Overlay::Picker) {
                state.overlay = Some(Overlay::NewSession);
            }
        }
        if let Some(info) = result
            .result
            .get("session")
            .and_then(SessionInfo::from_json)
        {
            state.session = Some(SessionView {
                id: info.session_id.clone(),
                title: info.title.clone(),
                workspace: info.workspace.clone(),
                phase: info.phase.clone(),
                round: info.round,
                ..Default::default()
            });
            state.overlay = None;
            effects.extend(subscribe_effect(state, 0));
        }
        if let Some(warnings) = result.result.get("warnings").and_then(Value::as_array) {
            for warning in warnings.iter().filter_map(Value::as_str) {
                state.activity.push("warning", warning.to_owned());
            }
        }
    }
    if let Some(error) = &result.error {
        if error.code.starts_with("E_MODE_") || error.code == "E_SESSION_SUBAGENT" {
            modes::apply_error(state, &error.code, &error.message);
        }
        if state.subagents.loading.take().is_some() {
            state.subagents.error = Some(format!("{}: {}", error.code, error.message));
        }
        if error.code == "E_SESSION_LOCKED" {
            state.connection = ConnectionState::ReadOnly;
        }
        if error.code == "E_APPROVAL_GONE" {
            if let Some(call_id) = state.approvals.open.clone() {
                state
                    .approvals
                    .pending
                    .retain(|card| card.call_id != call_id);
            } else {
                state.approvals.pending.clear();
            }
            state.approvals.open = None;
            if state.overlay == Some(Overlay::Approvals) {
                state.overlay = None;
            }
        }
        if error.code == "E_SESSION_NOT_FOUND" {
            self::state::reset_after_session_loss(state, "session not found");
        }
        state.last_error = Some(error.clone());
        effects.push(Effect::ShowToast(Toast {
            text: format!("{}: {}", error.code, error.message),
        }));
    }

    let code = result
        .error
        .as_ref()
        .map(|error| error.code.as_str())
        .unwrap_or("");
    let has_id = result.result.get("client_message_id").is_some();
    let gateway_rejection = matches!(code, "E_BUSY" | "E_SESSION_LOCKED");
    let accepted_result = result.ok && result.result.get("accepted").is_some();
    let restored = if has_id || gateway_rejection || accepted_result {
        state.queue.apply_result(result)
    } else {
        None
    };
    if let Some(text) = restored {
        state.composer.text = text;
    }
    effects.extend(mode_effects);
    effects
}

fn resend_effects(state: &TuiState) -> Vec<Effect> {
    state
        .queue
        .resend_after_reconnect()
        .into_iter()
        .map(|entry| {
            let mut args = Map::new();
            args.insert("text".into(), Value::String(entry.text));
            args.insert(
                "client_message_id".into(),
                Value::String(entry.client_message_id),
            );
            Effect::SendCommand(CommandSpec {
                name: "user.message".to_owned(),
                args,
                session_id: session_id(state),
            })
        })
        .collect()
}

fn send_action(state: &mut TuiState) -> Vec<Effect> {
    if state.overlay == Some(Overlay::Approvals) {
        return Vec::new();
    }
    if matches!(
        state.connection,
        ConnectionState::Offline | ConnectionState::ReadOnly | ConnectionState::Fatal
    ) {
        return vec![Effect::ShowToast(Toast {
            text: "not connected — not sent".to_owned(),
        })];
    }
    let text = state.composer.text.clone();
    if text.trim().is_empty() {
        return Vec::new();
    }
    state.suppressed_cancel = false;
    let client_message_id = queue::new_client_message_id();
    state.queue.enqueue(text.clone(), client_message_id.clone());
    state.composer.text.clear();
    state.composer.cursor = 0;
    state.slash = Default::default();
    let mut args = Map::new();
    args.insert("text".into(), Value::String(text));
    args.insert("client_message_id".into(), Value::String(client_message_id));
    vec![Effect::SendCommand(CommandSpec {
        name: "user.message".to_owned(),
        args,
        session_id: session_id(state),
    })]
}

fn interrupt_action(state: &mut TuiState) -> Vec<Effect> {
    state.suppressed_cancel = true;
    state.approvals.pending.clear();
    state.approvals.open = None;
    state.approvals.decided.clear();
    if state.overlay == Some(Overlay::Approvals) {
        state.overlay = None;
    }

    let busy = state
        .session
        .as_ref()
        .map(|session| session.phase != "idle")
        .unwrap_or(false)
        || !state.queue.entries.is_empty();
    if !busy {
        if !state.composer.text.is_empty() {
            state.composer.text.clear();
            state.composer.cursor = 0;
            state.slash = Default::default();
        }
        return Vec::new();
    }
    let mut args = Map::new();
    args.insert("reason".into(), Value::String("tui".to_owned()));
    vec![Effect::SendCommand(CommandSpec {
        name: "user.interrupt".to_owned(),
        args,
        session_id: session_id(state),
    })]
}

fn sidebar_root(state: &TuiState) -> Option<String> {
    state
        .session
        .as_ref()
        .and_then(|session| session.workspace.clone())
        .or_else(|| state.workspace_root.clone())
        .or_else(|| state.picker_workspace.clone())
}

fn toggle_sidebar(state: &mut TuiState) -> Vec<Effect> {
    if state.sidebar.visible {
        state.sidebar.visible = false;
        state.focus = Focus::Composer;
        Vec::new()
    } else {
        let layout = state.sidebar.layout;
        let effects = open_sidebar_panel(state, state.sidebar.panel);
        state.sidebar.layout = layout;
        effects
    }
}

fn toggle_focus(state: &mut TuiState) -> Vec<Effect> {
    state.focus = if state.sidebar.visible && state.focus == Focus::Composer {
        Focus::Sidebar
    } else {
        Focus::Composer
    };
    Vec::new()
}

fn open_sidebar_panel(state: &mut TuiState, panel: SidebarPanel) -> Vec<Effect> {
    state.sidebar.panel = panel;
    // Selecting a panel means "show that panel", not "keep the last split".
    // `/split` opts back into the multi-panel layout explicitly.
    state.sidebar.layout = SidebarLayout::Merged;
    state.focus = Focus::Composer;
    let root = sidebar_root(state);
    state
        .sidebar
        .open(root)
        .map(|path| Effect::ReadWorkspaceDir { path })
        .into_iter()
        .collect()
}

fn set_sidebar_layout(state: &mut TuiState, layout: SidebarLayout) -> Vec<Effect> {
    state.sidebar.layout = layout;
    if state.sidebar.visible {
        state
            .sidebar
            .request_root()
            .map(|path| Effect::ReadWorkspaceDir { path })
            .into_iter()
            .collect()
    } else {
        let effects = open_sidebar_panel(state, state.sidebar.panel);
        // Opening a panel forces Merged; an explicit `/split` must win.
        state.sidebar.layout = layout;
        effects
    }
}

fn refresh_sidebar(state: &mut TuiState) -> Vec<Effect> {
    if !state.sidebar.visible {
        let layout = state.sidebar.layout;
        let effects = open_sidebar_panel(state, state.sidebar.panel);
        state.sidebar.layout = layout;
        return effects;
    }
    let expanded: Vec<String> = state.sidebar.expanded.iter().cloned().collect();
    state.sidebar.loaded.clear();
    state.sidebar.pending.clear();
    state.sidebar.error = None;
    state.sidebar.rebuild();
    let mut paths = Vec::new();
    if let Some(root) = state.sidebar.request_root() {
        paths.push(root);
    }
    for path in expanded {
        if state.sidebar.loaded.contains_key(&path) || state.sidebar.pending.contains(&path) {
            continue;
        }
        state.sidebar.pending.insert(path.clone());
        paths.push(path);
    }
    state.sidebar.rebuild();
    paths
        .into_iter()
        .map(|path| Effect::ReadWorkspaceDir { path })
        .collect()
}

fn toggle_selected_folder(state: &mut TuiState) -> Vec<Effect> {
    let Some(row) = state.sidebar.selected_row().cloned() else {
        return Vec::new();
    };
    if !row.is_dir {
        return Vec::new();
    }
    state
        .sidebar
        .toggle_selected()
        .map(|path| Effect::ReadWorkspaceDir { path })
        .into_iter()
        .collect()
}

fn collapse_selected_folder(state: &mut TuiState) -> Vec<Effect> {
    let Some(row) = state.sidebar.selected_row().cloned() else {
        return vec![toast("no folder selected")];
    };
    if !row.is_dir {
        return vec![toast("selected entry is not a folder")];
    }
    if !row.expanded {
        return vec![toast("folder is already collapsed")];
    }
    state.sidebar.collapse_selected();
    Vec::new()
}

fn expand_selected_folder(state: &mut TuiState) -> Vec<Effect> {
    let Some(row) = state.sidebar.selected_row().cloned() else {
        return vec![toast("no folder selected")];
    };
    if !row.is_dir {
        return vec![toast("selected entry is not a folder")];
    }
    if row.expanded && state.sidebar.loaded.contains_key(&row.path) {
        return vec![toast("folder is already expanded")];
    }
    state
        .sidebar
        .expand_selected()
        .map(|path| Effect::ReadWorkspaceDir { path })
        .into_iter()
        .collect()
}

fn toast(text: &str) -> Effect {
    Effect::ShowToast(Toast {
        text: text.to_owned(),
    })
}

fn exit_session(state: &mut TuiState) -> Vec<Effect> {
    state.composer.text.clear();
    state.composer.cursor = 0;
    state.slash = Default::default();
    let Some(session) = state.session.take() else {
        return vec![Effect::ShowToast(Toast {
            text: "no active session".to_owned(),
        })];
    };

    state.cursor.reset();
    state.transcript = Default::default();
    state.usage = Default::default();
    state.subagents = Default::default();
    state.queue = Default::default();
    state.approvals = Default::default();
    state.pending_tools.clear();
    state.last_error = None;
    state.artifact = None;
    state.suppressed_cancel = false;
    state.sidebar = Default::default();
    state.focus = Focus::Composer;
    state.overlay = Some(Overlay::Picker);

    let mut effects = Vec::new();
    if !session.id.is_empty() {
        effects.push(unsubscribe_effect(&session.id));
    }
    effects.extend(session_list_effect(state, 0));
    effects
}

fn execute_slash_command(state: &mut TuiState) -> Vec<Effect> {
    if let Some(effects) = modes::execute_slash(state) {
        return effects;
    }
    let Some(command) = slash::selected(&state.composer.text, state.slash.selected) else {
        let input = state.composer.text.clone();
        state.composer.text.clear();
        state.composer.cursor = 0;
        state.slash = Default::default();
        return vec![Effect::ShowToast(Toast {
            text: format!("unknown command: {input}"),
        })];
    };

    state.composer.text.clear();
    state.composer.cursor = 0;
    state.slash = Default::default();
    reduce_action(state, command.action)
}

/// Tab autocomplete: replace the typed query with the highlighted command so
/// the composer reads `/<id>`. The menu stays open and Enter still runs it.
fn complete_slash_command(state: &mut TuiState) -> Vec<Effect> {
    let Some(command) = slash::selected(&state.composer.text, state.slash.selected) else {
        return Vec::new();
    };
    state.composer.text = format!("/{}", command.id);
    state.composer.cursor = state.composer.text.len();
    state.slash.selected = 0;
    Vec::new()
}

fn close_slash_menu(state: &mut TuiState) -> Vec<Effect> {
    if state.composer.text.starts_with('/') {
        state.composer.text.clear();
        state.composer.cursor = 0;
    }
    state.slash = Default::default();
    Vec::new()
}

fn session_id(state: &TuiState) -> Option<String> {
    state.session.as_ref().map(|session| session.id.clone())
}

fn apply_connection(state: &mut TuiState, event: ConnectionEvent) {
    match event {
        ConnectionEvent::Connecting => state.connection = ConnectionState::Connecting,
        ConnectionEvent::Connected { features, .. } => {
            state.connection = ConnectionState::Live;
            state.features = features;
        }
        ConnectionEvent::Reconnected => state.connection = ConnectionState::Replaying,
        ConnectionEvent::Disconnected {
            retryable: true, ..
        } => {
            state.connection = ConnectionState::Reconnecting {
                retry_in: Duration::from_secs(1),
            }
        }
        ConnectionEvent::Disconnected {
            retryable: false, ..
        } => state.connection = ConnectionState::Offline,
        ConnectionEvent::ChildExited {
            status: _,
            stderr_tail,
        } => {
            state.connection = ConnectionState::Fatal;
            state.child_stderr_tail = stderr_tail;
            state.overlay = Some(Overlay::Fatal);
        }
        ConnectionEvent::Fatal { error } => {
            state.connection = ConnectionState::Fatal;
            state.last_error = Some(error);
            state.overlay = Some(Overlay::Fatal);
        }
    }
}
