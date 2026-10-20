use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use linteum_tui::connection::supervisor::{
    Backoff, BackoffConfig, Connector, Mode, SupervisorConfig, SupervisorControl, SupervisorExit,
    run_supervisor,
};
use linteum_tui::connection::{Transport, TransportError, TransportReader, TransportWriter};
use linteum_tui::viewmodel::{CommandSpec, ConnectionEvent, Input};
use tokio::sync::mpsc;
use tokio::time::timeout;

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

struct Script {
    messages: Vec<String>,
    close: bool,
}

struct FakeConnector {
    mode: Mode,
    scripts: Mutex<VecDeque<Script>>,
    attempts: AtomicUsize,
    command_tx: mpsc::UnboundedSender<mpsc::Receiver<String>>,
    keepalive: Mutex<Vec<mpsc::Sender<String>>>,
}

impl FakeConnector {
    fn new(
        mode: Mode,
        scripts: Vec<Script>,
        command_tx: mpsc::UnboundedSender<mpsc::Receiver<String>>,
    ) -> Self {
        Self {
            mode,
            scripts: Mutex::new(scripts.into()),
            attempts: AtomicUsize::new(0),
            command_tx,
            keepalive: Mutex::new(Vec::new()),
        }
    }

    fn attempts(&self) -> usize {
        self.attempts.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl Connector for FakeConnector {
    async fn connect(&self) -> Result<Box<dyn Transport>, TransportError> {
        self.attempts.fetch_add(1, Ordering::SeqCst);
        let script = self
            .scripts
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| Script {
                messages: vec![hello_result(1, &["view", "replay"])],
                close: false,
            });
        let (out_tx, out_rx) = mpsc::channel(16);
        let (in_tx, in_rx) = mpsc::channel(16);
        for message in script.messages {
            in_tx
                .send(message)
                .await
                .map_err(|_| TransportError::Closed)?;
        }
        if script.close {
            // Let the supervisor send hello before the scripted EOF. A real
            // gateway replies only after receiving the command.
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(50)).await;
                drop(in_tx);
            });
        } else {
            self.keepalive.lock().unwrap().push(in_tx);
        }
        let _ = self.command_tx.send(out_rx);
        Ok(Box::new(FakeTransport {
            out: out_tx,
            input: in_rx,
        }))
    }

    fn mode(&self) -> Mode {
        self.mode
    }
}

fn hello_result(protocol: u64, features: &[&str]) -> String {
    serde_json::json!({
        "v": 1, "kind": "result", "id": "r", "reply_to": "c-1", "ok": true,
        "result": {"protocol": protocol, "features": features, "limits": {}}
    })
    .to_string()
}

fn bye_frame(reason: &str) -> String {
    serde_json::json!({
        "v": 1, "kind": "frame", "id": "f", "frame": "bye",
        "payload": {"reason": reason}, "session_id": "s", "seq": 10
    })
    .to_string()
}

fn script(messages: Vec<String>, close: bool) -> Script {
    Script { messages, close }
}

fn fast_backoff() -> BackoffConfig {
    BackoffConfig {
        initial: Duration::from_millis(10),
        max: Duration::from_millis(20),
    }
}

struct Harness {
    connector: Arc<FakeConnector>,
    command_rx: mpsc::UnboundedReceiver<mpsc::Receiver<String>>,
    input_rx: mpsc::Receiver<Input>,
    control_tx: mpsc::Sender<SupervisorControl>,
    task: tokio::task::JoinHandle<SupervisorExit>,
    _command_tx: mpsc::Sender<CommandSpec>,
}

fn harness(mode: Mode, scripts: Vec<Script>, backoff: BackoffConfig) -> Harness {
    let (command_inspect_tx, command_rx) = mpsc::unbounded_channel();
    let connector = Arc::new(FakeConnector::new(mode, scripts, command_inspect_tx));
    let (input_tx, input_rx) = mpsc::channel(64);
    let (command_tx, command_rx_in) = mpsc::channel(8);
    let (control_tx, control_rx) = mpsc::channel(8);
    let config = SupervisorConfig {
        connector: connector.clone(),
        backoff,
    };
    let task = tokio::spawn(run_supervisor(config, input_tx, command_rx_in, control_rx));
    Harness {
        connector,
        command_rx,
        input_rx,
        control_tx,
        task,
        _command_tx: command_tx,
    }
}

