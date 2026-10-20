use clap::Parser;
use clap::error::ErrorKind;
use linteum_tui::cli::Cli;

#[test]
fn cli_rejects_spawn_and_connect_together() {
    let error = Cli::try_parse_from(["linteum-tui", "--spawn", "--connect", "ws://x"]).unwrap_err();
    assert_eq!(error.kind(), ErrorKind::ArgumentConflict);
}

#[test]
fn cli_rejects_multiple_session_selectors() {
    let error = Cli::try_parse_from(["linteum-tui", "--session", "s1", "--new"]).unwrap_err();
    assert_eq!(error.kind(), ErrorKind::ArgumentConflict);
}

#[test]
fn cli_rejects_invalid_session_id() {
    let error = Cli::try_parse_from(["linteum-tui", "--session", "bad/id"]).unwrap_err();
    assert_eq!(error.kind(), ErrorKind::ValueValidation);
}

#[test]
fn cli_accepts_defaults() {
    let cli = Cli::try_parse_from(["linteum-tui"]).unwrap();
    assert!(!cli.spawn);
    assert!(cli.connect.is_none());
    assert!(cli.session.is_none());
}
