//! Plugin panel: snapshot of the host's plugin catalog.
//!
//! The panel sends `plugin.list` and renders the returned summary; the
//! management keys additionally send `plugin.enable`/`plugin.disable`, and the
//! attach-mode `Ctrl+A` sends `plugin.set_allowed(mode="apply")` (post-v0.4
//! extension). The host partition (`host:<uuid>`) is not a session partition,
//! so lifecycle events are deliberately not subscribed to (plugin design
//! §11.4.6).

use serde_json::{Map, Value};

use super::edit;
use super::{CommandSpec, ConnectionState, Effect, GatewayMode, Overlay, Toast, TuiState};
use crate::protocol::ErrorInfo;

pub const USAGE: &str = "/plugins";

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PluginEntry {
    pub id: String,
    pub version: String,
    pub description: String,
    pub state: String,
    pub trust: String,
    pub source: String,
    pub tools: Vec<String>,
    pub skills: Vec<String>,
    pub modes: Vec<String>,
    pub digest: String,
    /// File-level `agent.plugins.allow` membership; `None` on old gateways.
    pub enabled: Option<bool>,
    pub error: Option<PluginError>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct PluginError {
    pub code: String,
    pub message: String,
    pub hint: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct InvalidPlugin {
    pub source: String,
    pub code: String,
    pub error: String,
}

impl PluginEntry {
    fn from_json(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        Some(Self {
            id: string(object, "id"),
            version: string(object, "version"),
            description: string(object, "description"),
            state: string(object, "state"),
            trust: string(object, "trust"),
            source: string(object, "source"),
            tools: string_list(object.get("tools")),
            skills: string_list(object.get("skills")),
            modes: string_list(object.get("modes")),
            digest: string(object, "digest"),
            enabled: object.get("enabled").and_then(Value::as_bool),
            error: object.get("error").and_then(PluginError::from_json),
            warnings: string_list(object.get("warnings")),
        })
    }
}

impl PluginError {
    fn from_json(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        Some(Self {
            code: string(object, "code"),
            message: string(object, "message"),
            hint: object
                .get("hint")
                .and_then(Value::as_str)
                .filter(|hint| !hint.is_empty())
                .map(str::to_owned),
        })
    }
}

impl InvalidPlugin {
    fn from_json(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        Some(Self {
            source: string(object, "source"),
            code: string(object, "code"),
            error: string(object, "error"),
        })
    }
}

fn string(object: &Map<String, Value>, key: &str) -> String {
    object
        .get(key)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned()
}

fn string_list(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

/// One rendered row: a real plugin or an invalid manifest directory.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PluginRow<'a> {
    Plugin(&'a PluginEntry),
    Invalid(&'a InvalidPlugin),
}

impl<'a> PluginRow<'a> {
    pub fn id(&self) -> &'a str {
        match self {
            Self::Plugin(entry) => &entry.id,
            Self::Invalid(invalid) => &invalid.source,
        }
    }

    pub fn source(&self) -> &'a str {
        match self {
            Self::Plugin(entry) => &entry.source,
            Self::Invalid(invalid) => &invalid.source,
        }
    }

    pub fn state(&self) -> &'a str {
        match self {
            Self::Plugin(entry) => &entry.state,
            Self::Invalid(_) => "invalid",
        }
    }

    pub fn version(&self) -> &'a str {
        match self {
            Self::Plugin(entry) => &entry.version,
            Self::Invalid(_) => "-",
        }
    }

    pub fn trust(&self) -> &'a str {
        match self {
            Self::Plugin(entry) => &entry.trust,
            Self::Invalid(_) => "-",
        }
    }

    pub fn caps(&self) -> (usize, usize, usize) {
        match self {
            Self::Plugin(entry) => (entry.tools.len(), entry.skills.len(), entry.modes.len()),
            Self::Invalid(_) => (0, 0, 0),
        }
    }

    pub fn enabled(&self) -> Option<bool> {
        match self {
            Self::Plugin(entry) => entry.enabled,
            Self::Invalid(_) => None,
        }
    }
}

/// File-level enablement differs from the runtime state: a saved change only
/// takes effect after the host restarts (§11.5.4).
pub fn pending_restart(entry: &PluginEntry) -> bool {
    match entry.enabled {
        Some(true) => matches!(entry.state.as_str(), "disabled" | "discovered" | "unloaded"),
        Some(false) => matches!(entry.state.as_str(), "active" | "loading" | "quiescing"),
        None => false,
    }
}

