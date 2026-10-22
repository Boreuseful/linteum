#![cfg(unix)]

use std::ffi::OsString;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use linteum_tui::connection::{
    SpawnConfig, SpawnError, Transport, resolve_linteum_bin, spawn_stdio,
};
use tempfile::tempdir;

fn script(dir: &Path, name: &str, body: &str) -> PathBuf {
    let path = dir.join(name);
    std::fs::write(&path, body).unwrap();
    let mut permissions = std::fs::metadata(&path).unwrap().permissions();
    permissions.set_mode(0o755);
    std::fs::set_permissions(&path, permissions).unwrap();
    // Parallel tests on overlayfs can briefly return ETXTBSY when exec'ing a
    // just-written script. Let the write handle settle before spawn.
    std::thread::sleep(Duration::from_millis(20));
    path
}

fn config(bin: PathBuf, stderr_log: PathBuf) -> SpawnConfig {
    SpawnConfig {
        linteum_bin: bin,
        config_path: None,
        no_lease: true,
        cwd: None,
        stderr_log,
        teardown_grace: Duration::from_millis(50),
        term_grace: Duration::from_millis(50),
    }
}

#[test]
fn resolve_prefers_explicit_then_env_then_path() {
    let explicit = PathBuf::from("/bin/echo");
    assert_eq!(
        resolve_linteum_bin(Some(explicit.clone()), Some(OsString::from("/no"))).unwrap(),
        explicit
    );
}

#[test]
fn missing_binary_is_an_error() {
    assert!(matches!(
        resolve_linteum_bin(Some(PathBuf::from("/definitely/missing")), None),
        Err(SpawnError::NotFound)
    ));
}

#[tokio::test]
async fn spawn_reads_jsonl_and_writes_jsonl() {
    let dir = tempdir().unwrap();
    let bin = script(
        dir.path(),
        "echo.sh",
        "#!/bin/sh\nwhile IFS= read -r line; do printf '%s\\n' '{\"v\":1,\"kind\":\"result\",\"id\":\"r\",\"reply_to\":\"c-1\",\"ok\":true,\"result\":{}}'; done\n",
    );
    let transport = spawn_stdio(config(bin, dir.path().join("gw.log")))
        .await
        .unwrap();
    let (mut reader, mut writer) = Box::new(transport).split();
    writer
        .send_text(r#"{"v":1,"kind":"cmd","id":"c-1","name":"hello"}"#.into())
        .await
        .unwrap();
    let line = reader.recv_text().await.unwrap().unwrap();
    assert!(line.contains("\"reply_to\":\"c-1\""));
    writer.close().await.unwrap();
}

#[tokio::test]
async fn stderr_is_captured_to_log_and_tail() {
    let dir = tempdir().unwrap();
    let log = dir.path().join("gw.log");
    let bin = script(
        dir.path(),
        "stderr.sh",
        "#!/bin/sh\nprintf 'err-line\\n' >&2\nsleep 0.2\n",
    );
    let transport = spawn_stdio(config(bin, log.clone())).await.unwrap();
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        if transport
            .stderr_tail()
            .iter()
            .any(|line| line.contains("err-line"))
        {
            break;
        }
        assert!(Instant::now() < deadline, "stderr tail not populated");
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let contents = std::fs::read_to_string(&log).unwrap();
    assert!(contents.contains("err-line"));
}

#[tokio::test]
async fn teardown_is_bounded() {
    let dir = tempdir().unwrap();
    let bin = script(
        dir.path(),
        "ignore-term.sh",
        "#!/bin/sh\ntrap '' TERM\nwhile true; do sleep 0.05; done\n",
    );
    let transport = spawn_stdio(config(bin, dir.path().join("gw.log")))
        .await
        .unwrap();
    let (_, mut writer) = Box::new(transport).split();
    let started = Instant::now();
    writer.close().await.unwrap();
    assert!(started.elapsed() < Duration::from_secs(2));
}

#[tokio::test]
async fn child_exit_is_reported() {
    let dir = tempdir().unwrap();
    let bin = script(dir.path(), "exit.sh", "#!/bin/sh\nexit 7\n");
    let mut transport = spawn_stdio(config(bin, dir.path().join("gw.log")))
        .await
        .unwrap();
    let status = transport.wait_exit().await.unwrap();
    assert_eq!(status.code(), Some(7));
}
