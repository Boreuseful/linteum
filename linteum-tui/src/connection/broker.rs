//! Command broker: ids, `reply_to` correlation, timeouts, and frame routing.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use tokio::sync::{mpsc, oneshot};
use tokio::time::Instant;

use crate::protocol::{
    Command, CommandResult, Envelope, ErrorInfo, command, decode_envelope, encode_command,
};
use crate::viewmodel::{CommandSpec, ConnectionEvent, Input};

use super::{Transport, TransportReader, TransportWriter};

#[derive(Debug, Clone, thiserror::Error)]
pub enum CommandError {
    #[error("command timed out")]
    Timeout,
    #[error("transport: {0}")]
    Transport(String),
    #[error("gateway error: {0:?}")]
    Gateway(Box<ErrorInfo>),
    #[error("protocol: {0}")]
    Protocol(String),
    #[error("connection closed")]
    Closed,
}

struct Pending {
    reply: oneshot::Sender<Result<CommandResult, CommandError>>,
    deadline: Instant,
}

struct BrokerRequest {
    command: Command,
    reply: oneshot::Sender<Result<CommandResult, CommandError>>,
}

/// The reader/writer pair plus the request receiver. Run it on a task and send
/// commands through the cloneable [`BrokerHandle`].
pub struct CommandBroker {
    reader: Box<dyn TransportReader>,
    writer: Box<dyn TransportWriter>,
    requests: mpsc::Receiver<BrokerRequest>,
    timeout: Duration,
}

#[derive(Clone)]
pub struct BrokerHandle {
    tx: mpsc::Sender<BrokerRequest>,
    next_id: Arc<AtomicU64>,
    timeout: Duration,
}

impl CommandBroker {
    pub fn new(transport: Box<dyn Transport>, timeout: Duration) -> (Self, BrokerHandle) {
        let (reader, writer) = transport.split();
        let (tx, requests) = mpsc::channel(32);
        (
            Self {
                reader,
                writer,
                requests,
                timeout,
            },
            BrokerHandle {
                tx,
                next_id: Arc::new(AtomicU64::new(1)),
                timeout,
            },
        )
    }

    /// Read frames/results and write commands until the transport closes or
    /// the UI drops the input receiver.
    pub async fn run(self, input_tx: mpsc::Sender<Input>) {
        let Self {
            mut reader,
            mut writer,
            mut requests,
            timeout,
        } = self;
        let mut pending: HashMap<String, Pending> = HashMap::new();
        // A result may be read before the request branch has registered the
        // pending waiter (the scripted transports in tests can preload it).
        // Buffer a bounded number of unmatched results so correlation still
        // succeeds; real gateways only reply after a command, but the reader
        // and writer are independent and this closes the race.
        let mut early: HashMap<String, CommandResult> = HashMap::new();
        let mut expiry = tokio::time::interval(Duration::from_millis(50));
        expiry.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

        loop {
            tokio::select! {
                request = requests.recv() => {
                    let Some(request) = request else {
                        let _ = writer.close().await;
                        break;
                    };
                    let id = request.command.id.clone();
                    tracing::debug!(id = %id, command = %request.command.name, "sending command");
                    match encode_command(&request.command) {
                        Ok(line) => {
                            if let Err(error) = writer.send_text(line).await {
                                let _ = request.reply.send(Err(CommandError::Transport(error.to_string())));
                            } else if let Some(result) = early.remove(&id) {
                                let _ = request.reply.send(Ok(result));
                            } else {
                                pending.insert(id, Pending {
                                    reply: request.reply,
                                    deadline: Instant::now() + timeout,
                                });
                            }
                        }
                        Err(error) => {
                            let _ = request.reply.send(Err(CommandError::Protocol(error.to_string())));
                        }
                    }
                }
                message = reader.recv_text() => {
                    match message {
                        Some(Ok(line)) => {
                            match decode_envelope(&line) {
                                Ok(Envelope::Result(result)) => {
                                    tracing::debug!(reply_to = %result.reply_to, ok = result.ok, "command result");
                                    if let Some(waiter) = pending.remove(&result.reply_to) {
                                        let _ = waiter.reply.send(Ok(result));
                                    } else if early.len() < 64 {
                                        early.insert(result.reply_to.clone(), result);
                                    }
                                }
                                Ok(Envelope::Frame(frame)) => {
                                    if input_tx.send(Input::Frame(frame)).await.is_err() {
                                        break;
                                    }
                                }
                                Ok(Envelope::Error(error)) => {
                                    if input_tx.send(Input::Connection(ConnectionEvent::Fatal {
                                        error: error.error_info,
                                    })).await.is_err() {
                                        break;
                                    }
                                }
                                // The gateway never sends these to a client.
                                Ok(Envelope::Cmd(_)) | Ok(Envelope::Unknown { .. }) => {}
                                Err(error) => {
                                    let info = ErrorInfo {
                                        code: "E_PROTOCOL".to_owned(),
                                        source: "tui".to_owned(),
                                        location: "connection::broker".to_owned(),
                                        message: error.to_string(),
                                        exc_type: None,
                                        detail: String::new(),
                                        retryable: false,
                                        enters_history: false,
                                        context: serde_json::Value::Null,
                                    };
                                    if input_tx.send(Input::Connection(ConnectionEvent::Fatal {
                                        error: info,
                                    })).await.is_err() {
                                        break;
                                    }
                                }
                            }
                        }
                        Some(Err(error)) => {
                            fail_all(&mut pending, CommandError::Transport(error.to_string()));
                            let _ = input_tx.send(Input::Connection(ConnectionEvent::Disconnected {
                                reason: error.to_string(),
                                retryable: true,
                            })).await;
                            break;
                        }
                        None => {
                            fail_all(&mut pending, CommandError::Closed);
                            let _ = input_tx.send(Input::Connection(ConnectionEvent::Disconnected {
                                reason: "connection closed".to_owned(),
                                retryable: true,
                            })).await;
                            break;
                        }
                    }
                }
                _ = expiry.tick() => {
                    let now = Instant::now();
                    let expired: Vec<String> = pending
                        .iter()
                        .filter(|(_, waiter)| waiter.deadline <= now)
                        .map(|(id, _)| id.clone())
                        .collect();
                    for id in expired {
                        if let Some(waiter) = pending.remove(&id) {
                            let _ = waiter.reply.send(Err(CommandError::Timeout));
                        }
                    }
                }
            }
        }
        fail_all(&mut pending, CommandError::Closed);
    }
}

impl BrokerHandle {
    pub async fn send(&self, spec: CommandSpec) -> Result<CommandResult, CommandError> {
        let id = format!("c-{}", self.next_id.fetch_add(1, Ordering::Relaxed));
        let command = command(id, &spec.name, spec.args, spec.session_id);
        self.send_raw(command).await
    }

    pub async fn send_raw(&self, command: Command) -> Result<CommandResult, CommandError> {
        let (tx, rx) = oneshot::channel();
        self.tx
            .send(BrokerRequest { command, reply: tx })
            .await
            .map_err(|_| CommandError::Closed)?;
        match tokio::time::timeout(self.timeout, rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(CommandError::Closed),
            Err(_) => Err(CommandError::Timeout),
        }
    }
}

fn fail_all(pending: &mut HashMap<String, Pending>, error: CommandError) {
    for waiter in pending.drain().map(|(_, waiter)| waiter) {
        let _ = waiter.reply.send(Err(error.clone()));
    }
}