/// One pending confirmation in the `/plugins` panel (§11.5.5).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PluginAction {
    Enable,
    Disable,
    /// Restart the spawned gateway so saved plugin changes take effect.
    Restart,
    /// Apply the saved allow list to the running gateway (attach mode only,
    /// post-v0.4 extension).
    Apply,
}

fn state_rank(state: &str) -> u8 {
    match state {
        "active" => 0,
        "loading" => 1,
        "quiescing" => 2,
        "failed" => 3,
        "disabled" => 4,
        "discovered" => 5,
        "unloaded" => 6,
        "invalid" => 7,
        _ => 8,
    }
}

#[derive(Debug, Clone, Default)]
pub struct PluginsState {
    pub entries: Vec<PluginEntry>,
    pub invalid: Vec<InvalidPlugin>,
    pub warnings: Vec<String>,
    pub loaded: bool,
    pub loading: bool,
    pub stale: bool,
    pub error: Option<String>,
    pub filter: String,
    pub cursor: usize,
    pub selected: usize,
    /// Whether the gateway accepts `plugin.enable`/`plugin.disable` right now.
    pub manage: bool,
    /// A manage command is in flight (single-flight, like `loading`).
    pub managing: bool,
    /// Confirmation bar waiting for `y` / `n`.
    pub pending: Option<PluginAction>,
    pub last_manage_error: Option<String>,
    /// At least one row has a saved change that needs a host restart.
    pub restart_required: bool,
    /// A confirmed restart is in flight; wait for the reconnect.
    pub restarting: bool,
    /// File-level `agent.plugins.allow` from `plugin.list` (post-v0.4 apply
    /// extension); `None` when the gateway omits the field.
    pub allow: Option<Vec<String>>,
    /// A confirmed `plugin.set_allowed(mode="apply")` is in flight.
    pub applying: bool,
}

impl PluginsState {
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

    pub fn move_selection(&mut self, delta: isize) {
        let count = self.filtered_rows().len();
        if count == 0 {
            self.selected = 0;
            return;
        }
        let current = self.selected.min(count - 1) as isize;
        self.selected = (current + delta).clamp(0, count as isize - 1) as usize;
    }

    pub fn clamp_selection(&mut self) {
        let count = self.filtered_rows().len();
        self.selected = if count == 0 {
            0
        } else {
            self.selected.min(count - 1)
        };
    }

    /// Rows matching the current filter, in stable display order.
    pub fn filtered_rows(&self) -> Vec<PluginRow<'_>> {
        let needle = self.filter.trim().to_ascii_lowercase();
        let mut rows = Vec::with_capacity(self.entries.len() + self.invalid.len());
        rows.extend(self.entries.iter().map(PluginRow::Plugin));
        rows.extend(self.invalid.iter().map(PluginRow::Invalid));
        if !needle.is_empty() {
            rows.retain(|row| matches(row, &needle));
        }
        rows.sort_by(|left, right| {
            state_rank(left.state())
                .cmp(&state_rank(right.state()))
                .then_with(|| {
                    left.id()
                        .to_ascii_lowercase()
                        .cmp(&right.id().to_ascii_lowercase())
                })
                .then_with(|| left.id().cmp(right.id()))
                .then_with(|| left.source().cmp(right.source()))
        });
        rows
    }

    pub fn selected_row(&self) -> Option<PluginRow<'_>> {
        self.filtered_rows().into_iter().nth(self.selected)
    }
}

fn matches(row: &PluginRow<'_>, needle: &str) -> bool {
    let haystack = match row {
        PluginRow::Plugin(entry) => format!(
            "{} {} {} {} {} {} {} {}",
            entry.id,
            entry.source,
            entry.description,
            entry.state,
            entry.trust,
            entry.tools.join(" "),
            entry.skills.join(" "),
            entry.modes.join(" "),
        ),
        PluginRow::Invalid(invalid) => {
            format!("{} {} {}", invalid.source, invalid.code, invalid.error)
        }
    };
    haystack.to_ascii_lowercase().contains(needle)
}

