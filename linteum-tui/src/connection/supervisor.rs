//! Connection supervisor: connect/spawn, hello, command forwarding, reconnect.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use serde_json::{Map, Value};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use crate::protocol::{CommandResult, ErrorInfo};
use crate::viewmodel::{CommandSpec, ConnectionEvent, Input, Limits};

use super::Transport;
use super::broker::{BrokerHandle, CommandBroker, CommandError};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Spawn,
    Attach,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SupervisorControl {
    ReconnectNow,
    RestartChild,
    Shutdown,
}

#[derive(Debug, Clone)]
pub struct BackoffConfig {
    pub initial: Duration,
    pub max: Duration,
}

impl Default for BackoffConfig {
    fn default() -> Self {
        Self {
            initial: Duration::from_millis(500),
            max: Duration::from_secs(30),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Backoff {
    config: BackoffConfig,
    attempt: u32,
}

impl Backoff {
    pub fn new(config: BackoffConfig) -> Self {
        Self { config, attempt: 0 }
    }

    /// Exponential delay with a deterministic 0–15% jitter. The jitter keeps
    /// reconnect storms from aligning without making tests flaky.
    pub fn next_delay(&mut self) -> Duration {
        let exponent = self.attempt.min(10);
        let base = self.config.initial.saturating_mul(1_u32 << exponent);
        let capped = base.min(self.config.max);
        self.attempt = self.attempt.saturating_add(1);
        let jitter = capped / 10;
        capped + jitter * (self.attempt % 3) / 2
    }

    pub fn reset(&mut self) {
        self.attempt = 0;
    }
}

/// A connect strategy. The real implementations are added when `run_app` is
/// wired; tests use a scripted connector.
#[async_trait]
pub trait Connector: Send + Sync {
    async fn connect(&self) -> Result<Box<dyn Transport>, super::TransportError>;
    fn mode(&self) -> Mode;

    /// Recent stderr lines from the spawned gateway child (spawn mode). The
    /// attach connectors have no child and return nothing (§7.5).
    fn stderr_tail(&self) -> Vec<String> {
        Vec::new()
    }
}

pub struct SupervisorConfig {
    pub connector: Arc<dyn Connector>,
    pub backoff: BackoffConfig,
}

#[derive(Debug, Clone)]
pub enum SupervisorExit {
    Closed,
    Fatal(ErrorInfo),
}

pub fn handshake_spec() -> CommandSpec {
    let mut args = Map::new();
    args.insert("protocol".to_owned(), Value::from(1_u64));
    args.insert(
        "client".to_owned(),
        serde_json::json!({
            "name": "linteum-tui",
            "version": env!("CARGO_PKG_VERSION"),
        }),
    );
    args.insert(
        "features".to_owned(),
        serde_json::json!(["view", "replay", "approval", "artifacts"]),
    );
    CommandSpec {
        name: "hello".to_owned(),
        args,
        session_id: None,
    }
}

pub fn check_features(features: &[String]) -> Result<(), Box<ErrorInfo>> {
    let has = |name: &str| features.iter().any(|feature| feature == name);
    if has("view") && has("replay") {
        Ok(())
    } else {
        Err(Box::new(error_info(
            "E_PROTOCOL",
            "gateway does not advertise view/replay",
        )))
    }
}

pub async fn run_supervisor(
    config: SupervisorConfig,
    input_tx: mpsc::Sender<Input>,
    mut command_rx: mpsc::Receiver<CommandSpec>,
    mut control_rx: mpsc::Receiver<SupervisorControl>,
) -> SupervisorExit {
    let mut backoff = Backoff::new(config.backoff);
    let mut first_connection = true;

    loop {
        let _ = input_tx
            .send(Input::Connection(ConnectionEvent::Connecting))
            .await;

        tracing::info!(mode = ?config.connector.mode(), "connecting to gateway");
        let transport = match config.connector.connect().await {
            Ok(transport) => transport,
            Err(error) => {
                if config.connector.mode() == Mode::Spawn {
                    // The supervisor stays alive so `r`/`q` on the fatal
                    // screen can restart the child or quit (§7.5).
                    let info = error_info("E_SPAWN_FAILED", error.to_string());
                    match spawn_fatal_wait(info, &input_tx, &mut control_rx).await {
                        WaitOutcome::ReconnectNow => continue,
                        WaitOutcome::Shutdown => return SupervisorExit::Closed,
                        WaitOutcome::Timeout => unreachable!("spawn fatal wait has no timeout"),
                    }
                }
                let _ = input_tx
                    .send(Input::Connection(ConnectionEvent::Disconnected {
                        reason: error.to_string(),
                        retryable: true,
                    }))
                    .await;
                match wait_or_control(&mut control_rx, backoff.next_delay()).await {
                    WaitOutcome::Timeout | WaitOutcome::ReconnectNow => continue,
                    WaitOutcome::Shutdown => return SupervisorExit::Closed,
                }
            }
        };

        let (broker, handle) = CommandBroker::new(transport, Duration::from_secs(30));
        let mut broker_task: JoinHandle<Option<i32>> = tokio::spawn(broker.run(input_tx.clone()));

        let hello = handle.send(handshake_spec()).await;
        let result = match hello {
            Ok(result) => result,
            Err(error) => {
                let info = match error {
                    CommandError::Timeout => error_info("E_REQUEST_TIMEOUT", "hello timed out"),
                    CommandError::Transport(message) => error_info("E_TRANSPORT", message),
                    CommandError::Protocol(message) => error_info("E_PROTOCOL", message),
                    CommandError::Gateway(info) => *info,
                    CommandError::Closed => {
                        error_info("E_CONNECTION_CLOSED", "connection closed during hello")
                    }
                };
                if config.connector.mode() == Mode::Spawn {
                    close_and_join(&handle, &mut broker_task).await;
                    match spawn_fatal_wait(info, &input_tx, &mut control_rx).await {
                        WaitOutcome::ReconnectNow => continue,
                        WaitOutcome::Shutdown => return SupervisorExit::Closed,
                        WaitOutcome::Timeout => unreachable!("spawn fatal wait has no timeout"),
                    }
                }
                broker_task.abort();
                let _ = input_tx
                    .send(Input::Connection(ConnectionEvent::Disconnected {
                        reason: info.message.clone(),
                        retryable: true,
                    }))
                    .await;
                match wait_or_control(&mut control_rx, backoff.next_delay()).await {
                    WaitOutcome::Timeout | WaitOutcome::ReconnectNow => continue,
                    WaitOutcome::Shutdown => return SupervisorExit::Closed,
                }
            }
        };

        if result.result.get("protocol").and_then(Value::as_u64) != Some(1) {
            let info = error_info("E_PROTOCOL_VERSION", "gateway protocol is not 1");
            if config.connector.mode() == Mode::Spawn {
                close_and_join(&handle, &mut broker_task).await;
                match spawn_fatal_wait(info, &input_tx, &mut control_rx).await {
                    WaitOutcome::ReconnectNow => continue,
                    WaitOutcome::Shutdown => return SupervisorExit::Closed,
                    WaitOutcome::Timeout => unreachable!("spawn fatal wait has no timeout"),
                }
            }
            broker_task.abort();
            let _ = input_tx
                .send(Input::Connection(ConnectionEvent::Fatal {
                    error: info.clone(),
                }))
                .await;
            return SupervisorExit::Fatal(info);
        }
        let features: Vec<String> = result
            .result
            .get("features")
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        if let Err(info) = check_features(&features) {
            let info = *info;
            if config.connector.mode() == Mode::Spawn {
                close_and_join(&handle, &mut broker_task).await;
                match spawn_fatal_wait(info, &input_tx, &mut control_rx).await {
                    WaitOutcome::ReconnectNow => continue,
                    WaitOutcome::Shutdown => return SupervisorExit::Closed,
                    WaitOutcome::Timeout => unreachable!("spawn fatal wait has no timeout"),
                }
            }
            broker_task.abort();
            let _ = input_tx
                .send(Input::Connection(ConnectionEvent::Fatal {
                    error: info.clone(),
                }))
                .await;
            return SupervisorExit::Fatal(info);
        }
        tracing::info!(?features, "gateway connected");
        let limits = parse_limits(result.result.get("limits"));
        let instance_id = parse_instance_id(result.result.get("instance_id"));
        let connected = if first_connection {
            ConnectionEvent::Connected {
                features,
                limits,
                instance_id,
            }
        } else {
            ConnectionEvent::Reconnected {
                features,
                instance_id,
            }
        };
        let _ = input_tx.send(Input::Connection(connected)).await;
        first_connection = false;
        backoff.reset();

        let mut reconnect = false;
        let mut shutdown = false;
        let mut broker_done = false;
        // The broker task's exit status. Captured when the broker-done branch
        // fires: a completed `JoinHandle` must not be polled again.
        let mut broker_status: Option<i32> = None;
        loop {
            tokio::select! {
                command = command_rx.recv() => {
                    let Some(spec) = command else {
                        shutdown = true;
                        break;
                    };
                    let send = handle.send(spec);
                    tokio::pin!(send);
                    let result = tokio::select! {
                        result = &mut send => result,
                        control = control_rx.recv() => {
                            match control {
                                Some(SupervisorControl::ReconnectNow)
                                | Some(SupervisorControl::RestartChild) => {
                                    reconnect = true;
                                }
                                Some(SupervisorControl::Shutdown) | None => {
                                    shutdown = true;
                                }
                            }
                            break;
                        }
                    };
                    let forwarded = match result {
                        Ok(result) => input_tx.send(Input::CommandResult(result)).await,
                        Err(error) => input_tx
                            .send(Input::CommandResult(synthetic_error(error)))
                            .await,
                    };
                    if forwarded.is_err() {
                        shutdown = true;
                        break;
                    }
                }
                control = control_rx.recv() => {
                    match control {
                        Some(SupervisorControl::ReconnectNow)
                        | Some(SupervisorControl::RestartChild) => {
                            reconnect = true;
                            break;
                        }
                        Some(SupervisorControl::Shutdown) | None => {
                            shutdown = true;
                            break;
                        }
                    }
                }
                status = &mut broker_task => {
                    broker_done = true;
                    broker_status = status.ok().flatten();
                    break;
                }
            }
        }
        if shutdown {
            // Closing the request channel lets the broker call the writer's
            // `close`, which performs the child teardown in spawn mode.
            drop(handle);
            let _ = tokio::time::timeout(Duration::from_secs(10), &mut broker_task).await;
            return SupervisorExit::Closed;
        }
        if reconnect {
            // Close the old transport and wait for the broker before
            // respawning: never leave the previous child running (§7.5).
            close_and_join(&handle, &mut broker_task).await;
            continue;
        }
        debug_assert!(broker_done);
        let status = broker_status;
        if config.connector.mode() == Mode::Spawn {
            // The child exited on its own. Report it and stay alive until the
            // user restarts (`r`) or quits; do not respawn automatically.
            tracing::error!(?status, "gateway child exited");
            let _ = input_tx
                .send(Input::Connection(ConnectionEvent::ChildExited {
                    status,
                    stderr_tail: config.connector.stderr_tail(),
                }))
                .await;
            match wait_for_spawn_control(&mut control_rx).await {
                WaitOutcome::ReconnectNow => continue,
                WaitOutcome::Shutdown => return SupervisorExit::Closed,
                WaitOutcome::Timeout => unreachable!("spawn control wait has no timeout"),
            }
        }

        let _ = input_tx
            .send(Input::Connection(ConnectionEvent::Disconnected {
                reason: "connection lost".to_owned(),
                retryable: true,
            }))
            .await;
        match wait_or_control(&mut control_rx, backoff.next_delay()).await {
            WaitOutcome::Timeout | WaitOutcome::ReconnectNow => continue,
            WaitOutcome::Shutdown => return SupervisorExit::Closed,
        }
    }
}

enum WaitOutcome {
    Timeout,
    ReconnectNow,
    Shutdown,
}

async fn wait_or_control(
    control_rx: &mut mpsc::Receiver<SupervisorControl>,
    delay: Duration,
) -> WaitOutcome {
    tokio::select! {
        _ = tokio::time::sleep(delay) => WaitOutcome::Timeout,
        control = control_rx.recv() => match control {
            Some(SupervisorControl::ReconnectNow)
            | Some(SupervisorControl::RestartChild) => WaitOutcome::ReconnectNow,
            Some(SupervisorControl::Shutdown) | None => WaitOutcome::Shutdown,
        },
    }
}

/// Wait (without a timeout) for a restart request or shutdown. Spawn-mode
/// child exits and failures wait here so the fatal screen's `r`/`q` work.
async fn wait_for_spawn_control(control_rx: &mut mpsc::Receiver<SupervisorControl>) -> WaitOutcome {
    match control_rx.recv().await {
        Some(SupervisorControl::RestartChild) | Some(SupervisorControl::ReconnectNow) => {
            WaitOutcome::ReconnectNow
        }
        Some(SupervisorControl::Shutdown) | None => WaitOutcome::Shutdown,
    }
}

/// Emit the fatal event and stay alive until the user restarts or quits.
/// Spawn mode only (§7.5); a failed respawn emits `Fatal` again and waits.
async fn spawn_fatal_wait(
    info: ErrorInfo,
    input_tx: &mpsc::Sender<Input>,
    control_rx: &mut mpsc::Receiver<SupervisorControl>,
) -> WaitOutcome {
    let _ = input_tx
        .send(Input::Connection(ConnectionEvent::Fatal { error: info }))
        .await;
    wait_for_spawn_control(control_rx).await
}

/// Close the transport through the broker and wait (bounded) for the broker
/// task to end. A stuck broker is aborted; dropping its writer then triggers
/// the best-effort child kill in `StdioChildWriter::drop`.
async fn close_and_join(handle: &BrokerHandle, broker_task: &mut JoinHandle<Option<i32>>) {
    let _ = handle.close_transport().await;
    if tokio::time::timeout(Duration::from_secs(10), &mut *broker_task)
        .await
        .is_err()
    {
        tracing::warn!("broker did not stop after transport close; aborting");
        broker_task.abort();
    }
}

/// Read the optional `hello` instance id. Pre-v0.4 gateways omit it; a blank
/// string is treated the same as absent so a malformed value never looks like
/// a real gateway identity.
fn parse_instance_id(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

fn parse_limits(value: Option<&Value>) -> Limits {
    let object = value.and_then(Value::as_object);
    let field = |name: &str| {
        object
            .and_then(|object| object.get(name))
            .and_then(Value::as_u64)
            .unwrap_or(0)
    };
    Limits {
        max_command_bytes: field("max_command_bytes"),
        max_frame_bytes: field("max_frame_bytes"),
        client_queue_frames: field("client_queue_frames"),
        history_page_size: field("history_page_size"),
    }
}

fn synthetic_error(error: CommandError) -> CommandResult {
    let code = match &error {
        CommandError::Timeout => "E_REQUEST_TIMEOUT",
        CommandError::Transport(_) => "E_TRANSPORT",
        CommandError::Gateway(info) => &info.code,
        CommandError::Protocol(_) => "E_PROTOCOL",
        CommandError::Closed => "E_CONNECTION_CLOSED",
    };
    CommandResult {
        v: 1,
        id: String::new(),
        reply_to: String::new(),
        ok: false,
        result: Value::Null,
        error: Some(error_info(code, error.to_string())),
        name: None,
    }
}

fn error_info(code: &str, message: impl Into<String>) -> ErrorInfo {
    ErrorInfo {
        code: code.to_owned(),
        source: "gateway".to_owned(),
        location: "connection::supervisor".to_owned(),
        message: message.into(),
        exc_type: None,
        detail: String::new(),
        retryable: false,
        enters_history: false,
        context: Value::Null,
    }
}
