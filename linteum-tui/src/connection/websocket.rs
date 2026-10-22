//! WebSocket transport for attach mode.

use async_trait::async_trait;
use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, connect_async};

use super::{Transport, TransportError, TransportReader, TransportWriter};

pub struct WebSocketTransport {
    stream: WebSocketStream<MaybeTlsStream<TcpStream>>,
}

pub async fn connect_websocket(url: &str) -> Result<WebSocketTransport, TransportError> {
    let (stream, _) = connect_async(url)
        .await
        .map_err(|error| TransportError::WebSocket(error.to_string()))?;
    Ok(WebSocketTransport { stream })
}

impl Transport for WebSocketTransport {
    fn split(self: Box<Self>) -> (Box<dyn TransportReader>, Box<dyn TransportWriter>) {
        let (sink, stream) = self.stream.split();
        (
            Box::new(WebSocketReader { stream }),
            Box::new(WebSocketWriter { sink }),
        )
    }
}

struct WebSocketReader {
    stream: SplitStream<WebSocketStream<MaybeTlsStream<TcpStream>>>,
}

#[async_trait]
impl TransportReader for WebSocketReader {
    async fn recv_text(&mut self) -> Option<Result<String, TransportError>> {
        loop {
            match self.stream.next().await {
                Some(Ok(Message::Text(text))) => return Some(Ok(text.to_string())),
                Some(Ok(Message::Binary(bytes))) => {
                    return Some(
                        String::from_utf8(bytes.to_vec())
                            .map_err(|error| TransportError::Protocol(error.to_string())),
                    );
                }
                Some(Ok(Message::Ping(_))) | Some(Ok(Message::Pong(_))) => continue,
                Some(Ok(Message::Frame(_))) => continue,
                Some(Ok(Message::Close(_))) | None => return None,
                Some(Err(error)) => {
                    return Some(Err(TransportError::WebSocket(error.to_string())));
                }
            }
        }
    }
}

struct WebSocketWriter {
    sink: SplitSink<WebSocketStream<MaybeTlsStream<TcpStream>>, Message>,
}

#[async_trait]
impl TransportWriter for WebSocketWriter {
    async fn send_text(&mut self, text: String) -> Result<(), TransportError> {
        self.sink
            .send(Message::Text(text))
            .await
            .map_err(|error| TransportError::WebSocket(error.to_string()))
    }

    async fn close(&mut self) -> Result<(), TransportError> {
        self.sink
            .close()
            .await
            .map_err(|error| TransportError::WebSocket(error.to_string()))
    }
}
