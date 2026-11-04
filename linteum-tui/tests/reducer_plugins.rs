#![allow(clippy::field_reassign_with_default)]

use linteum_tui::protocol::{CommandResult, ErrorInfo};
use linteum_tui::viewmodel::{
    CommandSpec, ConnectionEvent, ConnectionState, Effect, GatewayMode, Input, Limits, Overlay,
    PluginAction, SessionView, TuiState, UiAction, reduce,
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
        name: None,
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
        name: None,
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

/// Attach mode with runtime apply advertised, manage enabled, saved changes
/// pending a restart, and a parsed file-level allow list.
fn attach_apply_state() -> TuiState {
    let mut state = managed_state();
    state.gateway_mode = GatewayMode::Attach;
    state.features = vec!["view".into(), "plugins".into(), "plugins.runtime".into()];
    state.plugins.restart_required = true;
    state.plugins.allow = Some(vec!["acme.search".into()]);
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
            instance_id: None,
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
            instance_id: None,
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
            instance_id: None,
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
    assert_eq!(state.plugins.allow, None);
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

#[test]
fn restart_requires_spawn_and_pending_changes() {
    // Spawn + restart_required: Ctrl+T opens a Restart confirmation.
    let mut spawn = state();
    spawn.plugins.restart_required = true;
    let effects = reduce(&mut spawn, Input::Action(UiAction::PluginRestart));
    assert!(effects.is_empty());
    assert_eq!(spawn.plugins.pending, Some(PluginAction::Restart));

    // Attach mode: the gateway is managed externally, so only toast.
    let mut attach = state();
    attach.gateway_mode = GatewayMode::Attach;
    attach.plugins.restart_required = true;
    let effects = reduce(&mut attach, Input::Action(UiAction::PluginRestart));
    assert_eq!(attach.plugins.pending, None);
    assert!(has_toast(&effects, "attach mode"));
    assert!(commands(&effects).is_empty());

    // No saved plugin changes: nothing to apply, so only toast.
    let mut clean = state();
    let effects = reduce(&mut clean, Input::Action(UiAction::PluginRestart));
    assert_eq!(clean.plugins.pending, None);
    assert!(has_toast(&effects, "no saved plugin changes"));
}

#[test]
fn restart_confirm_emits_effect_and_is_idempotent() {
    let mut state = state();
    state.plugins.restart_required = true;
    assert!(reduce(&mut state, Input::Action(UiAction::PluginRestart)).is_empty());
    assert_eq!(state.plugins.pending, Some(PluginAction::Restart));

    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    assert_eq!(effects, vec![Effect::RestartChild]);
    assert!(state.plugins.restarting);
    assert_eq!(state.plugins.pending, None);

    // A second confirm is a no-op: the pending action is gone.
    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    assert!(effects.is_empty());
    assert!(state.plugins.restarting);
}

#[test]
fn reconnected_new_instance_clears_restart_state() {
    let mut state = state();
    state.gateway_instance = Some("a".into());
    state.plugins.restarting = true;
    state.plugins.restart_required = true;
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Reconnected {
            features: vec![],
            instance_id: Some("b".into()),
        }),
    );
    assert!(!state.plugins.restarting);
    assert!(!state.plugins.restart_required);
    assert!(state.plugins.stale);
}

#[test]
fn spawn_reconnect_without_instance_id_clears_restarting() {
    let mut state = state();
    state.plugins.restarting = true;
    state.plugins.restart_required = true;
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Reconnected {
            features: vec![],
            instance_id: None,
        }),
    );
    assert!(!state.plugins.restarting);
    // The old gateway reported no instance id, so the saved-change truth is
    // unknown: restart_required stays as-is.
    assert!(state.plugins.restart_required);
    // A subsequent restart can be requested again.
    let effects = reduce(&mut state, Input::Action(UiAction::PluginRestart));
    assert_eq!(state.plugins.pending, Some(PluginAction::Restart));
    assert!(effects.is_empty());
}

#[test]
fn spawn_connected_without_instance_id_clears_restarting() {
    let mut state = state();
    state.plugins.restarting = true;
    state.plugins.restart_required = true;
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Connected {
            features: vec![],
            limits: Limits {
                max_command_bytes: 0,
                max_frame_bytes: 0,
                client_queue_frames: 0,
                history_page_size: 0,
            },
            instance_id: None,
        }),
    );
    assert!(!state.plugins.restarting);
    assert!(state.plugins.restart_required);
}

#[test]
fn attach_reconnect_keeps_restarting() {
    let mut state = state();
    state.gateway_mode = GatewayMode::Attach;
    state.plugins.restarting = true;
    state.plugins.restart_required = true;
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Reconnected {
            features: vec![],
            instance_id: None,
        }),
    );
    assert!(state.plugins.restarting);
    assert!(state.plugins.restart_required);
}

