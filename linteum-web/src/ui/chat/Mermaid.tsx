import { Check, Code, Copy, Download, Eye, Maximize2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useStrings } from "../../hooks/useStrings";
import { copyText, downloadBlob } from "../common/format";
import { MermaidViewer } from "./MermaidViewer";
import { mermaidFilename, mermaidMarkdown, withSvgBackground } from "./mermaidExport";

/**
 * Renders a ```mermaid fenced block to SVG.
 *
 * Mermaid is imported on first use, so the main bundle stays small. The source
 * is shown until the fence closes: a half-recorded diagram is invalid and
 * parsing it on every delta would flicker. Mermaid runs at its strict security
 * level, which sanitises labels with DOMPurify before the SVG is injected.
 *
 * Mermaid appends a temporary element to `<body>` while rendering. With
 * `suppressErrorRendering` a parse failure removes it and throws, so the
 * built-in "Syntax error in text" SVG never leaks into the page; the error is
 * logged and the component falls back to the source.
 */

type MermaidApi = typeof import("mermaid")["default"];

let mermaidLoader: Promise<MermaidApi> | null = null;
let diagramCounter = 0;

/** Mermaid mutates global config, so render one diagram at a time. */
let renderQueue: Promise<unknown> = Promise.resolve();

function enqueueRender<T>(task: () => Promise<T>): Promise<T> {
  const run = renderQueue.then(task, task);
  renderQueue = run.catch(() => undefined);
  return run;
}

function loadMermaid(): Promise<MermaidApi> {
  mermaidLoader ??= import("mermaid").then((module) => module.default);
  return mermaidLoader;
}

/** Removes the temporary nodes Mermaid appends to `<body>` while rendering. */
function removeMermaidArtifacts(id: string): void {
  if (typeof document === "undefined") return;
  document.getElementById(id)?.remove();
  document.getElementById(`d${id}`)?.remove();
  document.getElementById(`i${id}`)?.remove();
}

function readToken(styles: CSSStyleDeclaration, name: string, fallback: string): string {
  return styles.getPropertyValue(name).trim() || fallback;
}

/** Maps the app's semantic tokens onto Mermaid's base theme. */
function themeVariables(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const styles = window.getComputedStyle(document.documentElement);
  return {
    background: readToken(styles, "--surface-raised", "#ffffff"),
    primaryColor: readToken(styles, "--surface-raised", "#ffffff"),
    primaryTextColor: readToken(styles, "--text-primary", "#1b1f27"),
    primaryBorderColor: readToken(styles, "--border-strong", "#c9ced8"),
    secondaryColor: readToken(styles, "--surface-sunken", "#f2f3f7"),
    tertiaryColor: readToken(styles, "--surface-hover", "#f0f1f5"),
    lineColor: readToken(styles, "--text-muted", "#7b8290"),
    textColor: readToken(styles, "--text-primary", "#1b1f27"),
    mainBkg: readToken(styles, "--surface-raised", "#ffffff"),
    nodeBorder: readToken(styles, "--border-strong", "#c9ced8"),
    clusterBkg: readToken(styles, "--surface-sunken", "#f2f3f7"),
    clusterBorder: readToken(styles, "--border-subtle", "#e3e5eb"),
    edgeLabelBackground: readToken(styles, "--surface-raised", "#ffffff"),
    fontFamily: readToken(styles, "--font-sans", "sans-serif"),
    fontSize: readToken(styles, "--text-md", "14px"),
    errorBkgColor: readToken(styles, "--danger-soft", "rgba(198, 74, 85, 0.1)"),
    errorTextColor: readToken(styles, "--danger", "#c64a55"),
  };
}

