use std::time::Duration;

use async_trait::async_trait;
use linteum_tui::connection::{
    BrokerHandle, CommandBroker, CommandError, Transport, TransportError, TransportReader,
    TransportWriter,
};
use linteum_tui::protocol::Command;
use linteum_tui::viewmodel::{CommandSpec, ConnectionEvent, Input};
use tokio::sync::mpsc;

struct FakeTransport {
    out: mpsc::Sender<String>,
    input: mpsc::Receiver<String>,
}

impl Transport for FakeTransport {
    fn split(self: Box<Self>) -> (Box<dyn TransportReader>, Box<dyn TransportWriter>) {
        (
            Box::new(FakeReader { input: self.input }),
            Box::new(FakeWriter { out: self.out }),
        )
    }
}

struct FakeReader {
    input: mpsc::Receiver<String>,
}

#[async_trait]
impl TransportReader for FakeReader {
    async fn recv_text(&mut self) -> Option<Result<String, TransportError>> {
        self.input.recv().await.map(Ok)
    }
}

struct FakeWriter {
    out: mpsc::Sender<String>,
}

#[async_trait]
impl TransportWriter for FakeWriter {
    async fn send_text(&mut self, text: String) -> Result<(), TransportError> {
        self.out
            .send(text)
            .await
            .map_err(|_| TransportError::Closed)
    }

    async fn close(&mut self) -> Result<(), TransportError> {
        Ok(())
    }
}

fn spec(name: &str) -> CommandSpec {
    CommandSpec {
        name: name.into(),
        args: Default::default(),
        session_id: None,
    }
}

#[tokio::test]
async fn broker_correlates_result_by_reply_to() {
    let (out_tx, mut out_rx) = mpsc::channel(8);
    let (in_tx, in_rx) = mpsc::channel(8);
    let transport = Box::new(FakeTransport {
        out: out_tx,
        input: in_rx,
    });
    let (broker, handle) = CommandBroker::new(transport, Duration::from_secs(30));
    let (input_tx, _input_rx) = mpsc::channel(8);
    let task = tokio::spawn(broker.run(input_tx));

    let send = tokio::spawn(async move { handle.send(spec("hello")).await });
    let line = out_rx.recv().await.unwrap();
    let command: serde_json::Value = serde_json::from_str(&line).unwrap();
    let id = command["id"].as_str().unwrap().to_string();
    in_tx
        .send(format!(
            r#"{{"v":1,"kind":"result","id":"r","reply_to":"{id}","ok":true,"result":{{"protocol":1}}}}"#
        ))
        .await
        .unwrap();

    let result = send.await.unwrap().unwrap();
    assert_eq!(result.result["protocol"], 1);
    task.abort();
}

