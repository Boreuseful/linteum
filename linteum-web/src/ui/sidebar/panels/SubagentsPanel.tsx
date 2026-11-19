import { Bot, RefreshCw } from "lucide-react";
import { useEffect } from "react";
import { useStrings } from "../../../hooks/useStrings";
import { store, useAppState } from "../../../hooks/useStore";
import { controller } from "../../../transport/controller";
import { PanelEmpty, PanelLoading } from "../../common/EmptyState";
import { formatTokens } from "../../common/format";
import { StatusBadge } from "../../common/StatusBadge";
import type { PanelProps } from "../types";

export function SubagentsPanel(_props: PanelProps) {
  const strings = useStrings();
  const state = useAppState();
  const active = state.active;
  const open = state.ui.openPanels.subagents === true;

  useEffect(() => {
    if (open && active) controller.refreshChildren();
  }, [open, active?.id]);

  if (!active) return <PanelEmpty>{strings.sidebar.empty}</PanelEmpty>;

  const subagents = Object.values(active.subagents);
  const orphanChildren = active.childSessions.filter(
    (child) => !subagents.some((sub) => sub.childSessionId === child.session_id),
  );

  const openChildContext = (childId: string, title?: string) => {
    store.dispatch({
      type: "ui/openOverlay",
      overlay: { kind: "child-context", childId, title },
    });
  };

  return (
    <>
      <div className="panel-toolbar">
        <button className="btn btn-sm" onClick={() => controller.refreshChildren()}>
          <RefreshCw size={13} /> {strings.sidebar.refresh}
        </button>
      </div>

      {subagents.length === 0 && orphanChildren.length === 0 ? (
        <PanelEmpty>{strings.sidebar.noSubagents}</PanelEmpty>
      ) : null}

      {subagents.map((sub) => {
        const tokens = (sub.tokensInput ?? 0) + (sub.tokensOutput ?? 0);
        return (
          <div className="task-row" key={sub.callId}>
            <div className="task-row-head">
              <Bot size={14} />
              <span className="task-name">{sub.title ?? sub.callId}</span>
              <StatusBadge status={sub.status} />
            </div>
            <div className="task-meta">
              <span>{sub.phase ?? "—"}</span>
              {sub.round !== undefined ? (
                <span>
                  {strings.panel.roundShort} {sub.round}
                </span>
              ) : null}
              {sub.lastTool ? <span>{sub.lastTool}</span> : null}
              {tokens > 0 ? (
                <span>
                  {formatTokens(tokens)} {strings.panel.tokensShort}
                </span>
              ) : null}
            </div>
            {sub.childSessionId ? (
              <div className="task-actions">
                <button
                  className="btn btn-sm"
                  onClick={() =>
                    openChildContext(
                      sub.childSessionId as string,
                      sub.title ?? sub.callId,
                    )
                  }
                >
                  {strings.sidebar.openChild}
                </button>
              </div>
            ) : null}
          </div>
        );
      })}

      {orphanChildren.length > 0 ? (
        <div className="panel-section">
          <div className="panel-section-title">{strings.sidebar.childSessions}</div>
          {orphanChildren.map((child) => (
            <div className="task-row" key={child.session_id}>
              <div className="task-row-head">
                <Bot size={14} />
                <span className="task-name">
                  {child.title ?? child.session_id.slice(0, 12)}
                </span>
                <span className="badge">{child.phase}</span>
              </div>
              <div className="task-meta">
                <span className="mono">{child.session_id.slice(0, 12)}</span>
                <span>
                  {child.message_count} {strings.nav.messageCount}
                </span>
              </div>
              <div className="task-actions">
                <button
                  className="btn btn-sm"
                  onClick={() =>
                    openChildContext(
                      child.session_id,
                      child.title ?? undefined,
                    )
                  }
                >
                  {strings.sidebar.openChild}
                </button>
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}
