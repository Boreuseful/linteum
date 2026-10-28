//! Application bootstrap and event loop.

use std::io::IsTerminal;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use crossterm::event::{Event, EventStream, KeyCode, KeyEvent, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use futures_util::StreamExt;
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use serde_json::{Map, Value};
use tokio::sync::mpsc;

use crate::cli::Cli;
use crate::config::{ConfigError, Env, ResolvedConfig, resolve};
use crate::connection::{
    BackoffConfig, Connector, Mode, SpawnConfig, SpawnError, SupervisorConfig, SupervisorControl,
    Transport, TransportError, connect_websocket, run_supervisor, spawn_stdio,
};
use crate::logging::{LogError, init_logging};
use crate::ui::{App, TerminalGuard, TerminalOps, Theme, UiError, key_to_action, palette};
use crate::viewmodel::{
    ApprovalDecision, CommandSpec, Effect, Focus, Input, Overlay, QuitMode, TuiState, UiAction,
    reduce,
};

#[derive(Debug, thiserror::Error)]
pub enum AppError {
    #[error("config: {0}")]
    Config(#[from] ConfigError),
    #[error("log: {0}")]
    Log(#[from] LogError),
    #[error("ui: {0}")]
    Ui(#[from] UiError),
    #[error("linteum-tui requires an interactive terminal")]
    NotATerminal,
}

struct SpawnConnector {
    config: SpawnConfig,
}

#[async_trait]
impl Connector for SpawnConnector {
    async fn connect(&self) -> Result<Box<dyn Transport>, TransportError> {
        let transport = spawn_stdio(self.config.clone())
            .await
            .map_err(|error| match error {
                SpawnError::NotFound => TransportError::Io("linteum binary not found".to_owned()),
                SpawnError::Io(message) => TransportError::Io(message),
                SpawnError::Exited { stderr_tail, .. } => {
                    TransportError::Io(format!("child exited: {}", stderr_tail.join(" | ")))
                }
            })?;
        Ok(Box::new(transport))
    }

    fn mode(&self) -> Mode {
        Mode::Spawn
    }
}

struct WsConnector {
    url: String,
}

#[async_trait]
impl Connector for WsConnector {
    async fn connect(&self) -> Result<Box<dyn Transport>, TransportError> {
        Ok(Box::new(connect_websocket(&self.url).await?))
    }

    fn mode(&self) -> Mode {
        Mode::Attach
    }
}

struct SingleConnector {
    transport: tokio::sync::Mutex<Option<Box<dyn Transport>>>,
    mode: Mode,
}

#[async_trait]
impl Connector for SingleConnector {
    async fn connect(&self) -> Result<Box<dyn Transport>, TransportError> {
        self.transport
            .lock()
            .await
            .take()
            .ok_or(TransportError::Closed)
    }

    fn mode(&self) -> Mode {
        self.mode
    }
}

struct CrosstermOps {
    terminal: Terminal<CrosstermBackend<std::io::Stdout>>,
}

impl CrosstermOps {
    fn new() -> Result<Self, UiError> {
        let backend = CrosstermBackend::new(std::io::stdout());
        let terminal =
            Terminal::new(backend).map_err(|error| UiError::Terminal(error.to_string()))?;
        Ok(Self { terminal })
    }
}

impl TerminalOps for CrosstermOps {
    fn enter(&mut self) -> Result<(), UiError> {
        enable_raw_mode().map_err(|error| UiError::Terminal(error.to_string()))?;
        execute!(self.terminal.backend_mut(), EnterAlternateScreen)
            .map_err(|error| UiError::Terminal(error.to_string()))?;
        Ok(())
    }

    fn leave(&mut self) -> Result<(), UiError> {
        let _ = disable_raw_mode();
        let _ = execute!(self.terminal.backend_mut(), LeaveAlternateScreen);
        let _ = self.terminal.show_cursor();
        Ok(())
    }

    fn draw(&mut self, state: &TuiState) -> Result<(), UiError> {
        let theme = Theme::from_choice(state.prefs.theme);
        self.terminal
            .draw(|frame| crate::ui::render(frame, state, &theme))
            .map(|_| ())
            .map_err(|error| UiError::Terminal(error.to_string()))
    }
}

pub async fn run_app(cli: Cli) -> Result<(), AppError> {
    terminal_check(
        std::io::stdout().is_terminal(),
        std::env::var("TERM").ok().as_deref(),
    )?;
    let env = Env::from_process();
    let config = resolve(cli, &env)?;
    let _log = init_logging(&config.log_path, &config.log_level, config.debug_content)?;
    tracing::info!(mode = ?config.mode, url = %config.url, "starting linteum-tui");

    let connector: Arc<dyn Connector> = match config.mode {
        Mode::Spawn => Arc::new(SpawnConnector {
            config: SpawnConfig {
                linteum_bin: config.linteum_bin.clone(),
                config_path: config.config_path.clone(),
                no_lease: config.no_lease,
                cwd: None,
                stderr_log: config.log_path.with_file_name("tui-gateway.log"),
                teardown_grace: Duration::from_secs(5),
                term_grace: Duration::from_secs(2),
            },
        }),
        Mode::Attach => Arc::new(WsConnector {
            url: config.url.clone(),
        }),
    };

    run_loop(config, connector, CrosstermOps::new()?).await
}

/// Test seam: run the real loop over a supplied transport and terminal ops.
pub async fn run_with_transport<T: TerminalOps>(
    transport: Box<dyn Transport>,
    config: ResolvedConfig,
    mode: Mode,
    ops: T,
) -> Result<(), AppError> {
    let connector = Arc::new(SingleConnector {
        transport: tokio::sync::Mutex::new(Some(transport)),
        mode,
    });
    run_loop(config, connector, ops).await
}

pub async fn run_loop<T: TerminalOps>(
    config: ResolvedConfig,
    connector: Arc<dyn Connector>,
    ops: T,
) -> Result<(), AppError> {
    let (input_tx, mut input_rx) = mpsc::channel(4096);
    let (command_tx, command_rx) = mpsc::channel(64);
    let (control_tx, control_rx) = mpsc::channel(8);
    let supervisor_config = SupervisorConfig {
        connector,
        backoff: BackoffConfig::default(),
    };
    let mut supervisor = tokio::spawn(run_supervisor(
        supervisor_config,
        input_tx,
        command_rx,
        control_rx,
    ));

    let mut guard = TerminalGuard::new(ops)?;
    let mut app = App::new(TuiState::default(), Theme::from_choice(config.theme));
    app.state.prefs.theme = config.theme;
    app.state.prefs.show_reasoning = config.show_reasoning;
    app.state.quit_mode = match config.mode {
        Mode::Spawn => QuitMode::Spawn,
        Mode::Attach => QuitMode::Attach,
    };

    let workspace = match config.mode {
        Mode::Spawn => config
            .workspace
            .clone()
            .or_else(|| std::env::current_dir().ok()),
        Mode::Attach => config.workspace.clone(),
    };
    let workspace_text = workspace.map(|path| path.to_string_lossy().into_owned());
    app.state.new_session.workspace = workspace_text.clone().unwrap_or_default();
    app.state.new_session.workspace_cursor = app.state.new_session.workspace.len();
    app.state.picker_workspace = workspace_text.clone();
    app.state.workspace_root = workspace_text;
    app.state.new_session.profile = config
        .profile
        .clone()
        .or_else(|| config.python.default_profile.clone())
        .unwrap_or_else(|| "default".to_owned());
    app.state.new_session.mode = config.default_mode.clone().unwrap_or_default();
    for warning in &config.warnings {
        app.state.activity.push("config", warning.clone());
    }
    app.state.overlay = if config.new {
        Some(Overlay::NewSession)
    } else {
        Some(Overlay::Picker)
    };

    if let Some(session_id) = config.session.clone() {
        app.state.overlay = None;
        app.state.session = Some(crate::viewmodel::SessionView {
            id: session_id.clone(),
            ..Default::default()
        });
        let _ = command_tx.send(subscribe_spec(&session_id)).await;
    }

    let mut events = EventStream::new();
    let mut tick = tokio::time::interval(Duration::from_millis(50));
    let mut quit = false;

    while !quit {
        tokio::select! {
            maybe_event = events.next() => {
                if let Some(Ok(event)) = maybe_event {
                    match event {
                        Event::Key(key) => {
                            let key = normalize_key(key);
                            if let Some(effects) = overlay_effects(&mut app.state, key) {
                                quit = execute_effects(
                                    effects, &command_tx, &control_tx, &mut app,
                                ).await?;
                            } else if let Some(effects) = app.handle_slash_key(key) {
                                quit = execute_effects(
                                    effects, &command_tx, &control_tx, &mut app,
                                ).await?;
                            } else if let Some(effects) = app.handle_sidebar_key(key) {
                                quit = execute_effects(
                                    effects, &command_tx, &control_tx, &mut app,
                                ).await?;
                            } else if let Some(action) = key_to_action(key) {
                                let effects = app.handle_action(action);
                                quit = execute_effects(
                                    effects, &command_tx, &control_tx, &mut app,
                                ).await?;
                            } else if app.state.overlay.is_none()
                                && app.state.focus == Focus::Composer
                                && let KeyCode::Char(ch) = key.code
                                && !key.modifiers.contains(KeyModifiers::CONTROL)
                                && !key.modifiers.contains(KeyModifiers::ALT)
                            {
                                let _ = reduce(
                                    &mut app.state,
                                    Input::Text(ch.to_string()),
                                );
                            }
                        }
                        Event::Paste(text) => match app.state.overlay {
                            Some(Overlay::NewSession) => app.state.new_session.insert_str(&text),
                            Some(Overlay::Palette) => app.state.palette.insert_str(&text),
                            Some(Overlay::Mode) => app.state.mode.picker.insert_str(&text),
                            Some(_) => {}
                            None if app.state.focus == Focus::Composer => {
                                let _ = reduce(&mut app.state, Input::Paste(text));
                            }
                            None => {}
                        },
                        Event::Resize(_, _) => {}
                        _ => {}
                    }
                }
            }
            Some(input) = input_rx.recv() => {
                let mut effects = reduce(&mut app.state, input);
                while let Ok(input) = input_rx.try_recv() {
                    effects.extend(reduce(&mut app.state, input));
                }
                quit = execute_effects(effects, &command_tx, &control_tx, &mut app).await?;
            }
            _ = tick.tick() => {}
        }
        guard.ops_mut().draw(&app.state)?;
    }

    let _ = control_tx.send(SupervisorControl::Shutdown).await;
    let _ = tokio::time::timeout(Duration::from_secs(10), &mut supervisor).await;
    Ok(())
}

fn subscribe_spec(session_id: &str) -> CommandSpec {
    let mut args = Map::new();
    args.insert("session_id".into(), Value::String(session_id.to_owned()));
    args.insert("mode".into(), Value::String("view".into()));
    args.insert("after_seq".into(), Value::from(0_u64));
    CommandSpec {
        name: "session.subscribe".to_owned(),
        args,
        session_id: Some(session_id.to_owned()),
    }
}

fn overlay_effects(state: &mut TuiState, key: crossterm::event::KeyEvent) -> Option<Vec<Effect>> {
    match state.overlay? {
        Overlay::Picker => Some(match key.code {
            KeyCode::Enter => reduce(state, Input::Action(UiAction::OpenSelectedSession)),
            KeyCode::Down => reduce(state, Input::Action(UiAction::SelectNext)),
            KeyCode::Up => reduce(state, Input::Action(UiAction::SelectPrevious)),
            KeyCode::Char('n') => reduce(state, Input::Action(UiAction::NewSession)),
            KeyCode::Char('r') => reduce(state, Input::Action(UiAction::RefreshSessions)),
            KeyCode::Char('a') => reduce(state, Input::Action(UiAction::ToggleAllWorkspaces)),
            KeyCode::Char('m') => reduce(state, Input::Action(UiAction::LoadMoreSessions)),
            KeyCode::Esc => reduce(state, Input::Action(UiAction::CloseOverlay)),
            _ => Vec::new(),
        }),
        Overlay::Approvals => Some(match key.code {
            KeyCode::Char('y') => state
                .approvals
                .open
                .clone()
                .map(|id| state.decide(&id, ApprovalDecision::Grant))
                .unwrap_or_default(),
            KeyCode::Char('n') => state
                .approvals
                .open
                .clone()
                .map(|id| state.decide(&id, ApprovalDecision::Deny { reason: None }))
                .unwrap_or_default(),
            KeyCode::Esc => reduce(state, Input::Action(UiAction::CloseOverlay)),
            _ => Vec::new(),
        }),
        Overlay::NewSession => Some(match key.code {
            KeyCode::Enter => reduce(state, Input::Action(UiAction::SubmitNewSession)),
            KeyCode::Esc => reduce(state, Input::Action(UiAction::CloseOverlay)),
            KeyCode::Tab | KeyCode::BackTab | KeyCode::Up | KeyCode::Down => {
                state.new_session.focus = (state.new_session.focus + 1) % 2;
                Vec::new()
            }
            KeyCode::Left => {
                state.new_session.move_left();
                Vec::new()
            }
            KeyCode::Right => {
                state.new_session.move_right();
                Vec::new()
            }
            KeyCode::Home => {
                state.new_session.move_home();
                Vec::new()
            }
            KeyCode::End => {
                state.new_session.move_end();
                Vec::new()
            }
            KeyCode::Backspace => {
                state.new_session.backspace();
                Vec::new()
            }
            KeyCode::Delete => {
                state.new_session.delete();
                Vec::new()
            }
            KeyCode::Char(ch)
                if !key.modifiers.contains(KeyModifiers::CONTROL)
                    && !key.modifiers.contains(KeyModifiers::ALT) =>
            {
                state.new_session.insert_char(ch);
                Vec::new()
            }
            _ => Vec::new(),
        }),
        Overlay::Palette => Some(match key.code {
            KeyCode::Up => {
                state.palette.selected = state.palette.selected.saturating_sub(1);
                Vec::new()
            }
            KeyCode::Down => {
                let len = palette::filtered_actions(&state.palette.filter).len();
                if len > 0 {
                    state.palette.selected = (state.palette.selected + 1).min(len - 1);
                }
                Vec::new()
            }
            KeyCode::Enter => {
                let actions = palette::filtered_actions(&state.palette.filter);
                if let Some(action) = actions.get(state.palette.selected).copied() {
                    state.overlay = None;
                    palette::execute(action, state)
                } else {
                    Vec::new()
                }
            }
            KeyCode::Left => {
                state.palette.move_left();
                Vec::new()
            }
            KeyCode::Right => {
                state.palette.move_right();
                Vec::new()
            }
            KeyCode::Home => {
                state.palette.move_home();
                Vec::new()
            }
            KeyCode::End => {
                state.palette.move_end();
                Vec::new()
            }
            KeyCode::Backspace => {
                state.palette.backspace();
                Vec::new()
            }
            KeyCode::Delete => {
                state.palette.delete();
                Vec::new()
            }
            KeyCode::Char(ch)
                if !key.modifiers.contains(KeyModifiers::CONTROL)
                    && !key.modifiers.contains(KeyModifiers::ALT) =>
            {
                state.palette.insert_char(ch);
                Vec::new()
            }
            KeyCode::Esc => reduce(state, Input::Action(UiAction::CloseOverlay)),
            _ => Vec::new(),
        }),
        Overlay::Subagents => Some(match key.code {
            KeyCode::Up => reduce(state, Input::Action(UiAction::SelectPreviousSubagent)),
            KeyCode::Down => reduce(state, Input::Action(UiAction::SelectNextSubagent)),
            KeyCode::Enter => reduce(state, Input::Action(UiAction::OpenSelectedSubagent)),
            KeyCode::Esc => reduce(state, Input::Action(UiAction::CloseOverlay)),
            _ => Vec::new(),
        }),
        Overlay::SubagentContext => Some(match key.code {
            KeyCode::Esc => {
                state.overlay = Some(Overlay::Subagents);
                Vec::new()
            }
            KeyCode::Up => {
                state.subagents.scroll = state.subagents.scroll.saturating_sub(1);
                Vec::new()
            }
            KeyCode::Down => {
                let limit = subagent_scroll_limit(state);
                state.subagents.scroll = state.subagents.scroll.saturating_add(1).min(limit);
                Vec::new()
            }
            KeyCode::PageUp => {
                state.subagents.scroll = state.subagents.scroll.saturating_sub(10);
                Vec::new()
            }
            KeyCode::PageDown => {
                let limit = subagent_scroll_limit(state);
                state.subagents.scroll = state.subagents.scroll.saturating_add(10).min(limit);
                Vec::new()
            }
            KeyCode::Char('r') => reduce(state, Input::Action(UiAction::ReloadSubagentContext)),
            _ => Vec::new(),
        }),
        Overlay::Mode => Some(match key.code {
            KeyCode::Enter => reduce(state, Input::Action(UiAction::ModeSelect)),
            KeyCode::Down => reduce(state, Input::Action(UiAction::ModeSelectNext)),
            KeyCode::Up => reduce(state, Input::Action(UiAction::ModeSelectPrevious)),
            KeyCode::Char('r') => reduce(state, Input::Action(UiAction::ModeRefresh)),
            KeyCode::Esc => reduce(state, Input::Action(UiAction::CloseOverlay)),
            KeyCode::Left => {
                state.mode.picker.move_left();
                Vec::new()
            }
            KeyCode::Right => {
                state.mode.picker.move_right();
                Vec::new()
            }
            KeyCode::Home => {
                state.mode.picker.move_home();
                Vec::new()
            }
            KeyCode::End => {
                state.mode.picker.move_end();
                Vec::new()
            }
            KeyCode::Backspace => {
                state.mode.picker.backspace();
                Vec::new()
            }
            KeyCode::Delete => {
                state.mode.picker.delete();
                Vec::new()
            }
            KeyCode::Char(ch)
                if !key.modifiers.contains(KeyModifiers::CONTROL)
                    && !key.modifiers.contains(KeyModifiers::ALT) =>
            {
                state.mode.picker.insert_char(ch);
                Vec::new()
            }
            _ => Vec::new(),
        }),
        Overlay::Fatal => Some(match key.code {
            KeyCode::Char('q') => vec![Effect::Quit {
                mode: state.quit_mode,
            }],
            _ => Vec::new(),
        }),
        Overlay::Help | Overlay::Activity | Overlay::Artifact => Some(match key.code {
            KeyCode::Esc => reduce(state, Input::Action(UiAction::CloseOverlay)),
            _ => Vec::new(),
        }),
    }
}

/// Stop Down/PageDown at the final screenful of the subagent context.
fn subagent_scroll_limit(state: &TuiState) -> usize {
    let (width, height) = crossterm::terminal::size().unwrap_or((80, 24));
    crate::ui::overlays::subagent_context_scroll_limit(state, width, height)
}

/// Terminals disagree on the byte Backspace sends. Most send DEL (`0x7F`),
/// which crossterm reports as `Backspace`; some clients (MobaXterm defaults to
/// this) send BS (`0x08`), which crossterm reports as Ctrl+H. Treat that as
/// Backspace so editing works regardless of the client's setting.
fn normalize_key(key: KeyEvent) -> KeyEvent {
    if key.code == KeyCode::Char('h') && key.modifiers == KeyModifiers::CONTROL {
        KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE)
    } else {
        key
    }
}

fn terminal_check(is_tty: bool, term: Option<&str>) -> Result<(), AppError> {
    if !is_tty || term == Some("dumb") {
        Err(AppError::NotATerminal)
    } else {
        Ok(())
    }
}

async fn execute_effects(
    effects: Vec<Effect>,
    command_tx: &mpsc::Sender<CommandSpec>,
    control_tx: &mpsc::Sender<SupervisorControl>,
    app: &mut App,
) -> Result<bool, AppError> {
    for effect in effects {
        match effect {
            Effect::SendCommand(spec) => {
                let _ = command_tx.send(spec).await;
            }
            Effect::ReconnectNow => {
                let _ = control_tx.send(SupervisorControl::ReconnectNow).await;
            }
            Effect::RestartChild => {
                let _ = control_tx.send(SupervisorControl::RestartChild).await;
            }
            Effect::Quit { mode } => {
                app.state.quit_mode = mode;
                return Ok(true);
            }
            Effect::SavePrefs(_) => {}
            Effect::CopyToClipboard(text) => copy_osc52(&text),
            Effect::ShowToast(toast) => app.state.activity.push("toast", toast.text),
            Effect::ReadWorkspaceDir { path } => {
                let (entries, error) = crate::workspace::read_dir(&path).await;
                let _ = reduce(
                    &mut app.state,
                    Input::WorkspaceDir {
                        path,
                        entries,
                        error,
                    },
                );
            }
        }
    }
    Ok(false)
}

fn copy_osc52(text: &str) {
    use base64::Engine as _;
    let encoded = base64::engine::general_purpose::STANDARD.encode(text);
    use std::io::Write as _;
    print!("\x1b]52;c;{encoded}\x07");
    let _ = std::io::stdout().flush();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_tty_is_rejected() {
        assert!(matches!(
            terminal_check(false, Some("xterm")),
            Err(AppError::NotATerminal)
        ));
    }

    #[test]
    fn dumb_terminal_is_rejected() {
        assert!(matches!(
            terminal_check(true, Some("dumb")),
            Err(AppError::NotATerminal)
        ));
    }

    #[test]
    fn normal_terminal_is_accepted() {
        assert!(terminal_check(true, Some("xterm-256color")).is_ok());
    }

    #[test]
    fn ctrl_h_is_normalized_to_backspace() {
        let key = normalize_key(KeyEvent::new(KeyCode::Char('h'), KeyModifiers::CONTROL));
        assert_eq!(key.code, KeyCode::Backspace);
        assert_eq!(key.modifiers, KeyModifiers::NONE);
        assert_eq!(key_to_action(key), Some(UiAction::DeleteBackward));
    }

    #[test]
    fn other_control_keys_are_untouched() {
        let key = normalize_key(KeyEvent::new(KeyCode::Char('j'), KeyModifiers::CONTROL));
        assert_eq!(key.code, KeyCode::Char('j'));
        assert_eq!(key.modifiers, KeyModifiers::CONTROL);
    }

    #[test]
    fn editing_new_session_updates_the_focused_field() {
        let mut state = TuiState::default();
        state.new_session.focus = 0;
        state.new_session.insert_char('x');
        assert_eq!(state.new_session.workspace, "x");
        state.new_session.focus = 1;
        state.new_session.insert_char('y');
        assert_eq!(state.new_session.title, "y");
        state.new_session.backspace();
        assert_eq!(state.new_session.title, "");
    }
}
