//! View-model state and the frame reducer.
//!
//! This module is pure: it performs no I/O, reads no clock, and imports neither
//! `crossterm` nor `ratatui`. The UI layer feeds frames, command results,
//! connection events, terminal actions, and ticks into [`super::reduce`].

use std::collections::{BTreeMap, VecDeque};
use std::time::{Duration, SystemTime};

use serde_json::Value;

use crate::protocol::{ErrorInfo, Frame};

use super::approvals::ApprovalState;
use super::artifacts::ArtifactViewerState;
use super::cursor::SessionCursor;
use super::edit;
use super::modes::ModeState;
use super::queue::QueueState;
use super::sessions::{NewSessionForm, SessionPickerState};
use super::sidebar::SidebarState;
use super::slash::SlashState;
use super::subagents::SubagentsState;
use super::transcript::{
    ApprovalCard, AssistantItem, CompactionCard, ErrorCard, GapMarker, Item, ToolCard, ToolState,
    Transcript, UserItem,
};

/// The state of the transport/subscription.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum ConnectionState {
    #[default]
    Starting,
    Connecting,
    Replaying,
    Live,
    Reconnecting {
        retry_in: Duration,
    },
    Offline,
    ReadOnly,
    Fatal,
}

/// Limits advertised by `hello`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Limits {
    pub max_command_bytes: u64,
    pub max_frame_bytes: u64,
    pub client_queue_frames: u64,
    pub history_page_size: u64,
}

/// Lifecycle messages from the connection supervisor.
#[derive(Debug, Clone)]
pub enum ConnectionEvent {
    Connecting,
    Connected {
        features: Vec<String>,
        limits: Limits,
    },
    Reconnected,
    Disconnected {
        reason: String,
        retryable: bool,
    },
    ChildExited {
        status: Option<i32>,
        stderr_tail: Vec<String>,
    },
    Fatal {
        error: ErrorInfo,
    },
}

/// The active session metadata shown in the header.
#[derive(Debug, Clone, Default)]
pub struct SessionView {
    pub id: String,
    pub title: Option<String>,
    pub workspace: Option<String>,
    pub phase: String,
    pub round: u64,
    pub bound_model: Option<String>,
    pub context_digest: Option<String>,
}

/// Token and cost aggregates.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct UsageView {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read: u64,
    pub cache_write: u64,
    pub usd: f64,
}

/// Composer buffer.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ComposerState {
    pub text: String,
    pub cursor: usize,
    pub scroll: u16,
}

impl ComposerState {
    pub fn insert(&mut self, text: &str) {
        edit::insert(&mut self.text, &mut self.cursor, text);
    }

    pub fn insert_char(&mut self, ch: char) {
        edit::insert_char(&mut self.text, &mut self.cursor, ch);
    }

    pub fn backspace(&mut self) {
        edit::backspace(&mut self.text, &mut self.cursor);
    }

    pub fn delete(&mut self) {
        edit::delete(&mut self.text, self.cursor);
    }

    pub fn move_left(&mut self) {
        self.cursor = edit::left(&self.text, self.cursor);
    }

    pub fn move_right(&mut self) {
        self.cursor = edit::right(&self.text, self.cursor);
    }

    pub fn move_up(&mut self) {
        self.cursor = edit::up(&self.text, self.cursor);
    }

    pub fn move_down(&mut self) {
        self.cursor = edit::down(&self.text, self.cursor);
    }

    pub fn move_home(&mut self) {
        self.cursor = edit::line_start(&self.text, self.cursor);
    }

    pub fn move_end(&mut self) {
        self.cursor = edit::line_end(&self.text, self.cursor);
    }
}

/// Persisted UI preferences (the TUI-owned part).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Prefs {
    pub theme: ThemeChoice,
    pub show_reasoning: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ThemeChoice {
    #[default]
    Dark,
    Light,
    Mono,
}

/// Which modal/panel is on top.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Overlay {
    Help,
    Picker,
    NewSession,
    Approvals,
    Activity,
    Artifact,
    Subagents,
    SubagentContext,
    Palette,
    Mode,
    Fatal,
}

/// Which widget owns the keyboard.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Focus {
    #[default]
    Composer,
    Sidebar,
}

/// How quitting affects the gateway process.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum QuitMode {
    #[default]
    Spawn,
    Attach,
}

