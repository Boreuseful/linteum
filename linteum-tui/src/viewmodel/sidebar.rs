//! Right sidebar: workspace tree state and panel layout.

use std::collections::{HashMap, HashSet};
use std::path::Path;

/// A directory entry reported by the host.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkspaceEntry {
    pub name: String,
    pub path: String,
    pub is_dir: bool,
}

/// One visible row in the file tree.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TreeRow {
    pub name: String,
    pub path: String,
    pub depth: usize,
    pub is_dir: bool,
    pub expanded: bool,
}

/// How the sidebar lays out its panels.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SidebarLayout {
    #[default]
    Merged,
    Split,
}

/// Which panel fills the merged sidebar.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum SidebarPanel {
    #[default]
    Files,
    Preview,
    Info,
}

impl SidebarPanel {
    pub fn title(self) -> &'static str {
        match self {
            Self::Files => "Files",
            Self::Preview => "Preview",
            Self::Info => "Info",
        }
    }
}

/// The right sidebar. Closed by default.
#[derive(Debug, Clone, Default)]
pub struct SidebarState {
    pub visible: bool,
    pub layout: SidebarLayout,
    pub panel: SidebarPanel,
    pub root: Option<String>,
    /// Children keyed by directory path.
    pub loaded: HashMap<String, Vec<WorkspaceEntry>>,
    pub expanded: HashSet<String>,
    pub pending: HashSet<String>,
    pub rows: Vec<TreeRow>,
    pub selected: usize,
    pub error: Option<String>,
}

impl SidebarState {
    /// Open the sidebar against `root`, resetting tree state when the root
    /// changes. Returns a directory that still needs to be read.
    pub fn open(&mut self, root: Option<String>) -> Option<String> {
        self.visible = true;
        if self.root != root {
            self.loaded.clear();
            self.expanded.clear();
            self.pending.clear();
            self.rows.clear();
            self.selected = 0;
            self.error = None;
            if let Some(root) = &root {
                self.expanded.insert(root.clone());
            }
            self.root = root;
        }
        self.rebuild();
        self.request_root()
    }

    /// Ensure the workspace root is loaded. Returns a read request.
    pub fn request_root(&mut self) -> Option<String> {
        let root = self.root.clone()?;
        if self.loaded.contains_key(&root) || self.pending.contains(&root) {
            return None;
        }
        self.pending.insert(root.clone());
        self.rebuild();
        Some(root)
    }

    /// Apply one directory read result.
    pub fn apply_dir(&mut self, path: &str, entries: Vec<WorkspaceEntry>, error: Option<String>) {
        self.pending.remove(path);
        if let Some(error) = error {
            self.error = Some(error);
            return;
        }
        self.error = None;
        let mut entries = entries;
        entries.sort_by(|a, b| {
            b.is_dir
                .cmp(&a.is_dir)
                .then_with(|| {
                    a.name
                        .to_ascii_lowercase()
                        .cmp(&b.name.to_ascii_lowercase())
                })
                .then_with(|| a.name.cmp(&b.name))
        });
        self.loaded.insert(path.to_owned(), entries);
        self.rebuild();
    }

    /// Rebuild the visible rows from the loaded directories.
    pub fn rebuild(&mut self) {
        self.rows.clear();
        let Some(root) = self.root.clone() else {
            self.selected = 0;
            return;
        };
        let name = Path::new(&root)
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| root.clone());
        let expanded = self.expanded.contains(&root);
        self.rows.push(TreeRow {
            name,
            path: root.clone(),
            depth: 0,
            is_dir: true,
            expanded,
        });
        if expanded {
            self.push_children(&root, 1);
        }
        self.selected = self.selected.min(self.rows.len().saturating_sub(1));
    }

    fn push_children(&mut self, parent: &str, depth: usize) {
        let Some(entries) = self.loaded.get(parent).cloned() else {
            return;
        };
        for entry in entries {
            let expanded = entry.is_dir && self.expanded.contains(&entry.path);
            self.rows.push(TreeRow {
                name: entry.name,
                path: entry.path.clone(),
                depth,
                is_dir: entry.is_dir,
                expanded,
            });
            if expanded {
                self.push_children(&entry.path, depth + 1);
            }
        }
    }

    pub fn selected_row(&self) -> Option<&TreeRow> {
        self.rows.get(self.selected)
    }

    pub fn select_next(&mut self) {
        if !self.rows.is_empty() {
            self.selected = (self.selected + 1).min(self.rows.len() - 1);
        }
    }

    pub fn select_previous(&mut self) {
        self.selected = self.selected.saturating_sub(1);
    }

    pub fn select_first(&mut self) {
        self.selected = 0;
    }

    pub fn select_last(&mut self) {
        self.selected = self.rows.len().saturating_sub(1);
    }

    /// Expand the selected directory, returning its path when it must be read.
    pub fn expand_selected(&mut self) -> Option<String> {
        let row = self.selected_row().cloned()?;
        if !row.is_dir {
            return None;
        }
        self.expanded.insert(row.path.clone());
        self.rebuild();
        if self.loaded.contains_key(&row.path) || self.pending.contains(&row.path) {
            None
        } else {
            self.pending.insert(row.path.clone());
            Some(row.path)
        }
    }

    /// Collapse the selected directory. Returns whether it was expanded.
    pub fn collapse_selected(&mut self) -> bool {
        let Some(row) = self.selected_row().cloned() else {
            return false;
        };
        if !row.is_dir {
            return false;
        }
        let changed = self.expanded.remove(&row.path);
        self.rebuild();
        changed
    }

    /// Toggle the selected directory, returning its path when it must be read.
    pub fn toggle_selected(&mut self) -> Option<String> {
        let row = self.selected_row().cloned()?;
        if !row.is_dir {
            return None;
        }
        if row.expanded {
            self.collapse_selected();
            None
        } else {
            self.expand_selected()
        }
    }
}
