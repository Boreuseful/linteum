import {
  ChevronDown,
  ChevronRight,
  FileText,
  Folder,
  FolderOpen,
  RefreshCw,
} from "lucide-react";
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useStrings } from "../../hooks/useStrings";
import {
  workspaceProvider,
  type WorkspaceEntry,
  type WorkspaceFileEntry,
} from "../../workspace";
import { PanelEmpty, PanelLoading } from "../common/EmptyState";
import { filterFileEntries } from "./filter";

interface DirState {
  entries: WorkspaceEntry[];
  loaded: boolean;
  loading: boolean;
  error: string | null;
}

interface FileExplorerProps {
  root: string;
  /** Focus the filter when the picker becomes visible. */
  visible: boolean;
  activePath: string | null;
  /** Flat recursive listing backing the count and the search results. */
  files: readonly WorkspaceFileEntry[];
  filesLoading: boolean;
  filesError: string | null;
  filesTruncated: boolean;
  onOpen: (entry: { path: string; name: string }) => void;
  /** Reload the flat listing (the count) alongside the tree. */
  onRefresh: () => void;
}

function branchIndent(depth: number) {
  return { paddingLeft: 6 + depth * 14 };
}

interface TreeBranchProps {
  dirPath: string;
  depth: number;
  dirs: Record<string, DirState>;
  expanded: ReadonlySet<string>;
  activePath: string | null;
  onToggle: (entry: WorkspaceEntry) => void;
  onRetry: (path: string) => void;
  onOpen: (entry: { path: string; name: string }) => void;
}

/** One directory level, rendered recursively as a VS Code-style outline. */
function TreeBranch({
  dirPath,
  depth,
  dirs,
  expanded,
  activePath,
  onToggle,
  onRetry,
  onOpen,
}: TreeBranchProps) {
  const strings = useStrings();
  const state = dirs[dirPath];
  if (!state) return null;
  const indent = branchIndent(depth);

  return (
    <>
      {state.loading && state.entries.length === 0 ? (
        <div className="file-tree-note" style={indent}>
          {strings.sidebar.loading}
        </div>
      ) : null}
      {state.error ? (
        <div className="file-tree-note error" style={indent}>
          <span className="truncate" title={state.error}>
            {state.error}
          </span>
          <button
            className="btn btn-sm"
            onClick={() => onRetry(dirPath)}
            title={strings.sidebar.refresh}
          >
            {strings.overlays.retry}
          </button>
        </div>
      ) : null}
      {!state.loading && !state.error && state.loaded && state.entries.length === 0 ? (
        <div className="file-tree-note" style={indent}>
          {strings.files.emptyFolder}
        </div>
      ) : null}

      {state.entries.map((entry) => {
        const open = expanded.has(entry.path);
        return (
          <Fragment key={entry.path}>
            <button
              className={`file-tree-item${
                !entry.isDir && activePath === entry.path ? " active" : ""
              }`}
              style={indent}
              onClick={() => (entry.isDir ? onToggle(entry) : onOpen(entry))}
              title={entry.path}
              role="treeitem"
              aria-level={depth + 1}
              aria-expanded={entry.isDir ? open : undefined}
              aria-selected={!entry.isDir ? activePath === entry.path : undefined}
            >
              {entry.isDir ? (
                open ? (
                  <ChevronDown size={13} />
                ) : (
                  <ChevronRight size={13} />
                )
              ) : (
                <span className="file-tree-chevron" aria-hidden="true" />
              )}
              {entry.isDir ? (
                open ? (
                  <FolderOpen size={14} />
                ) : (
                  <Folder size={14} />
                )
              ) : (
                <FileText size={14} />
              )}
              <span className="truncate">{entry.name}</span>
            </button>
            {entry.isDir && open ? (
              <TreeBranch
                dirPath={entry.path}
                depth={depth + 1}
                dirs={dirs}
                expanded={expanded}
                activePath={activePath}
                onToggle={onToggle}
                onRetry={onRetry}
                onOpen={onOpen}
              />
            ) : null}
          </Fragment>
        );
      })}
    </>
  );
}