/// One activity-panel entry.
#[derive(Debug, Clone)]
pub struct ActivityEntry {
    /// Filled by the UI layer; the reducer stays clock-free.
    pub at: Option<SystemTime>,
    pub kind: String,
    pub text: String,
}

#[derive(Debug, Clone, Default)]
pub struct ActivityLog {
    pub entries: VecDeque<ActivityEntry>,
}

#[derive(Debug, Clone, Default)]
pub struct PaletteState {
    pub selected: usize,
    pub filter: String,
    pub cursor: usize,
}

impl PaletteState {
    pub fn insert_char(&mut self, ch: char) {
        edit::insert_char(&mut self.filter, &mut self.cursor, ch);
        self.selected = 0;
    }

    /// Insert pasted text into the single-line filter.
    pub fn insert_str(&mut self, value: &str) {
        edit::insert(
            &mut self.filter,
            &mut self.cursor,
            &value.replace('\n', " "),
        );
        self.selected = 0;
    }

    pub fn backspace(&mut self) {
        edit::backspace(&mut self.filter, &mut self.cursor);
        self.selected = 0;
    }

    pub fn delete(&mut self) {
        edit::delete(&mut self.filter, self.cursor);
        self.selected = 0;
    }

    pub fn move_left(&mut self) {
        self.cursor = edit::left(&self.filter, self.cursor);
    }

    pub fn move_right(&mut self) {
        self.cursor = edit::right(&self.filter, self.cursor);
    }

    pub fn move_home(&mut self) {
        self.cursor = edit::line_start(&self.filter, self.cursor);
    }

    pub fn move_end(&mut self) {
        self.cursor = edit::line_end(&self.filter, self.cursor);
    }
}

impl ActivityLog {
    pub fn push(&mut self, kind: impl Into<String>, text: impl Into<String>) {
        self.entries.push_back(ActivityEntry {
            at: None,
            kind: kind.into(),
            text: text.into(),
        });
        while self.entries.len() > 500 {
            self.entries.pop_front();
        }
    }
}

/// The complete UI state.
#[derive(Debug, Clone, Default)]
pub struct TuiState {
    pub connection: ConnectionState,
    pub session: Option<SessionView>,
    pub usage: UsageView,
    pub transcript: Transcript,
    pub queue: QueueState,
    pub approvals: ApprovalState,
    pub pending_tools: BTreeMap<String, ToolCard>,
    pub activity: ActivityLog,
    pub picker: SessionPickerState,
    pub picker_workspace: Option<String>,
    pub new_session: NewSessionForm,
    pub palette: PaletteState,
    pub slash: SlashState,
    pub drafts: std::collections::HashMap<String, String>,
    pub artifact: Option<ArtifactViewerState>,
    pub subagents: SubagentsState,
    pub mode: ModeState,
    pub features: Vec<String>,
    pub overlay: Option<Overlay>,
    pub focus: Focus,
    pub sidebar: SidebarState,
    /// Workspace path resolved by the host, used when the session has none.
    pub workspace_root: Option<String>,
    pub composer: ComposerState,
    pub prefs: Prefs,
    pub last_error: Option<ErrorInfo>,
    pub cursor: SessionCursor,
    pub degraded_replay: bool,
    pub quit_mode: QuitMode,
    /// Last lines of the spawned gateway's stderr, shown on the fatal screen.
    pub child_stderr_tail: Vec<String>,
    /// Set by a user interrupt so cancellation errors are folded into the
    /// interrupt notice instead of becoming new error cards.
    pub suppressed_cancel: bool,
}

impl TuiState {
    /// Apply one frame using its `replay` flag.
    pub fn apply_frame(&mut self, frame: &Frame) {
        self.apply_frame_with(frame, frame.replay);
    }

    /// Apply one frame with an explicit replay flag (tests and attach logic).
    pub(crate) fn apply_frame_with(&mut self, frame: &Frame, replay: bool) {
        if frame
            .payload
            .get("_offloaded")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            self.degraded_replay = true;
        }

        if let (Some(current), Some(frame_session)) =
            (self.session.as_ref(), frame.session_id.as_deref())
            && !current.id.is_empty()
            && current.id != frame_session
        {
            return;
        }

        let control = matches!(
            frame.frame.as_str(),
            "snapshot" | "gap" | "flow" | "bye" | "artifact.chunk" | "history.page" | "pong"
        );
        if !control && !frame.ephemeral && frame.seq.is_some() && !self.cursor.observe(frame) {
            return;
        }

