//! Configuration resolution: CLI > env > TUI TOML > Python YAML > defaults.

pub mod paths;
pub mod python_yaml;
pub mod tui_file;

use std::path::PathBuf;

pub use crate::viewmodel::ThemeChoice;
pub use python_yaml::read_python_defaults;
pub use tui_file::{TuiPrefs, load_tui_config};

use crate::cli::{Cli, ThemeArg};
use crate::connection::Mode;

#[derive(Debug, Clone, PartialEq)]
pub struct PythonDefaults {
    pub ws_host: String,
    pub ws_port: u16,
    pub theme: Option<ThemeChoice>,
    pub show_reasoning: Option<bool>,
    pub language: Option<String>,
    pub default_profile: Option<String>,
}

impl Default for PythonDefaults {
    fn default() -> Self {
        Self {
            ws_host: "127.0.0.1".to_owned(),
            ws_port: 8765,
            theme: None,
            show_reasoning: None,
            language: None,
            default_profile: None,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct Env {
    pub linteum_home: Option<PathBuf>,
    pub linteum_config: Option<PathBuf>,
    pub tui_config: Option<PathBuf>,
    pub linteum_bin: Option<PathBuf>,
    pub tui_mode: Option<String>,
    pub tui_url: Option<String>,
    pub tui_theme: Option<String>,
    pub tui_show_reasoning: Option<bool>,
    pub no_color: bool,
}

impl Env {
    pub fn from_process() -> Self {
        fn path(name: &str) -> Option<PathBuf> {
            std::env::var_os(name).map(PathBuf::from)
        }
        fn text(name: &str) -> Option<String> {
            std::env::var(name).ok()
        }
        fn flag(name: &str) -> Option<bool> {
            std::env::var(name)
                .ok()
                .map(|value| matches!(value.as_str(), "1" | "true" | "yes" | "on"))
        }
        Self {
            linteum_home: path("LINTEUM_HOME"),
            linteum_config: path("LINTEUM_CONFIG"),
            tui_config: path("LINTEUM_TUI_CONFIG"),
            linteum_bin: path("LINTEUM_BIN"),
            tui_mode: text("LINTEUM_TUI_MODE"),
            tui_url: text("LINTEUM_TUI_URL"),
            tui_theme: text("LINTEUM_TUI_THEME"),
            tui_show_reasoning: flag("LINTEUM_TUI_SHOW_REASONING"),
            no_color: std::env::var_os("NO_COLOR").is_some(),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("io: {0}")]
    Io(String),
    #[error("toml: {0}")]
    Toml(String),
    #[error("yaml: {0}")]
    Yaml(String),
    #[error("attach mode rejects --config/--no-lease")]
    AttachRejectsGatewayFlags,
}

#[derive(Debug, Clone)]
pub struct ResolvedConfig {
    pub mode: Mode,
    pub url: String,
    pub linteum_bin: PathBuf,
    pub config_path: Option<PathBuf>,
    pub no_lease: bool,
    pub theme: ThemeChoice,
    pub show_reasoning: bool,
    pub log_level: String,
    pub debug_content: bool,
    pub log_path: PathBuf,
    pub prefs: TuiPrefs,
    pub python: PythonDefaults,
    pub warnings: Vec<String>,
    pub workspace: Option<PathBuf>,
    pub session: Option<String>,
    pub continue_: bool,
    pub new: bool,
    pub profile: Option<String>,
}

impl From<ThemeArg> for ThemeChoice {
    fn from(value: ThemeArg) -> Self {
        match value {
            ThemeArg::Dark => ThemeChoice::Dark,
            ThemeArg::Light => ThemeChoice::Light,
            ThemeArg::Mono => ThemeChoice::Mono,
        }
    }
}

pub fn resolve(cli: Cli, env: &Env) -> Result<ResolvedConfig, ConfigError> {
    let mut warnings = Vec::new();

    let tui_path = paths::tui_config_path(cli.tui_config.clone(), env);
    let prefs = match load_tui_config(&tui_path) {
        Ok(prefs) => prefs,
        Err(error) => {
            warnings.push(format!("tui config: {error}"));
            TuiPrefs::default()
        }
    };

    let python_path = paths::python_config_path(env);
    let python = match read_python_defaults(&python_path) {
        Ok(defaults) => defaults,
        Err(error) => {
            warnings.push(format!("python config: {error}"));
            PythonDefaults::default()
        }
    };

    let mode = if cli.connect.is_some() {
        Mode::Attach
    } else if cli.spawn {
        Mode::Spawn
    } else if let Some(mode) = env.tui_mode.as_deref().and_then(tui_file::parse_mode) {
        mode
    } else {
        prefs.mode.unwrap_or(Mode::Spawn)
    };

    if mode == Mode::Attach && (cli.config.is_some() || cli.no_lease) {
        return Err(ConfigError::AttachRejectsGatewayFlags);
    }

    let url = cli
        .connect
        .clone()
        .or_else(|| env.tui_url.clone())
        .or_else(|| prefs.url.clone())
        .unwrap_or_else(|| format!("ws://{}:{}/", python.ws_host, python.ws_port));

    let linteum_bin = cli
        .linteum_bin
        .clone()
        .or_else(|| env.linteum_bin.clone())
        .or_else(|| prefs.linteum_bin.as_ref().map(PathBuf::from))
        .unwrap_or_else(|| PathBuf::from("linteum"));

    let config_path = cli
        .config
        .clone()
        .or_else(|| env.linteum_config.clone())
        .or_else(|| prefs.config.as_ref().map(PathBuf::from));

    let no_lease = cli.no_lease || prefs.no_lease;

    let theme = if cli.no_color || env.no_color {
        ThemeChoice::Mono
    } else if let Some(theme) = cli.theme {
        theme.into()
    } else if let Some(theme) = env.tui_theme.as_deref().and_then(tui_file::parse_theme) {
        theme
    } else if let Some(theme) = prefs.theme {
        theme
    } else {
        python.theme.unwrap_or(ThemeChoice::Dark)
    };

    let show_reasoning = if cli.show_reasoning {
        true
    } else if let Some(value) = env.tui_show_reasoning {
        value
    } else if let Some(value) = prefs.show_reasoning {
        value
    } else {
        python.show_reasoning.unwrap_or(false)
    };

    let log_level = if cli.debug {
        "debug".to_owned()
    } else {
        prefs.log_level.clone().unwrap_or_else(|| "info".to_owned())
    };

    Ok(ResolvedConfig {
        mode,
        url,
        linteum_bin,
        config_path,
        no_lease,
        theme,
        show_reasoning,
        log_level,
        debug_content: cli.debug_content,
        log_path: paths::tui_log_path(env),
        prefs,
        python,
        warnings,
        workspace: cli.workspace,
        session: cli.session,
        continue_: cli.continue_,
        new: cli.new,
        profile: cli.profile,
    })
}