impl Harness {
    async fn next_commands(&mut self) -> mpsc::Receiver<String> {
        timeout(Duration::from_secs(5), self.command_rx.recv())
            .await
            .unwrap()
            .unwrap()
    }

    async fn wait_for_connected(&mut self) {
        loop {
            match timeout(Duration::from_secs(5), self.input_rx.recv())
                .await
                .unwrap()
                .unwrap()
            {
                Input::Connection(ConnectionEvent::Connected { .. })
                | Input::Connection(ConnectionEvent::Reconnected) => break,
                _ => {}
            }
        }
    }

    async fn wait_for_disconnected(&mut self) {
        loop {
            if let Input::Connection(ConnectionEvent::Disconnected { .. }) =
                timeout(Duration::from_secs(5), self.input_rx.recv())
                    .await
                    .unwrap()
                    .unwrap()
            {
                break;
            }
        }
    }

    async fn shutdown(self) -> SupervisorExit {
        self.control_tx
            .send(SupervisorControl::Shutdown)
            .await
            .unwrap();
        timeout(Duration::from_secs(5), self.task)
            .await
            .unwrap()
            .unwrap()
    }
}

#[tokio::test]
async fn handshake_is_the_first_command() {
    let mut h = harness(
        Mode::Attach,
        vec![script(vec![hello_result(1, &["view", "replay"])], false)],
        fast_backoff(),
    );
    let mut commands = h.next_commands().await;
    let line = timeout(Duration::from_secs(5), commands.recv())
        .await
        .unwrap()
        .unwrap();
    let value: serde_json::Value = serde_json::from_str(&line).unwrap();
    assert_eq!(value["name"], "hello");
    assert_eq!(value["args"]["protocol"], 1);
    h.wait_for_connected().await;
    assert!(matches!(h.shutdown().await, SupervisorExit::Closed));
}

#[tokio::test]
async fn protocol_mismatch_is_fatal() {
    let h = harness(
        Mode::Attach,
        vec![script(vec![hello_result(2, &["view", "replay"])], false)],
        fast_backoff(),
    );
    let exit = timeout(Duration::from_secs(5), h.task)
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(exit, SupervisorExit::Fatal(info) if info.code == "E_PROTOCOL_VERSION"));
}

#[tokio::test]
async fn missing_view_feature_is_fatal() {
    let h = harness(
        Mode::Attach,
        vec![script(vec![hello_result(1, &["replay"])], false)],
        fast_backoff(),
    );
    let exit = timeout(Duration::from_secs(5), h.task)
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(exit, SupervisorExit::Fatal(info) if info.code == "E_PROTOCOL"));
}

#[tokio::test]
async fn attach_reconnects_with_backoff() {
    let scripts = vec![
        script(vec![hello_result(1, &["view", "replay"])], true),
        script(vec![hello_result(1, &["view", "replay"])], false),
    ];
    let mut h = harness(Mode::Attach, scripts, fast_backoff());
    let _first = h.next_commands().await;
    h.wait_for_connected().await;
    let _second = h.next_commands().await;
    h.wait_for_connected().await;
    assert!(h.connector.attempts() >= 2);
    assert!(matches!(h.shutdown().await, SupervisorExit::Closed));
}