/** Re-renders diagrams when `ThemeProvider` swaps `<html data-theme>`. */
function useDocumentTheme(): string {
  const [theme, setTheme] = useState(() =>
    typeof document === "undefined"
      ? "dark"
      : (document.documentElement.dataset.theme ?? "dark"),
  );

  useEffect(() => {
    if (typeof MutationObserver === "undefined") return;
    const root = document.documentElement;
    const observer = new MutationObserver(() => setTheme(root.dataset.theme ?? "dark"));
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);

  return theme;
}

interface RenderState {
  status: "loading" | "ready" | "error";
  svg?: string;
}

export function MermaidDiagram({
  code,
  ready,
}: {
  code: string;
  ready: boolean;
}) {
  const strings = useStrings();
  const theme = useDocumentTheme();
  const [diagramId] = useState(() => `mermaid-${(diagramCounter += 1)}`);
  const attempt = useRef(0);
  const [result, setResult] = useState<RenderState>({ status: "loading" });
  const [copied, setCopied] = useState(false);
  const [showSource, setShowSource] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const onDownloadSvg = () => {
    if (!result.svg) return;
    const background =
      window
        .getComputedStyle(document.documentElement)
        .getPropertyValue("--surface-raised")
        .trim() || "#ffffff";
    downloadBlob(
      mermaidFilename(code),
      new Blob([withSvgBackground(result.svg, background)], {
        type: "image/svg+xml;charset=utf-8",
      }),
    );
  };

  const onCopyMarkdown = async () => {
    if (await copyText(mermaidMarkdown(code))) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  useEffect(() => {
    if (!ready) {
      // Avoid a state update per streamed delta when there is nothing to show.
      setResult((current) =>
        current.status === "loading" ? current : { status: "loading" },
      );
      return;
    }

    const source = code.trim();
    if (!source) {
      setResult({ status: "error" });
      return;
    }

    let cancelled = false;
    // A fresh id per attempt keeps an overlapping render from reusing the
    // temporary element Mermaid creates for this one.
    attempt.current += 1;
    const renderId = `${diagramId}-${attempt.current}`;

    // Keep a previous SVG on screen while the theme changes; only fall back to
    // the source when there is nothing valid to show yet.
    setResult((current) => (current.status === "ready" ? current : { status: "loading" }));

    void enqueueRender(async () => {
      try {
        const mermaid = await loadMermaid();
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          suppressErrorRendering: true,
          theme: "base",
          themeVariables: themeVariables(),
        });
        const rendered = await mermaid.render(renderId, source);
        if (!cancelled) setResult({ status: "ready", svg: rendered.svg });
      } catch (error) {
        removeMermaidArtifacts(renderId);
        console.warn("Mermaid render failed", error);
        if (!cancelled) setResult({ status: "error" });
      }
    });

    return () => {
      cancelled = true;
    };
  }, [code, diagramId, ready, theme]);

  if (result.status === "ready" && result.svg) {
    return (
      <>
        <div className="mermaid-block" data-state={showSource ? "source" : "ready"}>
          <div className="mermaid-actions">
            <button
              className="icon-btn"
              onClick={() => setExpanded(true)}
              title={strings.chat.expandDiagram}
              aria-label={strings.chat.expandDiagram}
            >
              <Maximize2 size={14} />
            </button>
            <button
              className="icon-btn"
              onClick={onDownloadSvg}
              title={strings.chat.downloadSvg}
              aria-label={strings.chat.downloadSvg}
            >
              <Download size={14} />
            </button>
            <button
              className="icon-btn"
              onClick={() => void onCopyMarkdown()}
              title={strings.chat.copyMarkdown}
              aria-label={strings.chat.copyMarkdown}
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
            <button
              className="icon-btn"
              onClick={() => setShowSource((value) => !value)}
              title={showSource ? strings.chat.showDiagram : strings.chat.showSource}
              aria-label={showSource ? strings.chat.showDiagram : strings.chat.showSource}
              aria-pressed={showSource}
            >
              {showSource ? <Eye size={14} /> : <Code size={14} />}
            </button>
          </div>
          {showSource ? (
            <pre className="mermaid-code">
              <code>{code.replace(/\n$/, "")}</code>
            </pre>
          ) : (
            <div
              className="mermaid-diagram"
              // Mermaid's strict level sanitises labels before producing this SVG.
              dangerouslySetInnerHTML={{ __html: result.svg }}
            />
          )}
        </div>
        {expanded ? (
          <MermaidViewer svg={result.svg} onClose={() => setExpanded(false)} />
        ) : null}
      </>
    );
  }

  // A failed render keeps the source visible; Mermaid's parser diagnostics go
  // to the console instead of the transcript.
  return (
    <div className="mermaid-block" data-state={result.status}>
      <pre className="mermaid-source">
        <code>{code.replace(/\n$/, "")}</code>
      </pre>
    </div>
  );
}
