#![allow(clippy::field_reassign_with_default)]

use linteum_tui::protocol::{CommandResult, ErrorInfo};
use linteum_tui::viewmodel::{
    CommandSpec, ConnectionEvent, ConnectionState, Effect, Input, Overlay, PluginAction,
    SessionView, TuiState, UiAction, reduce,
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
        enabled: Some(true),
        error: None,
        warnings: Vec::new(),
    }];
    state
}

fn managed_state() -> TuiState {
    let mut state = loaded_state();
    state.features = vec!["view".into(), "plugins".into(), "plugins.manage".into()];
    state.plugins.manage = true;
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

#[test]
fn list_parses_enabled_and_manage() {
    let mut state = state();
    state.plugins.loading = true;
    let mut enabled = entry("acme.search", "active");
    enabled["enabled"] = json!(true);
    let mut disabled = entry("other.disabled", "disabled");
    disabled["enabled"] = json!(false);

    reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugins": [enabled, disabled],
            "invalid": [],
            "warnings": [],
            "manage": true,
        }))),
    );

    assert!(state.plugins.manage);
    assert_eq!(state.plugins.entries[0].enabled, Some(true));
    assert_eq!(state.plugins.entries[1].enabled, Some(false));
}

#[test]
fn old_gateway_fields_are_optional() {
    let mut state = state();
    state.plugins.loading = true;
    reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugins": [entry("acme.search", "active")],
            "invalid": [],
            "warnings": [],
        }))),
    );
    assert!(!state.plugins.manage);
    assert_eq!(state.plugins.entries[0].enabled, None);
}

#[test]
fn disable_opens_confirmation_and_confirm_sends_the_command() {
    let mut state = managed_state();
    let effects = reduce(&mut state, Input::Action(UiAction::PluginDisable));

    assert!(commands(&effects).is_empty());
    assert_eq!(state.plugins.pending, Some(PluginAction::Disable));

    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    let sent = commands(&effects);
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0].name, "plugin.disable");
    assert_eq!(sent[0].args.get("id"), Some(&json!("acme.search")));
    assert!(sent[0].session_id.is_none());
    assert!(state.plugins.managing);
    assert_eq!(state.plugins.pending, None);
}

#[test]
fn enable_is_gated_by_feature_and_manage() {
    let mut state = loaded_state();
    state.plugins.entries[0].enabled = Some(false);
    state.features = vec!["plugins".into()];
    let effects = reduce(&mut state, Input::Action(UiAction::PluginEnable));
    assert_eq!(state.plugins.pending, None);
    assert!(commands(&effects).is_empty());
    assert!(has_toast(&effects, "management is disabled"));

    let mut state = loaded_state();
    state.plugins.entries[0].enabled = Some(false);
    state.features = vec!["plugins".into(), "plugins.manage".into()];
    state.plugins.manage = false;
    let effects = reduce(&mut state, Input::Action(UiAction::PluginEnable));
    assert_eq!(state.plugins.pending, None);
    assert!(has_toast(&effects, "management is disabled"));
}

#[test]
fn already_enabled_and_disabled_are_noops() {
    let mut state = managed_state();
    let effects = reduce(&mut state, Input::Action(UiAction::PluginEnable));
    assert_eq!(state.plugins.pending, None);
    assert!(has_toast(&effects, "already enabled"));

    state.plugins.entries[0].enabled = Some(false);
    state.plugins.entries[0].state = "disabled".into();
    let effects = reduce(&mut state, Input::Action(UiAction::PluginDisable));
    assert_eq!(state.plugins.pending, None);
    assert!(has_toast(&effects, "already disabled"));
}

#[test]
fn invalid_rows_have_no_management_action() {
    let mut state = state();
    state.features = vec!["plugins".into(), "plugins.manage".into()];
    state.plugins.manage = true;
    state.plugins.loaded = true;
    state.plugins.invalid = vec![linteum_tui::viewmodel::InvalidPlugin {
        source: "broken".into(),
        code: "E_PLUGIN_MANIFEST".into(),
        error: "bad yaml".into(),
    }];
    let effects = reduce(&mut state, Input::Action(UiAction::PluginEnable));
    assert_eq!(state.plugins.pending, None);
    assert!(has_toast(&effects, "not a plugin id"));
}

#[test]
fn manage_result_updates_the_row_and_pending_restart() {
    let mut state = managed_state();
    state.plugins.managing = true;
    state.plugins.pending = Some(PluginAction::Disable);

    let mut plugin = entry("acme.search", "active");
    plugin["enabled"] = json!(false);
    let effects = reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugin": plugin,
            "changed": true,
            "persisted": true,
            "restart_required": true,
            "warnings": [],
        }))),
    );

    assert!(!state.plugins.managing);
    assert_eq!(state.plugins.pending, None);
    assert_eq!(state.plugins.entries[0].enabled, Some(false));
    assert_eq!(state.plugins.entries[0].state, "active");
    assert!(state.plugins.restart_required);
    assert!(commands(&effects).is_empty());
}

#[test]
fn manage_noop_results_toast() {
    let mut state = managed_state();
    state.plugins.managing = true;
    let mut plugin = entry("acme.search", "active");
    plugin["enabled"] = json!(true);
    let effects = reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugin": plugin,
            "changed": false,
            "persisted": true,
            "restart_required": false,
        }))),
    );
    assert!(has_toast(&effects, "no change"));
}

#[test]
fn manage_error_clears_managing_and_keeps_rows() {
    let mut state = managed_state();
    state.plugins.managing = true;
    state.plugins.pending = Some(PluginAction::Disable);
    let effects = reduce(&mut state, Input::CommandResult(err("E_PLUGIN_CONFIG")));

    assert!(!state.plugins.managing);
    assert_eq!(state.plugins.pending, None);
    assert_eq!(state.plugins.entries.len(), 1);
    assert!(
        state
            .plugins
            .last_manage_error
            .as_deref()
            .is_some_and(|error| error.contains("E_PLUGIN_CONFIG"))
    );
    assert!(has_toast(&effects, "E_PLUGIN_CONFIG"));
}

#[test]
fn manage_disabled_error_hides_management_keys() {
    let mut state = managed_state();
    state.plugins.managing = true;
    reduce(
        &mut state,
        Input::CommandResult(err("E_PLUGIN_MANAGE_DISABLED")),
    );
    assert!(!state.plugins.manage);
}

#[test]
fn disconnect_cancels_a_pending_management_action() {
    let mut state = managed_state();
    state.plugins.managing = true;
    state.plugins.pending = Some(PluginAction::Enable);
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Disconnected {
            retryable: true,
            reason: "eof".into(),
        }),
    );
    assert!(!state.plugins.managing);
    assert_eq!(state.plugins.pending, None);
}

#[test]
fn management_commands_never_carry_a_session() {
    let mut state = managed_state();
    state.plugins.entries[0].enabled = Some(false);
    reduce(&mut state, Input::Action(UiAction::PluginEnable));
    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    assert!(
        commands(&effects)
            .iter()
            .all(|command| { command.name == "plugin.enable" && command.session_id.is_none() })
    );
}
