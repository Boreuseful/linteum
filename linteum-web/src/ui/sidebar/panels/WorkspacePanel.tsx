import { ArrowUp, FileText, Folder, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useStrings } from "../../../hooks/useStrings";
import { store, useAppState } from "../../../hooks/useStore";
import { workspaceProvider, type WorkspaceEntry } from "../../../workspace";
import { PanelEmpty, PanelLoading } from "../../common/EmptyState";
import type { PanelProps } from "../types";

export function WorkspacePanel(_props: PanelProps) {
  const strings = useStrings();
  const state = useAppState();
  const active = state.active;
  const open = state.ui.openPanels.workspace === true;
  const root = active?.info?.workspace ?? "";
  const available = workspaceProvider.isAvailable() && root !== "";

  const [current, setCurrent] = useState(root);
  const [stack, setStack] = useState<string[]>([]);
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (path: string) => {
    setLoading(true);
    setError(null);
    const result = await workspaceProvider.list(root, path);
    setEntries(result.entries);
    setError(result.error ?? null);
    setLoading(false);
  };

  useEffect(() => {
    if (!open || !available) return;
    let cancelled = false;
    setCurrent(root);
    setStack([]);
    setLoading(true);
    setError(null);
    void workspaceProvider.list(root, root).then((result) => {
      if (cancelled) return;
      setEntries(result.entries);
      setError(result.error ?? null);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [open, root, available]);

  if (!active) return <PanelEmpty>{strings.sidebar.empty}</PanelEmpty>;
  if (!root) return <PanelEmpty>{strings.sidebar.noWorkspace}</PanelEmpty>;
  if (!available) {
    return <PanelEmpty>{strings.sidebar.workspaceUnavailable}</PanelEmpty>;
  }

  const relative = (path: string): string => {
    if (path === root) return ".";
    if (path.startsWith(`${root}/`)) return path.slice(root.length + 1);
    return path;
  };

  const enter = (entry: WorkspaceEntry) => {
    setStack((previous) => [...previous, current]);
    setCurrent(entry.path);
    void load(entry.path);
  };

  const up = () => {
    if (stack.length === 0) return;
    const parent = stack[stack.length - 1];
    setStack((previous) => previous.slice(0, -1));
    setCurrent(parent);
    void load(parent);
  };

  const openFile = (entry: WorkspaceEntry) => {
    // The file sidebar owns the content and keeps one tab per path.
    store.dispatch({
      type: "file/open",
      root,
      path: entry.path,
      name: entry.name,
    });
  };

  return (
    <>
      <div className="row" style={{ gap: 4 }}>
        <button
          className="icon-btn"
          onClick={up}
          disabled={stack.length === 0}
          title={strings.panel.up}
          aria-label={strings.panel.up}
        >
          <ArrowUp size={15} />
        </button>
        <span className="tree-path grow truncate" title={current}>
          {relative(current)}
        </span>
        <button
          className="icon-btn"
          onClick={() => void load(current)}
          title={strings.sidebar.refresh}
          aria-label={strings.sidebar.refresh}
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {loading ? <PanelLoading label={strings.sidebar.loading} /> : null}
      {!loading && error ? <PanelEmpty>{error}</PanelEmpty> : null}
      {!loading && !error && entries.length === 0 ? (
        <PanelEmpty>{strings.sidebar.workspaceEmpty}</PanelEmpty>
      ) : null}

      {!loading && !error && entries.length > 0 ? (
        <div className="tree">
          {entries.map((entry) => (
            <button
              key={entry.path}
              className={`tree-item${
                state.ui.filePaneOpen && state.ui.activeFilePath === entry.path
                  ? " active"
                  : ""
              }`}
              onClick={() => (entry.isDir ? enter(entry) : openFile(entry))}
              title={entry.path}
            >
              {entry.isDir ? <Folder size={14} /> : <FileText size={14} />}
              <span className="truncate">{entry.name}</span>
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}
