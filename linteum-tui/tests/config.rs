use std::path::Path;

use clap::Parser;
use linteum_tui::cli::Cli;
use linteum_tui::config::{ConfigError, Env, ThemeChoice, resolve};
use linteum_tui::connection::Mode;
use tempfile::tempdir;

fn cli(args: &[&str]) -> Cli {
    Cli::try_parse_from(std::iter::once("linteum-tui").chain(args.iter().copied())).unwrap()
}

fn env(home: &Path) -> Env {
    Env {
        linteum_home: Some(home.to_path_buf()),
        linteum_config: Some(home.join("config.yaml")),
        tui_config: Some(home.join("tui.toml")),
        ..Default::default()
    }
}

#[test]
fn precedence_cli_over_env_over_tui_over_python() {
    let dir = tempdir().unwrap();
    std::fs::write(
        dir.path().join("config.yaml"),
        "gateway:\n  websocket:\n    host: 127.0.0.1\n    port: 8000\n",
    )
    .unwrap();
    std::fs::write(
        dir.path().join("tui.toml"),
        "[gateway]\nurl = \"ws://127.0.0.1:8001/\"\n",
    )
    .unwrap();
    let mut environment = env(dir.path());
    environment.tui_url = Some("ws://127.0.0.1:8002/".into());

    let resolved = resolve(cli(&["--connect", "ws://127.0.0.1:8003/"]), &environment).unwrap();
    assert_eq!(resolved.url, "ws://127.0.0.1:8003/");

    let resolved = resolve(cli(&[]), &environment).unwrap();
    assert_eq!(resolved.url, "ws://127.0.0.1:8002/");

    environment.tui_url = None;
    let resolved = resolve(cli(&[]), &environment).unwrap();
    assert_eq!(resolved.url, "ws://127.0.0.1:8001/");

    std::fs::remove_file(dir.path().join("tui.toml")).unwrap();
    let resolved = resolve(cli(&[]), &environment).unwrap();
    assert_eq!(resolved.url, "ws://127.0.0.1:8000/");
}

#[test]
fn attach_rejects_config_and_no_lease() {
    let dir = tempdir().unwrap();
    let error = resolve(
        cli(&["--connect", "ws://x", "--config", "/tmp/x"]),
        &env(dir.path()),
    )
    .unwrap_err();
    assert!(matches!(error, ConfigError::AttachRejectsGatewayFlags));
}

#[test]
fn python_config_is_never_written() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("config.yaml");
    let yaml = "gateway:\n  websocket:\n    port: 8000\n";
    std::fs::write(&path, yaml).unwrap();
    let before = std::fs::read_to_string(&path).unwrap();
    let _ = resolve(cli(&[]), &env(dir.path())).unwrap();
    assert_eq!(std::fs::read_to_string(&path).unwrap(), before);
}

#[test]
fn corrupt_tui_config_falls_back() {
    let dir = tempdir().unwrap();
    std::fs::write(dir.path().join("tui.toml"), "not = [valid").unwrap();
    let resolved = resolve(cli(&[]), &env(dir.path())).unwrap();
    assert_eq!(resolved.mode, Mode::Spawn);
    assert!(!resolved.warnings.is_empty());
}

#[test]
fn missing_python_config_uses_defaults() {
    let dir = tempdir().unwrap();
    let resolved = resolve(cli(&[]), &env(dir.path())).unwrap();
    assert_eq!(resolved.python.ws_port, 8765);
    assert_eq!(resolved.python.ws_host, "127.0.0.1");
}

#[test]
fn python_env_refs_are_resolved_or_ignored() {
    let dir = tempdir().unwrap();
    std::fs::write(
        dir.path().join("config.yaml"),
        "gateway:\n  websocket:\n    host: ${DEFINITELY_MISSING_LINTEUM_TEST_VAR}\n",
    )
    .unwrap();
    let resolved = resolve(cli(&[]), &env(dir.path())).unwrap();
    assert_eq!(resolved.python.ws_host, "127.0.0.1");
}

#[test]
fn no_color_forces_mono() {
    let dir = tempdir().unwrap();
    let resolved = resolve(cli(&["--no-color"]), &env(dir.path())).unwrap();
    assert_eq!(resolved.theme, ThemeChoice::Mono);
}
