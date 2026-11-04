//! Command-line interface.

use std::path::PathBuf;

use clap::{Parser, ValueEnum};

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
pub enum ThemeArg {
    Dark,
    Light,
    Mono,
}

#[derive(Debug, Clone, Parser)]
#[command(
    name = "linteum-tui",
    version,
    about = "Standalone terminal client for linteum_agent"
)]
pub struct Cli {
    /// Start `linteum serve --stdio` as a child (the default).
    #[arg(long, conflicts_with = "connect")]
    pub spawn: bool,

    /// Attach to a running gateway, e.g. ws://127.0.0.1:8765/.
    #[arg(long, value_name = "URL")]
    pub connect: Option<String>,

    /// Python executable used in spawn mode.
    #[arg(long, value_name = "PATH")]
    pub linteum_bin: Option<PathBuf>,

    /// Python config passed to `linteum serve --config` (spawn mode only).
    #[arg(long, value_name = "PATH")]
    pub config: Option<PathBuf>,

    /// Pass `--no-lease` to the spawned gateway (spawn mode only).
    #[arg(long)]
    pub no_lease: bool,

    /// Workspace for a new session.
    #[arg(long, value_name = "DIR")]
    pub workspace: Option<PathBuf>,

    /// Attach to an existing session.
    #[arg(
        long,
        value_name = "ID",
        value_parser = parse_session_id,
        conflicts_with_all = ["continue_", "new"]
    )]
    pub session: Option<String>,

    /// Attach to the most recently updated session.
    #[arg(long = "continue", conflicts_with_all = ["session", "new"])]
    pub continue_: bool,

    /// Open the new-session dialog.
    #[arg(long, conflicts_with_all = ["session", "continue_"])]
    pub new: bool,

    /// Profile passed to `session.create`.
    #[arg(long, value_name = "NAME")]
    pub profile: Option<String>,

    /// Explicit TUI preferences file.
    #[arg(long, value_name = "PATH")]
    pub tui_config: Option<PathBuf>,

    /// Start theme.
    #[arg(long, value_enum)]
    pub theme: Option<ThemeArg>,

    /// Start with reasoning sections expanded.
    #[arg(long)]
    pub show_reasoning: bool,

    /// Force the monochrome theme.
    #[arg(long)]
    pub no_color: bool,

    /// Verbose metadata logging.
    #[arg(long)]
    pub debug: bool,

    /// Include payload content in logs.
    #[arg(long)]
    pub debug_content: bool,
}

fn parse_session_id(value: &str) -> Result<String, String> {
    let valid = !value.is_empty()
        && value.len() <= 128
        && value
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | ':' | '-'));
    if valid {
        Ok(value.to_owned())
    } else {
        Err("session id must match [A-Za-z0-9._:-]{1,128}".to_owned())
    }
}
