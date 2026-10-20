use futures_util::{SinkExt, StreamExt};
use linteum_tui::connection::{Transport, connect_websocket};
use tokio::net::TcpListener;
use tokio_tungstenite::accept_async;

#[tokio::test]
async fn websocket_round_trips() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = accept_async(stream).await.unwrap();
        let message = ws.next().await.unwrap().unwrap();
        ws.send(message).await.unwrap();
    });

    let transport = connect_websocket(&format!("ws://{addr}/")).await.unwrap();
    let (mut reader, mut writer) = Box::new(transport).split();
    writer.send_text("hello".into()).await.unwrap();
    let reply = reader.recv_text().await.unwrap().unwrap();
    assert_eq!(reply, "hello");

    server.await.unwrap();
}

#[tokio::test]
async fn invalid_url_is_an_error() {
    assert!(connect_websocket("not-a-url").await.is_err());
}