        match frame.frame.as_str() {
            "snapshot" => apply_snapshot(self, frame, replay),
            "mode" => super::modes::apply_frame(self, &frame.payload),
            "user.message" => apply_user_message(self, frame),
            "assistant.started" => apply_assistant_started(self, frame, replay),
            "assistant.delta" => apply_assistant_delta(self, frame, false),
            "reasoning.delta" => apply_assistant_delta(self, frame, true),
            "assistant.message" => apply_assistant_message(self, frame, replay),
            "tool.requested" => apply_tool_requested(self, frame),
            "tool.started" => apply_tool_started(self, frame),
            "tool.delta" => apply_tool_delta(self, frame),
            "tool.result" => apply_tool_result(self, frame),
            "subagent.requested" | "subagent.started" | "subagent.progress" | "subagent.result"
            | "subagent.error" => self.subagents.apply_frame(frame),
            "approval.required" => {
                let card = parse_approval(&frame.payload);
                if let Some(existing) = approval_card_mut(self, &card.call_id) {
                    *existing = card.clone();
                } else {
                    self.transcript.items.push(Item::Approval(card.clone()));
                }
                if !replay {
                    let was_open = self.approvals.open.is_some();
                    if !self
                        .approvals
                        .pending
                        .iter()
                        .any(|existing| existing.call_id == card.call_id)
                    {
                        self.approvals.pending.push(card.clone());
                    }
                    if !was_open {
                        self.approvals.open = Some(card.call_id.clone());
                        self.overlay = Some(Overlay::Approvals);
                    }
                }
            }
            "approval.resolved" => apply_approval_resolved(self, frame),
            "phase.changed" => {
                if !replay && let Some(to) = str_field(&frame.payload, "to") {
                    ensure_session(self, frame.session_id.as_deref()).phase = to.to_owned();
                }
            }
            "agent.completed" => apply_agent_completed(self, frame, replay),
            "compaction.started" | "compaction.applied" => apply_compaction(self, frame),
            "notice" => {
                if frame.payload.get("kind").and_then(Value::as_str) == Some("subagent_cancel") {
                    self.subagents.apply_cancel(&frame.payload);
                }
                if !replay {
                    super::notices::route_notice(self, &frame.payload);
                }
            }
            "error" => {
                let code = str_field(&frame.payload, "code").unwrap_or("");
                let info_code = frame
                    .payload
                    .get("error_info")
                    .and_then(|value| value.get("code"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if self.suppressed_cancel
                    && (code == "E_CANCELLED"
                        || code == "E_TIMEOUT"
                        || info_code == "E_CANCELLED"
                        || info_code == "E_TIMEOUT")
                {
                    self.activity.push("interrupt", "cancelled after interrupt");
                } else {
                    let card = parse_error_card(frame);
                    self.last_error = card.error_info.clone();
                    self.transcript.items.push(Item::Error(card));
                }
            }
            "gap" => {
                let from_seq = u64_field(&frame.payload, "from_seq").unwrap_or(0);
                let to_seq = u64_field(&frame.payload, "to_seq").unwrap_or(0);
                let marker = GapMarker { from_seq, to_seq };
                self.cursor.resubscribe_after_gap(&marker);
                self.transcript.items.push(Item::Gap(marker));
            }
            "bye" => apply_bye(self, frame),
            // Control frames the v1 view-model does not render.
            "flow" | "history.page" | "artifact.chunk" | "pong" | "event" => {}
            _ => {}
        }
    }
}

fn apply_snapshot(state: &mut TuiState, frame: &Frame, replay: bool) {
    let payload = &frame.payload;
    {
        let session = ensure_session(state, frame.session_id.as_deref());
        if let Some(phase) = str_field(payload, "phase") {
            session.phase = phase.to_owned();
        }
        if let Some(round) = u64_field(payload, "round") {
            session.round = round;
        }
        if let Some(model) = str_field(payload, "bound_model") {
            session.bound_model = Some(model.to_owned());
        }
        if let Some(digest) = str_field(payload, "context_digest") {
            session.context_digest = Some(digest.to_owned());
        }
    }

    if let Some(usage) = payload.get("usage") {
        set_usage(state, usage);
    }
    if let Some(error) = payload.get("last_error") {
        state.last_error = serde_json::from_value(error.clone()).ok();
    }
    super::modes::apply_snapshot(state, payload);
    state.connection = if replay {
        ConnectionState::Replaying
    } else {
        ConnectionState::Live
    };

    if let Some(streaming) = payload.get("streaming").and_then(Value::as_object) {
        let mut text = streaming
            .get("partial_text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
        if streaming
            .get("truncated")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            text.push_str(" … (truncated)");
        }
        // ``kind`` tells which section the partial belongs to. Writing a
        // reasoning partial into ``text`` merges the thinking into the answer.
        let reasoning = streaming.get("kind").and_then(Value::as_str) == Some("reasoning");
        if state.transcript.active_stream.is_some()
            && let Some(item) = last_assistant_mut(state)
        {
            if reasoning {
                item.reasoning = text;
            } else {
                item.text = text;
            }
        } else {
            let mut item = AssistantItem::default();
            if reasoning {
                item.reasoning = text;
            } else {
                item.text = text;
            }
            state.transcript.items.push(Item::Assistant(item));
            state.transcript.active_stream = Some("snapshot".to_owned());
        }
    }

    if let Some(pending) = payload.get("pending").and_then(Value::as_array) {
        for value in pending {
            if let Some(call_id) = value.as_str() {
                let needs_card = tool_card_mut(state, call_id).is_none();
                if needs_card {
                    let card = ToolCard {
                        call_id: call_id.to_owned(),
                        name: "tool".to_owned(),
                        state: ToolState::Requested,
                        ..Default::default()
                    };
                    state.transcript.items.push(Item::Tool(card.clone()));
                    state.pending_tools.insert(call_id.to_owned(), card);
                }
            }
        }
    }

    state.approvals.pending.clear();
    state.approvals.open = None;
    state.approvals.decided.clear();
    if let Some(approvals) = payload.get("pending_approvals").and_then(Value::as_array) {
        for value in approvals {
            let card = parse_approval(value);
            if let Some(existing) = approval_card_mut(state, &card.call_id) {
                *existing = card.clone();
            } else {
                state.transcript.items.push(Item::Approval(card.clone()));
            }
            state.approvals.pending.push(card);
        }
    }
    let phase = state
        .session
        .as_ref()
        .map(|session| session.phase.clone())
        .unwrap_or_default();
    if phase == "awaiting_approval"
        && let Some(card) = state.approvals.pending.first()
    {
        state.approvals.open = Some(card.call_id.clone());
        state.overlay = Some(Overlay::Approvals);
    }
}

fn apply_user_message(state: &mut TuiState, frame: &Frame) {
    let text = str_field(&frame.payload, "text").unwrap_or("").to_owned();
    let client_message_id = str_field(&frame.payload, "client_message_id").map(str::to_owned);
    state.transcript.items.push(Item::User(UserItem {
        client_message_id,
        text,
    }));
}

fn apply_assistant_started(state: &mut TuiState, frame: &Frame, replay: bool) {
    if replay {
        return;
    }
    let provider = str_field(&frame.payload, "provider").map(str::to_owned);
    let model = str_field(&frame.payload, "model").map(str::to_owned);
    let round = u64_field(&frame.payload, "round");
    if state.transcript.active_stream.is_some()
        && let Some(item) = last_assistant_mut(state)
    {
        item.provider = provider;
        item.model = model;
        item.round = round;
        return;
    }
    state.transcript.items.push(Item::Assistant(AssistantItem {
        provider,
        model,
        round,
        ..Default::default()
    }));
    state.transcript.active_stream = Some(format!("stream-{}", frame.seq.unwrap_or(0)));
}

fn apply_assistant_delta(state: &mut TuiState, frame: &Frame, reasoning: bool) {
    let text = str_field(&frame.payload, "text").unwrap_or("");
    if state.transcript.active_stream.is_some()
        && let Some(item) = last_assistant_mut(state)
    {
        if reasoning {
            item.reasoning.push_str(text);
        } else {
            item.text.push_str(text);
        }
        return;
    }
    if state
        .transcript
        .items
        .iter()
        .any(|item| matches!(item, Item::Assistant(_)))
    {
        // A late delta after the final message must not mutate durable text.
        return;
    }
    let mut item = AssistantItem::default();
    if reasoning {
        item.reasoning.push_str(text);
    } else {
        item.text.push_str(text);
    }
    state.transcript.items.push(Item::Assistant(item));
    state.transcript.active_stream = Some(format!("stream-{}", frame.seq.unwrap_or(0)));
}

fn apply_assistant_message(state: &mut TuiState, frame: &Frame, replay: bool) {
    let payload = &frame.payload;
    let text = payload.get("message").map(message_text).unwrap_or_default();
    let provider = str_field(payload, "provider").map(str::to_owned);
    let model = str_field(payload, "model_actual").map(str::to_owned);
    let round = u64_field(payload, "round");
    let interrupted = bool_field(payload, "interrupted").unwrap_or(false);

    let active = state.transcript.active_stream.is_some();
    if active && let Some(item) = last_assistant_mut(state) {
        item.text = text;
        item.final_message = payload.get("message").cloned();
        item.interrupted = interrupted;
        item.provider = provider;
        item.model = model;
        item.round = round;
        item.usage = payload.get("usage").cloned();
    } else {
        state.transcript.items.push(Item::Assistant(AssistantItem {
            text,
            reasoning: String::new(),
            final_message: payload.get("message").cloned(),
            interrupted,
            usage: payload.get("usage").cloned(),
            provider,
            model,
            round,
        }));
    }
    state.transcript.active_stream = None;

    if !replay {
        add_usage(state, payload.get("usage"), payload.get("cost"));
    }
}

fn apply_tool_requested(state: &mut TuiState, frame: &Frame) {
    let mut card = parse_tool_card(&frame.payload);
    if let Some(existing) = tool_card_mut(state, &card.call_id) {
        existing.name = card.name.clone();
        existing.args = card.args.clone();
        existing.state = ToolState::Requested;
        card = existing.clone();
    } else {
        state.transcript.items.push(Item::Tool(card.clone()));
    }
    state.pending_tools.insert(card.call_id.clone(), card);
}

fn apply_tool_started(state: &mut TuiState, frame: &Frame) {
    let call_id = str_field(&frame.payload, "call_id").unwrap_or("");
    let name = str_field(&frame.payload, "name");
    let side_effect = str_field(&frame.payload, "side_effect").map(str::to_owned);
    if let Some(card) = tool_card_mut(state, call_id) {
        if let Some(name) = name {
            card.name = name.to_owned();
        }
        card.side_effect = side_effect;
        card.state = ToolState::Started;
    }
}

fn apply_tool_delta(state: &mut TuiState, frame: &Frame) {
    let call_id = str_field(&frame.payload, "call_id").unwrap_or("");
    let text = str_field(&frame.payload, "text").unwrap_or("");
    if tool_card_mut(state, call_id).is_none() {
        let card = parse_tool_card(&frame.payload);
        state.transcript.items.push(Item::Tool(card));
    }
    if let Some(card) = tool_card_mut(state, call_id)
        && matches!(
            card.state,
            ToolState::Requested | ToolState::Started | ToolState::Streaming
        )
    {
        card.output.get_or_insert_with(String::new).push_str(text);
        card.state = ToolState::Streaming;
    }
}

fn apply_tool_result(state: &mut TuiState, frame: &Frame) {
    let payload = &frame.payload;
    let call_id = str_field(payload, "call_id").unwrap_or("").to_owned();
    if tool_card_mut(state, &call_id).is_none() {
        let mut card = parse_tool_card(payload);
        card.call_id = call_id.clone();
        state.transcript.items.push(Item::Tool(card));
    }
    let ok = bool_field(payload, "ok").unwrap_or(false);
    let code = str_field(payload, "code");
    let output = payload.get("output").map(render_value);
    let output_ref = str_field(payload, "output_ref").map(str::to_owned);
    let dedup = bool_field(payload, "dedup").unwrap_or(false);
    let safety = bool_field(payload, "safety").unwrap_or(false);
    let error = payload
        .get("error_info")
        .and_then(|value| serde_json::from_value(value.clone()).ok());
    if let Some(card) = tool_card_mut(state, &call_id) {
        card.state = if ok {
            ToolState::Success
        } else if code == Some("E_DENIED") {
            ToolState::Denied
        } else {
            ToolState::Failure
        };
        card.output = output;
        card.output_ref = output_ref;
        card.dedup = dedup;
        card.safety = safety;
        card.error = error;
    }
    state.pending_tools.remove(&call_id);
}

fn apply_approval_resolved(state: &mut TuiState, frame: &Frame) {
    let call_id = str_field(&frame.payload, "call_id").unwrap_or("");
    let decision = str_field(&frame.payload, "decision").map(str::to_owned);
    let reason = str_field(&frame.payload, "reason").map(str::to_owned);
    for item in state.transcript.items.iter_mut() {
        if let Item::Approval(card) = item
            && card.call_id == call_id
        {
            card.decision = decision.clone();
            card.reason = reason.clone();
        }
    }
    state.approvals.decided.insert(call_id.to_owned());
    let was_open = state.approvals.open.as_deref() == Some(call_id);
    if let Some(index) = state
        .approvals
        .pending
        .iter()
        .position(|card| card.call_id == call_id)
    {
        state.approvals.pending.remove(index);
    }
    if was_open {
        state.approvals.open = state
            .approvals
            .pending
            .first()
            .map(|card| card.call_id.clone());
        if state.approvals.open.is_some() {
            state.overlay = Some(Overlay::Approvals);
        } else if state.overlay == Some(Overlay::Approvals) {
            state.overlay = None;
        }
    }
}

fn apply_agent_completed(state: &mut TuiState, frame: &Frame, replay: bool) {
    if !replay {
        ensure_session(state, frame.session_id.as_deref()).phase = "idle".to_owned();
    }
    if str_field(&frame.payload, "finish") == Some("error") {
        let card = parse_error_card(frame);
        state.transcript.items.push(Item::Error(card));
    }
}

fn apply_compaction(state: &mut TuiState, frame: &Frame) {
    let fresh = parse_compaction(frame);
    let mut found = false;
    for item in state.transcript.items.iter_mut() {
        if let Item::Compaction(card) = item
            && card.from_index == fresh.from_index
            && card.to_index == fresh.to_index
        {
            *card = fresh.clone();
            found = true;
            break;
        }
    }
    if !found {
        state.transcript.items.push(Item::Compaction(fresh));
    }
}

fn apply_bye(state: &mut TuiState, frame: &Frame) {
    match str_field(&frame.payload, "reason") {
        Some("session deleted") => reset_after_session_loss(state, "session deleted"),
        Some(reason) => {
            state.activity.push("bye", reason.to_owned());
            state.connection = ConnectionState::Reconnecting {
                retry_in: Duration::from_secs(1),
            };
        }
        None => {
            state.connection = ConnectionState::Reconnecting {
                retry_in: Duration::from_secs(1),
            };
        }
    }
}

pub(crate) fn reset_after_session_loss(state: &mut TuiState, reason: &str) {
    state.session = None;
    state.transcript = Transcript::default();
    state.subagents = SubagentsState::default();
    state.queue = QueueState::default();
    state.approvals = ApprovalState::default();
    state.slash = SlashState::default();
    state.sidebar = SidebarState::default();
    state.focus = Focus::Composer;
    state.pending_tools.clear();
    state.last_error = None;
    state.artifact = None;
    state.connection = ConnectionState::Offline;
    state.overlay = Some(Overlay::Picker);
    state.activity.push("session", reason.to_owned());
}

fn ensure_session<'a>(state: &'a mut TuiState, id: Option<&str>) -> &'a mut SessionView {
    if state.session.is_none() {
        state.session = Some(SessionView {
            id: id.unwrap_or("unknown").to_owned(),
            ..Default::default()
        });
    }
    state.session.as_mut().expect("session inserted above")
}

