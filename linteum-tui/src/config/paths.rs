//! Path resolution shared by the TUI and the spawned gateway.

use std::path::PathBuf;

use super::Env;

pub fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn xdg_config_home() -> PathBuf {
    std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".config"))
}

pub fn linteum_home(env: &Env) -> PathBuf {
    env.linteum_home
        .clone()
        .or_else(|| std::env::var_os("LINTEUM_HOME").map(PathBuf::from))
        .unwrap_or_else(|| home_dir().join(".linteum"))
}

pub fn python_config_path(env: &Env) -> PathBuf {
    env.linteum_config
        .clone()
        .or_else(|| std::env::var_os("LINTEUM_CONFIG").map(PathBuf::from))
        .unwrap_or_else(|| linteum_home(env).join("config.yaml"))
}

pub fn tui_config_path(explicit: Option<PathBuf>, env: &Env) -> PathBuf {
    explicit
        .or_else(|| env.tui_config.clone())
        .or_else(|| std::env::var_os("LINTEUM_TUI_CONFIG").map(PathBuf::from))
        .unwrap_or_else(|| xdg_config_home().join("linteum-tui").join("config.toml"))
}

pub fn tui_log_path(env: &Env) -> PathBuf {
    linteum_home(env).join("logs").join("tui.log")
}
