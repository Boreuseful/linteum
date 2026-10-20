//! Host-side workspace directory reads for the sidebar.
//!
//! The TUI can only see the workspace directly when it runs on the same host
//! as the gateway; attach mode returns the OS error in the sidebar.

use crate::viewmodel::WorkspaceEntry;

pub async fn read_dir(path: &str) -> (Vec<WorkspaceEntry>, Option<String>) {
    let mut dir = match tokio::fs::read_dir(path).await {
        Ok(dir) => dir,
        Err(error) => return (Vec::new(), Some(error.to_string())),
    };
    let mut entries = Vec::new();
    loop {
        match dir.next_entry().await {
            Ok(Some(entry)) => {
                let file_type = match entry.file_type().await {
                    Ok(file_type) => file_type,
                    Err(_) => continue,
                };
                entries.push(WorkspaceEntry {
                    name: entry.file_name().to_string_lossy().into_owned(),
                    path: entry.path().to_string_lossy().into_owned(),
                    is_dir: file_type.is_dir(),
                });
            }
            Ok(None) => break,
            Err(error) => return (entries, Some(error.to_string())),
        }
    }
    (entries, None)
}
