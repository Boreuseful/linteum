//! Read-only extraction of a few defaults from the Python config.

use std::path::Path;

use serde_json::Value;

use super::tui_file::parse_theme;
use super::{ConfigError, PythonDefaults};

pub fn read_python_defaults(path: &Path) -> Result<PythonDefaults, ConfigError> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(PythonDefaults::default());
        }
        Err(error) => return Err(ConfigError::Io(error.to_string())),
    };
    let value: Value =
        serde_yaml_ng::from_str(&text).map_err(|error| ConfigError::Yaml(error.to_string()))?;

    let mut defaults = PythonDefaults::default();
    if let Some(host) = resolve_str(get(&value, &["gateway", "websocket", "host"])) {
        defaults.ws_host = host;
    }
    if let Some(port) = get(&value, &["gateway", "websocket", "port"]).and_then(Value::as_u64)
        && port <= u16::MAX as u64
    {
        defaults.ws_port = port as u16;
    }
    defaults.theme = get(&value, &["ui", "theme"])
        .and_then(Value::as_str)
        .and_then(parse_theme);
    defaults.show_reasoning = get(&value, &["ui", "show_reasoning"]).and_then(Value::as_bool);
    defaults.language = resolve_str(get(&value, &["ui", "language"]));
    defaults.default_profile = resolve_str(get(&value, &["agent", "default_profile"]));
    Ok(defaults)
}

fn get<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut current = value;
    for key in path {
        current = current.get(*key)?;
    }
    Some(current)
}

fn resolve_str(value: Option<&Value>) -> Option<String> {
    let text = value?.as_str()?;
    if let Some(name) = text
        .strip_prefix("${")
        .and_then(|rest| rest.strip_suffix('}'))
    {
        std::env::var(name).ok()
    } else {
        Some(text.to_owned())
    }
}