fn toast(text: impl Into<String>) -> Vec<Effect> {
    vec![Effect::ShowToast(Toast { text: text.into() })]
}

fn list_effect() -> Effect {
    Effect::SendCommand(CommandSpec {
        name: "plugin.list".to_owned(),
        args: Map::new(),
        session_id: None,
    })
}

fn can_send(state: &TuiState) -> Result<(), Vec<Effect>> {
    if !matches!(
        state.connection,
        ConnectionState::Live | ConnectionState::ReadOnly
    ) {
        return Err(toast("gateway not ready"));
    }
    if !state.features.iter().any(|feature| feature == "plugins") {
        return Err(toast("plugins are not supported by this gateway"));
    }
    Ok(())
}

fn begin_request(state: &mut TuiState) -> Vec<Effect> {
    state.plugins.stale = true;
    if state.plugins.loading {
        return Vec::new();
    }
    state.plugins.loading = true;
    vec![list_effect()]
}

pub fn open(state: &mut TuiState) -> Vec<Effect> {
    if let Err(effects) = can_send(state) {
        return effects;
    }
    state.overlay = Some(Overlay::Plugins);
    state.plugins.filter.clear();
    state.plugins.cursor = 0;
    state.plugins.selected = 0;
    state.plugins.error = None;
    begin_request(state)
}

pub fn refresh(state: &mut TuiState) -> Vec<Effect> {
    if let Err(effects) = can_send(state) {
        return effects;
    }
    begin_request(state)
}

pub fn execute_slash(state: &mut TuiState) -> Option<Vec<Effect>> {
    let text = state.composer.text.trim().to_owned();
    let mut parts = text.split_whitespace();
    if parts.next() != Some("/plugins") {
        return None;
    }
    let args: Vec<&str> = parts.collect();
    state.composer.text.clear();
    state.composer.cursor = 0;
    state.slash = Default::default();
    if !args.is_empty() {
        return Some(toast(format!("usage: {USAGE}")));
    }
    Some(open(state))
}

pub fn move_selection(state: &mut TuiState, delta: isize) {
    state.plugins.move_selection(delta);
}

pub fn apply_list(state: &mut TuiState, result: &Value) -> Vec<Effect> {
    state.plugins.entries = result
        .get("plugins")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(PluginEntry::from_json).collect())
        .unwrap_or_default();
    state.plugins.invalid = result
        .get("invalid")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(InvalidPlugin::from_json).collect())
        .unwrap_or_default();
    state.plugins.warnings = string_list(result.get("warnings"));
    state.plugins.manage = result
        .get("manage")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    state.plugins.allow = result.get("allow").and_then(Value::as_array).map(|items| {
        items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect()
    });
    state.plugins.loaded = true;
    state.plugins.loading = false;
    state.plugins.stale = false;
    state.plugins.error = None;
    state.plugins.last_manage_error = None;
    state.plugins.restart_required = state.plugins.entries.iter().any(pending_restart);
    state.plugins.clamp_selection();
    Vec::new()
}

pub fn apply_error(state: &mut TuiState, error: &ErrorInfo) {
    state.plugins.loading = false;
    state.plugins.stale = state.plugins.loaded;
    state.plugins.error = Some(format!("{}: {}", error.code, error.message));
}

fn can_manage(state: &TuiState) -> bool {
    state
        .features
        .iter()
        .any(|feature| feature == "plugins.manage")
        && state.plugins.manage
}

fn selected_plugin(state: &TuiState) -> Option<(String, Option<bool>)> {
    match state.plugins.selected_row()? {
        PluginRow::Plugin(entry) => Some((entry.id.clone(), entry.enabled)),
        PluginRow::Invalid(_) => None,
    }
}

/// `Ctrl+E` / `Ctrl+D`: open a confirmation bar (never sends by itself).
pub fn begin_manage(state: &mut TuiState, action: PluginAction) -> Vec<Effect> {
    if state.plugins.managing || state.plugins.pending.is_some() {
        return Vec::new();
    }
    if !can_manage(state) {
        return toast("plugin management is disabled");
    }
    let Some((_id, enabled)) = selected_plugin(state) else {
        return toast("not a plugin id");
    };
    match (action, enabled) {
        (PluginAction::Enable, Some(true)) => return toast("already enabled"),
        (PluginAction::Disable, Some(false)) => return toast("already disabled"),
        _ => {}
    }
    state.plugins.pending = Some(action);
    Vec::new()
}

