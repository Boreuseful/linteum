#![cfg(unix)]

use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;

use linteum_tui::connection::{CommandBroker, connect_websocket, resolve_linteum_bin};
use linteum_tui::viewmodel::CommandSpec;
use serde_json::json;
use tempfile::tempdir;
use tokio::sync::mpsc;
use tokio::time::timeout;

fn spec(name: &str, args: serde_json::Value) -> CommandSpec {
    CommandSpec {
        name: name.into(),
        args: args.as_object().unwrap().clone(),
        session_id: None,
    }
}

#[tokio::test]
#[ignore = "requires the linteum CLI and the websockets extra"]
async fn attach_handshake_and_list_sessions() {
    let dir = tempdir().unwrap();
    let config = format!(
        "version: 1\nstorage:\n  state_db: {}\n  artifacts: {}\nlogging:\n  dir: {}\n",
        dir.path().join("state.db").display(),
        dir.path().join("artifacts").display(),
        dir.path().join("logs").display()
    );
    let config_path = dir.path().join("config.yaml");
    std::fs::write(&config_path, config).unwrap();

    let bin = resolve_linteum_bin(None, None)
        .or_else(|_| resolve_linteum_bin(Some(PathBuf::from("/root/.local/bin/linteum")), None));
    let Ok(bin) = bin else {
        eprintln!("skipping: linteum CLI not found");
        return;
    };

    let port = {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.local_addr().unwrap().port()
    };
    let mut server = tokio::process::Command::new(bin)
        .args([
            "serve",
            "--ws",
            "--host",
            "127.0.0.1",
            "--port",
            &port.to_string(),
            "--config",
            &config_path.to_string_lossy(),
            "--no-lease",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();

    let mut ready = false;
    for _ in 0..50 {
        if tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .is_ok()
        {
            ready = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ready, "gateway did not open port {port}");

    let transport = connect_websocket(&format!("ws://127.0.0.1:{port}/"))
        .await
        .unwrap();
    let (broker, handle) = CommandBroker::new(Box::new(transport), Duration::from_secs(30));
    let (input_tx, _input_rx) = mpsc::channel(256);
    let task = tokio::spawn(broker.run(input_tx));

    let hello = handle
        .send(spec(
            "hello",
            json!({
                "protocol": 1,
                "client": {"name": "linteum-tui-test"},
                "features": ["view", "replay"]
            }),
        ))
        .await
        .unwrap();
    assert_eq!(hello.result["protocol"], 1);

    let list = handle
        .send(spec("session.list", json!({"limit": 50, "offset": 0})))
        .await
        .unwrap();
    assert!(list.ok, "session.list failed: {:?}", list.error);

    drop(handle);
    let _ = timeout(Duration::from_secs(5), task).await;
    let _ = server.kill().await;
    let _ = server.wait().await;
}