fn last_assistant_mut(state: &mut TuiState) -> Option<&mut AssistantItem> {
    for item in state.transcript.items.iter_mut().rev() {
        if let Item::Assistant(assistant) = item {
            return Some(assistant);
        }
    }
    None
}

fn approval_card_mut<'a>(state: &'a mut TuiState, call_id: &str) -> Option<&'a mut ApprovalCard> {
    for item in state.transcript.items.iter_mut() {
        if let Item::Approval(card) = item
            && card.call_id == call_id
        {
            return Some(card);
        }
    }
    None
}

fn tool_card_mut<'a>(state: &'a mut TuiState, call_id: &str) -> Option<&'a mut ToolCard> {
    for item in state.transcript.items.iter_mut() {
        if let Item::Tool(card) = item
            && card.call_id == call_id
        {
            return Some(card);
        }
    }
    None
}

fn set_usage(state: &mut TuiState, usage: &Value) {
    state.usage.input_tokens = u64_field(usage, "input_tokens").unwrap_or(0);
    state.usage.output_tokens = u64_field(usage, "output_tokens").unwrap_or(0);
    state.usage.cache_read = u64_field(usage, "cache_read").unwrap_or(0);
    state.usage.cache_write = u64_field(usage, "cache_write").unwrap_or(0);
    state.usage.usd = f64_field(usage, "usd").unwrap_or(0.0);
}