#[test]
fn reconnected_same_instance_clears_restarting_but_keeps_restart_required() {
    let mut state = state();
    state.gateway_instance = Some("a".into());
    state.plugins.restarting = true;
    state.plugins.restart_required = true;
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Reconnected {
            features: vec![],
            instance_id: Some("a".into()),
        }),
    );
    // A successful spawn-mode reconnect is the restart-success signal; the
    // saved-change flag stays because the instance did not change.
    assert!(!state.plugins.restarting);
    assert!(state.plugins.restart_required);
}

#[test]
fn restart_preserves_composer_and_draft() {
    let mut state = state();
    state.plugins.restart_required = true;
    state.composer.text = "hello gateway".into();
    state.composer.cursor = state.composer.text.len();
    state.drafts.insert("s1".into(), "older draft".into());

    assert!(reduce(&mut state, Input::Action(UiAction::PluginRestart)).is_empty());
    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    assert_eq!(effects, vec![Effect::RestartChild]);

    assert_eq!(state.composer.text, "hello gateway");
    assert_eq!(state.composer.cursor, "hello gateway".len());
    assert_eq!(
        state.drafts.get("s1").map(String::as_str),
        Some("older draft")
    );
}

#[test]
fn list_parses_allow() {
    let mut state = state();
    state.plugins.loading = true;
    reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugins": [entry("acme.search", "active")],
            "invalid": [],
            "warnings": [],
            "allow": ["stale.extra", "acme.search"],
        }))),
    );
    assert_eq!(
        state.plugins.allow.as_deref(),
        Some(&["stale.extra".to_owned(), "acme.search".to_owned()][..])
    );
}

#[test]
fn apply_opens_confirmation_and_confirm_sends_set_allowed() {
    let mut state = attach_apply_state();
    let effects = reduce(&mut state, Input::Action(UiAction::PluginApply));

    assert!(effects.is_empty());
    assert_eq!(state.plugins.pending, Some(PluginAction::Apply));

    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    let sent = commands(&effects);
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0].name, "plugin.set_allowed");
    assert_eq!(sent[0].args.get("ids"), Some(&json!(["acme.search"])));
    assert_eq!(sent[0].args.get("mode"), Some(&json!("apply")));
    assert!(sent[0].session_id.is_none());
    assert!(state.plugins.applying);
    assert_eq!(state.plugins.pending, None);

    // A repeated confirm is empty: the pending action is gone.
    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    assert!(effects.is_empty());
    assert!(state.plugins.applying);
}

#[test]
fn apply_set_allowed_result_clears_flags_and_refreshes() {
    let mut state = attach_apply_state();
    state.plugins.applying = true;
    state.plugins.pending = Some(PluginAction::Apply);
    let effects = reduce(
        &mut state,
        Input::CommandResult(CommandResult {
            name: Some("plugin.set_allowed".into()),
            ..ok(json!({
                "allow": ["acme.search"],
                "mode": "apply",
                "changed": true,
                "restart_required": false,
                "plugins": [],
                "changes": [],
            }))
        }),
    );

    assert!(!state.plugins.applying);
    assert_eq!(state.plugins.pending, None);
    assert!(!state.plugins.restart_required);
    assert!(state.plugins.last_manage_error.is_none());
    // The snapshot is refreshed so row states follow the real runtime list.
    let sent = commands(&effects);
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0].name, "plugin.list");
    assert!(state.plugins.loading);
}