/**
 * Explorer for the file sidebar: directories expand in place, files open as
 * a preview tab. Typing in the filter switches to a flat recursive search,
 * matching how VS Code's explorer doubles as its file search.
 */
export function FileExplorer({
  root,
  visible,
  activePath,
  files,
  filesLoading,
  filesError,
  filesTruncated,
  onOpen,
  onRefresh,
}: FileExplorerProps) {
  const strings = useStrings();
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [dirs, setDirs] = useState<Record<string, DirState>>({});
  const [reloadKey, setReloadKey] = useState(0);
  const epoch = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const loadDir = useCallback(
    async (path: string) => {
      if (!workspaceProvider.isAvailable()) return;
      const token = epoch.current;
      setDirs((previous) => ({
        ...previous,
        [path]: {
          entries: previous[path]?.entries ?? [],
          loaded: previous[path]?.loaded ?? false,
          loading: true,
          error: null,
        },
      }));
      const result = await workspaceProvider.list(root, path);
      if (token !== epoch.current) return;
      setDirs((previous) => ({
        ...previous,
        [path]: {
          entries: result.entries,
          loaded: true,
          loading: false,
          error: result.error ?? null,
        },
      }));
    },
    [root],
  );

  useEffect(() => {
    epoch.current += 1;
    setExpanded(new Set());
    setDirs({});
    if (root) void loadDir(root);
  }, [root, reloadKey, loadDir]);

  useEffect(() => {
    if (visible) inputRef.current?.focus();
  }, [visible]);

  const toggleDir = (entry: WorkspaceEntry) => {
    const willExpand = !expanded.has(entry.path);
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(entry.path)) next.delete(entry.path);
      else next.add(entry.path);
      return next;
    });
    const state = dirs[entry.path];
    if (willExpand && !state?.loading && !state?.loaded) {
      void loadDir(entry.path);
    }
  };

  const retryDir = (path: string) => {
    void loadDir(path);
  };

  const refresh = () => {
    setReloadKey((key) => key + 1);
    onRefresh();
  };

  const searching = query.trim().length > 0;
  const results = useMemo(
    () => filterFileEntries(files, query),
    [files, query],
  );

  if (!root) {
    return <PanelEmpty>{strings.sidebar.noWorkspace}</PanelEmpty>;
  }
  if (!workspaceProvider.isAvailable()) {
    return <PanelEmpty>{strings.sidebar.workspaceUnavailable}</PanelEmpty>;
  }

  return (
    <>
      <div className="file-picker-toolbar">
        <input
          ref={inputRef}
          className="input file-picker-filter"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={strings.files.filter}
          aria-label={strings.files.filter}
        />
        <button
          className="icon-btn"
          onClick={refresh}
          title={strings.files.refreshList}
          aria-label={strings.files.refreshList}
        >
          <RefreshCw size={14} />
        </button>
      </div>

      {searching ? (
        <>
          {filesLoading && results.length === 0 ? (
            <PanelLoading label={strings.sidebar.loading} />
          ) : null}
          {filesError ? <PanelEmpty>{filesError}</PanelEmpty> : null}
          {!filesError && !filesLoading && results.length === 0 ? (
            <PanelEmpty>{strings.files.noFiles}</PanelEmpty>
          ) : null}
          {results.length > 0 ? (
            <div className="file-picker-list" role="listbox">
              {results.map((entry) => (
                <button
                  key={entry.path}
                  className="file-picker-item"
                  role="option"
                  aria-selected={activePath === entry.path}
                  onClick={() => onOpen(entry)}
                  title={entry.path}
                >
                  <FileText size={13} />
                  <span className="truncate">{entry.relative}</span>
                </button>
              ))}
            </div>
          ) : null}
          {filesTruncated ? (
            <div className="file-picker-note">{strings.files.truncated}</div>
          ) : null}
        </>
      ) : (
        <div
          className="file-tree"
          role="tree"
          aria-label={strings.files.browse}
        >
          <TreeBranch
            dirPath={root}
            depth={0}
            dirs={dirs}
            expanded={expanded}
            activePath={activePath}
            onToggle={toggleDir}
            onRetry={retryDir}
            onOpen={onOpen}
          />
        </div>
      )}
    </>
  );
}