fn add_usage(state: &mut TuiState, usage: Option<&Value>, cost: Option<&Value>) {
    if let Some(usage) = usage {
        state.usage.input_tokens += u64_field(usage, "input_tokens").unwrap_or(0);
        state.usage.output_tokens += u64_field(usage, "output_tokens").unwrap_or(0);
        state.usage.cache_read += u64_field(usage, "cache_read").unwrap_or(0);
        state.usage.cache_write += u64_field(usage, "cache_write").unwrap_or(0);
        state.usage.usd += f64_field(usage, "usd").unwrap_or(0.0);
    }
    if let Some(cost) = cost {
        state.usage.usd += f64_field(cost, "total_usd").unwrap_or(0.0);
    }
}

fn message_text(message: &Value) -> String {
    match message.get("content") {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(parts)) => parts
            .iter()
            .filter_map(|part| {
                if part.get("type").and_then(Value::as_str) == Some("text") {
                    part.get("text").and_then(Value::as_str)
                } else {
                    None
                }
            })
            .collect::<Vec<_>>()
            .join(""),
        Some(other) => other.as_str().unwrap_or("").to_owned(),
        None => String::new(),
    }
}

fn parse_tool_card(payload: &Value) -> ToolCard {
    ToolCard {
        call_id: str_field(payload, "call_id").unwrap_or("").to_owned(),
        name: str_field(payload, "name").unwrap_or("tool").to_owned(),
        side_effect: str_field(payload, "side_effect").map(str::to_owned),
        state: ToolState::Requested,
        args: payload
            .get("args")
            .map(|value| serde_json::to_string_pretty(value).unwrap_or_default()),
        ..Default::default()
    }
}