#[test]
fn manage_result_keeps_allow_in_sync_for_apply() {
    // Rows: "a.plugin" carries a saved change (keeps restart_required true);
    // "b.plugin" is disabled in file and runtime.
    let mut state = state();
    state.gateway_mode = GatewayMode::Attach;
    state.features = vec!["view".into(), "plugins".into(), "plugins.runtime".into()];
    state.plugins.manage = true;
    state.plugins.loaded = true;
    state.plugins.restart_required = true;
    state.plugins.allow = Some(vec!["a.plugin".into()]);
    state.plugins.entries = vec![
        linteum_tui::viewmodel::PluginEntry {
            id: "a.plugin".into(),
            state: "active".into(),
            enabled: Some(false),
            ..Default::default()
        },
        linteum_tui::viewmodel::PluginEntry {
            id: "b.plugin".into(),
            state: "disabled".into(),
            enabled: Some(false),
            ..Default::default()
        },
    ];

    // The gateway persisted an enable of b.plugin: the saved allow is now
    // ["a.plugin", "b.plugin"]. The parsed snapshot must mirror that edit, or
    // a later Ctrl+A apply would submit the stale list and revert it.
    let mut enabled = entry("b.plugin", "disabled");
    enabled["enabled"] = json!(true);
    reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugin": enabled,
            "changed": true,
            "persisted": true,
            "restart_required": true,
            "warnings": [],
        }))),
    );

    let effects = reduce(&mut state, Input::Action(UiAction::PluginApply));
    assert!(effects.is_empty());
    assert_eq!(state.plugins.pending, Some(PluginAction::Apply));
    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    let sent = commands(&effects);
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0].name, "plugin.set_allowed");
    assert_eq!(
        sent[0].args.get("ids"),
        Some(&json!(["a.plugin", "b.plugin"]))
    );
    assert_eq!(sent[0].args.get("mode"), Some(&json!("apply")));

    // A later disable of b.plugin removes it again from the submitted list.
    state.plugins.applying = false;
    state.plugins.pending = None;
    let mut disabled = entry("b.plugin", "disabled");
    disabled["enabled"] = json!(false);
    reduce(
        &mut state,
        Input::CommandResult(ok(json!({
            "plugin": disabled,
            "changed": true,
            "persisted": true,
            "restart_required": true,
            "warnings": [],
        }))),
    );

    let effects = reduce(&mut state, Input::Action(UiAction::PluginApply));
    assert!(effects.is_empty());
    assert_eq!(state.plugins.pending, Some(PluginAction::Apply));
    let effects = reduce(&mut state, Input::Action(UiAction::PluginConfirm));
    let sent = commands(&effects);
    assert_eq!(sent.len(), 1);
    assert_eq!(sent[0].args.get("ids"), Some(&json!(["a.plugin"])));
}

#[test]
fn apply_gateway_error_clears_applying_and_sets_error() {
    let mut state = attach_apply_state();
    state.plugins.applying = true;
    state.plugins.pending = Some(PluginAction::Apply);
    let effects = reduce(
        &mut state,
        Input::CommandResult(CommandResult {
            name: Some("plugin.set_allowed".into()),
            ..err("E_PLUGIN_MANAGE_DISABLED")
        }),
    );

    assert!(!state.plugins.applying);
    assert_eq!(state.plugins.pending, None);
    assert_eq!(state.plugins.entries.len(), 1);
    assert!(
        state
            .plugins
            .last_manage_error
            .as_deref()
            .is_some_and(|error| error.contains("E_PLUGIN_MANAGE_DISABLED"))
    );
    assert!(has_toast(&effects, "E_PLUGIN_MANAGE_DISABLED"));
}

#[test]
fn apply_is_gated_by_mode_feature_manage_and_allow() {
    // Spawn mode: only a toast, pointing at the Ctrl+T restart instead.
    let mut spawn = attach_apply_state();
    spawn.gateway_mode = GatewayMode::Spawn;
    let effects = reduce(&mut spawn, Input::Action(UiAction::PluginApply));
    assert_eq!(spawn.plugins.pending, None);
    assert!(has_toast(&effects, "Ctrl+T"));
    assert!(commands(&effects).is_empty());

    // Missing plugins.runtime: toast, no pending.
    let mut no_runtime = attach_apply_state();
    no_runtime.features = vec!["view".into(), "plugins".into()];
    let effects = reduce(&mut no_runtime, Input::Action(UiAction::PluginApply));
    assert_eq!(no_runtime.plugins.pending, None);
    assert!(has_toast(&effects, "not supported"));
    assert!(commands(&effects).is_empty());

    // Management disabled: toast, no pending.
    let mut not_managed = attach_apply_state();
    not_managed.plugins.manage = false;
    let effects = reduce(&mut not_managed, Input::Action(UiAction::PluginApply));
    assert_eq!(not_managed.plugins.pending, None);
    assert!(has_toast(&effects, "management is disabled"));

    // No saved changes: toast, no pending.
    let mut clean = attach_apply_state();
    clean.plugins.restart_required = false;
    let effects = reduce(&mut clean, Input::Action(UiAction::PluginApply));
    assert_eq!(clean.plugins.pending, None);
    assert!(has_toast(&effects, "no saved plugin changes"));

    // The allow list is unavailable: toast, no pending, no command.
    let mut no_allow = attach_apply_state();
    no_allow.plugins.allow = None;
    let effects = reduce(&mut no_allow, Input::Action(UiAction::PluginApply));
    assert_eq!(no_allow.plugins.pending, None);
    assert!(has_toast(&effects, "unavailable"));
    assert!(commands(&effects).is_empty());
}

#[test]
fn disconnect_clears_an_inflight_apply() {
    let mut state = attach_apply_state();
    state.plugins.applying = true;
    state.plugins.pending = Some(PluginAction::Apply);
    reduce(
        &mut state,
        Input::Connection(ConnectionEvent::Disconnected {
            retryable: true,
            reason: "eof".into(),
        }),
    );
    assert!(!state.plugins.applying);
    assert_eq!(state.plugins.pending, None);
}
