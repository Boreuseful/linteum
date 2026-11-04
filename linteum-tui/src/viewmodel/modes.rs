//! Session modes: `/mode` command, id resolution, picker and status.
//!
//! The view model never guesses the current mode from `mode.list`; only the
//! gateway snapshot, a `mode` frame, or a successful `session.mode.set`
//! response updates [`CurrentMode`].

use serde_json::{Map, Value};

use super::edit;
use super::queue::QueueEntryState;
use super::{CommandSpec, ConnectionState, Effect, Overlay, Toast, TuiState};

pub const USAGE: &str = "/mode · /mode list · /mode status · /mode help · /mode <name-or-id>";

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ModeOption {
    pub id: String,
    pub name: String,
    pub description: String,
    pub source: String,
    pub builtin: bool,
    pub default: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CurrentMode {
    pub id: String,
    pub name: String,
    pub source: String,
    pub digest: String,
    pub refresh: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ModeSwitchResult {
    pub id: String,
    pub name: String,
    pub source: String,
    pub digest: String,
    pub tools: u64,
    pub skills: u64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ModePickerState {
    pub filter: String,
    pub cursor: usize,
    pub selected: usize,
}

impl ModePickerState {
    fn reset(&mut self) {
        self.filter.clear();
        self.cursor = 0;
        self.selected = 0;
    }

    pub fn insert_char(&mut self, ch: char) {
        edit::insert_char(&mut self.filter, &mut self.cursor, ch);
        self.selected = 0;
    }

    pub fn insert_str(&mut self, text: &str) {
        edit::insert(&mut self.filter, &mut self.cursor, &text.replace('\n', " "));
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

#[derive(Debug, Clone, Default)]
pub struct ModeState {
    pub current: Option<CurrentMode>,
    pub options: Vec<ModeOption>,
    pub list_loaded: bool,
    pub list_stale: bool,
    pub last_switch: Option<ModeSwitchResult>,
    pub last_error: Option<String>,
    pub picker: ModePickerState,
    /// `/mode <token>` waiting for a fresh `mode.list` to resolve an id.
    pub pending: Option<String>,
    /// `session.create` args waiting for a fresh `mode.list` to resolve `--mode`.
    pub pending_create: Option<(Map<String, Value>, String)>,
}

impl ModeState {
    pub fn resolve(&self, token: &str) -> Option<&ModeOption> {
        self.options
            .iter()
            .find(|option| option.name == token)
            .or_else(|| self.options.iter().find(|option| option.id == token))
    }

    pub fn filtered_options(&self) -> Vec<&ModeOption> {
        let needle = self.picker.filter.trim().to_ascii_lowercase();
        self.options
            .iter()
            .filter(|option| {
                needle.is_empty()
                    || option.name.to_ascii_lowercase().contains(&needle)
                    || option.id.to_ascii_lowercase().contains(&needle)
                    || option.description.to_ascii_lowercase().contains(&needle)
                    || option.source.to_ascii_lowercase().contains(&needle)
            })
            .collect()
    }

    pub fn picker_current(&self) -> Option<&ModeOption> {
        let options = self.filtered_options();
        options.get(self.picker.selected).copied()
    }
}

fn toast(text: impl Into<String>) -> Vec<Effect> {
    vec![Effect::ShowToast(Toast { text: text.into() })]
}

fn clear_composer(state: &mut TuiState) {
    state.composer.text.clear();
    state.composer.cursor = 0;
    state.slash = Default::default();
}

fn list_effect() -> Effect {
    Effect::SendCommand(CommandSpec {
        name: "mode.list".to_owned(),
        args: Map::new(),
        session_id: None,
    })
}

fn set_effect(state: &TuiState, name: &str) -> Option<Effect> {
    let session_id = state
        .session
        .as_ref()
        .map(|session| session.id.clone())
        .filter(|id| !id.is_empty())?;
    let mut args = Map::new();
    args.insert("mode".into(), Value::String(name.to_owned()));
    args.insert("session_id".into(), Value::String(session_id.clone()));
    Some(Effect::SendCommand(CommandSpec {
        name: "session.mode.set".to_owned(),
        args,
        session_id: Some(session_id),
    }))
}

pub fn busy_reason(state: &TuiState) -> Option<&'static str> {
    let Some(session) = state
        .session
        .as_ref()
        .filter(|session| !session.id.is_empty())
    else {
        return Some("no active session");
    };
    if state.connection == ConnectionState::ReadOnly {
        return Some("session is read-only; switching requires the lease owner");
    }
    if session.phase != "idle" {
        return Some("session is busy; wait for the current round or /interrupt");
    }
    if !state.pending_tools.is_empty()
        || !state.approvals.pending.is_empty()
        || state.transcript.active_stream.is_some()
    {
        return Some("session is busy; wait for the current round or /interrupt");
    }
    if state.queue.entries.iter().any(|entry| {
        matches!(
            entry.state,
            QueueEntryState::Sending | QueueEntryState::Queued
        )
    }) {
        return Some("session has queued input; wait for it to be delivered");
    }
    None
}

pub fn open_picker(state: &mut TuiState) -> Vec<Effect> {
    if state
        .session
        .as_ref()
        .is_none_or(|session| session.id.is_empty())
    {
        return toast("no active session");
    }
    state.overlay = Some(Overlay::Mode);
    state.mode.picker.reset();
    state.mode.last_error = None;
    vec![list_effect()]
}

pub fn refresh(state: &mut TuiState) -> Vec<Effect> {
    state.mode.list_stale = true;
    vec![list_effect()]
}

pub fn move_selection(state: &mut TuiState, delta: isize) {
    let count = state.mode.filtered_options().len();
    if count == 0 {
        state.mode.picker.selected = 0;
        return;
    }
    let current = state.mode.picker.selected.min(count - 1) as isize;
    state.mode.picker.selected = (current + delta).clamp(0, count as isize - 1) as usize;
}

pub fn select_current(state: &mut TuiState) -> Vec<Effect> {
    let Some(option) = state.mode.picker_current().cloned() else {
        return toast("no mode selected");
    };
    if let Some(reason) = busy_reason(state) {
        return toast(reason);
    }
    state.overlay = None;
    set_effect(state, &option.name).into_iter().collect()
}

pub fn apply_snapshot(state: &mut TuiState, payload: &Value) {
    if let Some(mode) = payload.get("mode") {
        apply_current(state, mode);
    }
}

pub fn apply_frame(state: &mut TuiState, payload: &Value) {
    apply_current(state, payload);
}

fn apply_current(state: &mut TuiState, value: &Value) {
    let Some(name) = value.get("name").and_then(Value::as_str) else {
        return;
    };
    let source = value
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("builtin")
        .to_owned();
    let id = value
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| format!("{source}/{name}"));
    state.mode.current = Some(CurrentMode {
        id,
        name: name.to_owned(),
        source,
        digest: value
            .get("digest")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        refresh: value
            .get("refresh")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    });
}

fn parse_option(value: &Value) -> Option<ModeOption> {
    let name = value.get("name").and_then(Value::as_str)?.to_owned();
    let source = value
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("builtin")
        .to_owned();
    Some(ModeOption {
        id: value
            .get("id")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| format!("{source}/{name}")),
        name,
        description: value
            .get("description")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        source,
        builtin: value
            .get("builtin")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        default: value
            .get("default")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

pub fn apply_list(state: &mut TuiState, result: &Value) -> Vec<Effect> {
    let mut options = Vec::new();
    if let Some(items) = result.get("modes").and_then(Value::as_array) {
        options.extend(items.iter().filter_map(parse_option));
    }
    state.mode.options = options;
    state.mode.list_loaded = true;
    state.mode.list_stale = false;
    state.mode.picker.selected = 0;
    if let Some(warnings) = result.get("warnings").and_then(Value::as_array) {
        for warning in warnings.iter().filter_map(Value::as_str) {
            state.activity.push("mode", warning.to_owned());
        }
    }

    let pending_switch = state.mode.pending.take();
    let pending_create = state.mode.pending_create.take();

    if let Some(token) = pending_switch {
        if let Some(reason) = busy_reason(state) {
            return toast(reason);
        }
        let resolved = state.mode.resolve(&token).map(|option| option.name.clone());
        return match resolved {
            Some(name) => set_effect(state, &name).into_iter().collect(),
            None => toast(format!(
                "mode not found: {token}; available: {}",
                available_modes(state)
            )),
        };
    }

    if let Some((mut args, token)) = pending_create {
        return match state.mode.resolve(&token).map(|option| option.name.clone()) {
            Some(name) => {
                args.insert("mode".into(), Value::String(name));
                vec![session_create_effect(args)]
            }
            None => {
                state.new_session.error = Some(format!(
                    "mode not found: {token}; available: {}",
                    available_modes(state)
                ));
                toast(format!("mode not found: {token}"))
            }
        };
    }

    Vec::new()
}

fn session_create_effect(args: Map<String, Value>) -> Effect {
    Effect::SendCommand(CommandSpec {
        name: "session.create".to_owned(),
        args,
        session_id: None,
    })
}

/// Resolve `--mode`/`default_mode` (name or id) before creating a session.
pub fn submit_create(state: &mut TuiState, mut args: Map<String, Value>) -> Vec<Effect> {
    let token = state.new_session.mode.trim().to_owned();
    if token.is_empty() {
        return vec![session_create_effect(args)];
    }
    state.new_session.error = None;
    if let Some(name) = state.mode.resolve(&token).map(|option| option.name.clone()) {
        args.insert("mode".into(), Value::String(name));
        return vec![session_create_effect(args)];
    }
    if !state.mode.list_loaded || state.mode.list_stale || state.mode.options.is_empty() {
        state.mode.pending_create = Some((args, token));
        return vec![list_effect()];
    }
    state.new_session.error = Some(format!(
        "mode not found: {token}; available: {}",
        available_modes(state)
    ));
    toast(format!("mode not found: {token}"))
}

pub fn apply_set_result(state: &mut TuiState, result: &Value) -> Vec<Effect> {
    let Some(mode) = result.get("mode") else {
        return Vec::new();
    };
    apply_current(state, mode);
    let Some(current) = state.mode.current.clone() else {
        return Vec::new();
    };
    state.mode.last_switch = Some(ModeSwitchResult {
        id: current.id.clone(),
        name: current.name.clone(),
        source: current.source.clone(),
        digest: current.digest.clone(),
        tools: mode.get("tools").and_then(Value::as_u64).unwrap_or(0),
        skills: mode.get("skills").and_then(Value::as_u64).unwrap_or(0),
    });
    if state.overlay == Some(Overlay::Mode) {
        state.overlay = None;
    }
    if let Some(warnings) = result.get("warnings").and_then(Value::as_array) {
        for warning in warnings.iter().filter_map(Value::as_str) {
            state.activity.push("mode", warning.to_owned());
        }
    }
    let changed = result
        .get("changed")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    if changed {
        toast(format!("[mode] {} [{}]", current.name, current.source))
    } else {
        toast(format!("already active: {}", current.name))
    }
}

pub fn apply_error(state: &mut TuiState, code: &str, message: &str) {
    state.mode.pending = None;
    state.mode.last_error = Some(format!("{code}: {message}"));
    if code == "E_MODE_NOT_FOUND" {
        state.mode.list_stale = true;
    }
}

pub fn execute_slash(state: &mut TuiState) -> Option<Vec<Effect>> {
    let text = state.composer.text.trim().to_owned();
    let mut parts = text.split_whitespace();
    if parts.next() != Some("/mode") {
        return None;
    }
    let args: Vec<&str> = parts.collect();
    clear_composer(state);
    if args.len() > 1 {
        return Some(toast(format!("usage: {USAGE}")));
    }
    match args.first().copied() {
        None | Some("list") => Some(open_picker(state)),
        Some("status") => Some(status_effects(state)),
        Some("help") => Some(toast(USAGE)),
        Some(token) => Some(switch_token(state, token)),
    }
}

fn switch_token(state: &mut TuiState, token: &str) -> Vec<Effect> {
    if state
        .session
        .as_ref()
        .is_none_or(|session| session.id.is_empty())
    {
        return toast("no active session");
    }
    if let Some(reason) = busy_reason(state) {
        return toast(reason);
    }
    if let Some(option) = state.mode.resolve(token).map(|option| option.name.clone()) {
        return set_effect(state, &option).into_iter().collect();
    }
    if !state.mode.list_loaded || state.mode.list_stale || state.mode.options.is_empty() {
        state.mode.pending = Some(token.to_owned());
        return vec![list_effect()];
    }
    toast(format!(
        "mode not found: {token}; available: {}",
        available_modes(state)
    ))
}

fn status_effects(state: &TuiState) -> Vec<Effect> {
    let Some(current) = state.mode.current.as_ref() else {
        return toast("mode: unknown (waiting for snapshot)");
    };
    let mut text = format!(
        "mode {} [{}] id={} digest={}",
        current.name, current.source, current.id, current.digest
    );
    if let Some(last) = state
        .mode
        .last_switch
        .as_ref()
        .filter(|last| last.id == current.id)
    {
        text.push_str(&format!(" tools={} skills={}", last.tools, last.skills));
    }
    toast(text)
}

fn available_modes(state: &TuiState) -> String {
    if state.mode.options.is_empty() {
        return "(none)".to_owned();
    }
    state
        .mode
        .options
        .iter()
        .map(|option| format!("{} ({})", option.name, option.id))
        .collect::<Vec<_>>()
        .join(", ")
}

pub fn mode_id(source: &str, name: &str) -> String {
    format!("{source}/{name}")
}