fn parse_approval(payload: &Value) -> ApprovalCard {
    ApprovalCard {
        call_id: str_field(payload, "call_id").unwrap_or("").to_owned(),
        name: str_field(payload, "name").unwrap_or("tool").to_owned(),
        side_effect: str_field(payload, "side_effect").map(str::to_owned),
        args_preview: str_field(payload, "args_preview")
            .unwrap_or("{}")
            .to_owned(),
        requested_at: f64_field(payload, "requested_at"),
        expires_at: f64_field(payload, "expires_at"),
        decision: None,
        reason: None,
    }
}

fn parse_compaction(frame: &Frame) -> CompactionCard {
    let payload = &frame.payload;
    CompactionCard {
        seq: frame.seq.unwrap_or(0),
        from_index: u64_field(payload, "from_index").unwrap_or(0),
        to_index: u64_field(payload, "to_index").unwrap_or(0),
        summary: str_field(payload, "summary").map(str::to_owned),
        summary_ref: str_field(payload, "summary_ref").map(str::to_owned),
        tokens: u64_field(payload, "tokens"),
        summarizer: str_field(payload, "summarizer").map(str::to_owned),
        fallback: bool_field(payload, "fallback").unwrap_or(false),
        error: payload
            .get("error_info")
            .and_then(|value| serde_json::from_value(value.clone()).ok()),
    }
}

