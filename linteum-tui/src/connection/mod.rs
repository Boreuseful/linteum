//! Transports, command correlation, and connection lifecycle.

pub mod broker;
pub mod stdio_child;
pub mod supervisor;
pub mod websocket;

pub use broker::{BrokerHandle, CommandBroker, CommandError};
pub use stdio_child::{
    SpawnConfig, SpawnError, StdioChildTransport, resolve_linteum_bin, spawn_stdio,
};
pub use supervisor::{
    Backoff, BackoffConfig, Connector, Mode, SupervisorConfig, SupervisorControl, SupervisorExit,
    check_features, handshake_spec, run_supervisor,
};
pub use websocket::{WebSocketTransport, connect_websocket};

use async_trait::async_trait;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TransportError {
    #[error("io: {0}")]
    Io(String),
    #[error("websocket: {0}")]
    WebSocket(String),
    #[error("protocol: {0}")]
    Protocol(String),
    #[error("closed")]
    Closed,
}

/// The read half of a transport. Frames may arrive before command results.
#[async_trait]
pub trait TransportReader: Send {
    async fn recv_text(&mut self) -> Option<Result<String, TransportError>>;
}

/// The write half of a transport.
#[async_trait]
pub trait TransportWriter: Send {
    async fn send_text(&mut self, text: String) -> Result<(), TransportError>;
    async fn close(&mut self) -> Result<(), TransportError>;
}

/// A full-duplex transport. Splitting lets the broker read and write
/// concurrently without a mutex around a long `recv`.
pub trait Transport: Send {
    fn split(self: Box<Self>) -> (Box<dyn TransportReader>, Box<dyn TransportWriter>);
}
