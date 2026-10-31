#![allow(clippy::field_reassign_with_default)]

use linteum_tui::protocol::{CommandResult, ErrorInfo};
use linteum_tui::viewmodel::{
    CommandSpec, ConnectionEvent, ConnectionState, Effect, Input, Overlay, SessionView, TuiState,
    UiAction, reduce,
};
use serde_json::{Value, json};

fn state() -> TuiState {
    let mut state = TuiState {
        session: Some(SessionView {
            id: "s1".into(),
            phase: "idle".into(),
            ..Default::default()
        }),
        ..Default::default()
    };
    state.connection = ConnectionState::Live;
    state.features = vec!["view".into(), "plugins".into()];
    state
}

fn entry(id: &str, state: &str) -> Value {
    json!({
        "id": id,
        "version": "1.0.0",
        "state": state,
        "trust": "third-party",
        "description": format!("{id} description"),
        "source": id.replace('.', "-"),
        "tools": ["web_search"],
        "skills": [],
        "modes": ["research"],
        "digest": "sha256:abc",
    })
}

fn ok(result: Value) -> CommandResult {
    CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: true,
        result,
        error: None,
    }
}

fn err(code: &str) -> CommandResult {
    CommandResult {
        v: 1,
        id: "r".into(),
        reply_to: "c".into(),
        ok: false,
        result: json!({}),
        error: Some(ErrorInfo {
            code: code.into(),
            source: "gateway".into(),
            location: "test".into(),
            message: format!("{code} message"),
            exc_type: None,
            detail: String::new(),
            retryable: false,
            enters_history: false,
            context: json!({}),
        }),
    }
}

fn commands(effects: &[Effect]) -> Vec<&CommandSpec> {
    effects
        .iter()
        .filter_map(|effect| match effect {
            Effect::SendCommand(command) => Some(command),
            _ => None,
        })
        .collect()
}

fn has_toast(effects: &[Effect], needle: &str) -> bool {
    effects.iter().any(|effect| match effect {
        Effect::ShowToast(toast) => toast.text.contains(needle),
        _ => false,
    })
}

fn loaded_state() -> TuiState {
    let mut state = state();
    state.plugins.loaded = true;
    state.plugins.entries = vec![linteum_tui::viewmodel::PluginEntry {
        id: "acme.search".into(),
        version: "1.0.0".into(),
        description: "Web search and research helper.".into(),
        state: "active".into(),
        trust: "third-party".into(),
        source: "acme-search".into(),
        tools: vec!["web_search".into()],
        skills: vec!["web-research".into()],
        modes: vec!["research".into()],
        digest: "sha256:abc".into(),
        error: None,
        warnings: Vec::new(),
    }];
    state
}

#[test]
fn opening_plugins_sends_one_plugin_list_without_session_or_args() {
    let mut state = state();
    let effects = reduce(&mut state, Input::Action(UiAction::OpenPlugins));

    assert_eq!(state.overlay, Some(Overlay::Plugins));
    let commands = commands(&effects);
    assert_eq!(commands.len(), 1);
    assert_eq!(commands[0].name, "plugin.list");
    assert!(commands[0].args.is_empty());
    assert!(commands[0].session_id.is_none());
    assert!(state.plugins.loading);
}

#[test]
fn opening_plugins_without_the_feature_only_toasts() {
    let mut state = state();
    state.features = vec!["view".into()];
    let effects = reduce(&mut state, Input::Action(UiAction::OpenPlugins));

    assert_eq!(state.overlay, None);
    assert!(commands(&effects).is_empty());
    assert!(has_toast(&effects, "not supported"));
}

#[test]
fn opening_plugins_while_replaying_does_not_send() {
    let mut state = state();
    state.connection = ConnectionState::Replaying;
    let effects = reduce(&mut state, Input::Action(UiAction::OpenPlugins));

    assert_eq!(state.overlay, None);
    assert!(commands(&effects).is_empty());
    assert!(has_toast(&effects, "not ready"));
}

#[test]
fn successful_list_sorts_rows_and_clears_loading() {
    let mut state = state();
    state.plugins.loading = true;
    state.plugins.stale = true;
    let effects = reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugins": [
                entry("z.disabled", "disabled"),
                entry("a.active", "active"),
                entry("m.failed", "failed"),
            ],
            "invalid": [
                {"source": "broken", "code": "E_PLUGIN_MANIFEST", "error": "bad yaml"}
            ],
            "warnings": ["plugin 'x' from allow was not found"],
            "unknown_field": true,
        }))),
    );

    assert!(commands(&effects).is_empty());
    assert!(!state.plugins.loading);
    assert!(!state.plugins.stale);
    assert!(state.plugins.loaded);
    assert_eq!(state.plugins.warnings.len(), 1);
    assert_eq!(state.plugins.invalid.len(), 1);

    let ids: Vec<&str> = state
        .plugins
        .filtered_rows()
        .iter()
        .map(|row| row.id())
        .collect();
    assert_eq!(ids, ["a.active", "m.failed", "z.disabled", "broken"]);
}

#[test]
fn per_plugin_error_and_warnings_are_parsed() {
    let mut state = state();
    state.plugins.loading = true;
    let mut failed = entry("acme.fail", "failed");
    failed["error"] = json!({
        "code": "E_PLUGIN_LOAD",
        "message": "RuntimeError: boom",
        "hint": "check setup()",
    });
    failed["warnings"] = json!(["plugin.load_timeout_abandoned: acme.fail"]);

    reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugins": [failed],
            "invalid": [],
            "warnings": [],
        }))),
    );

    let plugin = &state.plugins.entries[0];
    let error = plugin.error.as_ref().expect("error parsed");
    assert_eq!(error.code, "E_PLUGIN_LOAD");
    assert_eq!(error.message, "RuntimeError: boom");
    assert_eq!(error.hint.as_deref(), Some("check setup()"));
    assert_eq!(
        plugin.warnings,
        ["plugin.load_timeout_abandoned: acme.fail"]
    );
}