/// `y`: send the confirmed `plugin.enable` / `plugin.disable`, restart the
/// spawned gateway for a confirmed `Restart`, or apply the saved allow list
/// for a confirmed `Apply` (§11.5.5 / post-v0.4 extension).
pub fn confirm_manage(state: &mut TuiState) -> Vec<Effect> {
    let Some(action) = state.plugins.pending else {
        return Vec::new();
    };
    if state.plugins.managing {
        return Vec::new();
    }
    if action == PluginAction::Restart {
        state.plugins.pending = None;
        state.plugins.restarting = true;
        return vec![Effect::RestartChild];
    }
    if action == PluginAction::Apply {
        // The apply confirmation is not tied to the selected row: it submits
        // exactly the saved allow list (post-v0.4 extension).
        state.plugins.pending = None;
        let Some(allow) = state.plugins.allow.clone() else {
            return toast("plugin allow list unavailable; restart the gateway externally");
        };
        state.plugins.applying = true;
        state.plugins.last_manage_error = None;
        let mut args = Map::new();
        args.insert(
            "ids".into(),
            Value::Array(allow.into_iter().map(Value::String).collect()),
        );
        args.insert("mode".into(), Value::String("apply".to_owned()));
        return vec![Effect::SendCommand(CommandSpec {
            name: "plugin.set_allowed".to_owned(),
            args,
            session_id: None,
        })];
    }
    let Some((id, _enabled)) = selected_plugin(state) else {
        state.plugins.pending = None;
        return toast("not a plugin id");
    };
    state.plugins.pending = None;
    state.plugins.managing = true;
    state.plugins.last_manage_error = None;
    let mut args = Map::new();
    args.insert("id".into(), Value::String(id));
    vec![Effect::SendCommand(CommandSpec {
        name: match action {
            PluginAction::Enable => "plugin.enable".to_owned(),
            PluginAction::Disable => "plugin.disable".to_owned(),
            PluginAction::Restart | PluginAction::Apply => {
                unreachable!("restart/apply return above")
            }
        },
        args,
        session_id: None,
    })]
}

/// `n` / `Esc` while the confirmation bar is open.
pub fn cancel_manage(state: &mut TuiState) -> Vec<Effect> {
    state.plugins.pending = None;
    Vec::new()
}

/// `Ctrl+T`: open a restart confirmation. Only a spawned gateway with saved
/// plugin changes qualifies; anything else toasts (§11.5.4).
pub fn begin_restart(state: &mut TuiState) -> Vec<Effect> {
    if state.plugins.restarting || state.plugins.pending.is_some() {
        return Vec::new();
    }
    if state.gateway_mode == GatewayMode::Attach {
        return toast("attach mode: restart the gateway externally");
    }
    if !state.plugins.restart_required {
        return toast("no saved plugin changes to apply");
    }
    state.plugins.pending = Some(PluginAction::Restart);
    Vec::new()
}

/// `Ctrl+A`: open an apply confirmation. Only an attached gateway with the
/// runtime feature, management enabled, saved changes, and a parsed allow list
/// qualifies; anything else toasts (post-v0.4 extension).
pub fn begin_apply(state: &mut TuiState) -> Vec<Effect> {
    if state.plugins.managing || state.plugins.applying || state.plugins.pending.is_some() {
        return Vec::new();
    }
    if state.gateway_mode != GatewayMode::Attach {
        return toast("use Ctrl+T to restart the spawned gateway");
    }
    if !state
        .features
        .iter()
        .any(|feature| feature == "plugins.runtime")
    {
        return toast("runtime apply is not supported by this gateway");
    }
    if !state.plugins.manage {
        return toast("plugin management is disabled");
    }
    if !state.plugins.restart_required {
        return toast("no saved plugin changes to apply");
    }
    if state.plugins.allow.is_none() {
        return toast("plugin allow list unavailable; restart the gateway externally");
    }
    state.plugins.pending = Some(PluginAction::Apply);
    Vec::new()
}

