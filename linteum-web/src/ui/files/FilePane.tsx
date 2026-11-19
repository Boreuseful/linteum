import {
  ArrowLeftRight,
  Files,
  PanelRightClose,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import { store, useAppState } from "../../hooks/useStore";
import {
  workspaceProvider,
  type WorkspaceFileEntry,
} from "../../workspace";
import { PanelEmpty, PanelLoading } from "../common/EmptyState";
import { formatBytes } from "../common/format";
import { FileExplorer } from "./FileExplorer";
import { MarkdownFilePreview } from "./MarkdownFilePreview";
import { PdfPreview } from "./PdfPreview";
import { SvgPreview } from "./SvgPreview";
import { highlightFile } from "./highlight";
import {
  filePreviewKind,
  parseCsv,
  svgDataUrl,
  type CsvTable,
} from "./preview";

interface FileListing {
  root: string;
  files: WorkspaceFileEntry[];
  truncated: boolean;
}

function CsvPreview({
  table,
  truncated,
}: {
  table: CsvTable;
  truncated: boolean;
}) {
  const strings = useStrings();
  return (
    <div className="file-csv">
      <table className="file-csv-table">
        <thead>
          <tr>
            {table.header.map((cell, column) => (
              <th key={column} scope="col">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row, index) => (
            <tr key={index}>
              {table.header.map((_, column) => (
                <td key={column}>{row[column] ?? ""}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {table.truncated || truncated ? (
        <div className="file-picker-note">{strings.files.csvTruncated}</div>
      ) : null}
    </div>
  );
}

/**
 * Independent right-hand file sidebar: one tab per opened workspace file,
 * close to VS Code. The browse button opens an explorer that expands workspace
 * folders in place; SVG, PDF and CSV files get dedicated previews.
 */
export function FilePane() {
  const strings = useStrings();
  const state = useAppState();
  const { openFiles, activeFilePath } = state.ui;
  const active =
    openFiles.find((file) => file.path === activeFilePath) ?? openFiles[0] ?? null;
  const root = state.active?.info?.workspace || active?.root || "";

  const [pickerOpen, setPickerOpen] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [listing, setListing] = useState<FileListing | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [svgBroken, setSvgBroken] = useState(false);

  useEffect(() => {
    if (!root || !workspaceProvider.isAvailable()) {
      setListing(null);
      setListLoading(false);
      setListError(null);
      return;
    }
    let cancelled = false;
    setListLoading(true);
    setListError(null);
    void workspaceProvider.files(root).then((result) => {
      if (cancelled) return;
      setListing({
        root,
        files: result.files,
        truncated: result.truncated === true,
      });
      setListError(result.error ?? null);
      setListLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [root, reloadKey]);

  const current = listing && listing.root === root ? listing : null;
  const count = current
    ? `${current.files.length}${current.truncated ? "+" : ""}`
    : listLoading
      ? "…"
      : "—";

  const kind = active ? filePreviewKind(active.path) : "text";
  const html = useMemo(
    () =>
      active?.text !== undefined && (kind === "text" || kind === "svg")
        ? highlightFile(active.text, active.path)
        : "",
    [active?.text, active?.path, kind],
  );
  const svgUrl = useMemo(
    () =>
      kind === "svg" && !active?.binary && active?.text !== undefined
        ? svgDataUrl(active.text)
        : null,
    [kind, active?.binary, active?.text],
  );
  const csv = useMemo(
    () =>
      kind === "csv" && !active?.binary && active?.text !== undefined
        ? parseCsv(active.text)
        : null,
    [kind, active?.binary, active?.text],
  );
  const pdfUrl =
    active && kind === "pdf"
      ? workspaceProvider.rawUrl(active.root, active.path)
      : null;

  useEffect(() => {
    setSvgBroken(false);
  }, [active?.path, active?.text]);

  const openPath = (entry: { path: string; name: string }) => {
    store.dispatch({
      type: "file/open",
      root,
      path: entry.path,
      name: entry.name,
    });
    setPickerOpen(false);
  };

  return (
    <>
      <div className="file-pane-header">
        <span className="file-pane-title">{strings.files.title}</span>
        <span className="file-pane-meta" title={active?.path}>
          {active?.loading
            ? strings.sidebar.loading
            : active
              ? `${formatBytes(active.size)}${
                  active.truncated && kind !== "pdf"
                    ? ` · ${strings.sidebar.truncated}`
                    : ""
                }`
              : ""}
        </span>
        <button
          className={`file-browse-btn${pickerOpen ? " active" : ""}`}
          onClick={() => setPickerOpen((open) => !open)}
          title={`${strings.files.browse} (${count})`}
          aria-label={`${strings.files.browse} (${count})`}
          aria-expanded={pickerOpen}
        >
          <Files size={14} />
          <span>{count}</span>
        </button>
        <button
          className="icon-btn"
          onClick={() => store.dispatch({ type: "ui/swapRightPanes" })}
          title={strings.files.swap}
          aria-label={strings.files.swap}
        >
          <ArrowLeftRight size={15} />
        </button>
        <button
          className="icon-btn"
          onClick={() => store.dispatch({ type: "file/paneToggle" })}
          title={strings.files.closePane}
          aria-label={strings.files.closePane}
        >
          <PanelRightClose size={16} />
        </button>
      </div>

      <div className="file-tabs" role="tablist" aria-label={strings.files.title}>
        {openFiles.map((file) => {
          const selected = file.path === active?.path;
          return (
            <div
              key={file.path}
              className={`file-tab${selected ? " active" : ""}`}
              title={file.path}
            >
              <button
                className="file-tab-name"
                role="tab"
                aria-selected={selected}
                onClick={() =>
                  store.dispatch({ type: "file/activate", path: file.path })
                }
              >
                {file.name}
              </button>
              <button
                className="file-tab-close"
                onClick={() => store.dispatch({ type: "file/close", path: file.path })}
                title={strings.files.closeTab}
                aria-label={`${strings.files.closeTab}: ${file.name}`}
              >
                <X size={12} />
              </button>
            </div>
          );
        })}
      </div>

      <div className="file-pane-body">
        {/* Kept mounted while a preview is showing so the tree keeps its
            expanded folders and scroll state across toggles. */}
        <div
          className="file-picker"
          style={{ display: pickerOpen ? undefined : "none" }}
        >
          <FileExplorer
            root={root}
            visible={pickerOpen}
            activePath={active?.path ?? null}
            files={current?.files ?? []}
            filesLoading={listLoading}
            filesError={listError}
            filesTruncated={current?.truncated === true}
            onOpen={openPath}
            onRefresh={() => setReloadKey((key) => key + 1)}
          />
        </div>

        {!pickerOpen ? (
          <>
            {!active ? <PanelEmpty>{strings.files.empty}</PanelEmpty> : null}
            {active?.loading ? <PanelLoading label={strings.sidebar.loading} /> : null}
            {!active?.loading && active?.error ? (
              <PanelEmpty>
                <div>{active.error}</div>
                <button
                  className="btn btn-sm"
                  style={{ marginTop: 8 }}
                  onClick={() =>
                    store.dispatch({
                      type: "file/open",
                      root: active.root,
                      path: active.path,
                      name: active.name,
                    })
                  }
                >
                  {strings.overlays.retry}
                </button>
              </PanelEmpty>
            ) : null}

            {!active?.loading && !active?.error && active ? (
              kind === "pdf" ? (
                pdfUrl ? (
                  <PdfPreview src={pdfUrl} name={active.name} />
                ) : (
                  <PanelEmpty>{strings.files.pdfUnavailable}</PanelEmpty>
                )
              ) : active.binary ? (
                <PanelEmpty>
                  {strings.sidebar.binaryFile} · {formatBytes(active.size)}
                </PanelEmpty>
              ) : active.text !== undefined ? (
                kind === "svg" ? (
                  svgBroken || !svgUrl ? (
                    <>
                      <div className="file-preview-note">
                        {strings.files.svgFailed}
                      </div>
                      <pre className="file-pane-pre">
                        <code
                          className="hljs"
                          dangerouslySetInnerHTML={{ __html: html }}
                        />
                      </pre>
                    </>
                  ) : (
                    <SvgPreview
                      src={svgUrl}
                      name={active.name}
                      onError={() => setSvgBroken(true)}
                    />
                  )
                ) : kind === "markdown" ? (
                  <MarkdownFilePreview
                    text={active.text}
                    root={active.root}
                    path={active.path}
                  />
                ) : kind === "csv" && csv ? (
                  <CsvPreview table={csv} truncated={active.truncated === true} />
                ) : (
                  <pre className="file-pane-pre">
                    <code
                      className="hljs"
                      dangerouslySetInnerHTML={{ __html: html }}
                    />
                  </pre>
                )
              ) : null
            ) : null}
          </>
        ) : null}
      </div>
    </>
  );
}
