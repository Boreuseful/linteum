#![cfg(unix)]

use std::path::PathBuf;
use std::time::Duration;

use linteum_tui::connection::{CommandBroker, SpawnConfig, resolve_linteum_bin, spawn_stdio};
use linteum_tui::viewmodel::{CommandSpec, Input};
use serde_json::json;
use tempfile::tempdir;
use tokio::sync::mpsc;
use tokio::time::timeout;

fn spec(name: &str, args: serde_json::Value, session_id: Option<String>) -> CommandSpec {
    CommandSpec {
        name: name.into(),
        args: args.as_object().unwrap().clone(),
        session_id,
    }
}

fn write_config(dir: &std::path::Path) -> PathBuf {
    let config = format!(
        "version: 1\nstorage:\n  state_db: {}\n  artifacts: {}\nlogging:\n  dir: {}\n",
        dir.join("state.db").display(),
        dir.join("artifacts").display(),
        dir.join("logs").display()
    );
    let path = dir.join("config.yaml");
    std::fs::write(&path, config).unwrap();
    path
}

#[tokio::test]
#[ignore = "requires the linteum CLI"]
async fn spawn_handshake_create_and_subscribe() {
    let dir = tempdir().unwrap();
    let workspace = dir.path().join("ws");
    std::fs::create_dir_all(&workspace).unwrap();
    let config_path = write_config(dir.path());
    let bin = resolve_linteum_bin(None, None)
        .or_else(|_| resolve_linteum_bin(Some(PathBuf::from("/root/.local/bin/linteum")), None));
    let Ok(bin) = bin else {
        eprintln!("skipping: linteum CLI not found");
        return;
    };

    let transport = spawn_stdio(SpawnConfig {
        linteum_bin: bin,
        config_path: Some(config_path),
        no_lease: true,
        cwd: Some(workspace.clone()),
        stderr_log: dir.path().join("gateway.log"),
        teardown_grace: Duration::from_secs(5),
        term_grace: Duration::from_secs(2),
    })
    .await
    .unwrap();

    let (broker, handle) = CommandBroker::new(Box::new(transport), Duration::from_secs(30));
    let (input_tx, mut input_rx) = mpsc::channel(256);
    let task = tokio::spawn(broker.run(input_tx));

    let hello = handle
        .send(spec(
            "hello",
            json!({
                "protocol": 1,
                "client": {"name": "linteum-tui-test"},
                "features": ["view", "replay"]
            }),
            None,
        ))
        .await
        .unwrap();
    assert_eq!(hello.result["protocol"], 1);

    let created = handle
        .send(spec(
            "session.create",
            json!({
                "workspace": workspace.to_string_lossy(),
                "session_id": "s1",
                "title": "test"
            }),
            None,
        ))
        .await
        .unwrap();
    assert!(created.ok, "create failed: {:?}", created.error);

    let subscribed = handle
        .send(spec(
            "session.subscribe",
            json!({"session_id": "s1", "after_seq": 0, "mode": "view"}),
            Some("s1".into()),
        ))
        .await
        .unwrap();
    assert!(subscribed.ok, "subscribe failed: {:?}", subscribed.error);

    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let mut saw_snapshot = false;
    while !saw_snapshot {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        let input = timeout(remaining, input_rx.recv())
            .await
            .expect("snapshot timeout")
            .expect("broker closed");
        if let Input::Frame(frame) = input
            && frame.frame == "snapshot"
        {
            saw_snapshot = true;
        }
    }

    drop(handle);
    let _ = timeout(Duration::from_secs(10), task).await;
}