#[tokio::test]
async fn broker_routes_frames_before_result() {
    let (out_tx, mut out_rx) = mpsc::channel(8);
    let (in_tx, in_rx) = mpsc::channel(8);
    let transport = Box::new(FakeTransport {
        out: out_tx,
        input: in_rx,
    });
    let (broker, handle) = CommandBroker::new(transport, Duration::from_secs(30));
    let (input_tx, mut input_rx) = mpsc::channel(8);
    let task = tokio::spawn(broker.run(input_tx));

    let send = tokio::spawn(async move { handle.send(spec("session.subscribe")).await });
    let line = out_rx.recv().await.unwrap();
    let command: serde_json::Value = serde_json::from_str(&line).unwrap();
    let id = command["id"].as_str().unwrap().to_string();

    in_tx
        .send(r#"{"v":1,"kind":"frame","id":"f","frame":"user.message","payload":{"text":"hi"},"session_id":"s","seq":1}"#.into())
        .await
        .unwrap();
    let input = input_rx.recv().await.unwrap();
    assert!(matches!(input, Input::Frame(frame) if frame.frame == "user.message"));

    in_tx
        .send(format!(
            r#"{{"v":1,"kind":"result","id":"r","reply_to":"{id}","ok":true,"result":{{"subscribed":true}}}}"#
        ))
        .await
        .unwrap();
    assert!(send.await.unwrap().is_ok());
    task.abort();
}

#[tokio::test]
async fn broker_times_out_after_deadline() {
    let (out_tx, _out_rx) = mpsc::channel(8);
    let (_in_tx, in_rx) = mpsc::channel::<String>(8);
    let transport = Box::new(FakeTransport {
        out: out_tx,
        input: in_rx,
    });
    let (broker, handle) = CommandBroker::new(transport, Duration::from_millis(50));
    let (input_tx, _input_rx) = mpsc::channel(8);
    let task = tokio::spawn(broker.run(input_tx));

    let result = handle.send(spec("hello")).await;
    assert!(matches!(result, Err(CommandError::Timeout)));
    task.abort();
}

#[tokio::test]
async fn broker_ignores_standalone_error_for_pending() {
    let (out_tx, mut out_rx) = mpsc::channel(8);
    let (in_tx, in_rx) = mpsc::channel(8);
    let transport = Box::new(FakeTransport {
        out: out_tx,
        input: in_rx,
    });
    let (broker, handle) = CommandBroker::new(transport, Duration::from_secs(30));
    let (input_tx, mut input_rx) = mpsc::channel(8);
    let task = tokio::spawn(broker.run(input_tx));

    let send = tokio::spawn(async move { handle.send(spec("hello")).await });
    out_rx.recv().await.unwrap();
    in_tx
        .send(r#"{"v":1,"kind":"error","error_info":{"code":"E_INTERNAL","source":"gateway","location":"x","message":"boom","exc_type":null,"detail":"","retryable":false,"enters_history":false,"context":{}}}"#.into())
        .await
        .unwrap();
    let input = input_rx.recv().await.unwrap();
    assert!(matches!(
        input,
        Input::Connection(ConnectionEvent::Fatal { .. })
    ));
    let early = tokio::time::timeout(Duration::from_millis(50), send).await;
    assert!(early.is_err());
    task.abort();
}

#[tokio::test]
async fn broker_accepts_result_that_arrives_before_pending_insert() {
    let (out_tx, mut out_rx) = mpsc::channel(8);
    let (in_tx, in_rx) = mpsc::channel(8);
    // Preload the reply before the command is even sent.
    in_tx
        .send(r#"{"v":1,"kind":"result","id":"r","reply_to":"c-1","ok":true,"result":{"early":true}}"#.into())
        .await
        .unwrap();
    let transport = Box::new(FakeTransport {
        out: out_tx,
        input: in_rx,
    });
    let (broker, handle) = CommandBroker::new(transport, Duration::from_secs(30));
    let (input_tx, _input_rx) = mpsc::channel(8);
    let task = tokio::spawn(broker.run(input_tx));

    let result = tokio::time::timeout(Duration::from_secs(2), handle.send(spec("hello")))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(result.result["early"], true);
    out_rx.recv().await.unwrap();
    task.abort();
}

#[tokio::test]
async fn raw_command_keeps_its_id() {
    let (out_tx, mut out_rx) = mpsc::channel(8);
    let (in_tx, in_rx) = mpsc::channel(8);
    let transport = Box::new(FakeTransport {
        out: out_tx,
        input: in_rx,
    });
    let (broker, handle): (_, BrokerHandle) =
        CommandBroker::new(transport, Duration::from_secs(30));
    let (input_tx, _input_rx) = mpsc::channel(8);
    let task = tokio::spawn(broker.run(input_tx));

    let raw = Command {
        v: 1,
        id: "c-default".into(),
        name: "hello".into(),
        args: Default::default(),
        session_id: None,
        trace_id: None,
    };
    let send = tokio::spawn(async move { handle.send_raw(raw).await });
    let line = out_rx.recv().await.unwrap();
    assert!(line.contains("\"kind\":\"cmd\""));
    in_tx
        .send(
            r#"{"v":1,"kind":"result","id":"r","reply_to":"c-default","ok":true,"result":{}}"#
                .into(),
        )
        .await
        .unwrap();
    let _ = send.await.unwrap();
    task.abort();
}