#[tokio::test]
async fn f5_cancels_backoff() {
    let scripts = vec![
        script(vec![hello_result(1, &["view", "replay"])], true),
        script(vec![hello_result(1, &["view", "replay"])], false),
    ];
    let backoff = BackoffConfig {
        initial: Duration::from_secs(5),
        max: Duration::from_secs(5),
    };
    let mut h = harness(Mode::Attach, scripts, backoff);
    let _first = h.next_commands().await;
    h.wait_for_connected().await;
    h.wait_for_disconnected().await;
    h.control_tx
        .send(SupervisorControl::ReconnectNow)
        .await
        .unwrap();
    let _second = timeout(Duration::from_secs(2), h.next_commands())
        .await
        .unwrap();
    h.wait_for_connected().await;
    assert_eq!(h.connector.attempts(), 2);
    assert!(matches!(h.shutdown().await, SupervisorExit::Closed));
}

#[tokio::test]
async fn bye_server_shutdown_reconnects() {
    let scripts = vec![
        script(
            vec![
                hello_result(1, &["view", "replay"]),
                bye_frame("server shutdown"),
            ],
            true,
        ),
        script(vec![hello_result(1, &["view", "replay"])], false),
    ];
    let mut h = harness(Mode::Attach, scripts, fast_backoff());
    let _first = h.next_commands().await;
    h.wait_for_connected().await;
    let _second = timeout(Duration::from_secs(5), h.next_commands())
        .await
        .unwrap();
    assert!(h.connector.attempts() >= 2);
    assert!(matches!(h.shutdown().await, SupervisorExit::Closed));
}

#[tokio::test]
async fn bye_session_deleted_is_forwarded() {
    let mut h = harness(
        Mode::Attach,
        vec![script(
            vec![
                hello_result(1, &["view", "replay"]),
                bye_frame("session deleted"),
            ],
            false,
        )],
        fast_backoff(),
    );
    let _first = h.next_commands().await;
    let mut connected = false;
    let mut saw_bye = false;
    while !(connected && saw_bye) {
        match timeout(Duration::from_secs(5), h.input_rx.recv())
            .await
            .unwrap()
            .unwrap()
        {
            Input::Connection(ConnectionEvent::Connected { .. }) => connected = true,
            Input::Frame(frame) if frame.frame == "bye" => {
                assert_eq!(frame.payload["reason"], "session deleted");
                saw_bye = true;
            }
            _ => {}
        }
    }
    assert_eq!(h.connector.attempts(), 1);
    assert!(matches!(h.shutdown().await, SupervisorExit::Closed));
}

#[tokio::test]
async fn spawn_child_exit_is_fatal_without_auto_respawn() {
    let mut h = harness(
        Mode::Spawn,
        vec![script(vec![hello_result(1, &["view", "replay"])], true)],
        fast_backoff(),
    );
    let _first = h.next_commands().await;
    h.wait_for_connected().await;
    let exit = timeout(Duration::from_secs(5), h.task)
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(exit, SupervisorExit::ChildExited { .. }));
    assert_eq!(h.connector.attempts(), 1);
}

#[test]
fn backoff_grows_and_caps() {
    let config = BackoffConfig {
        initial: Duration::from_millis(10),
        max: Duration::from_millis(50),
    };
    let mut backoff = Backoff::new(config.clone());
    let first = backoff.next_delay();
    let second = backoff.next_delay();
    let third = backoff.next_delay();
    assert!(first >= Duration::from_millis(10));
    assert!(second >= first);
    assert!(third <= Duration::from_millis(60));
    backoff.reset();
    assert!(backoff.next_delay() <= Duration::from_millis(15));
}

#[tokio::test]
async fn shutdown_interrupts_inflight_command() {
    let mut h = harness(
        Mode::Attach,
        vec![script(vec![hello_result(1, &["view", "replay"])], false)],
        fast_backoff(),
    );
    h.wait_for_connected().await;
    h._command_tx
        .send(CommandSpec {
            name: "user.message".into(),
            args: Default::default(),
            session_id: Some("s1".into()),
        })
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(20)).await;
    h.control_tx
        .send(SupervisorControl::Shutdown)
        .await
        .unwrap();
    let exit = timeout(Duration::from_secs(2), h.task)
        .await
        .expect("shutdown did not interrupt the in-flight command")
        .unwrap();
    assert!(matches!(exit, SupervisorExit::Closed));
}
