import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  FileText,
  Globe,
  Terminal,
  Wrench,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { useStrings } from "../../../hooks/useStrings";
import { store } from "../../../hooks/useStore";
import type { ToolItem as ToolItemModel } from "../../../state/types";
import {
  argsText,
  firstLine,
  outputText,
  truncate,
} from "../../common/format";
import { StatusBadge } from "../../common/StatusBadge";
import {
  ToolInvocationView,
  ToolResultView,
} from "../../common/ToolInvocationView";
import { presentToolArgs, presentToolResult } from "../../common/toolPresentation";

function toolIcon(name: string): ReactNode {
  const lower = name.toLowerCase();
  if (lower.includes("bash") || lower.includes("shell") || lower.includes("exec")) {
    return <Terminal size={15} />;
  }
  if (
    lower.includes("read") ||
    lower.includes("write") ||
    lower.includes("edit") ||
    lower.includes("file")
  ) {
    return <FileText size={15} />;
  }
  if (lower.includes("web") || lower.includes("fetch") || lower.includes("http")) {
    return <Globe size={15} />;
  }
  return <Wrench size={15} />;
}

function statusClass(status: string): string {
  if (status === "ok") return "ok";
  if (status === "failed") return "failed";
  if (status === "running" || status === "queued" || status === "backgrounded") {
    return "running";
  }
  return "";
}

export function ToolItemView({ item }: { item: ToolItemModel }) {
  const strings = useStrings();
  const [open, setOpen] = useState(item.status === "failed");
  const args = argsText(item.args);
  const output = typeof item.output === "string" ? item.output : outputText(item.output);
  const presentation = presentToolArgs(item.name, item.args);
  const result = presentToolResult(item.name, item.output);
  const hasStructuredResult =
    result !== null && (result.fields.length > 0 || result.exitCode !== undefined);
  const summary = presentation.summary || firstLine(output) || "";

  return (
    <div className="tool-card">
      <button className="tool-head" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <span className={`tool-icon ${statusClass(item.status)}`}>{toolIcon(item.name)}</span>
        <span className="tool-name">{item.name || strings.chat.toolFallback}</span>
        <span className="tool-summary">{summary}</span>
        <span className="tool-head-meta">
          {item.sideEffect ? (
            <span className="badge" title={strings.chat.sideEffect}>
              {item.sideEffect}
            </span>
          ) : null}
          {item.background ? (
            <span className="badge badge-accent">{strings.chat.inBackground}</span>
          ) : null}
          <StatusBadge status={item.status} />
          {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        </span>
      </button>
      {open ? (
        <div className="tool-body">
          {args !== "{}" || presentation.structured ? (
            <div className="panel-section">
              <span className="tool-section-label">{strings.chat.arguments}</span>
              <ToolInvocationView name={item.name} args={item.args} />
            </div>
          ) : null}
          {hasStructuredResult ? (
            <div className="panel-section">
              <span className="tool-section-label">{strings.chat.output}</span>
              <ToolResultView name={item.name} output={item.output} />
            </div>
          ) : result === null && output ? (
            <div className="panel-section">
              <span className="tool-section-label">{strings.chat.output}</span>
              <pre className="tool-pre">{truncate(output, 8000)}</pre>
            </div>
          ) : null}
          {item.errorInfo ? (
            <div className="error-card">
              <div>
                <div className="error-card-title">{item.errorInfo.code}</div>
                <div>{item.errorInfo.message}</div>
              </div>
            </div>
          ) : null}
          <div className="tool-actions">
            {item.outputRef ? (
              <button
                className="btn btn-sm"
                onClick={() =>
                  store.dispatch({
                    type: "ui/openOverlay",
                    overlay: {
                      kind: "artifact",
                      ref: item.outputRef as string,
                      title: item.name,
                    },
                  })
                }
              >
                <ExternalLink size={13} /> {strings.chat.viewArtifact}
              </button>
            ) : null}
            {item.taskId ? (
              <button
                className="btn btn-sm"
                onClick={() => store.dispatch({ type: "ui/selectPanel", panelId: "background" })}
              >
                {strings.chat.backgroundTask} #{item.taskId.slice(0, 8)}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