fn parse_error_card(frame: &Frame) -> ErrorCard {
    let payload = &frame.payload;
    let info: Option<ErrorInfo> = payload
        .get("error_info")
        .and_then(|value| serde_json::from_value(value.clone()).ok());
    let code = str_field(payload, "code")
        .map(str::to_owned)
        .or_else(|| info.as_ref().map(|info| info.code.clone()))
        .unwrap_or_else(|| "E_UNKNOWN".to_owned());
    let message = str_field(payload, "error")
        .or_else(|| str_field(payload, "message"))
        .map(str::to_owned)
        .or_else(|| info.as_ref().map(|info| info.message.clone()))
        .unwrap_or_default();
    ErrorCard {
        seq: frame.seq,
        code,
        message,
        error_info: info,
    }
}

fn render_value(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => String::new(),
        other => serde_json::to_string(other).unwrap_or_default(),
    }
}

fn str_field<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}

fn u64_field(value: &Value, key: &str) -> Option<u64> {
    value.get(key).and_then(Value::as_u64)
}

fn f64_field(value: &Value, key: &str) -> Option<f64> {
    value.get(key).and_then(Value::as_f64)
}

fn bool_field(value: &Value, key: &str) -> Option<bool> {
    value.get(key).and_then(Value::as_bool)
}
