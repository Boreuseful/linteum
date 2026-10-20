//! Spawned `linteum serve --stdio` child transport.

use std::collections::VecDeque;
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::{ExitStatus, Stdio};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command};
use tokio::sync::Mutex as AsyncMutex;

use super::{Transport, TransportError, TransportReader, TransportWriter};

const MAX_LINE_BYTES: usize = 4 * 1024 * 1024;
const STDERR_TAIL_LINES: usize = 200;

#[derive(Debug, Clone)]
pub struct SpawnConfig {
    pub linteum_bin: PathBuf,
    pub config_path: Option<PathBuf>,
    pub no_lease: bool,
    pub cwd: Option<PathBuf>,
    pub stderr_log: PathBuf,
    pub teardown_grace: Duration,
    pub term_grace: Duration,
}

#[derive(Debug, thiserror::Error)]
pub enum SpawnError {
    #[error("linteum binary not found")]
    NotFound,
    #[error("io: {0}")]
    Io(String),
    #[error("child exited: {status:?}")]
    Exited {
        status: Option<i32>,
        stderr_tail: Vec<String>,
    },
}

/// Resolve the Python CLI: explicit path, then `LINTEUM_BIN`, then `PATH`.
pub fn resolve_linteum_bin(
    explicit: Option<PathBuf>,
    env: Option<OsString>,
) -> Result<PathBuf, SpawnError> {
    if let Some(path) = explicit {
        return if path.is_file() {
            Ok(path)
        } else {
            Err(SpawnError::NotFound)
        };
    }
    if let Some(path) = env {
        let path = PathBuf::from(path);
        return if path.is_file() {
            Ok(path)
        } else {
            Err(SpawnError::NotFound)
        };
    }
    if let Some(paths) = std::env::var_os("PATH") {
        for directory in std::env::split_paths(&paths) {
            let candidate = directory.join("linteum");
            if candidate.is_file() {
                return Ok(candidate);
            }
        }
    }
    Err(SpawnError::NotFound)
}

pub async fn spawn_stdio(config: SpawnConfig) -> Result<StdioChildTransport, SpawnError> {
    let mut command = Command::new(&config.linteum_bin);
    command.arg("serve").arg("--stdio");
    if let Some(path) = &config.config_path {
        command.arg("--config").arg(path);
    }
    if config.no_lease {
        command.arg("--no-lease");
    }
    if let Some(cwd) = &config.cwd {
        command.current_dir(cwd);
    }
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    tracing::info!(binary = ?config.linteum_bin, "spawning gateway child");
    let mut child = command
        .spawn()
        .map_err(|error| SpawnError::Io(error.to_string()))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| SpawnError::Io("child stdin unavailable".into()))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| SpawnError::Io("child stdout unavailable".into()))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| SpawnError::Io("child stderr unavailable".into()))?;

    if let Some(parent) = config.stderr_log.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let tail = Arc::new(Mutex::new(VecDeque::new()));
    spawn_stderr_task(stderr, config.stderr_log, tail.clone());

    Ok(StdioChildTransport {
        stdin: Some(stdin),
        stdout: BufReader::new(stdout),
        child,
        stderr_tail: tail,
        teardown_grace: config.teardown_grace,
        term_grace: config.term_grace,
    })
}

pub struct StdioChildTransport {
    stdin: Option<ChildStdin>,
    stdout: BufReader<ChildStdout>,
    child: Child,
    stderr_tail: Arc<Mutex<VecDeque<String>>>,
    teardown_grace: Duration,
    term_grace: Duration,
}

impl StdioChildTransport {
    pub fn stderr_tail(&self) -> Vec<String> {
        self.stderr_tail
            .lock()
            .map(|tail| tail.iter().cloned().collect())
            .unwrap_or_default()
    }

    pub async fn wait_exit(&mut self) -> Option<ExitStatus> {
        self.child.wait().await.ok()
    }
}

impl Transport for StdioChildTransport {
    fn split(self: Box<Self>) -> (Box<dyn TransportReader>, Box<dyn TransportWriter>) {
        let child = Arc::new(AsyncMutex::new(self.child));
        (
            Box::new(StdioChildReader {
                stdout: self.stdout,
            }),
            Box::new(StdioChildWriter {
                stdin: self.stdin,
                child,
                teardown_grace: self.teardown_grace,
                term_grace: self.term_grace,
            }),
        )
    }
}

struct StdioChildReader {
    stdout: BufReader<ChildStdout>,
}

#[async_trait]
impl TransportReader for StdioChildReader {
    async fn recv_text(&mut self) -> Option<Result<String, TransportError>> {
        let mut line = String::new();
        match self.stdout.read_line(&mut line).await {
            Ok(0) => None,
            Ok(size) => {
                if size > MAX_LINE_BYTES {
                    return Some(Err(TransportError::Protocol(
                        "stdio line exceeds 4 MiB".into(),
                    )));
                }
                while line.ends_with('\n') || line.ends_with('\r') {
                    line.pop();
                }
                Some(Ok(line))
            }
            Err(error) => Some(Err(TransportError::Io(error.to_string()))),
        }
    }
}

struct StdioChildWriter {
    stdin: Option<ChildStdin>,
    child: Arc<AsyncMutex<Child>>,
    teardown_grace: Duration,
    term_grace: Duration,
}

#[async_trait]
impl TransportWriter for StdioChildWriter {
    async fn send_text(&mut self, text: String) -> Result<(), TransportError> {
        let stdin = self.stdin.as_mut().ok_or(TransportError::Closed)?;
        stdin
            .write_all(text.as_bytes())
            .await
            .map_err(|error| TransportError::Io(error.to_string()))?;
        stdin
            .write_all(b"\n")
            .await
            .map_err(|error| TransportError::Io(error.to_string()))?;
        stdin
            .flush()
            .await
            .map_err(|error| TransportError::Io(error.to_string()))?;
        Ok(())
    }

    async fn close(&mut self) -> Result<(), TransportError> {
        tracing::info!("stopping gateway child");
        // EOF on stdin lets a well-behaved server shut down on its own.
        self.stdin.take();
        tokio::time::sleep(self.teardown_grace).await;

        let mut child = self.child.lock().await;
        #[cfg(unix)]
        if let Some(pid) = child.id() {
            // Safety: the pid belongs to a child we spawned and have not
            // reaped; SIGTERM is an asynchronous request, not memory access.
            unsafe {
                libc::kill(pid as i32, libc::SIGTERM);
            }
        }
        tokio::time::sleep(self.term_grace).await;
        if child.try_wait().ok().flatten().is_none() {
            let _ = child.start_kill();
        }
        let _ = child.wait().await;
        Ok(())
    }
}

fn spawn_stderr_task(stderr: ChildStderr, log_path: PathBuf, tail: Arc<Mutex<VecDeque<String>>>) {
    tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        let mut file = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .await
            .ok();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(file) = file.as_mut() {
                let _ = file.write_all(line.as_bytes()).await;
                let _ = file.write_all(b"\n").await;
                let _ = file.flush().await;
            }
            if let Ok(mut tail) = tail.lock() {
                tail.push_back(line);
                while tail.len() > STDERR_TAIL_LINES {
                    tail.pop_front();
                }
            }
        }
    });
}