#[test]
fn command_error_keeps_rows_and_sets_error() {
    let mut state = loaded_state();
    state.plugins.loading = true;
    let effects = reduce(&mut state, Input::CommandResult(err("E_UNKNOWN_COMMAND")));

    assert!(!state.plugins.loading);
    assert_eq!(state.plugins.entries.len(), 1);
    assert!(state.plugins.stale);
    assert!(
        state
            .plugins
            .error
            .as_deref()
            .is_some_and(|error| error.contains("E_UNKNOWN_COMMAND"))
    );
    assert!(has_toast(&effects, "E_UNKNOWN_COMMAND"));
}

#[test]
fn non_plugin_success_does_not_clear_loading() {
    let mut state = state();
    state.plugins.loading = true;
    reduce(
        &mut state,
        Input::CommandResult(ok(json!({"unrelated": true}))),
    );
    assert!(state.plugins.loading);
}

#[test]
fn filter_matches_capability_names_and_keeps_r_typable() {
    let mut state = loaded_state();
    state.plugins.insert_str("web_search");
    let ids: Vec<&str> = state
        .plugins
        .filtered_rows()
        .iter()
        .map(|row| row.id())
        .collect();
    assert_eq!(ids, ["acme.search"]);

    state.plugins.filter.clear();
    state.plugins.cursor = 0;
    state.plugins.insert_char('r');
    assert_eq!(state.plugins.filter, "r");
}

#[test]
fn selection_moves_and_clamps() {
    let mut state = loaded_state();
    state
        .plugins
        .entries
        .push(linteum_tui::viewmodel::PluginEntry {
            id: "b.other".into(),
            state: "disabled".into(),
            ..Default::default()
        });
    state.plugins.move_selection(1);
    assert_eq!(state.plugins.selected, 1);
    state.plugins.move_selection(10);
    assert_eq!(state.plugins.selected, 1);
    state.plugins.move_selection(-10);
    assert_eq!(state.plugins.selected, 0);
}

#[test]
fn refresh_without_connection_does_not_send() {
    let mut state = loaded_state();
    state.connection = ConnectionState::Offline;
    let effects = reduce(&mut state, Input::Action(UiAction::PluginRefresh));
    assert!(commands(&effects).is_empty());
    assert!(has_toast(&effects, "not ready"));
}

#[test]
fn slash_plugins_opens_and_rejects_extra_args() {
    let mut first = state();
    first.composer.text = "/plugins".into();
    let effects = reduce(&mut first, Input::Action(UiAction::ExecuteSlashCommand));
    assert_eq!(first.overlay, Some(Overlay::Plugins));
    assert!(commands(&effects).iter().any(|c| c.name == "plugin.list"));
    assert!(first.composer.text.is_empty());

    let mut second = state();
    second.composer.text = "/plugins extra".into();
    let effects = reduce(&mut second, Input::Action(UiAction::ExecuteSlashCommand));
    assert_eq!(second.overlay, None);
    assert!(commands(&effects).is_empty());
    assert!(has_toast(&effects, "/plugins"));
    assert!(second.composer.text.is_empty());
}

#[test]
fn reconnected_carries_features_and_defers_refetch_until_live() {
    let mut state = loaded_state();
    state.overlay = Some(Overlay::Plugins);
    state.plugins.stale = false;
    let effects = reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Reconnected {
            features: vec!["view".into(), "plugins".into()],
        }),
    );

    assert_eq!(state.connection, ConnectionState::Replaying);
    assert_eq!(state.features, ["view", "plugins"]);
    assert!(state.plugins.stale);
    assert!(!commands(&effects).iter().any(|c| c.name == "plugin.list"));

    let effects = reduce(
        &mut state,
        Input::CommandResult(ok(json!({"subscribed": true}))),
    );
    assert_eq!(state.connection, ConnectionState::Live);
    assert!(commands(&effects).iter().any(|c| c.name == "plugin.list"));
}

#[test]
fn reconnected_panel_stale_without_feature_does_not_refetch() {
    let mut state = loaded_state();
    state.overlay = Some(Overlay::Plugins);
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Reconnected {
            features: vec!["view".into()],
        }),
    );
    let effects = reduce(
        &mut state,
        Input::CommandResult(ok(json!({"subscribed": true}))),
    );
    assert!(!commands(&effects).iter().any(|c| c.name == "plugin.list"));
}

#[test]
fn reconnected_with_closed_panel_does_not_refetch() {
    let mut state = loaded_state();
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Reconnected {
            features: vec!["view".into(), "plugins".into()],
        }),
    );
    let effects = reduce(
        &mut state,
        Input::CommandResult(ok(json!({"subscribed": true}))),
    );
    assert!(!commands(&effects).iter().any(|c| c.name == "plugin.list"));
}

#[test]
fn plugin_actions_only_send_plugin_list() {
    let mut state = state();
    let open = reduce(&mut state, Input::Action(UiAction::OpenPlugins));
    assert!(commands(&open).iter().all(|c| c.name == "plugin.list"));
    assert!(commands(&open).iter().all(|c| c.session_id.is_none()));

    let refresh = reduce(&mut state, Input::Action(UiAction::PluginRefresh));
    assert!(commands(&refresh).iter().all(|c| c.name == "plugin.list"));
    assert!(commands(&refresh).iter().all(|c| c.session_id.is_none()));
}
