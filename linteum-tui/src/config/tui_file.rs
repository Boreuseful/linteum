//! TUI-owned TOML preferences.

use std::path::Path;

use serde::Deserialize;

use super::{ConfigError, ThemeChoice};
use crate::connection::Mode;

#[derive(Debug, Clone, Default)]
pub struct TuiPrefs {
    pub mode: Option<Mode>,
    pub url: Option<String>,
    pub linteum_bin: Option<String>,
    pub config: Option<String>,
    pub no_lease: bool,
    pub theme: Option<ThemeChoice>,
    pub show_reasoning: Option<bool>,
    pub log_level: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
struct TuiFile {
    #[serde(default)]
    gateway: GatewayFile,
    #[serde(default)]
    ui: UiFile,
    #[serde(default)]
    logging: LoggingFile,
}

#[derive(Debug, Deserialize, Default)]
struct GatewayFile {
    mode: Option<String>,
    url: Option<String>,
    linteum_bin: Option<String>,
    config: Option<String>,
    no_lease: Option<bool>,
}

#[derive(Debug, Deserialize, Default)]
struct UiFile {
    theme: Option<String>,
    show_reasoning: Option<bool>,
    #[allow(dead_code)]
    language: Option<String>,
}

#[derive(Debug, Deserialize, Default)]
struct LoggingFile {
    level: Option<String>,
}

pub fn load_tui_config(path: &Path) -> Result<TuiPrefs, ConfigError> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(TuiPrefs::default());
        }
        Err(error) => return Err(ConfigError::Io(error.to_string())),
    };
    let file: TuiFile =
        toml::from_str(&text).map_err(|error| ConfigError::Toml(error.to_string()))?;
    Ok(TuiPrefs {
        mode: file.gateway.mode.as_deref().and_then(parse_mode),
        url: file.gateway.url,
        linteum_bin: file.gateway.linteum_bin,
        config: file.gateway.config.filter(|value| !value.is_empty()),
        no_lease: file.gateway.no_lease.unwrap_or(false),
        theme: file.ui.theme.as_deref().and_then(parse_theme),
        show_reasoning: file.ui.show_reasoning,
        log_level: file.logging.level,
    })
}

pub fn parse_mode(value: &str) -> Option<Mode> {
    match value {
        "spawn" => Some(Mode::Spawn),
        "connect" | "attach" => Some(Mode::Attach),
        _ => None,
    }
}

pub fn parse_theme(value: &str) -> Option<ThemeChoice> {
    match value {
        "dark" => Some(ThemeChoice::Dark),
        "light" => Some(ThemeChoice::Light),
        "mono" => Some(ThemeChoice::Mono),
        "auto" => Some(ThemeChoice::Dark),
        _ => None,
    }
}