/// A successful `plugin.set_allowed` (post-v0.4 extension): clear the in-flight
/// flags, take the reported `restart_required`, and refresh the snapshot so the
/// row states follow the real runtime list.
pub fn apply_set_allowed_result(state: &mut TuiState, result: &Value) -> Vec<Effect> {
    state.plugins.applying = false;
    state.plugins.pending = None;
    state.plugins.last_manage_error = None;
    state.plugins.restart_required = result
        .get("restart_required")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    refresh(state)
}

pub fn apply_manage_result(state: &mut TuiState, result: &Value) -> Vec<Effect> {
    state.plugins.managing = false;
    state.plugins.pending = None;
    state.plugins.last_manage_error = None;
    if let Some(plugin) = result.get("plugin").and_then(Value::as_object) {
        let id = plugin.get("id").and_then(Value::as_str).unwrap_or_default();
        if let Some(entry) = state
            .plugins
            .entries
            .iter_mut()
            .find(|entry| entry.id == id)
        {
            entry.enabled = plugin.get("enabled").and_then(Value::as_bool);
            if let Some(runtime_state) = plugin.get("state").and_then(Value::as_str) {
                entry.state = runtime_state.to_owned();
            }
        }
        // Mirror the gateway's `plugin.enable`/`plugin.disable` list edit on
        // the parsed allow snapshot, so a later `Ctrl+A` apply submits the
        // persisted list instead of the stale pre-edit one and reverts the
        // just-saved change (post-v0.4 extension).
        if !id.is_empty()
            && let Some(allow) = state.plugins.allow.as_mut()
        {
            match plugin.get("enabled").and_then(Value::as_bool) {
                Some(true) => {
                    if !allow.iter().any(|existing| existing == id) {
                        allow.push(id.to_owned());
                    }
                }
                Some(false) => allow.retain(|existing| existing != id),
                None => {}
            }
        }
    }
    state.plugins.restart_required = state.plugins.entries.iter().any(pending_restart);

    let changed = result
        .get("changed")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let restart = result
        .get("restart_required")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    if changed {
        Vec::new()
    } else if restart {
        toast("already saved · restart required")
    } else {
        toast("no change")
    }
}

pub fn apply_manage_error(state: &mut TuiState, error: &ErrorInfo) -> Vec<Effect> {
    state.plugins.managing = false;
    state.plugins.applying = false;
    state.plugins.pending = None;
    state.plugins.last_manage_error = Some(format!("{}: {}", error.code, error.message));
    if error.code == "E_PLUGIN_MANAGE_DISABLED" {
        state.plugins.manage = false;
        return Vec::new();
    }
    if error.code == "E_PLUGIN_NOT_FOUND" {
        // The row was stale; refresh the snapshot (§11.5.5).
        return refresh(state);
    }
    Vec::new()
}

/// After a reconnect reaches `Live`, refetch if the panel is waiting.
pub fn on_live(state: &mut TuiState) -> Vec<Effect> {
    if state.overlay != Some(Overlay::Plugins) || !state.plugins.stale || state.plugins.loading {
        return Vec::new();
    }
    if !state.features.iter().any(|feature| feature == "plugins") {
        return Vec::new();
    }
    state.plugins.loading = true;
    vec![list_effect()]
}

/// A reconnect reached a different gateway process: saved plugin changes may
/// already be in effect, so restart state is cleared and the panel refetches
/// once the connection goes `Live`.
pub fn on_new_instance(state: &mut TuiState) {
    state.plugins.restart_required = false;
    state.plugins.restarting = false;
    state.plugins.stale = true;
}

/// The spawned gateway exited before the restart completed (`ChildExited` /
/// `Fatal`): the restart prompt is abandoned, not retried.
pub fn on_restart_failed(state: &mut TuiState) {
    state.plugins.restarting = false;
    state.plugins.pending = None;
}

/// A successful spawn-mode connection is the restart-success signal even when
/// the old gateway reports no instance id; the instance-gated
/// `restart_required` flag is cleared separately by [`on_new_instance`]
/// (§11.5.5).
pub fn on_restart_succeeded(state: &mut TuiState) {
    state.plugins.restarting = false;
    state.plugins.pending = None;
}

pub fn on_disconnect(state: &mut TuiState) {
    state.plugins.loading = false;
    // A confirmed action is not retried automatically (§11.5.5).
    state.plugins.managing = false;
    state.plugins.applying = false;
    state.plugins.pending = None;
    if state.plugins.loaded {
        state.plugins.stale = true;
    }
}
